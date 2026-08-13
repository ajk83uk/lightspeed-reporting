"""Manager's Daily Report importer: Google Sheet -> Postgres (manager_daily_report).

A duty manager submits one end-of-night report per site via a Google Form. Unlike
the cash-off forms (one sheet per site), this is a SINGLE responses sheet with all
five sites in it -- the site is a column ("What site are you completing this for").

We keep one row per (site, shift date); a later submission for the same night wins.
Columns are matched by keyword on the header (not position), so the form can gain or
reorder questions without breaking ingest.

Auth: the same Google service-account key the cash-off importer uses (share the sheet
read-only with cash-off-importer@tap-and-tandoor-metabase.iam.gserviceaccount.com).

    python -m ingest.manager_report --dry-run   # show mapping + sample, no DB write
    python -m ingest.manager_report             # import all rows

Run `python -m ingest.migrate` first so the manager_daily_report table exists.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime

import psycopg2
import psycopg2.extras

from .config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("manager_report")

# --- Where to find things ---------------------------------------------------
# Reuse the cash-off service-account key (same Google project). On Railway the
# key JSON is supplied whole in GCP_KEY_JSON; locally it falls back to the file.
KEY_PATH = os.getenv("GCP_KEY_PATH", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gcp-cashoff-key.json"))

# The single Manager's Daily Report responses sheet. Override with
# MANAGER_REPORT_SHEET so another brand/deployment can point at its own form.
SHEET_ID = os.getenv(
    "MANAGER_REPORT_SHEET", "1YpQO2PrXeSLV9TLbMS6XoQUA1gVHLT13r01_jHCWj7k")

# --- Keyword column mapping -------------------------------------------------
# canonical field -> predicate over a lowercased header. Order matters: each
# header is assigned to at most one field, most specific first.
FIELD_MATCHERS = [
    ("submitted_at",        lambda h: h.strip() == "timestamp"),
    ("business_date",       lambda h: "date of shift" in h),
    ("site",                lambda h: "site" in h),
    ("manager_name",        lambda h: "manager name" in h),
    ("positive_highlights", lambda h: "highlights" in h or "achievements" in h),
    ("stockout_detail",     lambda h: "critical stockout" in h),
    ("equipment_detail",    lambda h: "describe the equipment" in h),
    ("customer_comments",   lambda h: "notable comments" in h),
    ("incidents",           lambda h: "number of incidents" in h),
    ("further_comments",    lambda h: "further comments" in h),
]

TEXT_FIELDS = {"site", "manager_name", "positive_highlights", "stockout_detail",
               "equipment_detail", "customer_comments", "incidents",
               "further_comments"}

# The site column carries the venue name straight from the form's dropdown; these
# are the only valid values. Anything else (blank separator rows, typos) is dropped.
KNOWN_SITES = {"Solihull", "Peterborough", "Southampton", "Portsmouth", "Bournemouth"}


def build_column_map(header: list[str]) -> dict[str, int]:
    """Return {canonical_field: column_index} for the sheet's header row."""
    lowered = [(i, (h or "").strip().lower()) for i, h in enumerate(header)]
    used: set[int] = set()
    out: dict[str, int] = {}
    for field, match in FIELD_MATCHERS:
        for i, h in lowered:
            if i in used or not h:
                continue
            if match(h):
                out[field] = i
                used.add(i)
                break
    return out


def clean_text(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s != "" else None


def parse_date(v) -> date | None:
    if not v:
        return None
    s = str(v).strip().split(" ")[0]
    for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d", "%m/%d/%y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_ts(v):
    if not v:
        return None
    s = str(v).strip()
    for fmt in ("%m/%d/%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S",
                "%m/%d/%Y %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def parse_rows(header: list[str], rows: list[list]):
    """Return (records, column_map). One record per (site, business_date); the
    last matching row in the sheet wins (the form appends chronologically, so
    the latest submission for a night overwrites an earlier one)."""
    cmap = build_column_map(header)
    recs: dict[tuple, dict] = {}
    for row in rows:
        get = lambda f: (row[cmap[f]] if f in cmap and cmap[f] < len(row) else None)
        site = clean_text(get("site"))
        bd = parse_date(get("business_date"))
        if not site or site not in KNOWN_SITES or not bd:
            continue
        rec = {"site": site, "business_date": bd,
               "submitted_at": parse_ts(get("submitted_at"))}
        for f in TEXT_FIELDS:
            if f == "site":
                continue
            rec[f] = clean_text(get(f))
        rec["raw"] = {f: get(f) for f in cmap}
        recs[(site, bd)] = rec
    return list(recs.values()), cmap


# --- Google Sheets read -----------------------------------------------------
def _sheets_service():
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    key_json = os.getenv("GCP_KEY_JSON")
    if key_json:
        creds = Credentials.from_service_account_info(json.loads(key_json), scopes=scopes)
    else:
        creds = Credentials.from_service_account_file(KEY_PATH, scopes=scopes)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def read_sheet(service, sheet_id: str) -> list[list]:
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    first_tab = meta["sheets"][0]["properties"]["title"]
    resp = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{first_tab}'!A:AZ",
        valueRenderOption="UNFORMATTED_VALUE", dateTimeRenderOption="FORMATTED_STRING",
    ).execute()
    return resp.get("values", [])


# --- DB ---------------------------------------------------------------------
_COLS = ["site", "business_date", "manager_name", "positive_highlights",
         "stockout_detail", "equipment_detail", "customer_comments", "incidents",
         "further_comments", "submitted_at", "raw"]

_UPSERT = f"""
INSERT INTO manager_daily_report ({','.join(_COLS)}, updated_at)
VALUES ({','.join(['%s']*len(_COLS))}, now())
ON CONFLICT (site, business_date) DO UPDATE SET
  {', '.join(f'{c}=EXCLUDED.{c}' for c in _COLS if c not in ('site','business_date'))},
  updated_at=now();
"""


def upsert(conn, recs: list[dict]) -> int:
    rows = []
    for r in recs:
        rows.append([
            r["site"], r["business_date"], r.get("manager_name"),
            r.get("positive_highlights"), r.get("stockout_detail"),
            r.get("equipment_detail"), r.get("customer_comments"),
            r.get("incidents"), r.get("further_comments"),
            r.get("submitted_at"), json.dumps(r.get("raw"), default=str),
        ])
    if rows:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, _UPSERT, rows, page_size=200)
        conn.commit()
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Import the Manager's Daily Report sheet")
    p.add_argument("--dry-run", action="store_true", help="show mapping + sample, no DB write")
    args = p.parse_args(argv)

    # Skip cleanly (no failure) until Google credentials are configured -- same
    # guard cash-off/nory/bookings use -- so a deployment without the key ships
    # dark instead of failing the nightly run.
    if not os.getenv("GCP_KEY_JSON") and not os.path.exists(KEY_PATH):
        log.warning("Manager report: no GCP_KEY_JSON and no key file at %s -- skipping.",
                    KEY_PATH)
        return 0

    service = _sheets_service()
    values = read_sheet(service, SHEET_ID)
    if not values:
        log.warning("Manager report sheet is empty")
        return 0

    header, rows = values[0], values[1:]
    recs, cmap = parse_rows(header, rows)
    mapped = {f: header[i] for f, i in cmap.items() if i < len(header)}
    log.info("mapped columns: %s", mapped)
    missing = [f for f, _ in FIELD_MATCHERS if f not in cmap]
    if missing:
        log.warning("no column matched for: %s", ", ".join(missing))
    log.info("parsed %d site-day report(s)", len(recs))

    if args.dry_run:
        for r in sorted(recs, key=lambda x: (x["business_date"], x["site"]))[-6:]:
            log.info("   %s %s | mgr=%s | highlights=%.40s | incidents=%s",
                     r["business_date"], r["site"], r.get("manager_name"),
                     (r.get("positive_highlights") or ""), r.get("incidents"))
        return 0

    conn = psycopg2.connect(settings.database_url)
    try:
        n = upsert(conn, recs)
        log.info("upserted %d site-day report(s)", n)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
