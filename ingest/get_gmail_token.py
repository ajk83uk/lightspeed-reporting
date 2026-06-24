"""One-time helper to mint a Gmail refresh token (authorization-code flow).

The email route reads attachments from a personal googlemail mailbox, which the
Gmail API can only reach with OAuth *user* credentials (a service account can't
impersonate a consumer account). Run this once to get GMAIL_REFRESH_TOKEN.

Prereqs (Google Cloud console, one-off):
  1. Create / pick a project, enable the *Gmail API*.
  2. Configure the OAuth consent screen (External; add your Google account as a
     test user). Scope used here: gmail.modify (covers read + the optional
     processed-label).
  3. Create an OAuth client ID of type *Desktop app*. Put its id/secret in .env
     as GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET.

Then:
    python -m ingest.get_gmail_token

It prints an authorize URL; approve in the browser, copy the resulting code (or
the whole redirect URL) back in, and it prints GMAIL_REFRESH_TOKEN to set in
.env / Railway. Uses only `requests` (already a dependency).
"""
from __future__ import annotations

import urllib.parse

import requests

from .config import settings

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
# Desktop-app clients use the loopback/OOB style; we ask the user to paste the
# code from the redirected localhost URL.
REDIRECT_URI = "http://localhost"
SCOPES = "https://www.googleapis.com/auth/gmail.modify"


def main() -> None:
    if not settings.gmail_client_id or not settings.gmail_client_secret:
        raise SystemExit("Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in .env first.")

    params = {
        "response_type": "code",
        "client_id": settings.gmail_client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "access_type": "offline",   # required to get a refresh_token
        "prompt": "consent",        # force a refresh_token even on re-consent
    }
    print("\n1) Open this URL, sign in with the mailbox that receives the feed, "
          "and approve:\n")
    print(AUTH_URL + "?" + urllib.parse.urlencode(params))
    print(f"\n2) The browser will redirect to {REDIRECT_URI}/?code=XXXX "
          "(the page itself may fail to load -- that's fine).")
    raw = input("\n3) Paste the 'code' value (or the whole redirect URL): ").strip()
    code = _extract_code(raw)

    resp = requests.post(
        settings.gmail_token_url,
        data={
            "code": code,
            "client_id": settings.gmail_client_id,
            "client_secret": settings.gmail_client_secret,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=60,
    )
    if resp.status_code != 200:
        raise SystemExit(f"Token exchange failed ({resp.status_code}): {resp.text}")
    tokens = resp.json()
    refresh = tokens.get("refresh_token")

    print("\n--- SUCCESS -------------------------------------------------")
    print("access_token expires_in:", tokens.get("expires_in"), "s")
    if not refresh:
        print("\nNo refresh_token returned. Revoke prior access at "
              "https://myaccount.google.com/permissions and re-run (the "
              "'prompt=consent' + 'access_type=offline' combo is required).")
        return
    print("\nSet this in .env (and Railway variables):\n")
    print("GMAIL_REFRESH_TOKEN=" + str(refresh))


def _extract_code(raw: str) -> str:
    if "code=" in raw:
        qs = urllib.parse.urlparse(raw).query or raw.split("?", 1)[-1]
        params = urllib.parse.parse_qs(qs)
        if "code" in params:
            return params["code"][0]
    return raw


if __name__ == "__main__":
    main()
