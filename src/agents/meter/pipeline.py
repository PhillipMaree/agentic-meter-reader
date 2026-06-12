"""On-demand inbox pipeline: triage -> consensus analysis -> SQL -> reply.

There is no polling loop — `process_inbox()` runs once per invocation and is
triggered by an external caller: the CLI (`python -m src.agents.meter.pipeline`)
or the meter agent's `POST /inbox/process` endpoint (future UI).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
from email.utils import parseaddr, parsedate_to_datetime
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from src.agents.meter.agent import ImageAttachment, MeterAgent, TriageAgent
from src.agents.meter.consensus import NoConsensusError, consensus_read
from src.agents.meter.models import (
    ConsensusResult,
    EmailEnvelope,
    TriageDecision,
    WaterReport,
)
from src.agents.meter.replies import (
    confirmation_body,
    confirmation_subject,
    duplicate_body,
    rejection_body,
)
from src.agents.meter.repository import (
    email_hash,
    ensure_schema,
    get_report,
    has_report,
    save_report,
)
from src.utils import PROJECT_ROOT
from src.utils.gmail import Client
from src.utils.s3 import s3_client

log = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
ARTIFACTS_PREFIX = "meter"  # S3 key prefix: meter/<hash>/<filename>
RESIDENTS_PATH = PROJECT_ROOT / "config" / "residents.yaml"


class PipelineResult(BaseModel):
    msg_id: str
    hash: str
    status: Literal["saved", "skipped_duplicate", "rejected", "failed"]
    apartment_nr: str | None = None
    meters: list[dict] = Field(default_factory=list)
    rejection: str | None = None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _residents_by_email() -> dict[str, str]:
    """Invert config/residents.yaml into a lowercase email -> apartment map."""
    raw = yaml.safe_load(RESIDENTS_PATH.read_text(encoding="utf-8"))
    return {
        email.lower(): apartment
        for apartment, emails in raw.items()
        for email in (emails or [])
    }


def resident_apartment(sender_email: str) -> str | None:
    return _residents_by_email().get(sender_email.lower())


def envelope_from_message(message: dict) -> EmailEnvelope:
    headers = message["headers"]
    date_header = headers.get("Date", "")
    return EmailEnvelope(
        msg_id=message["id"],
        thread_id=message["thread_id"],
        subject=headers.get("Subject", ""),
        sender=headers.get("From", ""),
        date_header=date_header,
        reported_at=parsedate_to_datetime(date_header),
        body=message["body"],
        attachment_filenames=[a["filename"] for a in message["attachments"]],
    )


def _image_attachments(message: dict) -> list[dict]:
    return [
        a
        for a in message["attachments"]
        if a["attachment_id"] and Path(a["filename"]).suffix.lower() in IMAGE_EXTENSIONS
    ]


def archive_images(gmail: Client, message: dict, hash_: str) -> list[ImageAttachment]:
    """Download image attachments, archive them on S3, return them for analysis."""
    images = []
    for attachment in _image_attachments(message):
        data = gmail.get_attachment(message["id"], attachment["attachment_id"])
        name = Path(attachment["filename"]).name
        media_type = mimetypes.guess_type(name)[0] or "image/jpeg"
        s3_client().put(f"{ARTIFACTS_PREFIX}/{hash_}/{name}", data, media_type)
        images.append(
            ImageAttachment(name, media_type, base64.standard_b64encode(data).decode())
        )
    return images


def _reject(
    gmail: Client, message: dict, envelope: EmailEnvelope, hash_: str, category: str
) -> PipelineResult:
    gmail.reply(
        message, confirmation_subject(envelope.subject), rejection_body(category)
    )
    gmail.mark_read(envelope.msg_id)
    return PipelineResult(
        msg_id=envelope.msg_id, hash=hash_, status="rejected", rejection=category
    )


def _resolve_apartment(decision: TriageDecision, reporter_email: str) -> str | None:
    """Subject-inferred apartment first; sender-email lookup as fallback."""
    return decision.apartment_nr or resident_apartment(reporter_email)


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------


async def process_message(
    gmail: Client,
    msg_id: str,
    triage_agent: TriageAgent,
    meter_agent: MeterAgent,
) -> PipelineResult:
    message = gmail.get_message(msg_id)
    envelope = envelope_from_message(message)
    hash_ = email_hash(envelope.date_header, envelope.sender, envelope.subject)

    if has_report(hash_):
        rows = get_report(hash_)
        apartment = rows[0]["apartment"]
        gmail.reply(
            message,
            confirmation_subject(envelope.subject),
            duplicate_body(apartment, rows),
        )
        gmail.mark_read(msg_id)
        return PipelineResult(
            msg_id=msg_id,
            hash=hash_,
            status="skipped_duplicate",
            apartment_nr=apartment,
        )

    if not _image_attachments(message):
        return _reject(gmail, message, envelope, hash_, "no_images")

    images = archive_images(gmail, message, hash_)
    reporter_email = parseaddr(envelope.sender)[1]

    try:
        decision = await triage_agent.triage(envelope)
        if not decision.is_water_report:
            return _reject(gmail, message, envelope, hash_, "not_water_report")

        apartment = _resolve_apartment(decision, reporter_email)
        if apartment is None:
            return _reject(gmail, message, envelope, hash_, "apartment_unknown")

        report = WaterReport(
            hash=hash_,
            apartment_nr=apartment,
            reporter_email=reporter_email,
            artifacts_path=f"s3://{s3_client().bucket}/{ARTIFACTS_PREFIX}/{hash_}/",
            reported_at=envelope.reported_at,
        )

        try:
            results: list[ConsensusResult] = [
                await consensus_read(image, meter_agent) for image in images
            ]
        except NoConsensusError as exc:
            log.warning("no consensus for %s (%s)", exc.image_name, msg_id)
            return _reject(gmail, message, envelope, hash_, "unclear_images")

        save_report(report, results)

        gmail.reply(
            message,
            confirmation_subject(envelope.subject),
            confirmation_body(apartment, [r.reading for r in results]),
        )
        gmail.mark_read(msg_id)
        return PipelineResult(
            msg_id=msg_id,
            hash=hash_,
            status="saved",
            apartment_nr=apartment,
            meters=[r.reading.model_dump() for r in results],
        )
    except Exception:
        # transient failure (LLM/DB/Gmail): no reply, leave unread so the next
        # invocation retries this message
        log.exception("pipeline failed for message %s", msg_id)
        return PipelineResult(msg_id=msg_id, hash=hash_, status="failed")


async def process_inbox(max_results: int = 10) -> list[PipelineResult]:
    """Process unread inbox mail once. On-demand — no polling."""
    triage_agent = TriageAgent()
    meter_agent = MeterAgent()
    results = []
    with Client() as gmail:
        for ref in gmail.list_messages(
            query="in:inbox is:unread", max_results=max_results
        ):
            results.append(
                await process_message(gmail, ref["id"], triage_agent, meter_agent)
            )
    return results


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    ensure_schema()
    s3_client().ensure_bucket()
    for result in asyncio.run(process_inbox()):
        print(result.model_dump_json())


if __name__ == "__main__":
    main()
