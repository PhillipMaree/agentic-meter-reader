"""Offline tests for the inbox pipeline (fake Gmail, stub agents, fake repo)."""

from __future__ import annotations

import asyncio

import pytest

from src.agents.meter import pipeline
from src.agents.meter.consensus import NoConsensusError
from src.agents.meter.models import ConsensusResult, TriageDecision
from tests.conftest import (
    IMAGES,
    reading,
    requires_gateway,
    requires_gmail,
    requires_postgres,
)

DATE = "Thu, 12 Jun 2026 10:00:00 +0200"
SENDER = "Phillip Maree <phillip.maree@gmail.com>"


def _gmail_message(attachments: list[dict] | None = None) -> dict:
    return {
        "id": "m-1",
        "thread_id": "t-1",
        "headers": {"Date": DATE, "From": SENDER, "Subject": "Vannmåler L1"},
        "body": "Hei, her er avlesningen.",
        "attachments": attachments
        if attachments is not None
        else [
            {"filename": p.name, "size": 1, "attachment_id": f"att-{i}"}
            for i, p in enumerate(IMAGES)
        ],
    }


class FakeGmail:
    def __init__(self, message: dict) -> None:
        self._message = message
        self.replies: list[tuple[str, str]] = []
        self.marked_read: list[str] = []

    def get_message(self, msg_id: str) -> dict:
        return self._message

    def get_attachment(self, msg_id: str, attachment_id: str) -> bytes:
        index = int(attachment_id.split("-")[1])
        return IMAGES[index].read_bytes()

    def reply(self, original: dict, subject: str, body: str) -> dict:
        self.replies.append((subject, body))
        return {"id": "sent-1"}

    def mark_read(self, msg_id: str) -> None:
        self.marked_read.append(msg_id)


class StubTriage:
    def __init__(self, decision: TriageDecision | Exception) -> None:
        self._decision = decision
        self.calls = 0

    async def triage(self, envelope) -> TriageDecision:
        self.calls += 1
        if isinstance(self._decision, Exception):
            raise self._decision
        return self._decision


ACCEPT_L1 = TriageDecision(is_water_report=True, apartment_nr="L1", confidence=0.9)


def _consensus_stub(results: list):
    """Async stand-in for consensus_read, consumed per call."""
    queue = list(results)

    async def fake(image, agent):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return fake


def _ok_result(**overrides) -> ConsensusResult:
    return ConsensusResult(
        reading=reading(**overrides),
        votes=[reading(**overrides)],
        agreement={"meter_nr": 1.0, "meter_type": 1.0, "meter_reading": 1.0},
    )


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.bucket = "test-bucket"

    def put(self, key: str, body: bytes, content_type: str) -> None:
        self.objects[key] = body


STORED_ROWS = [
    {"meter_nr": "8APA2080051414", "meter_type": "cold_water", "meter_reading": 29.890, "apartment": "L3"},
    {"meter_nr": "9APA0190001595", "meter_type": "warm_water", "meter_reading": 10.781, "apartment": "L3"},
]


@pytest.fixture
def repo(monkeypatch):
    """Patch repository + S3 calls; returns the recording state."""
    state = {"saved": [], "has_report": False, "s3": FakeS3()}
    monkeypatch.setattr(pipeline, "has_report", lambda h: state["has_report"])
    monkeypatch.setattr(pipeline, "get_report", lambda h: STORED_ROWS)
    monkeypatch.setattr(
        pipeline, "save_report", lambda report, results: state["saved"].append((report, results))
    )
    monkeypatch.setattr(pipeline, "s3_client", lambda: state["s3"])
    return state


def _run(gmail, triage, consensus_results, repo_state=None):
    return asyncio.run(
        pipeline.process_message(gmail, "m-1", triage, meter_agent=None)
    )


def test_happy_path_saves_and_replies_in_norwegian(monkeypatch, repo) -> None:
    gmail = FakeGmail(_gmail_message())
    triage = StubTriage(ACCEPT_L1)
    warm = _ok_result(meter_nr="9APA0190001595", meter_type="warm_water", meter_reading=10.781)
    monkeypatch.setattr(pipeline, "consensus_read", _consensus_stub([_ok_result(), warm]))

    result = _run(gmail, triage, None)

    assert result.status == "saved"
    assert result.apartment_nr == "L1"
    assert len(result.meters) == 2

    (report, results) = repo["saved"][0]
    assert report.apartment_nr == "L1"
    assert report.reporter_email == "phillip.maree@gmail.com"
    assert len(results) == 2

    # images archived on S3 under meter/<hash>/, and the report points there
    s3_keys = sorted(repo["s3"].objects)
    assert s3_keys == sorted(f"meter/{result.hash}/{p.name}" for p in IMAGES)
    assert report.artifacts_path == f"s3://test-bucket/meter/{result.hash}/"

    (subject, body) = gmail.replies[0]
    assert subject == "Re: Vannmåler L1"
    assert body.startswith("Kjære L1,")
    assert "10.781 m3" in body
    assert gmail.marked_read == ["m-1"]


def test_duplicate_replies_with_previously_stored_readings(monkeypatch, repo) -> None:
    repo["has_report"] = True
    gmail = FakeGmail(_gmail_message())
    triage = StubTriage(ACCEPT_L1)

    result = _run(gmail, triage, None)

    assert result.status == "skipped_duplicate"
    assert result.apartment_nr == "L3"
    assert triage.calls == 0  # nothing re-analyzed
    assert repo["saved"] == []
    assert gmail.marked_read == ["m-1"]

    (subject, body) = gmail.replies[0]
    assert subject == "Re: Vannmåler L1"
    assert body.startswith("Kjære L3,")
    assert "allerede registrert" in body
    assert "Kaldtvann (måler 8APA2080051414): 29.890 m3" in body
    assert "Varmtvann (måler 9APA0190001595): 10.781 m3" in body


def test_no_images_rejected_before_any_llm_call(monkeypatch, repo) -> None:
    gmail = FakeGmail(_gmail_message(attachments=[]))
    triage = StubTriage(ACCEPT_L1)

    result = _run(gmail, triage, None)

    assert result.status == "rejected"
    assert result.rejection == "no_images"
    assert triage.calls == 0
    assert "ingen bilder" in gmail.replies[0][1]
    assert gmail.marked_read == ["m-1"]


def test_not_water_report_rejected(monkeypatch, repo) -> None:
    gmail = FakeGmail(_gmail_message())
    triage = StubTriage(
        TriageDecision(
            is_water_report=False,
            apartment_nr=None,
            rejection="not_water_report",
            confidence=0.9,
        )
    )

    result = _run(gmail, triage, None)

    assert result.status == "rejected"
    assert result.rejection == "not_water_report"
    assert "ikke gjenkjent" in gmail.replies[0][1]
    assert repo["saved"] == []


def test_apartment_falls_back_to_sender_email(monkeypatch, repo) -> None:
    # subject gives nothing, but phillip.maree@gmail.com is L3 in residents.yaml
    gmail = FakeGmail(_gmail_message())
    triage = StubTriage(
        TriageDecision(
            is_water_report=True,
            apartment_nr=None,
            rejection="apartment_unknown",
            confidence=0.6,
        )
    )
    monkeypatch.setattr(pipeline, "consensus_read", _consensus_stub([_ok_result(), _ok_result()]))

    result = _run(gmail, triage, None)

    assert result.status == "saved"
    assert result.apartment_nr == "L3"
    assert gmail.replies[0][1].startswith("Kjære L3,")


def test_unknown_apartment_rejected_with_example(monkeypatch, repo) -> None:
    message = _gmail_message()
    message["headers"]["From"] = "Unknown Person <nobody@example.com>"
    gmail = FakeGmail(message)
    triage = StubTriage(
        TriageDecision(
            is_water_report=True,
            apartment_nr=None,
            rejection="apartment_unknown",
            confidence=0.6,
        )
    )

    result = _run(gmail, triage, None)

    assert result.status == "rejected"
    assert result.rejection == "apartment_unknown"
    assert 'f.eks. "Vannmåler L3"' in gmail.replies[0][1]
    assert repo["saved"] == []


def test_consensus_failure_asks_for_new_pictures(monkeypatch, repo) -> None:
    gmail = FakeGmail(_gmail_message())
    triage = StubTriage(ACCEPT_L1)
    monkeypatch.setattr(
        pipeline,
        "consensus_read",
        _consensus_stub([_ok_result(), NoConsensusError("warm.jpg", votes=[])]),
    )

    result = _run(gmail, triage, None)

    assert result.status == "rejected"
    assert result.rejection == "unclear_images"
    assert "ta nye bilder" in gmail.replies[0][1]
    assert repo["saved"] == []
    assert gmail.marked_read == ["m-1"]


def test_unexpected_error_leaves_mail_unread_without_reply(monkeypatch, repo) -> None:
    gmail = FakeGmail(_gmail_message())
    triage = StubTriage(RuntimeError("LLM down"))

    result = _run(gmail, triage, None)

    assert result.status == "failed"
    assert gmail.replies == []
    assert gmail.marked_read == []
    assert repo["saved"] == []


def test_resident_apartment_lookup() -> None:
    assert pipeline.resident_apartment("phillip.maree@gmail.com") == "L3"
    assert pipeline.resident_apartment("Phillip.Maree@GMAIL.com") == "L3"
    assert pipeline.resident_apartment("nobody@example.com") is None


# --------------------------------------------------------------------------
# End-to-end (real Gmail + gateway + Postgres + S3): self-mail round trip
# --------------------------------------------------------------------------


@requires_gmail
@requires_gateway
@requires_postgres
def test_end_to_end_self_mail() -> None:
    """Ingest a self-sent test mail, run the pipeline, reply lands at self.

    Sends the two tests/data photos to the meter account with an L3 subject,
    processes that exact message (saved → then duplicate), and cleans up the
    DB rows, S3 objects, and the mail thread afterwards.
    """
    import uuid

    from src.agents.meter.agent import MeterAgent, TriageAgent
    from src.utils import settings
    from src.utils.gmail import Client
    from src.utils.s3 import s3_client
    from src.utils.sql import sql_db

    pipeline.ensure_schema()
    s3_client().ensure_bucket()
    subject = f"Vannmåler L3 e2e-test {uuid.uuid4().hex[:8]}"
    hash_ = None
    sent = None

    with Client() as gmail:
        try:
            sent = gmail.send(
                to=settings.mail.account,
                subject=subject,
                body="Automatisk e2e-test: vannmåleravlesning for L3.",
                attachments=tuple(IMAGES),
            )

            async def _run():
                return await pipeline.process_message(
                    gmail, sent["id"], TriageAgent(), MeterAgent()
                )

            result = asyncio.run(_run())
            hash_ = result.hash
            assert result.status == "saved", result
            assert result.apartment_nr == "L3"
            assert len(result.meters) == 2
            readings = sorted(m["meter_reading"] for m in result.meters)
            assert readings == pytest.approx([10.781, 29.890], abs=0.05)

            # the archived photos are on S3 under the report hash
            assert len(s3_client().list(f"meter/{hash_}/")) == 2

            # processing the same mail again replies with the stored values
            duplicate = asyncio.run(_run())
            assert duplicate.status == "skipped_duplicate"
            assert duplicate.apartment_nr == "L3"
        finally:
            if hash_:
                sql_db().execute(
                    "DELETE FROM meter_readings WHERE hash = %s", [hash_]
                )
                s3 = s3_client()
                for key in s3.list(f"meter/{hash_}/"):
                    s3._client.delete_object(Bucket=s3.bucket, Key=key)
            if sent:
                gmail.trash_thread(sent["threadId"])
