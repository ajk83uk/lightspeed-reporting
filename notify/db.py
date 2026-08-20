"""
Read-only Neon access for the notification layer.

Deliberately talks to Postgres directly rather than through the Metabase API.
The brief was originally built against Metabase, which meant two extra
credentials (METABASE_URL, METABASE_API_KEY), an extra network hop, and a
second thing that could be down at 11am. Railway already has DATABASE_URL for
the ingest service, and every view the brief needs lives in Neon anyway.

SELECT only. Nothing in notify/ should ever write.
"""

from __future__ import annotations

import os

import psycopg2
import psycopg2.extras


class NotifyDBError(RuntimeError):
    """Raised when the database is unreachable or a query fails."""


def connect():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise NotifyDBError("DATABASE_URL is not set")
    # .strip() mirrors ingest/db.py — a trailing newline pasted into Railway's
    # env-var field surfaces as 'invalid sslmode value: "require\n"'.
    conn = psycopg2.connect(url.strip())
    conn.set_session(readonly=True, autocommit=True)
    return conn


def query(sql: str) -> list[dict]:
    """Run SELECT and return rows as dicts."""
    try:
        with connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql)
                return [dict(r) for r in cur.fetchall()]
    except psycopg2.Error as exc:
        raise NotifyDBError(f"query failed: {exc}") from exc
