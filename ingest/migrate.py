"""One-off database migration / setup runner.

Loads the schema, views and category seed into DATABASE_URL in order. Safe to
re-run: schema uses IF NOT EXISTS, views use CREATE OR REPLACE, and the seed
clears + reinserts the category rules.

Usage (locally or as a one-off Railway service / `railway run`):
    python -m ingest.migrate            # schema + views + seed
    python -m ingest.migrate --no-seed  # schema + views only (keep edited rules)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

import psycopg2

from .config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("migrate")

# Resolve db/ relative to the repo root (one level up from this package).
_DB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db")


def _run_file(cur, filename: str) -> None:
    path = os.path.join(_DB_DIR, filename)
    with open(path, "r", encoding="utf-8") as f:
        sql = f.read()
    cur.execute(sql)
    log.info("applied %s", filename)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply DB schema/views/seed")
    parser.add_argument("--no-seed", action="store_true",
                        help="skip seed_categories.sql (preserve edited rules)")
    args = parser.parse_args(argv)

    files = ["schema.sql", "views.sql"]
    if not args.no_seed:
        files.append("seed_categories.sql")

    conn = psycopg2.connect(settings.database_url)
    conn.autocommit = True  # each file is its own transaction block already
    try:
        with conn.cursor() as cur:
            for f in files:
                _run_file(cur, f)
    finally:
        conn.close()
    log.info("migration complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
