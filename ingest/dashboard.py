"""Tap & Tandoor pocket dashboard -- mobile PWA.

Runs as a SEPARATE, always-on Railway service from the nightly ingest worker:

    gunicorn ingest.dashboard:app

Deliberately does NOT import ingest.config / ingest.db -- it connects with its
own env var (DASHBOARD_DATABASE_URL) pointed at the read-only `dashboard_ro`
Postgres role (SELECT only on a handful of reporting views), never the ingest
pipeline's full-access DATABASE_URL. This is a private single-user tool
reachable on the open internet via a Railway domain, so it sits behind a
single shared HTTP Basic Auth login (DASH_USER / DASH_PASS) rather than the
ingest pipeline's trust model.

SCOPE NOTE (2026-09-25): a first version also scored an "Employee of the
Week" card from v_staff_eotw_day, matching the Metabase EOTW dashboard.
Dropped it -- that view doesn't prune on a business_date filter (same as the
Metabase EOTW cards, which are known to take >30s) and a single-site 7-day
query timed out past 180s. Not something to run in a mobile page's request
path. Re-add only once EOTW has a precomputed/materialised table behind it.

v2 (2026-09-25): added delivery/takeaway split (cashoff_daily), guest metrics
-- spend/head + plates/cover, same definitions as the Guest Metrics Tracker
artifact (v_covers_clean private-function-netted cover basis, and
v_plates_per_cover_site) -- and estate week-to-date / month-to-date totals
with a vs-prior-period comparison.
"""
from __future__ import annotations

import hmac
import logging
import os
import time
from datetime import date, timedelta

import psycopg2
import psycopg2.extras
from flask import Flask, Response, jsonify, render_template, request, send_from_directory

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("dashboard")

app = Flask(__name__)

DATABASE_URL = os.environ["DASHBOARD_DATABASE_URL"]
DASH_USER = os.environ.get("DASH_USER", "")
DASH_PASS = os.environ.get("DASH_PASS", "")

# business_location_id -> display name (see reference_lightspeed_api / CLAUDE.md)
SITES = {
    1718940401139714: "Bournemouth",
    1718940401139717: "Southampton",
    1718940401139718: "Portsmouth",
    1718940401139719: "Peterborough",
    1718940401139720: "Solihull",
}
# cashoff_daily keys sites by name, not business_location_id
NAME_TO_ID = {v: k for k, v in SITES.items()}

_cache: dict = {"at": 0.0, "data": None}
CACHE_TTL = 300  # seconds -- Lightspeed/cashoff data only refreshes overnight anyway


def connect():
    conn = psycopg2.connect(DATABASE_URL.strip())
    conn.autocommit = True
    with conn.cursor() as _c:
        _c.execute("SET statement_timeout = 15000")  # ms -- fail fast, never hang a phone request
    return conn


# --- auth --------------------------------------------------------------
def _check_auth() -> bool:
    if not DASH_USER or not DASH_PASS:
        log.error("DASH_USER/DASH_PASS not set -- refusing all requests")
        return False
    auth = request.authorization
    if not auth:
        return False
    return hmac.compare_digest(auth.username, DASH_USER) and hmac.compare_digest(
        auth.password, DASH_PASS
    )


@app.before_request
def require_auth():
    if request.path == "/healthz":
        return
    if not _check_auth():
        return Response(
            "Sign in required.",
            401,
            {"WWW-Authenticate": 'Basic realm="Tap & Tandoor"'},
        )


# --- date-range helpers for WTD/MTD ---------------------------------------
def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())  # Monday


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _prior_period(start: date, end: date) -> tuple[date, date]:
    """Same-length window immediately before [start, end]."""
    length = (end - start).days
    prior_end = start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=length)
    return prior_start, prior_end


def _sum_in_range(daily: dict, lo: date, hi: date) -> float:
    return sum(v for d, v in daily.items() if lo <= d <= hi)


# --- data ----------------------------------------------------------------
def fetch_dashboard() -> dict:
    conn = connect()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Last night per site: each site's own most recent trading day (sites
        # ingest on the same nightly cron, but can drift a day on a bad run).
        cur.execute(
            """
            WITH latest AS (
                SELECT business_location_id, MAX(biz_date) AS d
                FROM v_site_day_apc GROUP BY business_location_id
            )
            SELECT s.business_location_id, s.biz_date,
                   COALESCE(s.dining_revenue,0) + COALESCE(s.bar_revenue,0) AS revenue,
                   s.covers_clean AS covers,
                   s.apc_dining AS apc
            FROM v_site_day_apc s
            JOIN latest l ON l.business_location_id = s.business_location_id
                          AND l.d = s.biz_date;
            """
        )
        last_night = {r["business_location_id"]: r for r in cur.fetchall()}

        # 65-day revenue-by-day per site: 14 days feeds the sparkline, the
        # full range feeds WTD/MTD-vs-prior-period math below.
        cur.execute(
            """
            SELECT business_location_id, biz_date,
                   COALESCE(dining_revenue,0) + COALESCE(bar_revenue,0) AS revenue
            FROM v_site_day_apc
            WHERE biz_date >= CURRENT_DATE - INTERVAL '65 days'
            ORDER BY business_location_id, biz_date;
            """
        )
        revenue_by_site: dict[int, dict[date, float]] = {}
        trend14: dict[int, list[dict]] = {}
        cutoff14 = date.today() - timedelta(days=14)
        for r in cur.fetchall():
            revenue_by_site.setdefault(r["business_location_id"], {})[r["biz_date"]] = float(
                r["revenue"] or 0
            )
            if r["biz_date"] >= cutoff14:
                trend14.setdefault(r["business_location_id"], []).append(
                    {"date": r["biz_date"].isoformat(), "revenue": float(r["revenue"] or 0)}
                )

        # 7-day trailing average revenue per site, for a quick vs-usual read.
        cur.execute(
            """
            SELECT business_location_id,
                   AVG(COALESCE(dining_revenue,0) + COALESCE(bar_revenue,0)) AS avg_rev
            FROM v_site_day_apc
            WHERE biz_date >= CURRENT_DATE - INTERVAL '8 days'
              AND biz_date < CURRENT_DATE
            GROUP BY business_location_id;
            """
        )
        avg7 = {r["business_location_id"]: float(r["avg_rev"] or 0) for r in cur.fetchall()}

        # Guest metrics -- same definitions as the Guest Metrics Tracker
        # artifact: cover basis is v_covers_clean.is_dining_check with the
        # private-function-split netting rule; spend/head = fb_rev/covers.
        cur.execute(
            """
            SELECT business_location_id, biz_date,
                   SUM(fb_rev) FILTER (
                       WHERE is_dining_check
                         AND NOT (nb_covers >= 5 AND fb_rev / NULLIF(nb_covers,0) < 3)
                   ) AS fb_rev_clean,
                   SUM(nb_covers) FILTER (
                       WHERE is_dining_check
                         AND NOT (nb_covers >= 5 AND fb_rev / NULLIF(nb_covers,0) < 3)
                   ) AS covers_clean
            FROM v_covers_clean
            WHERE biz_date >= CURRENT_DATE - INTERVAL '8 days'
            GROUP BY business_location_id, biz_date
            ORDER BY business_location_id, biz_date;
            """
        )
        spend_rows: dict[int, list[dict]] = {}
        for r in cur.fetchall():
            spend_rows.setdefault(r["business_location_id"], []).append(r)

        cur.execute(
            """
            SELECT business_location_id, business_date, plates, covers, plates_per_cover
            FROM v_plates_per_cover_site
            WHERE business_date >= CURRENT_DATE - INTERVAL '8 days'
            ORDER BY business_location_id, business_date;
            """
        )
        plates_rows: dict[int, list[dict]] = {}
        for r in cur.fetchall():
            plates_rows.setdefault(r["business_location_id"], []).append(r)

        # Delivery/takeaway split, last 7 days, from cashoff_daily (keyed by
        # site NAME, not business_location_id).
        cur.execute(
            """
            SELECT site,
                   SUM(COALESCE(uber_eats,0)) AS uber_eats,
                   SUM(COALESCE(just_eat,0)) AS just_eat,
                   SUM(COALESCE(deliveroo,0)) AS deliveroo,
                   SUM(COALESCE(online_orders,0)) AS online_orders
            FROM cashoff_daily
            WHERE business_date >= CURRENT_DATE - INTERVAL '7 days'
            GROUP BY site;
            """
        )
        delivery = {NAME_TO_ID.get(r["site"]): r for r in cur.fetchall() if r["site"] in NAME_TO_ID}

        cur.close()

        today = date.today()
        wk_start = _week_start(today)
        wk_prior_start, wk_prior_end = _prior_period(wk_start, today)
        mo_start = _month_start(today)
        mo_prior_start, mo_prior_end = _prior_period(mo_start, today)

        estate_wtd = estate_wtd_prior = estate_mtd = estate_mtd_prior = 0.0

        sites_out = []
        for bl_id, name in sorted(SITES.items(), key=lambda kv: kv[1]):
            daily = revenue_by_site.get(bl_id, {})
            wtd = _sum_in_range(daily, wk_start, today)
            wtd_prior = _sum_in_range(daily, wk_prior_start, wk_prior_end)
            mtd = _sum_in_range(daily, mo_start, today)
            mtd_prior = _sum_in_range(daily, mo_prior_start, mo_prior_end)
            estate_wtd += wtd
            estate_wtd_prior += wtd_prior
            estate_mtd += mtd
            estate_mtd_prior += mtd_prior

            s_rows = spend_rows.get(bl_id, [])
            p_rows = plates_rows.get(bl_id, [])
            guest = _guest_metrics(s_rows, p_rows)

            d = delivery.get(bl_id)
            delivery_out = (
                {
                    "uber_eats": float(d["uber_eats"]),
                    "just_eat": float(d["just_eat"]),
                    "deliveroo": float(d["deliveroo"]),
                    "online_orders": float(d["online_orders"]),
                    "total": float(d["uber_eats"] + d["just_eat"] + d["deliveroo"] + d["online_orders"]),
                }
                if d
                else None
            )

            sites_out.append(
                {
                    "id": bl_id,
                    "name": name,
                    "last_night": _fmt_last_night(last_night.get(bl_id)),
                    "avg_7d": round(avg7.get(bl_id, 0), 2),
                    "trend": trend14.get(bl_id, []),
                    "guest": guest,
                    "delivery_7d": delivery_out,
                    "wtd": _period(wtd, wtd_prior),
                    "mtd": _period(mtd, mtd_prior),
                }
            )

        return {
            "generated_at": time.time(),
            "estate": {
                "wtd": _period(estate_wtd, estate_wtd_prior),
                "mtd": _period(estate_mtd, estate_mtd_prior),
            },
            "sites": sites_out,
        }
    finally:
        conn.close()


def _period(current: float, prior: float) -> dict:
    pct = ((current - prior) / prior * 100) if prior else None
    return {"total": round(current, 2), "prior": round(prior, 2), "pct_vs_prior": round(pct, 1) if pct is not None else None}


def _guest_metrics(spend_rows, plates_rows) -> dict | None:
    if not spend_rows and not plates_rows:
        return None
    spend_last = spend_rows[-1] if spend_rows else None
    plates_last = plates_rows[-1] if plates_rows else None

    def _rate(rows, num_key, den_key):
        num = sum(float(r[num_key] or 0) for r in rows)
        den = sum(float(r[den_key] or 0) for r in rows)
        return (num / den) if den else None

    spend_per_head = (
        float(spend_last["fb_rev_clean"]) / float(spend_last["covers_clean"])
        if spend_last and spend_last["covers_clean"]
        else None
    )
    plates_per_cover = float(plates_last["plates_per_cover"]) if plates_last and plates_last["plates_per_cover"] is not None else None

    return {
        "spend_per_head": round(spend_per_head, 2) if spend_per_head is not None else None,
        "spend_per_head_avg7": round(_rate(spend_rows, "fb_rev_clean", "covers_clean"), 2) if spend_rows else None,
        "plates_per_cover": round(plates_per_cover, 2) if plates_per_cover is not None else None,
        "plates_per_cover_avg7": round(_rate(plates_rows, "plates", "covers"), 2) if plates_rows else None,
    }


def _fmt_last_night(r):
    if not r:
        return None
    return {
        "date": r["biz_date"].isoformat(),
        "revenue": float(r["revenue"] or 0),
        "covers": int(r["covers"] or 0),
        "apc": float(r["apc"]) if r["apc"] is not None else None,
    }


# --- routes --------------------------------------------------------------
@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/data")
def api_data():
    now = time.time()
    if _cache["data"] is None or now - _cache["at"] > CACHE_TTL:
        try:
            _cache["data"] = fetch_dashboard()
            _cache["at"] = now
        except Exception:
            log.exception("fetch_dashboard failed")
            if _cache["data"] is None:
                return jsonify({"error": "data unavailable"}), 502
    return jsonify(_cache["data"])


@app.get("/manifest.json")
def manifest():
    return send_from_directory(
        app.static_folder, "manifest.json", mimetype="application/manifest+json"
    )


@app.get("/sw.js")
def sw():
    return send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")
