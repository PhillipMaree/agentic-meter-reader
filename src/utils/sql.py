"""Generic Postgres connection wrapper — business-agnostic.

Exposes `execute / executemany / fetchall / fetchone`. No knowledge of
project tables, schemas, or domain models — callers issue their own SQL.

`sql_db()` is the process-wide cached accessor; the connection opens on
first call and is reused for the rest of the process lifetime.

Usage::

    sql_db().execute("CREATE TABLE IF NOT EXISTS t (k text PRIMARY KEY)")
    sql_db().execute("INSERT INTO t (k) VALUES (%s)", ["foo"])
    rows = sql_db().fetchall("SELECT * FROM t WHERE k = %s", ["foo"])
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import psycopg
from psycopg.rows import dict_row

from src.utils import SqlSettings, settings


class SqlDb:
    """Postgres connection — connects eagerly on construction (autocommit)."""

    def __init__(self, cfg: SqlSettings):
        self.name = cfg.name
        conninfo = (
            f"host={cfg.host} port={cfg.port} user={cfg.user} "
            f"password={cfg.password.get_secret_value()} dbname={cfg.name}"
        )
        self._conn = psycopg.connect(conninfo, autocommit=True)

    def execute(
        self,
        query: str,
        params: dict[str, Any] | list | tuple | None = None,
    ) -> None:
        """Run a non-SELECT statement (DDL, INSERT, UPDATE, DELETE)."""
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)

    def executemany(
        self,
        query: str,
        seq_of_params: list[dict[str, Any]] | list[list] | list[tuple],
    ) -> None:
        """Run the same statement against a sequence of parameter sets."""
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.executemany(query, seq_of_params)

    def fetchall(
        self,
        query: str,
        params: dict[str, Any] | list | tuple | None = None,
    ) -> list[dict[str, Any]]:
        """Run a SELECT and return all rows as `column → value` dicts."""
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)
            return cur.fetchall()

    def fetchone(
        self,
        query: str,
        params: dict[str, Any] | list | tuple | None = None,
    ) -> dict[str, Any] | None:
        """Run a SELECT and return the first row, or None if no rows."""
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)
            return cur.fetchone()


# Process-wide SqlDb — opened on first call and cached for the lifetime of
# the process. lru_cache(1) handles the singleton bookkeeping.
@lru_cache(maxsize=1)
def sql_db() -> SqlDb:
    return SqlDb(settings.sql)
