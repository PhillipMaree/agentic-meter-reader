"""Tests for email triage and the Norwegian reply templates."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from src.agents.meter.agent import TriageAgent
from src.agents.meter.models import EmailEnvelope, TriageDecision
from src.agents.meter.replies import (
    confirmation_body,
    confirmation_subject,
    rejection_body,
)
from tests.conftest import StubAsyncAnthropic, reading, requires_gateway


def _envelope(
    subject: str = "Vannmåler L1",
    sender: str = "Hilde Haugnes <haugneshilde@gmail.com>",
    body: str = "Hei, her er avlesningen.",
    attachments: list[str] | None = None,
) -> EmailEnvelope:
    return EmailEnvelope(
        msg_id="m-1",
        thread_id="t-1",
        subject=subject,
        sender=sender,
        date_header="Thu, 12 Jun 2026 10:00:00 +0200",
        reported_at=datetime(2026, 6, 12, 8, 0, tzinfo=UTC),
        body=body,
        attachment_filenames=attachments or ["cold.jpg", "warm.jpg"],
    )


# --------------------------------------------------------------------------
# Unit tests (stubbed client)
# --------------------------------------------------------------------------


def test_triage_renders_envelope_into_prompt() -> None:
    decision = TriageDecision(
        is_water_report=True, apartment_nr="L1", confidence=0.95
    )
    stub = StubAsyncAnthropic([decision])
    agent = TriageAgent(client=stub)  # type: ignore[arg-type]

    result = asyncio.run(agent.triage(_envelope()))

    assert result == decision
    (call,) = stub.calls
    assert call["output_format"] is TriageDecision
    prompt = call["messages"][0]["content"]
    assert "Vannmåler L1" in prompt
    assert "haugneshilde@gmail.com" in prompt
    assert "cold.jpg, warm.jpg" in prompt


def test_confirmation_body_lists_readings_in_norwegian() -> None:
    readings = [
        reading(),
        reading(meter_nr="9APA0190001595", meter_type="warm_water", meter_reading=10.781),
    ]

    body = confirmation_body("L1", readings)

    assert body.startswith("Kjære L1,")
    assert "Vi har lagret din vannmåleravlesning" in body
    assert "Kaldtvann (måler 8APA2080051414): 29.890 m3" in body
    assert "Varmtvann (måler 9APA0190001595): 10.781 m3" in body


def test_confirmation_subject_adds_re_prefix_once() -> None:
    assert confirmation_subject("Vannmåler L1") == "Re: Vannmåler L1"
    assert confirmation_subject("Re: Vannmåler L1") == "Re: Vannmåler L1"


@pytest.mark.parametrize(
    ("category", "phrase"),
    [
        ("no_images", "ingen bilder"),
        ("apartment_unknown", 'f.eks. "Vannmåler L3"'),
        ("not_water_report", "ikke gjenkjent"),
        ("unclear_images", "ta nye bilder"),
    ],
)
def test_rejection_bodies(category: str, phrase: str) -> None:
    assert phrase in rejection_body(category)


# --------------------------------------------------------------------------
# Integration tests (real gateway)
# --------------------------------------------------------------------------


@requires_gateway
@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Vannmåler L1", "L1"),
        ("Leilighet 3 avlesing", "L3"),
        ("Apartment 2 water meter", "L2"),
        ("Hovedmåler avlesing", "L0"),
    ],
)
def test_real_triage_normalizes_apartment(subject: str, expected: str) -> None:
    decision = asyncio.run(TriageAgent().triage(_envelope(subject=subject)))
    assert decision.is_water_report
    assert decision.apartment_nr == expected


@requires_gateway
def test_real_triage_rejects_street_address_form() -> None:
    decision = asyncio.run(TriageAgent().triage(_envelope(subject="36C")))
    assert decision.apartment_nr is None
    assert decision.rejection == "apartment_unknown"


@requires_gateway
def test_real_triage_rejects_marketing_mail() -> None:
    decision = asyncio.run(
        TriageAgent().triage(
            _envelope(
                subject="Limited offer: solar panels for your roof!",
                sender="sales@solarspam.example",
                body="Buy now and save 40%!",
                attachments=["brochure.jpg"],
            )
        )
    )
    assert not decision.is_water_report
    assert decision.rejection == "not_water_report"
