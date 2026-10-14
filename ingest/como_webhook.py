"""Como Export Event webhook receiver (always-on web service).

Runs as its OWN Railway service (like StoreKit):

    gunicorn ingest.como_webhook:app

Como's Export Event API documents no signature scheme -- only HTTPS + POST --
so the endpoint is protected by an unguessable token in the URL path:

    https://<service>.up.railway.app/webhooks/como/<COMO_WEBHOOK_TOKEN>

Fails CLOSED: no token configured -> every request is 404.

SWITCHED OFF BY DEFAULT: unless COMO_ENABLED=1 the receiver only parses,
logs a PII-free one-liner and returns 200 (dry-run). Nothing touches the DB.
Como disables an endpoint after consecutive days of bad responses, so we always
acknowledge (200) valid-token requests, and return 500 only for a genuine DB
failure so Como retries.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging

from flask import Flask, request

from . import como, db
from .config import settings

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("como_webhook")

app = Flask(__name__)


def _token_ok(token: str) -> bool:
    want = settings.como_webhook_token
    return bool(want) and hmac.compare_digest(token.encode(), want.encode())


@app.get("/health")
def health():
    """Identify the serving process without leaking secrets."""
    t = settings.como_webhook_token
    return {
        "status": "ok",
        "enabled": settings.como_enabled,
        "token_set": bool(t),
        "token_sha": hashlib.sha256(t.encode()).hexdigest()[:8] if t else None,
        "hash_salt_set": bool(settings.como_hash_salt),
    }, 200


@app.post("/webhooks/como/<token>")
def como_webhook(token: str):
    if not _token_ok(token):
        return {"error": "not found"}, 404

    raw = request.get_data()
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
    except ValueError:
        log.warning("COMO bad JSON (%d bytes).", len(raw))
        return {"error": "bad payload"}, 400

    if not settings.como_enabled:
        # Dry-run: prove delivery works without writing anything.
        print(f"COMO DRYRUN {como.summarise(payload)}", flush=True)
        return {"status": "dry-run"}, 200

    conn = db.connect()
    try:
        counts = como.store_event(conn, payload, raw)
        conn.commit()
        log.info("Como %s stored %s", payload.get("Trigger"), counts)
        return {"status": "ok", **counts}, 200
    except Exception:                       # noqa: BLE001
        conn.rollback()
        log.exception("Error handling Como %s.", payload.get("Trigger"))
        return {"error": "processing failed"}, 500     # Como retries
    finally:
        conn.close()


if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
