from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# L0 is hovedmåleren (the building's main meter); L1-L9 are the apartments.
Apartment = Literal["L0", "L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8", "L9"]


class MeterReading(BaseModel):
    """Validated result for a single meter photo."""

    meter_nr: str = Field(
        description="Serial number printed near the barcode, digits and spaces removed"
    )
    meter_type: Literal["cold_water", "warm_water", "unknown"] = Field(
        description="cold_water = blue accents / plain white face, warm_water = red ring"
    )
    meter_reading: float = Field(
        ge=0, description="Cumulative volume in m3, including decimals"
    )
    flow_lph: float | None = Field(
        default=None, ge=0, description="Instantaneous flow in l/h if visible"
    )
    manufacturer: str | None = None
    model: str | None = None
    production_year: int | None = Field(default=None, ge=2000, le=2100)
    confidence: float = Field(ge=0, le=1, description="Model's own confidence 0-1")
    notes: str | None = Field(
        default=None, description="Anything ambiguous: glare, partial digits, etc."
    )


class MeterReport(BaseModel):
    """Aggregate result for one analysis request (one or more photos)."""

    meters: list[MeterReading]


class ConsensusResult(BaseModel):
    """Outcome of the voter fan-out (+ optional arbiter) for one photo."""

    reading: MeterReading
    votes: list[MeterReading]
    agreement: dict[str, float]  # per-field agreement ratio
    escalated: bool = False
    dissent: list[str] = Field(default_factory=list)


class EmailEnvelope(BaseModel):
    """Runner-extracted view of one Gmail message (input to triage)."""

    msg_id: str
    thread_id: str
    subject: str
    sender: str  # raw From header
    date_header: str  # raw Date header (hash input)
    reported_at: datetime
    body: str
    attachment_filenames: list[str]


class TriageDecision(BaseModel):
    """LLM classification of one inbound email."""

    is_water_report: bool = Field(
        description="True if the email is most likely a water-meter reading report"
    )
    apartment_nr: Apartment | None = Field(
        description="Apartment inferred from the subject (or sender), L1-L9, or "
        "L0 for the building's main meter; null when it cannot be determined"
    )
    rejection: Literal["not_water_report", "apartment_unknown"] | None = Field(
        default=None, description="Why the email is rejected, null when accepted"
    )
    reason: str | None = Field(
        default=None, description="Free-text diagnostics, not shown to the resident"
    )
    confidence: float = Field(ge=0, le=1)


class WaterReport(BaseModel):
    """Triage artifact: an accepted report ready for analysis and persistence."""

    hash: str
    apartment_nr: Apartment
    reporter_email: str
    artifacts_path: str  # S3 prefix URI, e.g. s3://<bucket>/meter/<hash>/
    reported_at: datetime
