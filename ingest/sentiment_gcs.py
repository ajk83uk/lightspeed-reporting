"""Sentiment Search GCS route: pull the daily CSVs from a Cloud Storage bucket.

The originally-agreed delivery: Prithvi pushes the two daily files to a bucket
on our side (write access granted to his service account). This module lists the
objects under a prefix, downloads every .csv into a temp folder, and hands the
folder to the existing loader (`ingest.sentiment`).

Uses the Storage JSON API via google-api-python-client + a service-account key
(same auth style as cash-off), so no extra dependency beyond what's already in
requirements.txt. Read-only scope.

Idempotency is inherited from the loader's sentiment_files (filename + sha256)
log, so re-downloading the same object is a no-op load. The route is a safe
no-op until SENTIMENT_GCS_BUCKET is set.

    python -m ingest.sentiment_gcs --dry-run   # list objects, no download/load
    python -m ingest.sentiment_gcs             # download + load
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile

from .config import settings
from . import sentiment as sentiment_mod

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sentiment_gcs")

SCOPES = ["https://www.googleapis.com/auth/devstorage.read_only"]


def _configured() -> bool:
    return bool(settings.sentiment_gcs_bucket)


def _service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    key_path = settings.sentiment_gcs_key_path
    if not key_path or not os.path.exists(key_path):
        raise RuntimeError(f"service-account key not found: {key_path!r}")
    creds = service_account.Credentials.from_service_account_file(
        key_path, scopes=SCOPES)
    return build("storage", "v1", credentials=creds, cache_discovery=False)


def _list_csv_objects(svc, bucket: str, prefix: str) -> list[str]:
    names: list[str] = []
    req = svc.objects().list(bucket=bucket, prefix=prefix or None)
    while req is not None:
        resp = req.execute()
        for obj in resp.get("items", []):
            name = obj.get("name", "")
            if name.lower().endswith(".csv"):
                names.append(name)
        req = svc.objects().list_next(req, resp)
    return sorted(names)


def _download(svc, bucket: str, name: str, dest_dir: str) -> str:
    from googleapiclient.http import MediaIoBaseDownload
    import io

    path = os.path.join(dest_dir, os.path.basename(name))
    req = svc.objects().get_media(bucket=bucket, object=name)
    buf = io.FileIO(path, "wb")
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _status, done = dl.next_chunk()
    buf.close()
    return path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Load Sentiment Search CSVs from GCS")
    p.add_argument("--dry-run", action="store_true",
                   help="list objects only, no download/load")
    args = p.parse_args(argv)

    if not _configured():
        log.info("SENTIMENT_GCS_BUCKET not set; GCS route off -- nothing to do.")
        return 0

    bucket = settings.sentiment_gcs_bucket
    prefix = settings.sentiment_gcs_prefix
    try:
        svc = _service()
    except Exception:  # noqa: BLE001
        log.exception("could not init GCS client")
        return 1

    names = _list_csv_objects(svc, bucket, prefix)
    if not names:
        log.info("no .csv objects under gs://%s/%s", bucket, prefix)
        return 0
    log.info("found %d .csv object(s) under gs://%s/%s", len(names), bucket, prefix)
    for n in names:
        log.info("  %s", n)

    if args.dry_run:
        log.info("dry-run: not downloading or loading.")
        return 0

    tmp = tempfile.mkdtemp(prefix="sentiment_gcs_")
    written: list[str] = []
    try:
        for n in names:
            written.append(_download(svc, bucket, n, tmp))
        return sentiment_mod.main(["--src", tmp])
    finally:
        for fp in written:
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
