"""Nightly combined ingest -- the single job the Railway cron runs.

Runs, in order:
  1. items   -- catalogue refresh for all sites
  2. sales   -- incremental sales (watermark-based, with 48h overlap)
  3. cashoff -- Google Sheets cash-off forms -> cashoff_daily

Each step is isolated: if one fails it's logged and the others still run, so a
flaky Google Sheet never blocks the Lightspeed pull (and vice versa). Exits
non-zero if ANY step failed so Railway flags the run, but whatever succeeded is
already committed.

    python -m ingest.daily
"""
from __future__ import annotations

import logging
import sys

from . import run as run_mod
from . import cashoff as cashoff_mod

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
log = logging.getLogger("daily")

# (label, callable) -- each callable returns 0 on success, like a CLI main().
STEPS = [
    ("items",   lambda: run_mod.main(["items"])),
    ("sales",   lambda: run_mod.main(["sales"])),
    ("shifts",  lambda: run_mod.main(["shifts"])),
    ("cashoff", lambda: cashoff_mod.main([])),
]


def main() -> int:
    failed: list[str] = []
    for name, fn in STEPS:
        log.info("=== START %s ===", name)
        try:
            rc = fn()
            if rc:
                failed.append(name)
                log.error("=== %s returned non-zero (%s) ===", name, rc)
            else:
                log.info("=== DONE %s ===", name)
        except Exception:  # noqa: BLE001 -- isolate steps from each other
            failed.append(name)
            log.exception("=== %s CRASHED ===", name)
    if failed:
        log.error("Nightly ingest finished WITH FAILURES: %s", ", ".join(failed))
        return 1
    log.info("Nightly ingest finished cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
