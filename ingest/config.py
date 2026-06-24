"""Central configuration, loaded from environment variables.

All endpoint paths live here so they are easy to confirm against the OpenAPI
spec (https://api-docs.lsk.lightspeed.app/source.yaml). The two values most
worth double-checking before first run are noted with VERIFY.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _req(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


@dataclass(frozen=True)
class Settings:
    # --- Lightspeed API ----------------------------------------------------
    # API host (data endpoints). Prod: https://api.lsk.lightspeed.app
    #                           Trial: https://api.trial.lsk.lightspeed.app
    api_base: str = os.getenv("LS_API_BASE", "https://api.lsk.lightspeed.app")

    # OAuth (V2) lives on a SEPARATE auth host (Keycloak realm 'k-series'):
    #   Prod  auth: https://auth.lsk-prod.app/realms/k-series/protocol/openid-connect/...
    #   Trial auth: https://auth.lsk-demo.app/realms/k-series/protocol/openid-connect/...
    # Clients are environment-bound (a trial client won't work on prod).
    authorize_url: str = os.getenv(
        "LS_AUTHORIZE_URL",
        "https://auth.lsk-prod.app/realms/k-series/protocol/openid-connect/auth",
    )
    token_url: str = os.getenv(
        "LS_TOKEN_URL",
        "https://auth.lsk-prod.app/realms/k-series/protocol/openid-connect/token",
    )
    # V2 client id begins with devp-v2-prod-... or devp-v2-demo-...
    client_id: str = os.getenv("LS_CLIENT_ID", "")
    client_secret: str = os.getenv("LS_CLIENT_SECRET", "")
    # offline_access extends refresh-token lifetime (recommended).
    scopes: str = os.getenv("LS_SCOPES", "financial-api items staff-api offline_access")
    # Initial seed only. The live (rotating) refresh token is persisted in the
    # oauth_token table after the first refresh -- see auth.py.
    refresh_token: str = os.getenv("LS_REFRESH_TOKEN", "")
    redirect_uri: str = os.getenv("LS_REDIRECT_URI", "https://localhost/")

    # Endpoint path templates (VERIFY against source.yaml). {blid} = business
    # location id. Sales is FinancialV2; items is the Items endpoint.
    path_businesses: str = os.getenv("LS_PATH_BUSINESSES", "/f/data/businesses")
    path_sales_v2: str = os.getenv(
        "LS_PATH_SALES", "/f/v2/business-location/{blid}/sales"
    )
    path_items: str = os.getenv("LS_PATH_ITEMS", "/items/v1/items")
    # Staff API (needs staff-api scope + ROLE_CONFIG_USERS). Clock-in/out shifts.
    path_shifts: str = os.getenv("LS_PATH_SHIFTS", "/staff/v1/businessLocations/{blid}/shift")
    # How many days of shifts to (re)pull each run. Idempotent upsert.
    shifts_days: int = int(os.getenv("LS_SHIFTS_DAYS", "30"))

    # Pagination caps documented for each endpoint.
    sales_page_size: int = int(os.getenv("LS_SALES_PAGE_SIZE", "100"))   # V2 max 100
    # Extra objects to include in each sale. 'payments' is REQUIRED to get tips.
    # Allowed: staff, table, consumer, payments, revenue_center, account_profile,
    # payment_authorization (comma-separated).
    sales_include: str = os.getenv("LS_SALES_INCLUDE", "payments,staff")
    items_page_size: int = int(os.getenv("LS_ITEMS_PAGE_SIZE", "1000"))  # max 1000
    businesses_page_size: int = int(os.getenv("LS_BUSINESSES_PAGE_SIZE", "1000"))
    # Only ingest locations belonging to these business (head office) IDs.
    # Comma-separated; default is the current T&T head office 100055.
    # Empty string = no filter (ingest everything the token can see).
    business_ids: frozenset = frozenset(
        int(x) for x in os.getenv("LS_BUSINESS_IDS", "100055").replace(" ", "").split(",") if x
    )

    # --- Database ----------------------------------------------------------
    database_url: str = os.getenv(
        "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/lightspeed"
    )

    # --- Sentiment Search feed --------------------------------------------
    # The reviews/overview CSVs can arrive three ways; each is independent and
    # off by default, so the nightly job no-ops cleanly until one is configured.
    #
    # 1) Local folder (a synced mount, or a manual drop for backfills).
    sentiment_src: str = os.getenv("SENTIMENT_SRC", "")
    #
    # 2) EMAIL route -- pull the daily CSV attachments straight from the inbox.
    #    Gmail API with OAuth *user* credentials (personal googlemail account, so
    #    a service account won't do). Mint the refresh token once with
    #    `python -m ingest.get_gmail_token`. Route is OFF unless all three of
    #    client id / secret / refresh token are set.
    gmail_client_id: str = os.getenv("GMAIL_CLIENT_ID", "")
    gmail_client_secret: str = os.getenv("GMAIL_CLIENT_SECRET", "")
    gmail_refresh_token: str = os.getenv("GMAIL_REFRESH_TOKEN", "")
    gmail_token_url: str = os.getenv("GMAIL_TOKEN_URL", "https://oauth2.googleapis.com/token")
    # Who the feed comes from, and how far back to scan each run.
    sentiment_email_from: str = os.getenv("SENTIMENT_EMAIL_FROM", "contact@sentimentsearch.com")
    sentiment_email_subject: str = os.getenv("SENTIMENT_EMAIL_SUBJECT", "")
    # Optional raw Gmail query override; when set it wins over from/subject.
    sentiment_email_query: str = os.getenv("SENTIMENT_EMAIL_QUERY", "")
    sentiment_email_lookback_days: int = int(os.getenv("SENTIMENT_EMAIL_LOOKBACK_DAYS", "3"))
    # Optional Gmail label applied to messages after a successful load, and
    # excluded from the next search so we don't re-download. Empty = don't label
    # (idempotency still holds via the sentiment_files hash log).
    sentiment_email_label: str = os.getenv("SENTIMENT_EMAIL_LABEL", "")
    #
    # 3) GCS route -- Prithvi pushes objects to a bucket prefix on our side.
    #    Read with the same service-account key style as cash-off. Route is OFF
    #    unless the bucket is set.
    sentiment_gcs_bucket: str = os.getenv("SENTIMENT_GCS_BUCKET", "")
    sentiment_gcs_prefix: str = os.getenv("SENTIMENT_GCS_PREFIX", "")
    sentiment_gcs_key_path: str = os.getenv(
        "SENTIMENT_GCS_KEY_PATH",
        os.getenv("GCP_KEY_PATH", os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "gcp-cashoff-key.json")),
    )

    # --- Favourite Table bookings (pull API: GetBookingList) ---------------
    # Production host (same path as demo). Token goes in the URL path, not a
    # header. Step skips cleanly until FT_AUTH_TOKEN is set (see bookings.py).
    ft_base: str = os.getenv("FT_BASE", "https://api.favouritetable.com")
    ft_auth_token: str = os.getenv("FT_AUTH_TOKEN", "")
    # The 5 T&T sites; Bournemouth = 2082 (main) + 2102 (Darts & Shuffleboard),
    # both fold into Bournemouth in reporting.
    ft_site_codes: tuple = tuple(
        int(x) for x in os.getenv(
            "FT_SITE_CODES", "2084,2082,2102,2083,2086,2085"
        ).replace(" ", "").split(",") if x
    )
    # Nightly rolling re-pull window (days) so late status changes self-heal.
    ft_window_days: int = int(os.getenv("FT_WINDOW_DAYS", "14"))
    # Gap between calls; FT throttles at the global level if load spikes.
    ft_throttle_secs: float = float(os.getenv("FT_THROTTLE_SECS", "0.4"))

    # --- Ingestion behaviour ----------------------------------------------
    # How many days back a "full" backfill goes when there's no watermark.
    backfill_days: int = int(os.getenv("LS_BACKFILL_DAYS", "365"))
    # Overlap window re-pulled on incrementals to catch late-closing receipts.
    incremental_overlap_hours: int = int(os.getenv("LS_OVERLAP_HOURS", "48"))
    http_timeout: int = int(os.getenv("LS_HTTP_TIMEOUT", "60"))

    def require_credentials(self) -> None:
        # Refresh token is NOT required here: it may already be persisted in the
        # oauth_token table (it rotates). auth.py handles the seed/stored logic.
        for name, val in {
            "LS_CLIENT_ID": self.client_id,
            "LS_CLIENT_SECRET": self.client_secret,
        }.items():
            if not val:
                raise RuntimeError(f"Missing required environment variable: {name}")


settings = Settings()
