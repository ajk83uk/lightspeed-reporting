"""Sentiment Search EMAIL route: pull the daily CSV attachments from Gmail.

Prithvi (contact@sentimentsearch.com) sends the two daily files as email
attachments:

    Tap_Overview_YYYY-MM-DD-YYYY-MM-DD.csv   (one site-row per day, grain=day)
    Tap_Reviews_YYYY-MM-DD-YYYY-MM-DD.csv    (previous day's reviews only)

This module finds those messages, downloads every .csv attachment into a temp
folder, and hands the folder to the existing loader (`ingest.sentiment`), which
classifies by header, parses, and upserts. Idempotency is inherited from the
loader's sentiment_files (filename + sha256) log, so re-running never double
-loads; optionally we also stamp a Gmail label on processed messages and
exclude it from the next search to avoid re-downloading.

Auth: Gmail API with OAuth *user* credentials. The mailbox is a personal
googlemail account, so a service account can't impersonate it -- mint a refresh
token once with `python -m ingest.get_gmail_token` and set GMAIL_CLIENT_ID /
GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN. The route is a safe no-op until all
three are present.

    python -m ingest.sentiment_email --dry-run   # list what it would download
    python -m ingest.sentiment_email             # download + load
    python -m ingest.sentiment_email --since 14  # widen the search window

Needs: google-api-python-client, google-auth (already in requirements.txt).
"""
from __future__ import annotations

import argparse
import base64
import logging
import os
import sys
import tempfile

from .config import settings
from . import sentiment as sentiment_mod

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sentiment_email")

# Read-only is enough to fetch attachments; modify is only needed to apply the
# optional processed-label. We request modify so labelling works if configured.
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def _configured() -> bool:
    return bool(
        settings.gmail_client_id
        and settings.gmail_client_secret
        and settings.gmail_refresh_token
    )


def _service():
    """Build an authenticated Gmail API client from the stored refresh token."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None,
        refresh_token=settings.gmail_refresh_token,
        client_id=settings.gmail_client_id,
        client_secret=settings.gmail_client_secret,
        token_uri=settings.gmail_token_url,
        scopes=SCOPES,
    )
    creds.refresh(Request())
    # cache_discovery=False avoids a noisy warning on headless runners.
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _build_query() -> str:
    if settings.sentiment_email_query:
        return settings.sentiment_email_query
    parts = ["has:attachment", "filename:csv"]
    if settings.sentiment_email_from:
        parts.append(f"from:{settings.sentiment_email_from}")
    if settings.sentiment_email_subject:
        parts.append(f"subject:({settings.sentiment_email_subject})")
    if settings.sentiment_email_lookback_days:
        parts.append(f"newer_than:{settings.sentiment_email_lookback_days}d")
    if settings.sentiment_email_label:
        parts.append(f"-label:{settings.sentiment_email_label}")
    return " ".join(parts)


def _iter_csv_parts(payload: dict):
    """Yield (filename, attachment_id) for every .csv part, recursing parts."""
    if not payload:
        return
    fn = payload.get("filename") or ""
    body = payload.get("body") or {}
    if fn.lower().endswith(".csv") and body.get("attachmentId"):
        yield fn, body["attachmentId"]
    for part in payload.get("parts", []) or []:
        yield from _iter_csv_parts(part)


def _safe_name(name: str) -> str:
    """Keep just the basename and strip anything path-ish, for temp writing."""
    return os.path.basename(name).replace("/", "_").replace("\\", "_")


def _download(svc, msg_id: str, dest_dir: str) -> list[str]:
    """Download all .csv attachments of one message into dest_dir."""
    msg = svc.users().messages().get(
        userId="me", id=msg_id, format="full").execute()
    written: list[str] = []
    for fn, att_id in _iter_csv_parts(msg.get("payload", {})):
        att = svc.users().messages().attachments().get(
            userId="me", messageId=msg_id, id=att_id).execute()
        data = base64.urlsafe_b64decode(att["data"].encode("utf-8"))
        path = os.path.join(dest_dir, _safe_name(fn))
        # If two messages carry the same filename, keep both.
        if os.path.exists(path):
            base, ext = os.path.splitext(path)
            path = f"{base}.{msg_id[:8]}{ext}"
        with open(path, "wb") as f:
            f.write(data)
        written.append(path)
    return written


def _ensure_label(svc, name: str) -> str | None:
    """Return the id of label `name`, creating it if needed."""
    existing = svc.users().labels().list(userId="me").execute().get("labels", [])
    for lab in existing:
        if lab.get("name") == name:
            return lab.get("id")
    created = svc.users().labels().create(
        userId="me",
        body={"name": name, "labelListVisibility": "labelShow",
              "messageListVisibility": "show"},
    ).execute()
    return created.get("id")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Load Sentiment Search CSVs from Gmail")
    p.add_argument("--dry-run", action="store_true",
                   help="list matching messages + attachments, no download/load")
    p.add_argument("--since", type=int, default=None,
                   help="override lookback window (days)")
    args = p.parse_args(argv)

    if not _configured():
        log.info("Gmail creds not set (GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN); "
                 "email route off -- nothing to do.")
        return 0

    if args.since is not None:
        # one-off override for this run
        object.__setattr__(settings, "sentiment_email_lookback_days", args.since)

    try:
        svc = _service()
    except Exception:  # noqa: BLE001
        log.exception("could not authenticate to Gmail")
        return 1

    query = _build_query()
    log.info("Gmail search: %s", query)
    resp = svc.users().messages().list(
        userId="me", q=query, maxResults=50).execute()
    msgs = resp.get("messages", [])
    if not msgs:
        log.info("no matching messages found.")
        return 0
    log.info("found %d matching message(s)", len(msgs))

    tmp = tempfile.mkdtemp(prefix="sentiment_email_")
    all_files: list[str] = []
    msg_ids: list[str] = []
    try:
        for m in msgs:
            files = _download(svc, m["id"], tmp)
            if files:
                msg_ids.append(m["id"])
                all_files.extend(files)
                for fp in files:
                    log.info("  [%s] %s", m["id"][:8], os.path.basename(fp))

        if not all_files:
            log.info("matched messages but no .csv attachments; nothing to load.")
            return 0

        if args.dry_run:
            log.info("dry-run: downloaded %d file(s) to %s (not loading, not "
                     "labelling)", len(all_files), tmp)
            rc = sentiment_mod.main(["--src", tmp, "--dry-run"])
            return rc

        rc = sentiment_mod.main(["--src", tmp])
        if rc != 0:
            log.error("loader returned %s; NOT labelling messages.", rc)
            return rc

        # Mark processed so the next run skips them (best-effort).
        if settings.sentiment_email_label:
            try:
                label_id = _ensure_label(svc, settings.sentiment_email_label)
                for mid in msg_ids:
                    svc.users().messages().modify(
                        userId="me", id=mid,
                        body={"addLabelIds": [label_id]}).execute()
                log.info("labelled %d message(s) '%s'", len(msg_ids),
                         settings.sentiment_email_label)
            except Exception:  # noqa: BLE001
                log.warning("could not apply label '%s' (need gmail.modify "
                            "scope?); idempotency still holds via file hashes.",
                            settings.sentiment_email_label, exc_info=True)
        return 0
    finally:
        # leave files only on dry-run for inspection; clean up otherwise
        if not args.dry_run:
            for fp in all_files:
                try:
                    os.remove(fp)
                except OSError:
                    pass
            try:
                os.rmdir(tmp)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
