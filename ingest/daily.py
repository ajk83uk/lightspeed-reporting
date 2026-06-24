"""Nightly combined ingest -- the single job the Railway cron runs.

Runs, in order:
  1. items     -- catalogue refresh for all sites
  2. sales     -- incremental sales (watermark-based, with 48h overlap)
  3. shifts    -- staff clock-in/out
  4. nory      -- WFM labour from S3
  5. cashoff   -- Google Sheets cash-off forms -> cashoff_daily
  6. sentiment -- review feed (email / GCS / folder; each off until configured)

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
from . import sentiment as sentiment_mod
from . import sentiment_email as sentiment_email_mod
from . import sentiment_gcs as sentiment_gcs_mod
from . import nory as nory_mod
from . import bookings as bookings_mod

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
log = logging.getLogger("daily")

# (label, callable) -- each callable returns 0 on success, like a CLI main().
STEPS = [
    ("items",     lambda: run_mod.main(["items"])),
    ("sales",     lambda: run_mod.main(["sales"])),
    ("shifts",    lambda: run_mod.main(["shifts"])),
    # Nory WFM labour from S3. Reads the latest date folder; each file carries a
    # rolling ~32 days so the (branch_id,biz_date) upsert self-heals the month.
    # Skips cleanly if the NORY_/AWS_ env vars aren't set yet.
    ("nory",      lambda: nory_mod.main([])),
    # Favourite Table bookings (pull API). Re-pulls a rolling window so late
    # status changes (Booked -> Show/NoShow/Complete) self-heal. Skips cleanly
    # if FT_AUTH_TOKEN isn't set yet, so it ships dark until the env var lands.
    ("bookings",  lambda: bookings_mod.main([])),
    ("cashoff",   lambda: cashoff_mod.main([])),
    # Sentiment Search review feed. Three independent sources, each a safe no-op
    # until configured, so exactly one (or none) does work on a given night:
    #   * email -- pull daily CSV attachments from the inbox (GMAIL_* set)
    #   * gcs   -- pull objects from the vendor bucket (SENTIMENT_GCS_BUCKET set)
    #   * src   -- a local/synced landing folder (SENTIMENT_SRC set)
    ("sentiment_email", lambda: sentiment_email_mod.main([])),
    ("sentiment_gcs",   lambda: sentiment_gcs_mod.main([])),
    ("sentiment",       lambda: sentiment_mod.main([])),
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
