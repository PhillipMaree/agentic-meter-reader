"""Tests for the meter_readings persistence layer."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import polars as pl

from src.agents.meter import repository
from src.agents.meter.models import ConsensusResult, WaterReport
from tests.conftest import reading, requires_postgres

REPORT = WaterReport(
    hash="test-hash",
    apartment_nr="L3",
    reporter_email="phillip.maree@gmail.com",
    artifacts_path="s3://mollebakken-styret/meter/test-hash/",
    reported_at=datetime(2026, 6, 12, 8, 0, tzinfo=UTC),
)


def _result(**overrides) -> ConsensusResult:
    return ConsensusResult(
        reading=reading(**overrides),
        votes=[reading(**overrides)],
        agreement={"meter_nr": 1.0, "meter_type": 1.0, "meter_reading": 1.0},
    )


# --------------------------------------------------------------------------
# Unit tests (fake SqlDb)
# --------------------------------------------------------------------------


class FakeSqlDb:
    def __init__(self, rows: list | None = None) -> None:
        self.executed: list[tuple[str, list | None]] = []
        self._rows = rows or []

    def execute(self, query: str, params=None) -> None:
        self.executed.append((query, params))

    def fetchone(self, query: str, params=None):
        self.executed.append((query, params))
        return self._rows[0] if self._rows else None

    def fetchall(self, query: str, params=None):
        self.executed.append((query, params))
        return self._rows


def test_email_hash_is_canonical_sha256() -> None:
    date, sender, subject = "Thu, 12 Jun 2026 10:00:00 +0200", "a@b.c", "Vannmåler L1"
    expected = hashlib.sha256(f"{date}|{sender}|{subject}".encode()).hexdigest()

    assert repository.email_hash(date, sender, subject) == expected
    assert repository.email_hash(date, sender, subject) == expected  # deterministic
    assert repository.email_hash(date, sender, "other") != expected


def test_save_report_inserts_one_row_per_meter(monkeypatch) -> None:
    fake = FakeSqlDb()
    monkeypatch.setattr(repository, "sql_db", lambda: fake)

    cold = _result()
    warm = _result(meter_nr="9APA0190001595", meter_type="warm_water", meter_reading=10.781)
    warm.escalated = True
    repository.save_report(REPORT, [cold, warm])

    assert len(fake.executed) == 2
    _, cold_params = fake.executed[0]
    _, warm_params = fake.executed[1]
    assert cold_params[0] == "test-hash"
    assert cold_params[1] == "8APA2080051414"
    assert cold_params[10] is False  # escalated
    assert warm_params[1] == "9APA0190001595"
    assert warm_params[10] is True
    for params in (cold_params, warm_params):
        assert params[11] == "L3"
        assert params[12] == "phillip.maree@gmail.com"
        assert params[13] == REPORT.reported_at


def test_has_report(monkeypatch) -> None:
    monkeypatch.setattr(repository, "sql_db", lambda: FakeSqlDb(rows=[{"1": 1}]))
    assert repository.has_report("test-hash")

    monkeypatch.setattr(repository, "sql_db", lambda: FakeSqlDb())
    assert not repository.has_report("test-hash")


def test_read_meters_returns_polars_frame(monkeypatch) -> None:
    rows = [
        {
            "hash": "h1", "meter_nr": "8APA2080051414", "meter_type": "cold_water",
            "meter_reading": 29.890, "flow_lph": 1.0, "manufacturer": "Apator",
            "model": "Ultrimis NEO", "production_year": 2025, "confidence": 0.95,
            "notes": None, "escalated": False, "apartment": "L3",
            "reporter_email": "phillip.maree@gmail.com",
            "reported_at": datetime(2026, 6, 12, 8, 0, tzinfo=UTC),
            "created_at": datetime(2026, 6, 12, 8, 1, tzinfo=UTC),
        },
    ]
    monkeypatch.setattr(repository, "sql_db", lambda: FakeSqlDb(rows=rows))

    df = repository.read_meters()

    assert isinstance(df, pl.DataFrame)
    assert df.shape == (1, 15)
    assert df["meter_reading"].dtype == pl.Float64
    assert df["meter_reading"][0] == 29.890
    assert df["apartment"][0] == "L3"


def test_read_meters_empty_table_keeps_schema(monkeypatch) -> None:
    monkeypatch.setattr(repository, "sql_db", lambda: FakeSqlDb())

    df = repository.read_meters()

    assert df.is_empty()
    assert df.columns == list(repository._METERS_SCHEMA)


# --------------------------------------------------------------------------
# Integration test (real Postgres)
# --------------------------------------------------------------------------


@requires_postgres
def test_round_trip_against_postgres() -> None:
    from src.utils.sql import sql_db

    hash_ = "pytest-" + repository.email_hash("now", "pytest@test", "round-trip")
    repository.ensure_schema()
    sql_db().execute("DELETE FROM meter_readings WHERE hash = %s", [hash_])
    report = REPORT.model_copy(update={"hash": hash_})

    try:
        assert not repository.has_report(hash_)
        repository.save_report(
            report,
            [
                _result(),
                _result(meter_nr="9APA0190001595", meter_type="warm_water", meter_reading=10.781),
            ],
        )
        assert repository.has_report(hash_)

        rows = sql_db().fetchall(
            "SELECT * FROM meter_readings WHERE hash = %s ORDER BY meter_nr", [hash_]
        )
        assert len(rows) == 2
        assert rows[0]["apartment"] == "L3"
        assert float(rows[1]["meter_reading"]) == 10.781

        df = repository.read_meters().filter(pl.col("hash") == hash_)
        assert df.shape[0] == 2
        assert df["meter_reading"].dtype == pl.Float64
        assert sorted(df["meter_type"].to_list()) == ["cold_water", "warm_water"]
    finally:
        sql_db().execute("DELETE FROM meter_readings WHERE hash = %s", [hash_])
