"""Agentic consensus pattern for meter-photo extraction.

Strategy:
    1. Fan out N independent extraction calls (self-consistency sampling).
    2. Aggregate per field: mode for categorical, median for numeric.
    3. Compute empirical agreement; if below threshold, escalate the
       disputed image + candidate answers to an arbiter model.
    4. If arbitration itself is untrustworthy (refusal, or a low-confidence
       reading that matches no voter), raise NoConsensusError so the caller
       can ask the resident for new pictures.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
from collections import Counter

from src.agents.meter.agent import ImageAttachment, MeterAgent
from src.agents.meter.models import ConsensusResult, MeterReading
from src.agents.meter.prompts import ARBITER_PROMPT_SUFFIX, EXTRACTION_PROMPT

log = logging.getLogger(__name__)

READING_TOLERANCE_M3 = 0.001  # one display decimal-unit
ARBITER_MIN_CONFIDENCE = 0.6
GATED_FIELDS = ("meter_nr", "meter_type", "meter_reading")


class NoConsensusError(RuntimeError):
    """Neither the voters nor the arbiter could read the photo trustworthily."""

    def __init__(
        self,
        image_name: str,
        votes: list[MeterReading],
        arbiter_reading: MeterReading | None = None,
    ) -> None:
        super().__init__(f"no trustworthy reading for {image_name}")
        self.image_name = image_name
        self.votes = votes
        self.arbiter_reading = arbiter_reading


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


def _mode_with_ratio(values: list) -> tuple[object, float]:
    counts = Counter(values)
    winner, n = counts.most_common(1)[0]
    return winner, n / len(values)


def aggregate(votes: list[MeterReading]) -> tuple[MeterReading, dict[str, float]]:
    """Combine voter readings: mode for categorical, median for numeric."""
    serial, serial_agree = _mode_with_ratio([v.meter_nr for v in votes])
    mtype, type_agree = _mode_with_ratio([v.meter_type for v in votes])

    readings = [v.meter_reading for v in votes]
    reading = statistics.median(readings)
    # numeric agreement: fraction of votes within tolerance of the median
    reading_agree = sum(
        abs(r - reading) <= READING_TOLERANCE_M3 for r in readings
    ) / len(readings)

    flows = [v.flow_lph for v in votes if v.flow_lph is not None]
    consensus = MeterReading(
        meter_nr=str(serial),
        meter_type=mtype,  # type: ignore[arg-type]
        meter_reading=reading,
        flow_lph=statistics.median(flows) if flows else None,
        manufacturer=_mode_with_ratio([v.manufacturer for v in votes])[0],
        model=_mode_with_ratio([v.model for v in votes])[0],
        production_year=_mode_with_ratio([v.production_year for v in votes])[0],
        confidence=min(serial_agree, type_agree, reading_agree),
        notes=None,
    )
    agreement = {
        "meter_nr": serial_agree,
        "meter_type": type_agree,
        "meter_reading": reading_agree,
    }
    return consensus, agreement


def _matches_a_voter(
    arbited: MeterReading, votes: list[MeterReading], fields: list[str]
) -> bool:
    """True when the arbiter sided with at least one voter on every disputed field."""

    def field_equal(a: MeterReading, b: MeterReading, name: str) -> bool:
        if name == "meter_reading":
            return abs(a.meter_reading - b.meter_reading) <= READING_TOLERANCE_M3
        return getattr(a, name) == getattr(b, name)

    return any(
        all(field_equal(arbited, vote, name) for name in fields) for vote in votes
    )


# --------------------------------------------------------------------------
# Arbiter
# --------------------------------------------------------------------------


async def _arbitrate(
    agent: MeterAgent, image: ImageAttachment, votes: list[MeterReading]
) -> MeterReading:
    candidates = "\n".join(
        f"- Voter {i + 1}: serial={v.meter_nr}, type={v.meter_type}, "
        f"reading={v.meter_reading} m3"
        for i, v in enumerate(votes)
    )
    prompt = EXTRACTION_PROMPT + ARBITER_PROMPT_SUFFIX.format(candidates=candidates)
    return await agent.analyze(image, model=agent.llm.arbiter_model, prompt=prompt)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


async def consensus_read(image: ImageAttachment, agent: MeterAgent) -> ConsensusResult:
    """Read one photo with voter consensus, escalating to the arbiter on dissent."""
    cfg = agent.llm

    votes = list(
        await asyncio.gather(
            *(
                agent.analyze(
                    image,
                    model=cfg.voter_model,
                    temperature=cfg.voter_temperature,
                )
                for _ in range(cfg.n_voters)
            )
        )
    )

    consensus, agreement = aggregate(votes)
    dissent = [f for f, ratio in agreement.items() if ratio < cfg.agreement_threshold]

    if not dissent:
        return ConsensusResult(reading=consensus, votes=votes, agreement=agreement)

    log.info("consensus dissent on %s for %s — arbitrating", dissent, image.name)
    try:
        arbited = await _arbitrate(agent, image, votes)
    except ValueError as exc:  # refusal / unparseable arbiter response
        raise NoConsensusError(image.name, votes) from exc

    if (
        not _matches_a_voter(arbited, votes, dissent)
        and arbited.confidence < ARBITER_MIN_CONFIDENCE
    ):
        raise NoConsensusError(image.name, votes, arbited)

    return ConsensusResult(
        reading=arbited,
        votes=votes,
        agreement=agreement,
        escalated=True,
        dissent=dissent,
    )
