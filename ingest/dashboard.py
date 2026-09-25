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
"""
from __future__ import annotations

import hmac
import logging
import os
import time

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

        # 14-day revenue trend per site, for the sparkline.
        cur.execute(
            """
            SELECT business_location_id, biz_date,
                   COALESCE(dining_revenue,0) + COALESCE(bar_revenue,0) AS revenue
            FROM v_site_day_apc
            WHERE biz_date >= CURRENT_DATE - INTERVAL '14 days'
            ORDER BY business_location_id, biz_date;
            """
        )
        trend: dict = {}
        for r in cur.fetchall():
            trend.setdefault(r["business_location_id"], []).append(
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

        cur.close()
        return {
            "generated_at": time.time(),
            "sites": [
                {
                    "id": bl_id,
                    "name": name,
                    "last_night": _fmt_last_night(last_night.get(bl_id)),
                    "avg_7d": round(avg7.get(bl_id, 0), 2),
                    "trend": trend.get(bl_id, []),
                }
                for bl_id, name in sorted(SITES.items(), key=lambda kv: kv[1])
            ],
        }
    finally:
        conn.close()


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
