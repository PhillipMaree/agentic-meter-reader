"""Persistence for analyzed meter readings — mollebakken Postgres DB.

Mirrors mollebakken-styret's ingest conventions: idempotent CREATE TABLE
bootstrap, sha256 hash over a canonical string as the dedup key, and
check-then-insert (no ON CONFLICT). One email yields 1-2 meters, so the
primary key is (hash, meter_nr).
"""

from __future__ import annotations

import hashlib

import polars as pl

from src.agents.meter.models import ConsensusResult, MeterReading, WaterReport
from src.utils.sql import sql_db

_SCHEMA_METER_READINGS = """
CREATE TABLE IF NOT EXISTS meter_readings (
    hash            text NOT NULL,
    meter_nr        text NOT NULL,
    meter_type      text NOT NULL,
    meter_reading   numeric(12,4) NOT NULL,
    flow_lph        numeric(10,2),
    manufacturer    text,
    model           text,
    production_year integer,
    confidence      real NOT NULL,
    notes           text,
    escalated       boolean NOT NULL DEFAULT false,
    apartment       text NOT NULL,
    reporter_email  text NOT NULL,
    reported_at     timestamptz NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (hash, meter_nr)
)
"""


def ensure_schema() -> None:
    """Bootstrap the meter_readings table. Idempotent."""
    sql_db().execute(_SCHEMA_METER_READINGS)


def email_hash(date_header: str, sender: str, subject: str) -> str:
    """Stable id over (Date, From, Subject) — idempotent re-ingest key."""
    canonical = f"{date_header}|{sender}|{subject}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def has_report(hash_: str) -> bool:
    return bool(
        sql_db().fetchone("SELECT 1 FROM meter_readings WHERE hash = %s", [hash_])
    )


# Explicit schema so an empty table still yields a typed, empty frame.
_METERS_SCHEMA = {
    "hash": pl.String,
    "meter_nr": pl.String,
    "meter_type": pl.String,
    "meter_reading": pl.Float64,
    "flow_lph": pl.Float64,
    "manufacturer": pl.String,
    "model": pl.String,
    "production_year": pl.Int64,
    "confidence": pl.Float64,
    "notes": pl.String,
    "escalated": pl.Boolean,
    "apartment": pl.String,
    "reporter_email": pl.String,
    "reported_at": pl.Datetime("us", "UTC"),
    "created_at": pl.Datetime("us", "UTC"),
}


def read_meters() -> pl.DataFrame:
    """All persisted meter readings as a polars DataFrame, oldest first."""
    rows = sql_db().fetchall(
        """
        SELECT hash, meter_nr, meter_type,
               meter_reading::float8 AS meter_reading,
               flow_lph::float8 AS flow_lph,
               manufacturer, model, production_year,
               confidence::float8 AS confidence,
               notes, escalated, apartment, reporter_email,
               reported_at, created_at
        FROM meter_readings
        ORDER BY reported_at, meter_nr
        """
    )
    return pl.DataFrame(rows, schema=_METERS_SCHEMA)


def get_report(hash_: str) -> list[dict]:
    """Previously stored rows for one email hash (used in the duplicate reply)."""
    return sql_db().fetchall(
        """
        SELECT meter_nr, meter_type, meter_reading, apartment, created_at
        FROM meter_readings WHERE hash = %s ORDER BY meter_type
        """,
        [hash_],
    )


def _insert_reading(
    report: WaterReport, reading: MeterReading, escalated: bool
) -> None:
    sql_db().execute(
        """
        INSERT INTO meter_readings (
            hash,
            meter_nr, meter_type, meter_reading, flow_lph,
            manufacturer, model, production_year,
            confidence, notes, escalated,
            apartment, reporter_email, reported_at
        ) VALUES (
            %s,
            %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s
        )
        """,
        [
            report.hash,
            reading.meter_nr, reading.meter_type, reading.meter_reading,
            reading.flow_lph,
            reading.manufacturer, reading.model, reading.production_year,
            reading.confidence, reading.notes, escalated,
            report.apartment_nr, report.reporter_email, report.reported_at,
        ],
    )


def save_report(report: WaterReport, results: list[ConsensusResult]) -> None:
    """Insert one row per analyzed meter. Caller has already checked has_report()."""
    for result in results:
        _insert_reading(report, result.reading, result.escalated)
