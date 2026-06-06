"""One-time helper to obtain the first refresh token (authorization-code flow).

Run once after you have a V2 client_id/secret (begins with devp-v2-prod- or
devp-v2-demo-) and a registered redirect URI. It builds the authorize URL, you
log in with your backoffice POS admin credentials and approve, Lightspeed
redirects to your redirect URI with ?code=..., you paste that code back, and it
exchanges it for tokens using the Basic auth header.

It then stores the refresh token in the oauth_token table (so the worker/cron
picks it up automatically). Run `python -m ingest.migrate` first so the table
exists. If the DB isn't reachable it just prints the token for you to set as
LS_REFRESH_TOKEN.

    python -m ingest.get_refresh_token
"""
from __future__ import annotations

import urllib.parse

import requests

from . import db
from .auth import _basic_auth_header
from .config import settings


def main() -> None:
    if not settings.client_id or not settings.client_secret:
        raise SystemExit("Set LS_CLIENT_ID and LS_CLIENT_SECRET in .env first.")

    params = {
        "response_type": "code",
        "client_id": settings.client_id,
        "scope": settings.scopes,                 # incl. offline_access
        "redirect_uri": settings.redirect_uri,
    }
    print("\n1) Open this URL in your browser and approve access:\n")
    print(settings.authorize_url + "?" + urllib.parse.urlencode(params))
    print(f"\n2) You'll be redirected to {settings.redirect_uri}?...&code=XXXX")
    print("   The code is single-use and expires fast -- paste it promptly.")
    raw = input("\n3) Paste the 'code' value (or the whole redirect URL): ").strip()
    code = _extract_code(raw)

    resp = requests.post(
        settings.token_url,
        headers={
            "Authorization": _basic_auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.redirect_uri,
        },
        timeout=settings.http_timeout,
    )
    resp.raise_for_status()
    tokens = resp.json()
    refresh = tokens.get("refresh_token")

    print("\n--- SUCCESS -------------------------------------------------")
    print("access_token  expires_in:", tokens.get("expires_in"), "s")
    print("refresh_token expires_in:", tokens.get("refresh_expires_in"), "s")

    try:
        conn = db.connect()
        db.save_refresh_token(conn, refresh)
        conn.close()
        print("\nStored refresh token in the database (oauth_token). Done.")
    except Exception as exc:  # noqa: BLE001
        print(f"\nCould not store in DB ({exc}).")
        print("Set this in your .env instead:\n")
        print("LS_REFRESH_TOKEN=" + str(refresh))


def _extract_code(raw: str) -> str:
    """Accept either a bare code or a full redirect URL and pull out ?code=."""
    if "code=" in raw:
        qs = urllib.parse.urlparse(raw).query or raw.split("?", 1)[-1]
        params = urllib.parse.parse_qs(qs)
        if "code" in params:
            return params["code"][0]
    return raw


if __name__ == "__main__":
    main()
