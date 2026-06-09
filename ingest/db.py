"""Database layer: connection + idempotent upserts.

Field mapping from Lightspeed JSON to columns happens here and ONLY here. If a
field name differs in your tenant, fix it in one of the _map_* functions.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import psycopg2
import psycopg2.extras

from .config import settings

log = logging.getLogger(__name__)


# --- small parsing helpers --------------------------------------------------
def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _dt(v: Any) -> datetime | None:
    if not v:
        return None
    s = str(v).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _j(v: Any) -> str | None:
    return None if v is None else json.dumps(v)


def connect():
    conn = psycopg2.connect(settings.database_url)
    conn.autocommit = False
    return conn


# --- field mapping ----------------------------------------------------------
def _map_item(blid: int, it: dict) -> tuple:
    ag = it.get("accountingGroup") or {}
    return (
        blid,
        it.get("id"),
        it.get("sku"),
        it.get("name"),
        it.get("docketName"),
        ag.get("id"),
        ag.get("name"),
        _j(it.get("statisticGroups")),
        _num(it.get("costPrice")),
        it.get("itemType"),
        it.get("active"),
        _j(it),
    )


def _map_sale(blid: int, s: dict) -> tuple:
    return (
        blid,
        s.get("accountReference"),
        s.get("receiptId"),
        _dt(s.get("timeOfOpening") or s.get("timeofOpening")),
        _dt(s.get("timeClosed") or s.get("timeofCloseAndPaid")),
        s.get("cancelled"),
        s.get("dineIn"),
        s.get("nbCovers"),
        s.get("tableName"),
        s.get("ownerName"),
        s.get("deviceName"),
        _j(s),
    )


def _map_line(blid: int, acct: str, ln: dict) -> tuple:
    ag = ln.get("accountingGroup") or {}
    discount = ln.get("totalDiscountAmount")
    if discount is None:
        discount = ln.get("discountAmount")
    return (
        blid,
        acct,
        str(ln.get("id")),
        ln.get("sku"),
        ln.get("name"),
        _num(ln.get("quantity")),
        _num(ln.get("totalNetAmountWithTax")),
        _num(ln.get("totalNetAmountWithoutTax")),
        _num(ln.get("menuListPrice")),
        _num(ln.get("unitCostPrice")),
        _num(ln.get("taxAmount")),
        _num(discount),
        ag.get("accountingGroupId") or ag.get("id"),
        ag.get("name"),
        ln.get("revenueCenter"),
        _dt(ln.get("timeOfSale")),
        _j(ln),
    )


def _map_payment(blid: int, acct: str, p: dict) -> tuple:
    return (
        blid,
        acct,
        str(p.get("uuid") or p.get("externalReference") or id(p)),
        p.get("code"),
        p.get("description"),
        p.get("paymentMethodId"),
        _num(p.get("netAmountWithTax")),
        _num(p.get("tip")),
        _num(p.get("surcharge")),
        p.get("type"),
        _j(p),
    )


def _map_shift(blid: int, s: dict) -> tuple:
    ev = s.get("events") or []
    ins = [e.get("timestamp") for e in ev if e.get("eventType") == "CLOCK_IN" and e.get("timestamp")]
    outs = [e.get("timestamp") for e in ev if e.get("eventType") == "CLOCK_OUT" and e.get("timestamp")]
    return (
        blid,
        s.get("uuid"),
        s.get("staffId"),
        _dt(min(ins)) if ins else None,    # ISO 'Z' strings sort chronologically
        _dt(max(outs)) if outs else None,
        _dt(s.get("dateInUTC")),
        _j(s),
    )


# --- upserts ----------------------------------------------------------------
_ITEM_SQL = """
INSERT INTO items (business_location_id,item_id,sku,name,docket_name,
    accounting_group_id,accounting_group_name,statistic_groups,cost_price,
    item_type,active,raw,updated_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
ON CONFLICT (business_location_id,item_id) DO UPDATE SET
    sku=EXCLUDED.sku, name=EXCLUDED.name, docket_name=EXCLUDED.docket_name,
    accounting_group_id=EXCLUDED.accounting_group_id,
    accounting_group_name=EXCLUDED.accounting_group_name,
    statistic_groups=EXCLUDED.statistic_groups, cost_price=EXCLUDED.cost_price,
    item_type=EXCLUDED.item_type, active=EXCLUDED.active, raw=EXCLUDED.raw,
    updated_at=now();
"""

_SALE_SQL = """
INSERT INTO sales (business_location_id,account_reference,receipt_id,
    time_opening,time_closed,cancelled,dine_in,nb_covers,table_name,
    owner_name,device_name,raw,updated_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
ON CONFLICT (business_location_id,account_reference) DO UPDATE SET
    receipt_id=EXCLUDED.receipt_id, time_opening=EXCLUDED.time_opening,
    time_closed=EXCLUDED.time_closed, cancelled=EXCLUDED.cancelled,
    dine_in=EXCLUDED.dine_in, nb_covers=EXCLUDED.nb_covers,
    table_name=EXCLUDED.table_name, owner_name=EXCLUDED.owner_name,
    device_name=EXCLUDED.device_name, raw=EXCLUDED.raw, updated_at=now();
"""

_LINE_SQL = """
INSERT INTO sales_lines (business_location_id,account_reference,line_id,sku,name,
    quantity,net_with_tax,net_without_tax,menu_list_price,unit_cost_price,
    tax_amount,discount_amount,accounting_group_id,accounting_group_name,
    revenue_center,time_of_sale,raw,updated_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
ON CONFLICT (business_location_id,account_reference,line_id) DO UPDATE SET
    sku=EXCLUDED.sku, name=EXCLUDED.name, quantity=EXCLUDED.quantity,
    net_with_tax=EXCLUDED.net_with_tax, net_without_tax=EXCLUDED.net_without_tax,
    menu_list_price=EXCLUDED.menu_list_price, unit_cost_price=EXCLUDED.unit_cost_price,
    tax_amount=EXCLUDED.tax_amount, discount_amount=EXCLUDED.discount_amount,
    accounting_group_id=EXCLUDED.accounting_group_id,
    accounting_group_name=EXCLUDED.accounting_group_name,
    revenue_center=EXCLUDED.revenue_center, time_of_sale=EXCLUDED.time_of_sale,
    raw=EXCLUDED.raw, updated_at=now();
"""

_PAY_SQL = """
INSERT INTO payments (business_location_id,account_reference,payment_uuid,code,
    description,payment_method_id,net_with_tax,tip,surcharge,type,raw,updated_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
ON CONFLICT (business_location_id,account_reference,payment_uuid) DO UPDATE SET
    code=EXCLUDED.code, description=EXCLUDED.description,
    payment_method_id=EXCLUDED.payment_method_id, net_with_tax=EXCLUDED.net_with_tax,
    tip=EXCLUDED.tip, surcharge=EXCLUDED.surcharge, type=EXCLUDED.type,
    raw=EXCLUDED.raw, updated_at=now();
"""


_SHIFT_SQL = """
INSERT INTO staff_shifts (business_location_id,shift_uuid,staff_id,clock_in,
    clock_out,date_in_utc,raw,updated_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,now())
ON CONFLICT (business_location_id,shift_uuid) DO UPDATE SET
    staff_id=EXCLUDED.staff_id, clock_in=EXCLUDED.clock_in,
    clock_out=EXCLUDED.clock_out, date_in_utc=EXCLUDED.date_in_utc,
    raw=EXCLUDED.raw, updated_at=now();
"""


def upsert_shifts(conn, blid: int, shifts: list[dict]) -> int:
    rows = [_map_shift(blid, s) for s in shifts if s.get("uuid")]
    if rows:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, _SHIFT_SQL, rows, page_size=500)
    return len(rows)


def upsert_site(conn, blid: int, business_name: str | None, nickname: str | None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO sites (business_location_id,business_name,nickname,updated_at)
               VALUES (%s,%s,%s,now())
               ON CONFLICT (business_location_id) DO UPDATE SET
                 business_name=COALESCE(EXCLUDED.business_name, sites.business_name),
                 nickname=COALESCE(sites.nickname, EXCLUDED.nickname),
                 updated_at=now();""",
            (blid, business_name, nickname),
        )


def upsert_items(conn, blid: int, items: list[dict]) -> int:
    rows = [_map_item(blid, it) for it in items if it.get("id") is not None]
    if rows:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, _ITEM_SQL, rows, page_size=500)
    return len(rows)


def upsert_sales_batch(conn, blid: int, sales: list[dict]) -> tuple[int, int, int]:
    sale_rows, line_rows, pay_rows = [], [], []
    for s in sales:
        acct = s.get("accountReference")
        if not acct:
            continue
        sale_rows.append(_map_sale(blid, s))
        for ln in s.get("salesLines", []) or []:
            line_rows.append(_map_line(blid, acct, ln))
        for p in s.get("payments", []) or []:
            pay_rows.append(_map_payment(blid, acct, p))
    with conn.cursor() as cur:
        if sale_rows:
            psycopg2.extras.execute_batch(cur, _SALE_SQL, sale_rows, page_size=500)
        if line_rows:
            psycopg2.extras.execute_batch(cur, _LINE_SQL, line_rows, page_size=500)
        if pay_rows:
            psycopg2.extras.execute_batch(cur, _PAY_SQL, pay_rows, page_size=500)
    return len(sale_rows), len(line_rows), len(pay_rows)


def get_stored_refresh_token(conn) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT refresh_token FROM oauth_token WHERE id = 1")
        row = cur.fetchone()
        return row[0] if row and row[0] else None


def save_refresh_token(conn, token: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO oauth_token (id, refresh_token, updated_at)
               VALUES (1, %s, now())
               ON CONFLICT (id) DO UPDATE SET
                 refresh_token = EXCLUDED.refresh_token, updated_at = now();""",
            (token,),
        )
    conn.commit()


def get_watermark(conn, blid: int, resource: str) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT last_watermark FROM ingest_state WHERE business_location_id=%s AND resource=%s",
            (blid, resource),
        )
        row = cur.fetchone()
        return row[0] if row else None


def set_watermark(conn, blid: int, resource: str, watermark: datetime | None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO ingest_state (business_location_id,resource,last_run_at,last_watermark)
               VALUES (%s,%s,%s,%s)
               ON CONFLICT (business_location_id,resource) DO UPDATE SET
                 last_run_at=EXCLUDED.last_run_at,
                 last_watermark=GREATEST(ingest_state.last_watermark, EXCLUDED.last_watermark);""",
            (blid, resource, datetime.now(timezone.utc), watermark),
        )
