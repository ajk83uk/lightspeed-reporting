"""Database helpers for the feedback service (psycopg2, matching the rest of the repo)."""
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

from . import config

psycopg2.extras.register_uuid()


@contextmanager
def get_conn():
    """One connection per request. Commits on success, rolls back on any error, always closes."""
    conn = psycopg2.connect(config.database_url(), cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def q(conn, sql, params=None):
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur
