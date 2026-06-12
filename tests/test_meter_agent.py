"""Tests for the meter agent's single-call extraction and A2A plumbing.

Unit tests run offline against a stubbed Anthropic client. The integration
test talks to the LiteLLM gateway and is skipped when it is not reachable:

    uv run pytest tests/test_meter_agent.py -s
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pytest
from a2a.types import FilePart, FileWithBytes, Message, Part, Role, TextPart

from src.agents.meter.agent import MeterAgent, MeterAgentExecutor
from src.agents.meter.models import MeterReading
from tests.conftest import (
    IMAGES,
    StubAsyncAnthropic,
    attachment,
    reading,
    requires_gateway,
)


def _message_with_images(paths: list[Path]) -> Message:
    parts = [Part(root=TextPart(text="Read the attached meter photos."))]
    parts += [
        Part(
            root=FilePart(
                file=FileWithBytes(
                    name=p.name,
                    mime_type="image/jpeg",
                    bytes=base64.standard_b64encode(p.read_bytes()).decode(),
                )
            )
        )
        for p in paths
    ]
    return Message(message_id="msg-1", role=Role.user, parts=parts)


# --------------------------------------------------------------------------
# Unit tests (stubbed client, offline)
# --------------------------------------------------------------------------


def test_analyze_sends_image_and_returns_reading() -> None:
    canned = reading()
    stub = StubAsyncAnthropic([canned])
    agent = MeterAgent(client=stub)  # type: ignore[arg-type]

    result = asyncio.run(agent.analyze(attachment(IMAGES[0])))

    assert result == canned
    (call,) = stub.calls
    assert call["output_format"] is MeterReading
    assert call["model"] == agent.llm.voter_model
    assert "temperature" not in call
    image_block, text_block = call["messages"][0]["content"]
    assert image_block["type"] == "image"
    assert image_block["source"]["media_type"] == "image/jpeg"
    assert text_block["type"] == "text"


def test_analyze_passes_model_and_temperature_overrides() -> None:
    stub = StubAsyncAnthropic([reading()])
    agent = MeterAgent(client=stub)  # type: ignore[arg-type]

    asyncio.run(
        agent.analyze(attachment(IMAGES[0]), model="some-model", temperature=0.4)
    )

    (call,) = stub.calls
    assert call["model"] == "some-model"
    assert call["temperature"] == 0.4


def test_image_attachments_extracted_from_a2a_message() -> None:
    message = _message_with_images(IMAGES)

    attachments = asyncio.run(MeterAgentExecutor._image_attachments(message))

    assert [a.name for a in attachments] == [p.name for p in IMAGES]
    assert all(a.media_type == "image/jpeg" for a in attachments)
    for att, path in zip(attachments, IMAGES):
        assert base64.standard_b64decode(att.base64_data) == path.read_bytes()


def test_image_attachments_ignores_text_only_message() -> None:
    message = Message(
        message_id="msg-2",
        role=Role.user,
        parts=[Part(root=TextPart(text="no images here"))],
    )
    assert asyncio.run(MeterAgentExecutor._image_attachments(message)) == []


# --------------------------------------------------------------------------
# Integration test (real gateway, real photos)
# --------------------------------------------------------------------------


@requires_gateway
def test_real_meter_photos() -> None:
    agent = MeterAgent()

    async def _run() -> list:
        # one event loop for all calls — the cached AsyncAnthropic client's
        # connection pool must not outlive its loop
        return [await agent.analyze(attachment(p)) for p in IMAGES]

    readings = asyncio.run(_run())

    by_type = {r.meter_type: r for r in readings}
    assert set(by_type) == {"cold_water", "warm_water"}, readings

    # The last three display digits are decimals: 00002|9890 -> 29.890 and
    # 00001|0781 -> 10.781. Single calls occasionally misplace the decimal —
    # consensus_read guards that — so tolerate either here but never a wrong
    # digit sequence; the strict reading check lives in test_consensus.py.
    cold, warm = by_type["cold_water"], by_type["warm_water"]
    assert "1414" in cold.meter_nr
    assert "1595" in warm.meter_nr
    assert any(cold.meter_reading == pytest.approx(v, rel=0.01) for v in (29.890, 2.989))
    assert any(warm.meter_reading == pytest.approx(v, rel=0.01) for v in (10.781, 1.0781))

    for r in readings:
        print(r.model_dump_json(indent=2))
