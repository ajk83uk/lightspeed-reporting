"""Sentiment Search S3 route: pull the daily CSVs from an S3-compatible bucket.

This is the delivery Sentiment Search actually settled on (July 2026): Prithvi
pushes the two daily files to a Cloudflare R2 bucket on our side, using an S3
access key + secret. R2 speaks the S3 API, so we read it with boto3 pointed at
the R2 endpoint. This module lists the objects under a prefix, downloads every
.csv into a temp folder, and hands the folder to the existing loader
(`ingest.sentiment`) -- exactly like the GCS route it replaces.

Two deliberate choices:
  * It uses its OWN credential env vars (SENTIMENT_S3_ACCESS_KEY_ID /
    SENTIMENT_S3_SECRET_ACCESS_KEY) passed explicitly to the client -- NOT the
    ambient AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY that the Nory step relies on
    (those point at a different account). The two feeds never share a key.
  * Read-only in practice: give this side a token scoped "Object Read only";
    Prithvi holds a separate write token. Either can be rotated independently.

Idempotency is inherited from the loader's sentiment_files (filename + sha256)
log, so re-downloading the same object is a no-op load. Each daily file has a
unique dated name, so once loaded it's skipped on subsequent nights even though
it stays in the bucket. The route is a safe no-op until SENTIMENT_S3_BUCKET,
SENTIMENT_S3_ENDPOINT_URL and the two credential vars are all set.

    python -m ingest.sentiment_s3 --dry-run   # list objects, no download/load
    python -m ingest.sentiment_s3             # download + load
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
log = logging.getLogger("sentiment_s3")


def _configured() -> bool:
    return bool(
        settings.sentiment_s3_bucket
        and settings.sentiment_s3_endpoint
        and settings.sentiment_s3_access_key_id
        and settings.sentiment_s3_secret_access_key
    )


def _client():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=settings.sentiment_s3_endpoint,
        aws_access_key_id=settings.sentiment_s3_access_key_id,
        aws_secret_access_key=settings.sentiment_s3_secret_access_key,
        region_name=settings.sentiment_s3_region or "auto",
        config=Config(signature_version="s3v4"),
    )


def _list_csv_objects(s3, bucket: str, prefix: str) -> list[str]:
    keys: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix or ""):
        for obj in page.get("Contents", []):
            key = obj.get("Key", "")
            if key.lower().endswith(".csv"):
                keys.append(key)
    return sorted(keys)


def _download(s3, bucket: str, key: str, dest_dir: str) -> str:
    path = os.path.join(dest_dir, os.path.basename(key))
    s3.download_file(bucket, key, path)
    return path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Load Sentiment Search CSVs from S3/R2")
    p.add_argument("--dry-run", action="store_true",
                   help="list objects only, no download/load")
    args = p.parse_args(argv)

    if not _configured():
        log.info("SENTIMENT_S3_* not fully set; S3 route off -- nothing to do.")
        return 0

    bucket = settings.sentiment_s3_bucket
    prefix = settings.sentiment_s3_prefix
    try:
        s3 = _client()
    except Exception:  # noqa: BLE001
        log.exception("could not init S3 client")
        return 1

    try:
        keys = _list_csv_objects(s3, bucket, prefix)
    except Exception:  # noqa: BLE001
        log.exception("could not list s3://%s/%s", bucket, prefix)
        return 1

    if not keys:
        log.info("no .csv objects under s3://%s/%s", bucket, prefix)
        return 0
    log.info("found %d .csv object(s) under s3://%s/%s", len(keys), bucket, prefix)
    for k in keys:
        log.info("  %s", k)

    if args.dry_run:
        log.info("dry-run: not downloading or loading.")
        return 0

    tmp = tempfile.mkdtemp(prefix="sentiment_s3_")
    written: list[str] = []
    try:
        for k in keys:
            written.append(_download(s3, bucket, k, tmp))
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
