"""Thin Lightspeed K-Series API client (read-only endpoints).

Handles auth header injection, basic retry on 429/5xx, and pagination for the
two endpoints we care about: FinancialV2 Get Sales and Items Get All Items.

The exact JSON field names vary slightly between V1 and V2; this client returns
raw dicts and the db layer does the field mapping, so a schema tweak only
touches one place.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Iterator

import requests

from .auth import token_manager
from .config import settings

log = logging.getLogger(__name__)


class LightspeedClient:
    def __init__(self) -> None:
        self.session = requests.Session()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        url = settings.api_base.rstrip("/") + path
        for attempt in range(5):
            token = token_manager.get_token()
            resp = self.session.get(
                url,
                headers={"Authorization": f"Bearer {token}",
                         "Accept": "application/json"},
                params=params,
                timeout=settings.http_timeout,
            )
            if resp.status_code in (429, 500, 502, 503, 504):
                wait = min(2 ** attempt, 30)
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    wait = int(retry_after)
                log.warning("HTTP %s on %s; retry in %ss", resp.status_code, path, wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                # Surface the API's reason (e.g. why a location 400s) instead of
                # the bare HTTPError. Logs the request window + response body.
                _p = params or {}
                log.error("HTTP %s on %s from=%s to=%s body=%s",
                          resp.status_code, path, _p.get("from"), _p.get("to"),
                          resp.text[:500])
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()  # exhausted retries
        return {}

    # --- Businesses --------------------------------------------------------
    def get_businesses(self) -> list[dict]:
        """Return the businessList from GET /f/data/businesses.

        Response shape:
          { "_embedded": { "businessList": [
              { "businessName": ..., "businessId": ..., "currencyCode": ...,
                "businessLocations": [ {"blName":..., "blID":..., "country":...,
                                        "timezone":...}, ... ] }, ... ] } }
        """
        data = self._get(
            settings.path_businesses,
            {"page": 0, "size": settings.businesses_page_size},
        )
        embedded = data.get("_embedded") or {}
        return embedded.get("businessList") or []

    def iter_business_locations(self) -> Iterator[dict]:
        """Flatten businesses -> business locations (the unit reporting needs).

        Yields the raw location dict (so all original keys are preserved for ID
        extraction) merged with its parent business context.
        """
        for biz in self.get_businesses():
            for loc in biz.get("businessLocations", []) or []:
                yield {
                    **loc,
                    "businessId": biz.get("businessId"),
                    "businessName": biz.get("businessName"),
                }

    # --- Sales (FinancialV2) ----------------------------------------------
    def iter_sales(self, blid: int, frm: str, to: str | None = None) -> Iterator[dict]:
        """Yield individual sale objects for a location over [frm, to].

        frm/to are ISO-8601 strings, filtered on timeClosed. Token-paginated
        via nextPageToken in the response body.
        """
        path = settings.path_sales_v2.format(blid=blid)
        next_token: str | None = None
        first = True
        while True:
            params: dict[str, Any] = {
                "from": frm,
                "pageSize": settings.sales_page_size,
            }
            if settings.sales_include:
                params["include"] = settings.sales_include
            if to:
                params["to"] = to
            if next_token:
                params["nextPageToken"] = next_token
            try:
                data = self._get(path, params)
            except requests.HTTPError as exc:
                resp = getattr(exc, "response", None)
                # A brand-new location 400s if `from` predates when it became
                # operational. Lightspeed tells us the earliest valid date in the
                # error body ("...after: 2026-04-27T16:18:38+01:00...") — retry
                # once from there so newly-activated sites backfill cleanly.
                if first and resp is not None and resp.status_code == 400:
                    m = re.search(r"after:\s*([0-9T:.+\-]+)", resp.text or "")
                    if m:
                        frm = m.group(1)
                        params["from"] = frm
                        log.warning("sales[%s] 'from' too early; retrying from "
                                    "earliest available %s", blid, frm)
                        data = self._get(path, params)
                    else:
                        raise
                else:
                    raise
            sales = data.get("sales", []) if isinstance(data, dict) else []
            if first:
                if not sales and isinstance(data, dict):
                    log.warning("sales[%s] first page empty; keys=%s sample=%s",
                                blid, list(data.keys()), json.dumps(data)[:300])
                first = False
            for sale in sales or []:
                yield sale
            next_token = data.get("nextPageToken") if isinstance(data, dict) else None
            if not next_token:
                break

    # --- Shifts (Staff API) ------------------------------------------------
    def iter_shifts(self, blid: int, start: str, end: str) -> Iterator[dict]:
        """Yield shift objects for a location over [start, end].

        start/end are ISO-8601 WITH offset (e.g. 2026-06-09T22:00:00+00:00) --
        the endpoint rejects bare timestamps. Response shape:
        {"data": {"shifts": [{uuid, staffId, dateInUTC, events:[{eventType,
        timestamp}]}]}, "links", "page"}. Page/size pagination.
        """
        path = settings.path_shifts.format(blid=blid)
        page, size, first = 0, 200, True
        while True:
            params = {"startTime": start, "endTime": end, "page": page, "size": size}
            data = self._get(path, params)
            shifts = ((data.get("data") or {}).get("shifts")) if isinstance(data, dict) else None
            if first:
                log.info("shifts[%s] first page: %d", blid, len(shifts or []))
                first = False
            if not shifts:
                break
            for s in shifts:
                yield s
            if len(shifts) < size:
                break
            page += 1

    @staticmethod
    def _extract_item_list(data: Any) -> list[dict]:
        """Pull the list of items out of whatever shape the endpoint returns."""
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # HAL / Spring / common wrappers.
            emb = data.get("_embedded")
            if isinstance(emb, dict):
                for v in emb.values():
                    if isinstance(v, list):
                        return v
            for key in ("itemList", "items", "content", "data", "results"):
                v = data.get(key)
                if isinstance(v, list):
                    return v
            # A single item object (has an id) -> treat as one-element list.
            if "id" in data:
                return [data]
        return []

    # --- Items -------------------------------------------------------------
    def iter_items(self, blid: int) -> Iterator[dict]:
        """Yield catalogue items for a location (offset/limit pagination)."""
        offset = 0
        page = settings.items_page_size
        first = True
        while True:
            data = self._get(
                settings.path_items,
                {"businessLocationId": blid, "offset": offset, "amount": page},
            )
            batch = self._extract_item_list(data)
            if first:
                if not batch:
                    shape = (f"dict keys={list(data.keys())}" if isinstance(data, dict)
                             else type(data).__name__)
                    log.warning("items[%s] returned no rows; raw shape=%s; sample=%s",
                                blid, shape, json.dumps(data)[:400])
                else:
                    total = data.get("total") if isinstance(data, dict) else "?"
                    log.info("items[%s]: %d in first page (total=%s)", blid, len(batch), total)
                first = False
            if not batch:
                break
            for item in batch:
                yield item
            if len(batch) < page:
                break
            offset += page
