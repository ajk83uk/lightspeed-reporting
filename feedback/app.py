"""Tap & Tandoor guest feedback service (feedback.tapandtandoor.co.uk).

Routes
  GET  /r/<token>                  the survey page (static HTML)
  GET  /api/survey/<token>         survey data for one guest's visit
  POST /api/survey/<token>/submit  save answers; the link is consumed in the same transaction
  GET  /u/<token>                  unsubscribe confirmation page
  POST /u/<token>                  unsubscribe (also the RFC 8058 one-click endpoint)
  GET  /health                     liveness plus a database ping

A link works once. After a successful submit, or 14 days after sending, it cannot be used again.
"""
import html
import re
import uuid
from datetime import date, datetime

from flask import Flask, abort, jsonify, make_response, request, send_from_directory

from . import config
from .db import get_conn, q

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

CATEGORIES = ("food", "drink", "service", "atmosphere")
LOW_STAR = 3  # a rating at or below this is a low score
OPEN_STATES = ("queued", "sent", "opened")
MAX_TEXT = 2000
MAX_TAGS = 12


# ---------------------------------------------------------------- helpers
def parse_token(raw: str):
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError):
        abort(404)


def nice_date(d: date) -> str:
    return f"{d.strftime('%A')} {d.day} {d.strftime('%B')}"


def site_info(business_location_id: int) -> dict:
    return config.SITES.get(int(business_location_id), {"name": "Tap & Tandoor", "phone": "", "google_review_url": ""})


def no_store(resp):
    resp.headers.setdefault("Cache-Control", "no-store")
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@app.after_request
def _headers(resp):
    return no_store(resp)


LOAD_REQUEST = """
select r.token, r.business_location_id, r.booking_ref_no, r.visit_date, r.status,
       coalesce(r.sent_at, r.created_at) as live_from,
       (coalesce(r.sent_at, r.created_at) < now() - make_interval(days => %(days)s)) as expired,
       b.first_name, b.email, r.covers
from feedback_requests r
left join bookings b on b.business_location_id = r.business_location_id
                    and b.booking_ref_no = r.booking_ref_no
                    and b.booking_date = r.visit_date
where r.token = %(token)s
"""

LOAD_DISHES = """
select dish_name, sum(quantity)::int as qty
from v_feedback_dishes
where business_location_id = %(loc)s and booking_ref_no = %(ref)s and visit_date = %(vd)s
group by dish_name
order by qty desc, dish_name
limit 40
"""


def load_request(conn, token):
    return q(conn, LOAD_REQUEST, {"token": token, "days": config.LINK_LIFETIME_DAYS}).fetchone()


def load_dishes(conn, row):
    return q(conn, 
        LOAD_DISHES, {"loc": row["business_location_id"], "ref": row["booking_ref_no"], "vd": row["visit_date"]}
    ).fetchall()


def link_state(row) -> str:
    """open | completed | expired | gone"""
    if row["status"] == "completed":
        return "completed"
    if row["status"] in ("suppressed", "failed"):
        return "gone"
    if row["expired"]:
        return "expired"
    return "open"


# ---------------------------------------------------------------- pages
@app.get("/")
def home():
    return "", 404


@app.get("/health")
def health():
    try:
        with get_conn() as conn:
            q(conn, "select 1")
        return jsonify(ok=True)
    except Exception:  # noqa: BLE001
        return jsonify(ok=False), 503


@app.get("/assets/logo.png")
def logo():
    resp = send_from_directory("static", "logo.png")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.get("/r/<token>")
def survey_page(token):
    parse_token(token)
    return send_from_directory("static", "survey.html")


# ---------------------------------------------------------------- survey API
@app.get("/api/survey/<token>")
def survey_data(token):
    tok = parse_token(token)
    with get_conn() as conn:
        row = load_request(conn, tok)
        if row is None:
            abort(404)
        state = link_state(row)
        if state == "gone":
            abort(404)
        site = site_info(row["business_location_id"])
        if state != "open":
            return jsonify(status=state, site=site["name"])
        q(conn, 
            "update feedback_requests set opened_at = coalesce(opened_at, now()), "
            "status = case when status in ('queued','sent') then 'opened' else status end where token = %s",
            (tok,),
        )
        dishes = load_dishes(conn, row)
        conn.commit()
    name = (row["first_name"] or "").strip()
    return jsonify(
        status="open",
        site=site["name"],
        guestName=name.title() if name else "",
        visitDate=nice_date(row["visit_date"]),
        dishes=[{"name": d["dish_name"], "qty": d["qty"]} for d in dishes],
        googleReviewUrl=site["google_review_url"],
        sitePhone=site["phone"],
    )


def clean_text(value, limit=MAX_TEXT) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def clean_tags(value) -> list:
    if not isinstance(value, list):
        return []
    out = []
    for t in value[:MAX_TAGS]:
        if isinstance(t, str) and re.fullmatch(r"[A-Za-z0-9 _\-&/']{1,40}", t):
            out.append(t)
    return out


def clean_rating(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 5 else None


def parse_submission(body):
    """Validate and normalise the payload. Raises ValueError with a message for bad input."""
    if not isinstance(body, dict):
        raise ValueError("bad body")
    cats_in = body.get("categories")
    if not isinstance(cats_in, dict):
        raise ValueError("categories missing")
    cats = {}
    for c in CATEGORIES:
        a = cats_in.get(c) or {}
        rating = clean_rating(a.get("rating"))
        cats[c] = {
            "rating": rating,
            "skipped": rating is None,
            "tags": clean_tags(a.get("tags")),
            "comment": clean_text(a.get("comment")),
        }
    if not any(v["rating"] for v in cats.values()):
        raise ValueError("at least one rating is required")
    dishes_in = body.get("dishes") if isinstance(body.get("dishes"), list) else []
    dishes = []
    for d in dishes_in[:60]:
        if not isinstance(d, dict):
            continue
        rating = clean_rating(d.get("rating"))
        name = clean_text(d.get("dish"), 200)
        if rating and name:
            dishes.append({"dish": name, "rating": rating, "comment": clean_text(d.get("comment"))})
    return {
        "categories": cats,
        "dishes": dishes,
        "other": clean_text(body.get("other")),
        "contact_ok": bool(body.get("contactOk")),
    }


def build_alerts(sub, site_name, row):
    """Head Office alerts: one low_score and/or one health alert per response."""
    low = [f"{c} {v['rating']}*" for c, v in sub["categories"].items() if v["rating"] and v["rating"] <= LOW_STAR]
    low += [f"{d['dish']} {d['rating']}*" for d in sub["dishes"] if d["rating"] <= LOW_STAR]
    health = any("health" in v["tags"] for v in sub["categories"].values())
    who = (row["first_name"] or "Guest").strip().title()
    head = f"{site_name}, {nice_date(row['visit_date'])}, {who}, booking {row['booking_ref_no']}"
    snippets = [f"{c}: {v['comment']}" for c, v in sub["categories"].items() if v["comment"] and (v["rating"] or 5) <= LOW_STAR]
    if sub["other"]:
        snippets.append(f"other: {sub['other']}")
    body = " | ".join(snippets)[:900]
    alerts = []
    if health:
        alerts.append(("health", f"{head}. Guest reported an allergy, illness or hygiene concern. {body}"))
    if low:
        alerts.append(("low_score", f"{head}. Low scores: {', '.join(low)}. {body}"))
    return alerts


@app.post("/api/survey/<token>/submit")
def submit(token):
    tok = parse_token(token)
    try:
        sub = parse_submission(request.get_json(silent=True))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    with get_conn() as conn:
        row = load_request(conn, tok)
        if row is None:
            abort(404)
        state = link_state(row)
        if state == "completed":
            return jsonify(error="already completed"), 409
        if state == "expired":
            return jsonify(error="expired"), 410
        if state == "gone":
            abort(404)
        # Consume the link. Only one concurrent request can win this update.
        won = q(conn, 
            "update feedback_requests set status = 'completed', completed_at = now(), "
            "opened_at = coalesce(opened_at, now()) "
            "where token = %s and status = any(%s) returning token",
            (tok, list(OPEN_STATES)),
        ).fetchone()
        if not won:
            return jsonify(error="already completed"), 409
        rated = [v["rating"] for v in sub["categories"].values() if v["rating"]]
        overall = max(1, min(5, int(sum(rated) / len(rated) + 0.5)))  # half rounds up
        health = any("health" in v["tags"] for v in sub["categories"].values())
        q(conn, 
            "insert into feedback_responses (token, overall_rating, comment, flagged_health, contact_ok, other_text) "
            "values (%s,%s,%s,%s,%s,%s)",
            (tok, overall, sub["other"] or None, health, sub["contact_ok"], sub["other"] or None),
        )
        for c, v in sub["categories"].items():
            q(conn, 
                "insert into feedback_category_ratings (token, category, rating, skipped, tags, comment) "
                "values (%s,%s,%s,%s,%s,%s)",
                (tok, c, v["rating"], v["skipped"], v["tags"], v["comment"] or None),
            )
        allowed = {d["dish_name"]: d for d in load_dishes(conn, row)}
        seen = set()
        for d in sub["dishes"]:
            known = allowed.get(d["dish"])
            if not known or d["dish"] in seen:  # ignore dishes the guest did not have
                continue
            seen.add(d["dish"])
            q(conn, 
                "insert into feedback_dish_ratings (token, dish_name, quantity, rating, comment) values (%s,%s,%s,%s,%s)",
                (tok, d["dish"], known["qty"], d["rating"], d["comment"] or None),
            )
        site_name = site_info(row["business_location_id"])["name"]
        for kind, summary in build_alerts(sub, site_name, row):
            q(conn, 
                "insert into feedback_alerts (token, kind, summary) values (%s,%s,%s) on conflict do nothing",
                (tok, kind, summary),
            )
        conn.commit()
    return jsonify(ok=True)


# ---------------------------------------------------------------- unsubscribe
PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>Tap &amp; Tandoor</title>
<style>body{{margin:0;font:17px/1.5 system-ui,sans-serif;background:#f6f4ee;color:#17150f}}
header{{background:#141414;color:#ffd503;padding:18px 20px;font-weight:700;letter-spacing:.02em}}
main{{max-width:480px;margin:32px auto;padding:0 20px}}
button{{font:inherit;font-weight:700;background:#ffd503;color:#17150f;border:0;border-radius:12px;padding:14px 22px;cursor:pointer;width:100%}}
</style></head><body><header>TAP &amp; TANDOOR</header><main>{body}</main></body></html>"""


def page(body: str, status=200):
    resp = make_response(PAGE.format(body=body), status)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


@app.get("/u/<token>")
def unsubscribe_page(token):
    tok = parse_token(token)
    with get_conn() as conn:
        if load_request(conn, tok) is None:
            abort(404)
    return page(
        "<h1>Stop feedback emails?</h1><p>We will not send you any more survey emails.</p>"
        f'<form method="post" action="/u/{html.escape(str(tok))}"><button type="submit">Yes, unsubscribe me</button></form>'
    )


@app.post("/u/<token>")
def unsubscribe(token):
    tok = parse_token(token)
    with get_conn() as conn:
        row = load_request(conn, tok)
        if row is None:
            abort(404)
        if row["email"]:
            q(conn, 
                "insert into feedback_suppressions (email_lower, reason) values (%s, 'unsubscribed') on conflict do nothing",
                (row["email"].strip().lower(),),
            )
            conn.commit()
    return page("<h1>You are unsubscribed</h1><p>We will not email you survey requests again. Thank you.</p>")


if __name__ == "__main__":
    app.run(port=8080, debug=False)
