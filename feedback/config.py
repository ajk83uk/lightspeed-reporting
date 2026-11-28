"""Settings for the guest feedback service. Secrets come from environment variables only."""
import os

SENDER_ADDRESS = os.environ.get("FEEDBACK_SENDER", "feedback@tapandtandoor.co.uk")
PUBLIC_BASE_URL = os.environ.get("FEEDBACK_BASE_URL", "https://feedback.tapandtandoor.co.uk")
LINK_LIFETIME_DAYS = int(os.environ.get("FEEDBACK_LINK_DAYS", "14"))

# Per-site details. Google review links are filled in once pulled from each Google Business Profile.
# Phone numbers and place IDs were confirmed against each site's Google listing on 28 Nov 2026.
SITES = {
    1718940401139720: {"name": "Solihull", "address": "658 Warwick Road, Solihull B91 3DX", "phone": "0121 711 1166", "google_review_url": os.environ.get("GREVIEW_SOLIHULL", "https://search.google.com/local/writereview?placeid=ChIJjwCisUi5cEgRbGgZcFU17r4")},
    1718940401139719: {"name": "Peterborough", "address": "53 Cumbergate, Queensgate Shopping Centre, Peterborough PE1 1YR", "phone": "01733 312444", "google_review_url": os.environ.get("GREVIEW_PETERBOROUGH", "https://search.google.com/local/writereview?placeid=ChIJWVxUnIvxd0gRZmHs0jxlzs8")},
    1718940401139718: {"name": "Portsmouth", "address": "Gunwharf Quays, Portsmouth PO1 3TZ", "phone": "023 9381 3150", "google_review_url": os.environ.get("GREVIEW_PORTSMOUTH", "https://search.google.com/local/writereview?placeid=ChIJYa3XAudddEgRiGThN9L3L_o")},
    1718940401139714: {"name": "Bournemouth", "address": "7a-11 Richmond Hill, Bournemouth BH2 6HE", "phone": "01202 028382", "google_review_url": os.environ.get("GREVIEW_BOURNEMOUTH", "https://search.google.com/local/writereview?placeid=ChIJk9_hr0ehc0gRva7jAj9A0G4")},
    1718940401139717: {"name": "Southampton", "address": "West Quay Road, Southampton SO15 1DE", "phone": "023 8063 8852", "google_review_url": os.environ.get("GREVIEW_SOUTHAMPTON", "https://search.google.com/local/writereview?placeid=ChIJh7nSo0B3dEgRfqv11w3-HFc")},
}


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url
