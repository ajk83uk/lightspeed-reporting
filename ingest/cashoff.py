"""Cash-off forms importer: Google Sheets -> Postgres (cashoff_daily).

Each site has its own Google Sheet (a Google Form responses tab) with manager-
entered daily figures. Headers DIFFER per site, so columns are mapped by
keyword, not position. Loads the bits Lightspeed can't give: delivery sales by
channel, wage cost, and counted vs expected cash.

Auth: a Google service account key JSON (shared read-only on each sheet).

    python -m ingest.cashoff --site Solihull --dry-run   # show mapping + sample, no DB write
    python -m ingest.cashoff                              # import all sites
    python -m ingest.cashoff --site Bournemouth           # one site

Needs: pip install google-api-python-client google-auth   (already in requirements.txt)
Run `python -m ingest.migrate` first so the cashoff_daily table exists.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import date, datetime

import psycopg2
import psycopg2.extras

from .config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cashoff")

# --- Where to find things ---------------------------------------------------
KEY_PATH = os.getenv("GCP_KEY_PATH", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gcp-cashoff-key.json"))

# Per-site cash-off form spreadsheets. Defaults are the 5 T&T sheets; a
# deployment can replace the whole map via CASHOFF_SHEETS (a JSON object,
# e.g. '{"Zindiya": "1b--Uj2m..."}') so the same repo serves other brands
# (the Zindiya Railway service sets its own sheet and never sees these).
_DEFAULT_SHEETS = {
    "Solihull":     "1KfCEGeAMSqhCJAdcMm7wVhSGSxOEywpYr-L0aPuBufg",
    "Peterborough": "134v-MiukKSDmgiQDVou5HmUw9qIsQc7EvLnG5ieJyBQ",
    "Southampton":  "1FuILLwfb3HjX7aAgHKt6knqSgqcvYuYmPnPHEGKMxWQ",
    "Portsmouth":   "1KfmDYUpMoL7lxWHvCKdf6pF6sbi3MBL-5T81bo6MDng",
    "Bournemouth":  "1vlGnfjRHxv7Bctn1IBWvg9vJ38jIkfwHbI7rxEgpQ5k",
}
SHEETS = json.loads(os.environ["CASHOFF_SHEETS"]) if os.getenv("CASHOFF_SHEETS") \
    else _DEFAULT_SHEETS

# --- Keyword column mapping -------------------------------------------------
# canonical field -> predicate over a lowercased header. Order matters: more
# specific fields (tips) are matched before generic ones (card/cash), and each
# header is assigned to at most one field.
def _has(*words):
    return lambda h: all(w in h for w in words)

FIELD_MATCHERS = [
    ("business_date",   lambda h: h.strip() == "date"),
    ("card_tips",       _has("card", "tip")),
    ("cash_tips",       _has("cash", "tip")),
    ("service_charge",  _has("service", "charge")),  # Zindiya form; counts toward tips
    ("uber_eats",       lambda h: "uber" in h),
    ("just_eat",        lambda h: "just eat" in h or "justeat" in h),
    ("deliveroo",       lambda h: "deliveroo" in h),
    ("online_orders",   lambda h: "online order" in h),
    ("expected_cash",   _has("expected", "cash")),
    ("actual_cash",     lambda h: ("actual" in h and "cash" in h) or "cash in hand" in h),
    ("petty_cash",      lambda h: "petty" in h or "expense" in h),
    ("covers",          lambda h: "cover" in h),
    ("cashed_up_by",    lambda h: "cashed up" in h),
    ("discrepancy_note", lambda h: "discrepancy" in h or "explanation" in h),
    ("card_sales",      lambda h: "card machine" in h or ("card" in h and "total" in h and "tip" not in h)),
    ("total_sales",     lambda h: "total sales" in h),
]
NUMERIC_FIELDS = {"total_sales", "card_sales", "online_orders", "uber_eats", "just_eat",
                  "deliveroo", "petty_cash", "expected_cash", "actual_cash",
                  "card_tips", "cash_tips", "service_charge", "covers"}
TEXT_FIELDS = {"cashed_up_by", "discrepancy_note"}


def build_column_map(header: list[str]) -> dict[str, int]:
    """Return {canonical_field: column_index} for a sheet's header row."""
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


def clean_num(v) -> float | None:
    if v is None:
        return None
    s = str(v).strip().replace("£", "").replace(",", "").replace("%", "")
    if s == "":
        return None
    m = re.search(r"-?\d+(\.\d+)?", s)  # pull the first number out of messy text
    return float(m.group()) if m else None


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


def parse_rows(header: list[str], rows: list[list]) -> tuple[list[dict], dict[str, int]]:
    cmap = build_column_map(header)
    recs: dict[tuple, dict] = {}  # keyed by (date) -> latest wins
    for row in rows:
        get = lambda f: (row[cmap[f]] if f in cmap and cmap[f] < len(row) else None)
        bd = parse_date(get("business_date"))
        if not bd:
            continue
        rec = {"business_date": bd, "raw": {}}
        for f in NUMERIC_FIELDS:
            rec[f] = clean_num(get(f))
        for f in TEXT_FIELDS:
            val = get(f)
            rec[f] = str(val).strip() if val not in (None, "") else None
        # cash_variance is NOT computed here: expected cash comes from Lightspeed
        # (cash-method sales), reconciled against actual_cash in Metabase.
        rec["cash_variance"] = None
        # keep original mapped values for traceability
        rec["raw"] = {f: get(f) for f in cmap}
        recs[bd] = rec  # later submission for same day overwrites
    return list(recs.values()), cmap


# --- Google Sheets read -----------------------------------------------------
def _sheets_service():
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    # On a server (Railway) there's no key file -- the service-account JSON is
    # supplied whole in the GCP_KEY_JSON env var. Locally we fall back to the
    # gitignored key file on disk.
    key_json = os.getenv("GCP_KEY_JSON")
    if key_json:
        creds = Credentials.from_service_account_info(json.loads(key_json), scopes=scopes)
    else:
        creds = Credentials.from_service_account_file(KEY_PATH, scopes=scopes)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def read_sheet(service, sheet_id: str) -> list[list]:
    """Read the values of the first tab of a spreadsheet."""
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    first_tab = meta["sheets"][0]["properties"]["title"]
    resp = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{first_tab}'!A:AZ",
        valueRenderOption="UNFORMATTED_VALUE", dateTimeRenderOption="FORMATTED_STRING",
    ).execute()
    return resp.get("values", [])


# --- DB ---------------------------------------------------------------------
_COLS = ["site", "business_date", "total_sales", "card_sales", "online_orders",
         "uber_eats", "just_eat", "deliveroo", "petty_cash", "expected_cash",
         "actual_cash", "cash_variance", "card_tips", "cash_tips", "service_charge",
         "covers", "wage_cost", "cashed_up_by", "discrepancy_note", "raw"]

_UPSERT = f"""
INSERT INTO cashoff_daily ({','.join(_COLS)}, updated_at)
VALUES ({','.join(['%s']*len(_COLS))}, now())
ON CONFLICT (site, business_date) DO UPDATE SET
  {', '.join(f'{c}=EXCLUDED.{c}' for c in _COLS if c not in ('site','business_date'))},
  updated_at=now();
"""


def upsert(conn, site: str, recs: list[dict]) -> int:
    rows = []
    for r in recs:
        rows.append([
            site, r["business_date"], r.get("total_sales"), r.get("card_sales"),
            r.get("online_orders"), r.get("uber_eats"), r.get("just_eat"), r.get("deliveroo"),
            r.get("petty_cash"), r.get("expected_cash"), r.get("actual_cash"),
            r.get("cash_variance"), r.get("card_tips"), r.get("cash_tips"),
            r.get("service_charge"), r.get("covers"),
            r.get("wage_cost"), r.get("cashed_up_by"), r.get("discrepancy_note"),
            json.dumps(r.get("raw")),
        ])
    if rows:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, _UPSERT, rows, page_size=200)
        conn.commit()
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Import cash-off Google Sheets")
    p.add_argument("--site", help="one site name (default: all)")
    p.add_argument("--dry-run", action="store_true", help="show mapping + sample, no DB write")
    args = p.parse_args(argv)

    # Skip cleanly (no failure) until Google credentials are configured --
    # same guard nory/bookings use -- so a deployment without cash-off (or
    # before its key lands) ships dark instead of flagging the nightly run.
    if not os.getenv("GCP_KEY_JSON") and not os.path.exists(KEY_PATH):
        log.warning("Cash-off: no GCP_KEY_JSON env var and no key file at %s "
                    "-- skipping cash-off ingest.", KEY_PATH)
        return 0

    if args.site and args.site not in SHEETS:
        log.error("Unknown site '%s'. Known: %s", args.site, ", ".join(SHEETS))
        return 1
    targets = {args.site: SHEETS[args.site]} if args.site else SHEETS

    service = _sheets_service()
    conn = None if args.dry_run else psycopg2.connect(settings.database_url)
    try:
        for site, sid in targets.items():
            values = read_sheet(service, sid)
            if not values:
                log.warning("[%s] sheet is empty", site)
                continue
            header, rows = values[0], values[1:]
            recs, cmap = parse_rows(header, rows)
            mapped = {f: header[i] for f, i in cmap.items() if i < len(header)}
            log.info("[%s] mapped columns: %s", site, mapped)
            missing = [f for f, _ in FIELD_MATCHERS if f not in cmap]
            if missing:
                log.info("[%s] (no column matched for: %s)", site, ", ".join(missing))
            log.info("[%s] parsed %d day(s)", site, len(recs))
            if args.dry_run:
                for r in sorted(recs, key=lambda x: x["business_date"])[-3:]:
                    log.info("   %s: deliveroo=%s ubereats=%s justeat=%s online=%s actual_cash=%s",
                             r["business_date"], r.get("deliveroo"), r.get("uber_eats"),
                             r.get("just_eat"), r.get("online_orders"), r.get("actual_cash"))
            else:
                n = upsert(conn, site, recs)
                log.info("[%s] upserted %d day(s)", site, n)
    finally:
        if conn:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
