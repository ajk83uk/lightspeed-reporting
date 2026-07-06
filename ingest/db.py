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


# --- StoreKit online orders -------------------------------------------------
def _int_pence(v: Any) -> int | None:
    """StoreKit money is already integer pence; coerce defensively."""
    if v is None or v == "":
        return None
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _map_storekit_order(d: dict) -> tuple:
    """Map an order.created `data` block to a storekit_orders row tuple."""
    venue = d.get("venue") or {}
    cust = d.get("customer") or {}
    table = d.get("table") or {}
    return (
        d.get("id"),
        venue.get("id"),
        venue.get("name"),
        venue.get("slug"),
        d.get("code"),
        d.get("orderType"),
        d.get("asap"),
        _dt(d.get("createdAt")),
        _dt(d.get("deliveryTime")),
        _int_pence(d.get("total")),
        _int_pence(d.get("tip")),
        _int_pence(d.get("deliveryFee")),
        _int_pence(d.get("discountTotal")),
        cust.get("firstName"),
        cust.get("lastName"),
        cust.get("email"),
        cust.get("phone"),
        cust.get("marketingConsent"),
        table.get("covers"),
        _j(d.get("items")),
        _j(d),
    )


_STOREKIT_ORDER_SQL = """
INSERT INTO storekit_orders (order_id,venue_id,venue_name,venue_slug,code,
    order_type,asap,created_at,delivery_time,total_pence,tip_pence,
    delivery_fee_pence,discount_pence,customer_first,customer_last,
    customer_email,customer_phone,marketing_consent,covers,items,raw,
    last_event,last_svix_id,updated_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
ON CONFLICT (order_id) DO UPDATE SET
    venue_id=EXCLUDED.venue_id, venue_name=EXCLUDED.venue_name,
    venue_slug=EXCLUDED.venue_slug, code=EXCLUDED.code,
    order_type=EXCLUDED.order_type, asap=EXCLUDED.asap,
    created_at=EXCLUDED.created_at, delivery_time=EXCLUDED.delivery_time,
    total_pence=EXCLUDED.total_pence, tip_pence=EXCLUDED.tip_pence,
    delivery_fee_pence=EXCLUDED.delivery_fee_pence,
    discount_pence=EXCLUDED.discount_pence, customer_first=EXCLUDED.customer_first,
    customer_last=EXCLUDED.customer_last, customer_email=EXCLUDED.customer_email,
    customer_phone=EXCLUDED.customer_phone,
    marketing_consent=EXCLUDED.marketing_consent, covers=EXCLUDED.covers,
    items=EXCLUDED.items, raw=EXCLUDED.raw, last_event=EXCLUDED.last_event,
    last_svix_id=EXCLUDED.last_svix_id, updated_at=now();
"""

# Lifecycle/refund events carry only {order id, code, status, venue}. We patch
# an existing row, or insert a stub if the event arrives before order.created
# (out-of-order delivery). status/refund only ever move forward.
_STOREKIT_STATUS_SQL = """
INSERT INTO storekit_orders (order_id,venue_id,venue_name,status,is_refunded,
    refund_total_pence,last_event,last_svix_id,updated_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now())
ON CONFLICT (order_id) DO UPDATE SET
    status = CASE WHEN EXCLUDED.status <> '' THEN EXCLUDED.status
                  ELSE storekit_orders.status END,
    is_refunded = storekit_orders.is_refunded OR EXCLUDED.is_refunded,
    refund_total_pence = COALESCE(EXCLUDED.refund_total_pence,
                                  storekit_orders.refund_total_pence),
    venue_id = COALESCE(storekit_orders.venue_id, EXCLUDED.venue_id),
    venue_name = COALESCE(storekit_orders.venue_name, EXCLUDED.venue_name),
    last_event = EXCLUDED.last_event, last_svix_id = EXCLUDED.last_svix_id,
    updated_at = now();
"""

# Status string a given event implies (None = leave status unchanged).
_STOREKIT_EVENT_STATUS = {
    "order.accepted": "accepted",
    "order.preparing": "preparing",
    "order.ready_for_pickup": "ready_for_pickup",
    "order.out_for_delivery": "out_for_delivery",
    "order.completed": "completed",
    "order.rejected": "rejected",
    "order.canceled": "canceled",
    "order.refund.created": None,   # refund flag only; status untouched
}


def seen_webhook(conn, svix_id: str, event_type: str, order_id: str | None) -> bool:
    """Record a delivery; return True if this svix-id was already processed.

    At-least-once delivery means duplicates are expected; the PK on svix_id
    makes this the dedupe gate (per StoreKit's idempotency guidance).
    """
    if not svix_id:
        return False
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO storekit_webhook_events (svix_id,event_type,order_id)
               VALUES (%s,%s,%s) ON CONFLICT (svix_id) DO NOTHING;""",
            (svix_id, event_type, order_id),
        )
        return cur.rowcount == 0   # 0 rows inserted => already seen


def upsert_storekit_order(conn, data: dict, svix_id: str, event_type: str) -> None:
    """Full upsert from an order.created / order.accepted payload."""
    row = _map_storekit_order(data) + (event_type, svix_id)
    with conn.cursor() as cur:
        cur.execute(_STOREKIT_ORDER_SQL, row)


def patch_storekit_status(conn, data: dict, svix_id: str, event_type: str) -> None:
    """Patch status / refund flag from a slim lifecycle event payload."""
    order = data.get("order") or {}
    venue = data.get("venue") or {}
    refund = data.get("refund") or {}
    order_id = order.get("id") or data.get("id")
    if not order_id:
        return
    status = _STOREKIT_EVENT_STATUS.get(event_type)
    is_refunded = event_type == "order.refund.created"
    with conn.cursor() as cur:
        cur.execute(
            _STOREKIT_STATUS_SQL,
            (
                order_id,
                venue.get("id"),
                venue.get("name"),
                status or "",
                is_refunded,
                _int_pence(refund.get("amount")) if is_refunded else None,
                event_type,
                svix_id,
            ),
        )


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
