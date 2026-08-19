"""Apply one or more named db/*.sql files, without running the full migrate.

`python -m ingest.migrate` re-applies every file in order, which is what you
want for a fresh database. On an existing database it can fail on a file that
is unrelated to the change you actually want to ship (e.g. views.sql cannot
CREATE OR REPLACE v_report_lines once patch_line_staff_and_payment_method.sql
has added columns to it -- Postgres refuses to drop columns from a view). This
runner lets you apply just the files you changed.

Each file is its own transaction (they contain their own BEGIN/COMMIT), and
files are applied in the order given, so dependants come last.

Usage:
    python -m ingest.apply_sql views_leakage.sql views_eotw.sql
    python -m ingest.apply_sql --list          # show available files
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

import psycopg2

from .config import settings
from .migrate import _DB_DIR, _run_file

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("apply_sql")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply named db/*.sql files")
    parser.add_argument("files", nargs="*", help="file names inside db/, in order")
    parser.add_argument("--list", action="store_true", help="list available files")
    args = parser.parse_args(argv)

    if args.list:
        for f in sorted(os.listdir(_DB_DIR)):
            if f.endswith(".sql"):
                print(f)
        return 0

    if not args.files:
        parser.error("give at least one file name, or --list")

    missing = [f for f in args.files if not os.path.exists(os.path.join(_DB_DIR, f))]
    if missing:
        log.error("not found in db/: %s", ", ".join(missing))
        return 1

    conn = psycopg2.connect(settings.database_url)
    conn.autocommit = True  # each file carries its own transaction block
    failed = []
    try:
        with conn.cursor() as cur:
            for f in args.files:
                try:
                    _run_file(cur, f)
                except Exception:  # noqa: BLE001 -- report all, don't stop at the first
                    log.exception("FAILED %s", f)
                    failed.append(f)
    finally:
        conn.close()

    if failed:
        log.error("%d file(s) failed: %s", len(failed), ", ".join(failed))
        return 1
    log.info("applied %d file(s)", len(args.files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
