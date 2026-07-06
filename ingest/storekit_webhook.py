"""StoreKit webhook receiver (always-on web service).

Runs as a SEPARATE Railway service from the nightly cron worker:

    gunicorn ingest.storekit_webhook:app

StoreKit (via Svix) POSTs signed events here. We verify the signature, dedupe
on the svix-id, then upsert order.created/accepted into `storekit_orders` and
patch status from the slim lifecycle/refund events. Everything downstream is
the v_storekit_orders_* views -> Metabase.

Field mapping + SQL live in ingest/db.py (repo convention); this module only
does transport: verify -> route -> 2xx.
"""
from __future__ import annotations

import json
import logging

from flask import Flask, request

from . import db
from .config import settings

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("storekit_webhook")

app = Flask(__name__)

# Events that carry the full financial payload (same shape for both).
_FULL_EVENTS = {"order.created", "order.accepted"}
# Slim lifecycle/refund events we patch onto an existing order.
_STATUS_EVENTS = set(db._STOREKIT_EVENT_STATUS)


def _verify(raw: bytes, headers) -> dict | None:
    """Return the parsed payload if the Svix signature is valid, else None.

    Fails CLOSED: if no secret is configured we reject, unless STOREKIT_SKIP_VERIFY
    is set for local testing.
    """
    if settings.storekit_skip_verify:
        return json.loads(raw or b"{}")
    if not settings.storekit_webhook_secret:
        log.error("STOREKIT_WEBHOOK_SECRET not set; rejecting webhook.")
        return None
    try:
        from svix.webhooks import Webhook, WebhookVerificationError
    except ImportError:
        log.error("svix package not installed; cannot verify signatures.")
        return None
    svix_headers = {
        "svix-id": headers.get("svix-id", ""),
        "svix-timestamp": headers.get("svix-timestamp", ""),
        "svix-signature": headers.get("svix-signature", ""),
    }
    try:
        wh = Webhook(settings.storekit_webhook_secret)
        return wh.verify(raw, svix_headers)
    except WebhookVerificationError as exc:
        log.warning("Signature verification failed: %s", exc)
        return None


@app.get("/health")
def health():
    return {"status": "ok"}, 200


@app.post("/webhooks/storekit")
def storekit_webhook():
    raw = request.get_data()
    payload = _verify(raw, request.headers)
    if payload is None:
        return {"error": "invalid signature"}, 401

    event = payload.get("event", "")
    data = payload.get("data") or {}
    svix_id = request.headers.get("svix-id", "")
    # best-effort order id for the dedupe log
    order_id = data.get("id") or (data.get("order") or {}).get("id")

    conn = db.connect()
    try:
        # Dedupe gate: at-least-once delivery means re-sends are expected.
        if db.seen_webhook(conn, svix_id, event, order_id):
            conn.commit()
            log.info("Duplicate %s (svix-id=%s) ignored.", event, svix_id)
            return {"status": "duplicate"}, 200

        if event in _FULL_EVENTS:
            db.upsert_storekit_order(conn, data, svix_id, event)
            if event == "order.accepted":
                db.patch_storekit_status(conn, data, svix_id, event)
            log.info("Stored %s order %s (venue %s).",
                     event, order_id, (data.get("venue") or {}).get("id"))
        elif event in _STATUS_EVENTS:
            db.patch_storekit_status(conn, data, svix_id, event)
            log.info("Patched %s -> %s.", order_id, event)
        else:
            # store/printer/payout/payment_link events: not used for reporting.
            log.info("Ignored event %s.", event)

        conn.commit()
        return {"status": "ok"}, 200
    except Exception:                       # noqa: BLE001 - always 2xx-or-5xx cleanly
        conn.rollback()
        log.exception("Error handling %s (svix-id=%s).", event, svix_id)
        # 500 so Svix retries with backoff.
        return {"error": "processing failed"}, 500
    finally:
        conn.close()


if __name__ == "__main__":
    # Local dev only; production uses gunicorn (see deploy/STOREKIT.md).
    app.run(host="0.0.0.0", port=int(__import__("os").getenv("PORT", "8080")))
