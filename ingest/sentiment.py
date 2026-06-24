"""Sentiment Search importer: review/reputation CSVs -> Postgres.

Two file types come from the vendor (Prithvi @ Sentiment Search):

  * Review-level    -> sentiment_reviews  (one row per review; PK = content hash)
        historical: REVIEWS_*.csv
        live daily: Tap_Reviews_YYYY-MM-DD-YYYY-MM-DD.csv (previous day only)
        cols: Business, Source, Date, Rating, Text, User
  * Overview/metric -> sentiment_overview (one row per site per period)
        historical: MONYY.csv (e.g. JAN26.csv) -> grain='month'
        live daily: Tap_Overview_YYYY-MM-DD-YYYY-MM-DD.csv -> grain='day'
        27 cols, one row per site

Files are classified by *header* first (so the rename doesn't matter), with a
filename-prefix fallback. Three gotchas this handles:
  * The historical REVIEWS files are mislabelled — REVIEWS_2025.csv actually
    holds 2026 dates and vice-versa — so review_date is read from the *Date
    column inside each row*, never the filename. (One-off manual naming slip;
    the live daily files are named correctly.)
  * The overview files carry no date column; the period comes from the
    filename — JAN26 -> 2026-01-01 (month grain); a daily file's start date
    -> that day (day grain). See period_from_filename().
  * The live daily Reviews file contains only the previous day's reviews, so
    there is no overlap between days; the content-hash PK is just a safety net.

Idempotent: every file is hashed and logged in sentiment_files; a file whose
(name, sha256) is already logged is skipped. Reviews upsert on the content
hash, overview rows on (label, period_start, grain), so re-running is safe.

Usage:
    python -m ingest.sentiment --src "../Sentiment Reviews" --dry-run  # show, no write
    python -m ingest.sentiment --src "../Sentiment Reviews"            # backfill load
    python -m ingest.sentiment                                         # nightly: uses
                                                                       # SENTIMENT_SRC env

When the daily vendor feed is live, point SENTIMENT_SRC at the synced landing
folder (or extend _iter_files to pull from the bucket first). The nightly step
is a safe no-op until SENTIMENT_SRC is set.

Run `python -m ingest.migrate` first so the sentiment_* tables exist.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
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
log = logging.getLogger("sentiment")

# csv has small default field limits; reviews can be long. Lift the cap.
csv.field_size_limit(10 * 1024 * 1024)

_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}

# Overview header (lower-cased, stripped) -> (column, is_int)
_OVERVIEW_MAP = {
    "reviews":                     ("reviews", True),
    "rating":                      ("rating", False),
    "competitorrating":            ("competitor_rating", False),
    "5star":                       ("star5", True),
    "4star":                       ("star4", True),
    "3star":                       ("star3", True),
    "2star":                       ("star2", True),
    "1star":                       ("star1", True),
    "nps":                         ("nps", False),
    "critical":                    ("critical", True),
    "foodaveragesentiment":        ("food_sentiment", False),
    "foodmentions":                ("food_mentions", True),
    "serviceaveragesentiment":     ("service_sentiment", False),
    "servicementions":             ("service_mentions", True),
    "ambienceaveragesentiment":    ("ambience_sentiment", False),
    "ambiencementions":            ("ambience_mentions", True),
    "cleanlinessaveragesentiment": ("cleanliness_sentiment", False),
    "cleanlinessmentions":         ("cleanliness_mentions", True),
    "drinksaveragesentiment":      ("drinks_sentiment", False),
    "drinksmentions":              ("drinks_mentions", True),
    "costaveragesentiment":        ("cost_sentiment", False),
    "costmentions":                ("cost_mentions", True),
}
_OVERVIEW_COLS = [
    "sentiment_label", "period_start", "grain", "reviews", "rating",
    "competitor_rating", "star5", "star4", "star3", "star2", "star1", "nps",
    "critical", "food_sentiment", "food_mentions", "service_sentiment",
    "service_mentions", "ambience_sentiment", "ambience_mentions",
    "cleanliness_sentiment", "cleanliness_mentions", "drinks_sentiment",
    "drinks_mentions", "cost_sentiment", "cost_mentions", "raw", "source_file",
]


# --- small parsing helpers --------------------------------------------------
def clean_num(v) -> float | None:
    """Pull a number out of messy text. '-', '', 'N/A' -> None; handles £ , %."""
    if v is None:
        return None
    s = str(v).strip().replace("£", "").replace(",", "").replace("%", "")
    if s in ("", "-", "N/A", "n/a", "—"):
        return None
    m = re.search(r"-?\d+(\.\d+)?", s)
    return float(m.group()) if m else None


def clean_int(v) -> int | None:
    n = clean_num(v)
    return int(round(n)) if n is not None else None


def parse_date(v) -> date | None:
    if not v:
        return None
    s = str(v).strip().split(" ")[0].split("T")[0]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d/%m/%y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def review_hash(business: str, source: str, dt: str, user: str, text: str) -> str:
    key = "|".join((business or "", source or "", dt or "", user or "", text or ""))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def period_from_filename(name: str) -> tuple[date | None, str | None]:
    """Return (period_start, grain) from an overview filename.

    Two naming conventions are supported:
      * historical monthly:  JAN26.csv
                             -> (2026-01-01, 'month')
      * live daily feed:     Tap_Overview_2026-06-18-2026-06-18.csv
                             -> (2026-06-18, 'day')   (start of the range)

    Returns (None, None) if neither pattern matches.
    """
    base = os.path.basename(name)
    # Live feed: an ISO date in the name -> single-day grain. Take the first
    # date as the period start (the daily files use start==end).
    m = _ISO_DATE_RE.search(base)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        try:
            return date(y, mo, d), "day"
        except ValueError:
            pass
    # Historical monthly: MONYY (e.g. JAN26).
    m = re.match(r"([A-Za-z]{3})\s?(\d{2})", base)
    if m:
        mon = _MONTHS.get(m.group(1).upper())
        if mon:
            return date(2000 + int(m.group(2)), mon, 1), "month"
    return None, None


# --- classify + parse -------------------------------------------------------
def classify(path: str, header: list[str]) -> str:
    """'reviews' | 'overview' | 'unknown', from the header (preferred) then name."""
    h = {c.strip().lower() for c in header}
    if {"business", "source", "date"} <= h:
        return "reviews"
    if "label" in h and "rating" in h:
        return "overview"
    base = os.path.basename(path).upper()
    if base.startswith("REVIEWS") or base.startswith("TAP_REVIEWS"):
        return "reviews"
    if base.startswith("TAP_OVERVIEW") or period_from_filename(base)[0]:
        return "overview"
    return "unknown"


def parse_reviews(path: str) -> tuple[list[tuple], int]:
    """Return (rows ready for upsert, n_skipped). Dedupe within-file on hash."""
    src = os.path.basename(path)
    seen: dict[str, tuple] = {}
    skipped = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        rdr = csv.DictReader(f)
        # Normalise header keys to our expected names (case-insensitive).
        for raw in rdr:
            row = {(k or "").strip().lower(): v for k, v in raw.items()}
            biz = (row.get("business") or "").strip()
            rd = parse_date(row.get("date"))
            if not biz or not rd:
                skipped += 1
                continue
            source = (row.get("source") or "").strip() or None
            user = (row.get("user") or "").strip() or None
            text = (row.get("text") or "").strip() or None
            rating = clean_num(row.get("rating"))
            hh = review_hash(biz, source, str(rd), user, text)
            seen[hh] = (hh, biz, source, rd, rating, text, user, src)
    return list(seen.values()), skipped


def parse_overview(path: str) -> tuple[list[list], int]:
    period, grain = period_from_filename(path)
    if not period:
        log.warning("[%s] no period in filename; skipping overview file", os.path.basename(path))
        return [], 0
    src = os.path.basename(path)
    rows: list[list] = []
    skipped = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        rdr = csv.reader(f)
        header = next(rdr, [])
        idx = {c.strip().lower(): i for i, c in enumerate(header)}
        label_i = idx.get("label")
        for raw in rdr:
            if label_i is None or label_i >= len(raw):
                skipped += 1
                continue
            label = (raw[label_i] or "").strip()
            if not label:
                skipped += 1
                continue
            vals = {"sentiment_label": label, "period_start": period,
                    "grain": grain, "source_file": src}
            for hname, (col, is_int) in _OVERVIEW_MAP.items():
                i = idx.get(hname)
                cell = raw[i] if i is not None and i < len(raw) else None
                vals[col] = clean_int(cell) if is_int else clean_num(cell)
            vals["raw"] = json.dumps(dict(zip(header, raw)))
            rows.append([vals[c] for c in _OVERVIEW_COLS])
    return rows, skipped


# --- DB ---------------------------------------------------------------------
_REVIEW_SQL = """
INSERT INTO sentiment_reviews
    (review_hash, sentiment_label, source, review_date, rating, review_text,
     reviewer, source_file, loaded_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now())
ON CONFLICT (review_hash) DO NOTHING;
"""

_OVERVIEW_SQL = f"""
INSERT INTO sentiment_overview ({','.join(_OVERVIEW_COLS)}, loaded_at)
VALUES ({','.join(['%s'] * len(_OVERVIEW_COLS))}, now())
ON CONFLICT (sentiment_label, period_start, grain) DO UPDATE SET
  {', '.join(f'{c}=EXCLUDED.{c}' for c in _OVERVIEW_COLS
             if c not in ('sentiment_label', 'period_start', 'grain'))},
  loaded_at=now();
"""

_FILE_SEEN_SQL = "SELECT 1 FROM sentiment_files WHERE filename=%s AND sha256=%s"
_FILE_LOG_SQL = """
INSERT INTO sentiment_files (filename, sha256, kind, row_count, loaded_at)
VALUES (%s,%s,%s,%s, now())
ON CONFLICT (filename, sha256) DO UPDATE SET
  kind=EXCLUDED.kind, row_count=EXCLUDED.row_count, loaded_at=now();
"""


def _already_loaded(conn, name: str, sha: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(_FILE_SEEN_SQL, (name, sha))
        return cur.fetchone() is not None


# --- source discovery -------------------------------------------------------
def _iter_files(src: str) -> list[str]:
    if os.path.isfile(src):
        return [src]
    out = []
    for root, _dirs, files in os.walk(src):
        for fn in files:
            if fn.lower().endswith(".csv"):
                out.append(os.path.join(root, fn))
    return sorted(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Import Sentiment Search CSVs")
    p.add_argument("--src", help="file or folder of CSVs (default: $SENTIMENT_SRC)")
    p.add_argument("--dry-run", action="store_true",
                   help="parse + classify + sample, no DB write")
    p.add_argument("--force", action="store_true",
                   help="re-process files even if already logged")
    args = p.parse_args(argv)

    src = args.src or os.getenv("SENTIMENT_SRC")
    if not src:
        # Nightly safe no-op: nothing configured yet (bucket/feed not live).
        log.info("no --src and no SENTIMENT_SRC set; nothing to do (skipping).")
        return 0
    if not os.path.exists(src):
        log.error("source path does not exist: %s", src)
        return 1

    files = _iter_files(src)
    if not files:
        log.warning("no .csv files found under %s", src)
        return 0
    log.info("found %d CSV file(s) under %s", len(files), src)

    conn = None if args.dry_run else psycopg2.connect(settings.database_url)
    n_reviews = n_overview = n_files = 0
    try:
        for path in files:
            name = os.path.basename(path)
            with open(path, newline="", encoding="utf-8-sig") as f:
                header = next(csv.reader(f), [])
            kind = classify(path, header)
            if kind == "unknown":
                log.warning("[%s] could not classify (header=%s); skipping", name, header[:4])
                continue

            sha = file_sha256(path)
            if conn and not args.force and _already_loaded(conn, name, sha):
                log.info("[%s] already loaded (unchanged); skipping", name)
                continue

            if kind == "reviews":
                rows, skipped = parse_reviews(path)
                log.info("[%s] reviews: %d row(s), %d skipped", name, len(rows), skipped)
                if args.dry_run:
                    for r in rows[:3]:
                        log.info("   %s | %s | %s | %s* | %s",
                                 r[3], r[1], r[2], r[4], (r[5] or "")[:60])
                else:
                    with conn.cursor() as cur:
                        psycopg2.extras.execute_batch(cur, _REVIEW_SQL, rows, page_size=500)
                        cur.execute(_FILE_LOG_SQL, (name, sha, "reviews", len(rows)))
                    conn.commit()
                    n_reviews += len(rows)
            else:  # overview
                rows, skipped = parse_overview(path)
                period = rows[0][1] if rows else None
                log.info("[%s] overview: %d site row(s) for %s, %d skipped",
                         name, len(rows), period, skipped)
                if args.dry_run:
                    for r in rows[:2]:
                        log.info("   %s | reviews=%s rating=%s nps=%s food=%s",
                                 r[0], r[3], r[4], r[11], r[13])
                else:
                    with conn.cursor() as cur:
                        psycopg2.extras.execute_batch(cur, _OVERVIEW_SQL, rows, page_size=200)
                        cur.execute(_FILE_LOG_SQL, (name, sha, "overview", len(rows)))
                    conn.commit()
                    n_overview += len(rows)
            n_files += 1
    finally:
        if conn:
            conn.close()

    log.info("done: %d file(s); %d review row(s), %d overview row(s) upserted",
             n_files, n_reviews, n_overview)
    return 0


if __name__ == "__main__":
    sys.exit(main())
