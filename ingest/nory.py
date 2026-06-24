"""Nory WFM labour ingestion (S3 -> Neon).

Reads the latest date folder from the Nory export bucket and upserts the
per-site daily labour aggregates + wage-cost breakdown. Each per-site file is
a rolling ~32-day array, so the (branch_id, biz_date) upsert self-heals and
backfills the last month on every run -- one missed night is harmless.

Run:
    python -m ingest.nory            # ingest the latest available date folder
    python -m ingest.nory --date 2026-06-21
    python -m ingest.nory --all-dates   # walk every date folder in the bucket

Env (set in Railway):
    NORY_BUCKET        default nory-data-exporter-tap-and-tandoor
    NORY_REGION        default eu-west-1
    NORY_PREFIX        default "Tap & Tandoor"
    AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY   (read-only export key)
    DATABASE_URL       reused from the existing config
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

import boto3
import psycopg2
import psycopg2.extras

from .config import settings

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
log = logging.getLogger("ingest.nory")

BUCKET = os.getenv("NORY_BUCKET", "nory-data-exporter-tap-and-tandoor")
REGION = os.getenv("NORY_REGION", "eu-west-1")
PREFIX = os.getenv("NORY_PREFIX", "Tap & Tandoor")

# The 5 Tap & Tandoor sites only (Vita + Head Office excluded by decision).
CORE_SITES = ["Bournemouth", "Peterborough", "Portsmouth", "Solihull", "Southampton"]


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _s3():
    return boto3.client("s3", region_name=REGION)


def list_date_folders(s3) -> list[str]:
    """Date folder names (YYYY-MM-DD) directly under the prefix, sorted."""
    dates: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix=f"{PREFIX}/", Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            name = cp["Prefix"].rstrip("/").split("/")[-1]
            if len(name) == 10 and name[4] == "-":  # looks like a date
                dates.append(name)
    return sorted(dates)


def fetch_site_file(s3, date: str, site: str) -> list | None:
    key = f"{PREFIX}/{date}/{site}/wfm/labour_insights.json"
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=key)
    except s3.exceptions.NoSuchKey:
        log.warning("missing %s", key)
        return None
    return json.loads(obj["Body"].read())


# --- upserts ---------------------------------------------------------------
_DAILY_SQL = """
INSERT INTO nory_labour_daily (branch_id,biz_date,site_name,col,planned_col,
    hours,planned_hours,sales,orders,percentage,planned_percentage,splh,oplh,
    raw,updated_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
ON CONFLICT (branch_id,biz_date) DO UPDATE SET
    site_name=EXCLUDED.site_name, col=EXCLUDED.col, planned_col=EXCLUDED.planned_col,
    hours=EXCLUDED.hours, planned_hours=EXCLUDED.planned_hours, sales=EXCLUDED.sales,
    orders=EXCLUDED.orders, percentage=EXCLUDED.percentage,
    planned_percentage=EXCLUDED.planned_percentage, splh=EXCLUDED.splh,
    oplh=EXCLUDED.oplh, raw=EXCLUDED.raw, updated_at=now();
"""

_BREAKDOWN_SQL = """
INSERT INTO nory_labour_breakdown (branch_id,biz_date,category,value,planned_value,updated_at)
VALUES (%s,%s,%s,%s,%s,now())
ON CONFLICT (branch_id,biz_date,category) DO UPDATE SET
    value=EXCLUDED.value, planned_value=EXCLUDED.planned_value, updated_at=now();
"""


def _daily_row(site: str, r: dict) -> tuple:
    return (
        r.get("branch_id"), r.get("date"), site,
        _num(r.get("col")), _num(r.get("planned_col")),
        _num(r.get("hours")), _num(r.get("planned_hours")),
        _num(r.get("sales")), r.get("orders"),
        _num(r.get("percentage")), _num(r.get("planned_percentage")),
        _num(r.get("splh")), _num(r.get("oplh")),
        json.dumps(r),
    )


def _breakdown_rows(r: dict) -> list[tuple]:
    bid, date = r.get("branch_id"), r.get("date")
    planned = {x.get("name"): _num(x.get("value")) for x in (r.get("planned_breakdown") or [])}
    rows = []
    for x in r.get("breakdown") or []:
        cat = x.get("name")
        rows.append((bid, date, cat, _num(x.get("value")), planned.get(cat)))
    return rows


def ingest_date(conn, s3, date: str) -> None:
    daily, breakdown = [], []
    for site in CORE_SITES:
        records = fetch_site_file(s3, date, site)
        if not records:
            continue
        for r in records:
            if not r.get("branch_id") or not r.get("date"):
                continue
            daily.append(_daily_row(site, r))
            breakdown.extend(_breakdown_rows(r))
    with conn.cursor() as cur:
        if daily:
            psycopg2.extras.execute_batch(cur, _DAILY_SQL, daily, page_size=500)
        if breakdown:
            psycopg2.extras.execute_batch(cur, _BREAKDOWN_SQL, breakdown, page_size=500)
    conn.commit()
    log.info("[%s] upserted %d daily rows, %d breakdown rows", date, len(daily), len(breakdown))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nory labour ingestion (S3 -> Neon)")
    parser.add_argument("--date", help="specific date folder YYYY-MM-DD")
    parser.add_argument("--all-dates", action="store_true", help="walk every date folder")
    args = parser.parse_args(argv)

    # Skip cleanly (no failure) until the AWS credentials are configured, so the
    # nightly job doesn't fail this step before the Railway env vars are set.
    if not (os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY")):
        log.warning("Nory: AWS credentials not set (AWS_ACCESS_KEY_ID / "
                    "AWS_SECRET_ACCESS_KEY) -- skipping Nory ingest.")
        return 0

    s3 = _s3()
    folders = list_date_folders(s3)
    if not folders:
        log.error("No date folders found under %s/%s/", BUCKET, PREFIX)
        return 1

    if args.date:
        targets = [args.date]
    elif args.all_dates:
        targets = folders
    else:
        targets = [folders[-1]]  # latest; its files already carry ~32 days

    conn = psycopg2.connect(settings.database_url)
    conn.autocommit = False
    try:
        for date in targets:
            ingest_date(conn, s3, date)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
