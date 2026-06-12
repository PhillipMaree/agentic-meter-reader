"""Tests for the voter-consensus + arbiter pattern."""

from __future__ import annotations

import asyncio

import pytest

from src.agents.meter.agent import MeterAgent
from src.agents.meter.consensus import NoConsensusError, aggregate, consensus_read
from tests.conftest import (
    IMAGES,
    StubAsyncAnthropic,
    attachment,
    reading,
    refusal,
    requires_gateway,
)


def _agent(responses: list) -> tuple[MeterAgent, StubAsyncAnthropic]:
    stub = StubAsyncAnthropic(responses)
    return MeterAgent(client=stub), stub  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# aggregate()
# --------------------------------------------------------------------------


def test_aggregate_unanimous() -> None:
    votes = [reading(), reading(meter_reading=29.8900), reading()]

    consensus, agreement = aggregate(votes)

    assert agreement == {"meter_nr": 1.0, "meter_type": 1.0, "meter_reading": 1.0}
    assert consensus.meter_reading == pytest.approx(29.890)
    assert consensus.confidence == 1.0


def test_aggregate_reading_tolerance_does_not_mask_decimal_errors() -> None:
    votes = [reading(meter_reading=10.781), reading(meter_reading=1.078), reading(meter_reading=10.781)]

    consensus, agreement = aggregate(votes)

    assert consensus.meter_reading == pytest.approx(10.781)  # median
    assert agreement["meter_reading"] == pytest.approx(2 / 3)


def test_aggregate_mode_for_categorical_fields() -> None:
    votes = [
        reading(meter_nr="A", meter_type="cold_water"),
        reading(meter_nr="A", meter_type="warm_water"),
        reading(meter_nr="B", meter_type="warm_water"),
    ]

    consensus, agreement = aggregate(votes)

    assert consensus.meter_nr == "A"
    assert consensus.meter_type == "warm_water"
    assert agreement["meter_nr"] == pytest.approx(2 / 3)
    assert consensus.confidence == pytest.approx(2 / 3)


# --------------------------------------------------------------------------
# consensus_read()
# --------------------------------------------------------------------------


def test_unanimous_voters_skip_arbiter() -> None:
    agent, stub = _agent([reading(), reading(), reading()])

    result = asyncio.run(consensus_read(attachment(IMAGES[0]), agent))

    assert len(stub.calls) == 3
    assert not result.escalated
    assert result.dissent == []
    assert result.reading.meter_reading == pytest.approx(29.890)
    for call in stub.calls:
        assert call["model"] == agent.llm.voter_model
        assert call["temperature"] == agent.llm.voter_temperature


def test_dissent_escalates_to_arbiter() -> None:
    dissenting = reading(meter_reading=2.989)  # decimal misplaced vs 29.890
    arbiter_pick = reading(notes="sided with voters 1 and 2", confidence=0.9)
    agent, stub = _agent([reading(), reading(), dissenting, arbiter_pick])

    result = asyncio.run(consensus_read(attachment(IMAGES[0]), agent))

    assert len(stub.calls) == 4
    arbiter_call = stub.calls[3]
    assert arbiter_call["model"] == agent.llm.arbiter_model
    assert "temperature" not in arbiter_call
    prompt = arbiter_call["messages"][0]["content"][1]["text"]
    assert "Voter 1" in prompt and "2.989" in prompt

    assert result.escalated
    assert result.dissent == ["meter_reading"]
    assert result.reading == arbiter_pick


def test_arbiter_refusal_raises_no_consensus() -> None:
    agent, _ = _agent([reading(), reading(), reading(meter_nr="OTHER"), refusal()])

    with pytest.raises(NoConsensusError):
        asyncio.run(consensus_read(attachment(IMAGES[0]), agent))


def test_low_confidence_arbiter_matching_no_voter_raises() -> None:
    arbiter_pick = reading(meter_nr="NEITHER", confidence=0.3)
    agent, _ = _agent([reading(), reading(), reading(meter_nr="OTHER"), arbiter_pick])

    with pytest.raises(NoConsensusError) as exc_info:
        asyncio.run(consensus_read(attachment(IMAGES[0]), agent))

    assert exc_info.value.arbiter_reading == arbiter_pick


def test_confident_novel_arbiter_reading_is_accepted() -> None:
    arbiter_pick = reading(meter_nr="NEITHER", confidence=0.9)
    agent, _ = _agent([reading(), reading(), reading(meter_nr="OTHER"), arbiter_pick])

    result = asyncio.run(consensus_read(attachment(IMAGES[0]), agent))

    assert result.escalated
    assert result.reading == arbiter_pick


# --------------------------------------------------------------------------
# Integration (real gateway, real photos)
# --------------------------------------------------------------------------


@requires_gateway
def test_consensus_on_real_photos() -> None:
    agent = MeterAgent()

    async def _run() -> list:
        return [await consensus_read(attachment(p), agent) for p in IMAGES]

    results = asyncio.run(_run())

    by_type = {r.reading.meter_type: r for r in results}
    assert set(by_type) == {"cold_water", "warm_water"}

    # Ground truth (user-confirmed): last three display digits are decimals,
    # so 00002|9890 -> 29.890 and 00001|0781 -> 10.781.
    cold, warm = by_type["cold_water"], by_type["warm_water"]
    assert cold.reading.meter_reading == pytest.approx(29.890, abs=0.05)
    assert warm.reading.meter_reading == pytest.approx(10.781, abs=0.05)

    for result in results:
        print(
            f"{result.reading.meter_type}: {result.reading.meter_reading} m3 "
            f"(escalated={result.escalated}, agreement={result.agreement})"
        )
