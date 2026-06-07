"""OAuth2 access-token management (Lightspeed K-Series V2 / Keycloak).

Key facts from Lightspeed's OAuth quick-start that this implements:
  * Authorization-code grant on a separate auth host (auth.lsk-prod.app / -demo).
  * Client credentials go in an HTTP Basic Authorization header
    (base64 of "client_id:client_secret") -- NOT in the request body.
    Sending them in the body is the documented #1 failure.
  * The refresh token ROTATES: every refresh returns a NEW refresh token and the
    old one stops working. We persist the latest token in the oauth_token table
    so the next cron run (a fresh process) uses a valid token. The env var
    LS_REFRESH_TOKEN is only the first-time seed.
  * Token lifetimes are read dynamically from expires_in -- never hardcoded.
"""
from __future__ import annotations

import base64
import logging
import threading
import time

import requests

from . import db
from .config import settings

log = logging.getLogger(__name__)


def _basic_auth_header() -> str:
    raw = f"{settings.client_id}:{settings.client_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


class TokenManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    def get_token(self) -> str:
        with self._lock:
            # Refresh ~60s before expiry to avoid edge-of-expiry 401s.
            if self._access_token and time.time() < self._expires_at - 60:
                return self._access_token
            return self._refresh()

    def _current_refresh_token(self, conn) -> str:
        # Prefer the rotating token saved in the DB; fall back to the env seed.
        stored = db.get_stored_refresh_token(conn)
        if stored:
            return stored
        if not settings.refresh_token:
            raise RuntimeError(
                "No refresh token available: set LS_REFRESH_TOKEN (seed) or run "
                "get_refresh_token.py first."
            )
        return settings.refresh_token

    def _refresh(self) -> str:
        settings.require_credentials()
        conn = db.connect()
        try:
            refresh_token = self._current_refresh_token(conn)
            # --- TEMP DIAGNOSTIC (safe: hashes + lengths only, no secrets) ----
            import hashlib as _hl
            _fp = lambda s: _hl.sha256((s or "").encode()).hexdigest()[:12]
            log.info(
                "AUTH-DIAG url=%s cid_len=%d cid_fp=%s sec_len=%d sec_fp=%s "
                "tok_len=%d tok_fp=%s",
                settings.token_url,
                len(settings.client_id), _fp(settings.client_id),
                len(settings.client_secret), _fp(settings.client_secret),
                len(refresh_token), _fp(refresh_token),
            )
            # ------------------------------------------------------------------
            resp = requests.post(
                settings.token_url,
                headers={
                    "Authorization": _basic_auth_header(),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                timeout=settings.http_timeout,
            )
            if resp.status_code >= 400:  # TEMP DIAGNOSTIC: show Keycloak's reason
                log.error("AUTH-DIAG fail %s body=%s", resp.status_code, resp.text[:400])
            resp.raise_for_status()
            payload = resp.json()

            self._access_token = payload["access_token"]
            self._expires_at = time.time() + int(payload.get("expires_in", 300))

            # Persist the rotated refresh token immediately.
            new_refresh = payload.get("refresh_token")
            if new_refresh and new_refresh != refresh_token:
                db.save_refresh_token(conn, new_refresh)
                log.info("stored rotated refresh token")
            return self._access_token
        finally:
            conn.close()


token_manager = TokenManager()
