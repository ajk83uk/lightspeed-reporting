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
