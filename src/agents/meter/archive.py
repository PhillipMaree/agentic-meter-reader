"""Archive the meter_readings table to S3 and clear it.

Dumps the full table to ``meter/arkive/<YYYY-MM-DD>.parquet`` in the shared
bucket, verifies the upload by reading it back, and only then truncates the
table. Refuses to overwrite an existing archive for the same date so a
re-run can never silently drop a previous batch.

Run with::

    uv run python -m src.agents.meter.archive
"""

from __future__ import annotations

import io
import logging
import sys
from datetime import UTC, datetime

import polars as pl

from src.agents.meter.repository import ensure_schema, read_meters
from src.utils.s3 import s3_client
from src.utils.sql import sql_db

log = logging.getLogger(__name__)


def archive_key(date: str) -> str:
    return f"meter/arkive/{date}.parquet"


def archive_and_clear() -> str | None:
    """Archive all meter readings to S3, then truncate the table.

    Returns the S3 key written, or None when the table was empty.
    """
    ensure_schema()
    df = read_meters()
    if df.is_empty():
        log.info("meter_readings is empty — nothing to archive")
        return None

    s3 = s3_client()
    s3.ensure_bucket()

    key = archive_key(datetime.now(UTC).date().isoformat())
    if s3.list(key):
        raise RuntimeError(
            f"s3://{s3.bucket}/{key} already exists — refusing to overwrite "
            "an earlier archive for today"
        )

    buf = io.BytesIO()
    df.write_parquet(buf)
    s3.put(key, buf.getvalue(), "application/vnd.apache.parquet")

    # Read the upload back before the destructive step.
    archived = pl.read_parquet(io.BytesIO(s3.get(key)))
    if archived.height != df.height:
        raise RuntimeError(
            f"verification failed: table has {df.height} rows but "
            f"s3://{s3.bucket}/{key} has {archived.height} — table NOT cleared"
        )

    sql_db().execute("TRUNCATE meter_readings")
    log.info(
        "archived %d rows to s3://%s/%s and cleared the table",
        df.height,
        s3.bucket,
        key,
    )
    return key


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(0 if archive_and_clear() else 1)
