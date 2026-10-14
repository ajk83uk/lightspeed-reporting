"""Como loyalty webhook -> Neon mapping + SQL.

Pure functions (no Flask) so they can be unit-tested against the sample payloads
in Como's Export Event API docs. Transport lives in ingest/como_webhook.py.

Design rules
------------
* PII is never stored. `scrub()` removes name/email/phone-type keys from every
  JSON blob; `member_hashes()` keeps only salted SHA-256s for matching.
* Como has no event id, so `event_id()` = UserAction.Key, else sha256(body).
* Money is PENCE as sent. Timestamps: outer `TimeStamp` is epoch ms (treated as
  UTC); inner "dd.MM.yyyy HH:mm:ss" strings are parsed as UTC too (in Como's
  sample they differ from the epoch by seconds only, despite the TimeZone field).
  VERIFY against a real till sale before trusting hour-of-day cuts.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from .config import settings

log = logging.getLogger("como")

# Keys removed (case-insensitive) from every stored JSON blob.
_PII_KEYS = {
    "email", "phonenumber", "phone", "firstname", "lastname", "fullname", "name_",
    "birthday", "dateofbirth", "birthdate", "address", "city", "zip", "zipcode",
    "street", "gender", "membershipurl", "businesslogo",
    # seen in real payloads (14 Oct 2026): credentials, postcode, birthday, staff name
    "apikey", "api-key", "token", "addresszipcode", "birthdaymonth",
    "birthdaymonthandday", "cashier", "phonenumberhash",
}


def scrub(obj: Any) -> Any:
    """Recursively drop PII keys. Safe on dict/list/scalars."""
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items() if k.lower() not in _PII_KEYS}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    return obj


def _norm_email(v: Any) -> str | None:
    s = (str(v).strip().lower() if v else "")
    return s if "@" in s else None


def _norm_phone(v: Any) -> str | None:
    """UK-style: keep the last 10 digits so 07700..., +447700..., 00447700... match."""
    digits = re.sub(r"\D", "", str(v or ""))
    return digits[-10:] if len(digits) >= 10 else None


def _hash(value: str | None) -> str | None:
    salt = settings.como_hash_salt
    if not value or not salt:
        return None            # no salt configured -> store no hash rather than a weak one
    return hashlib.sha256((salt + value).encode()).hexdigest()


def member_hashes(membership: dict) -> tuple[str | None, str | None]:
    return (_hash(_norm_email(membership.get("Email"))),
            _hash(_norm_phone(membership.get("PhoneNumber"))))


def _int(v: Any) -> int | None:
    try:
        return None if v in (None, "") else int(float(str(v)))
    except (TypeError, ValueError):
        return None


def _bool(v: Any) -> bool | None:
    if v is None or v == "":
        return None
    return str(v).strip().lower() in ("true", "yes", "y", "1")


def _epoch_ms(v: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(v) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _como_dt(v: Any) -> datetime | None:
    """'27.07.2021 12:17:06' -> aware UTC datetime."""
    if not v:
        return None
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y"):
        try:
            return datetime.strptime(str(v), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _j(v: Any) -> str | None:
    return None if v is None else json.dumps(v)


def event_id(payload: dict, raw: bytes) -> str:
    key = ((payload.get("Data") or {}).get("UserAction") or {}).get("Key")
    return str(key) if key else "sha:" + hashlib.sha256(raw).hexdigest()


def _branch(data: dict) -> str | None:
    ua = data.get("UserAction") or {}
    b = data.get("BranchID") or ua.get("BranchID") or (ua.get("Data") or {}).get("BranchID")
    return str(b) if b not in (None, "") else None


# --------------------------------------------------------------------------
# SQL
# --------------------------------------------------------------------------
_SQL_EVENT = """
INSERT INTO como_events (event_id,trigger,rule_name,event_at,membership_key,branch_id,payload)
VALUES (%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (event_id) DO NOTHING;"""

_SQL_MEMBER = """
INSERT INTO como_members (membership_key,como_member_id,registered_at,registration_source,
    status,consent,allow_sms,allow_email,points,budget_weighted,tags,email_hash,phone_hash,
    joined_event_at,last_event,last_event_at,home_branch)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (membership_key) DO UPDATE SET
    como_member_id = COALESCE(EXCLUDED.como_member_id, como_members.como_member_id),
    registered_at  = COALESCE(como_members.registered_at, EXCLUDED.registered_at),
    registration_source = COALESCE(como_members.registration_source, EXCLUDED.registration_source),
    email_hash = COALESCE(EXCLUDED.email_hash, como_members.email_hash),
    phone_hash = COALESCE(EXCLUDED.phone_hash, como_members.phone_hash),
    joined_event_at = COALESCE(como_members.joined_event_at, EXCLUDED.joined_event_at),
    home_branch = COALESCE(como_members.home_branch, EXCLUDED.home_branch),
    -- state fields: only a NEWER (or equal) event may overwrite them
    status  = CASE WHEN EXCLUDED.last_event_at >= COALESCE(como_members.last_event_at,'-infinity')
                   THEN COALESCE(EXCLUDED.status, como_members.status) ELSE como_members.status END,
    consent = CASE WHEN EXCLUDED.last_event_at >= COALESCE(como_members.last_event_at,'-infinity')
                   THEN COALESCE(EXCLUDED.consent, como_members.consent) ELSE como_members.consent END,
    allow_sms = CASE WHEN EXCLUDED.last_event_at >= COALESCE(como_members.last_event_at,'-infinity')
                   THEN COALESCE(EXCLUDED.allow_sms, como_members.allow_sms) ELSE como_members.allow_sms END,
    allow_email = CASE WHEN EXCLUDED.last_event_at >= COALESCE(como_members.last_event_at,'-infinity')
                   THEN COALESCE(EXCLUDED.allow_email, como_members.allow_email) ELSE como_members.allow_email END,
    points = CASE WHEN EXCLUDED.last_event_at >= COALESCE(como_members.last_event_at,'-infinity')
                   THEN COALESCE(EXCLUDED.points, como_members.points) ELSE como_members.points END,
    budget_weighted = CASE WHEN EXCLUDED.last_event_at >= COALESCE(como_members.last_event_at,'-infinity')
                   THEN COALESCE(EXCLUDED.budget_weighted, como_members.budget_weighted) ELSE como_members.budget_weighted END,
    tags = CASE WHEN EXCLUDED.last_event_at >= COALESCE(como_members.last_event_at,'-infinity')
                   THEN COALESCE(EXCLUDED.tags, como_members.tags) ELSE como_members.tags END,
    last_event = CASE WHEN EXCLUDED.last_event_at >= COALESCE(como_members.last_event_at,'-infinity')
                   THEN EXCLUDED.last_event ELSE como_members.last_event END,
    last_event_at = GREATEST(EXCLUDED.last_event_at, COALESCE(como_members.last_event_at,'-infinity')),
    updated_at = now();"""

_SQL_FIRST_BRANCH = """
UPDATE como_members SET first_branch_id = %s, updated_at = now()
 WHERE membership_key = %s AND first_branch_id IS NULL;"""

_SQL_TX = """
INSERT INTO como_transactions (purchase_uid,event_id,membership_key,branch_id,transaction_id,
    pos_id,source,total_pence,relative_sum_pence,num_members,currency,purchased_at,
    cancelled,is_cancellation)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (purchase_uid) DO UPDATE SET     -- a later cancellation flags the original
    cancelled = COALESCE(como_transactions.cancelled,false) OR COALESCE(EXCLUDED.cancelled,false),
    is_cancellation = COALESCE(como_transactions.is_cancellation,false) OR COALESCE(EXCLUDED.is_cancellation,false);

"""
_SQL_ITEM = """
INSERT INTO como_transaction_items (purchase_uid,line_id,item_code,item_name,department_code,
    department_name,quantity,price_pence,gross_pence,action)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (purchase_uid,line_id) DO NOTHING;"""
_SQL_RED = """
INSERT INTO como_redemptions (redemption_id,event_id,membership_key,branch_id,transaction_id,
    asset_key,asset_name,asset_type,applied_pence,result,redeemed_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (redemption_id) DO NOTHING;"""


def summarise(payload: dict) -> str:
    """One-line description used in dry-run logs (no PII)."""
    d = payload.get("Data") or {}
    ua = d.get("UserAction") or {}
    return (f"trigger={payload.get('Trigger')} branch={_branch(d)} "
            f"total={d.get('TotalSum')} tx={ua.get('TransactionID') or (ua.get('Data') or {}).get('TransactionID')}")


def _purchase_items(data: dict, ua_data: dict) -> list:
    """Line items live at Data.Purchase.Data.Items in real payloads."""
    for src in ((data.get("Purchase") or {}).get("Data"), ua_data):
        if isinstance(src, dict) and isinstance(src.get("Items"), list):
            return src["Items"]
    return []


def _purchase_data(data: dict, ua_data: dict) -> dict:
    pd = (data.get("Purchase") or {}).get("Data")
    return pd if isinstance(pd, dict) else (ua_data if isinstance(ua_data, dict) else {})


def store_event(conn, payload: dict, raw: bytes) -> dict:
    """Persist one webhook payload. Returns counts. Caller commits."""
    trigger = str(payload.get("Trigger") or "")
    data = payload.get("Data") or {}
    ua = data.get("UserAction") or {}
    ua_data = ua.get("Data") or {}
    membership = data.get("Membership") or (ua_data.get("Membership") if isinstance(ua_data, dict) else None) or {}
    mkey = data.get("MembershipKey") or ua.get("MembershipKey") or membership.get("Key")
    branch = _branch(data)
    eid = event_id(payload, raw)
    event_at = _epoch_ms(payload.get("TimeStamp")) or _como_dt(data.get("TimeStamp"))
    counts = {"event": 0, "member": 0, "tx": 0, "redemptions": 0}

    with conn.cursor() as cur:
        cur.execute(_SQL_EVENT, (eid, trigger, payload.get("Name"), event_at, mkey, branch,
                                 _j(scrub(payload))))
        if cur.rowcount == 0:
            return counts                      # duplicate delivery: nothing else to do
        counts["event"] = 1

        if mkey and membership:
            eh, ph = member_hashes(membership)
            tags = membership.get("Tag")
            cur.execute(_SQL_MEMBER, (
                mkey,
                membership.get("comoMemberID") or membership.get("ComoMemberID"),
                _como_dt(membership.get("CreatedOn")),
                membership.get("RegistrationSource") or data.get("Source"),
                membership.get("MembershipStatus"),
                membership.get("Consent"),
                _bool(membership.get("AllowSMS")),
                _bool(membership.get("AllowEmail")),
                _int(membership.get("Points")),
                _int(membership.get("BudgetWeighted")),
                _j(tags if isinstance(tags, list) else None),
                eh, ph,
                event_at if trigger.lower() == "joinedclub" else None,
                trigger, event_at or datetime.now(timezone.utc),
                membership.get("HomeBranchID") or None,
            ))
            counts["member"] = 1

        if trigger.lower() == "purchase":
            pdata = _purchase_data(data, ua_data)
            total = _int(data.get("TotalSum") if data.get("TotalSum") is not None else ua_data.get("TotalSum"))
            cur.execute(_SQL_TX, (
                ua.get("PurchaseUID") or eid, eid, mkey, branch,
                str(ua.get("TransactionID") or ua_data.get("TransactionID") or "") or None,
                str(ua.get("PosID") or ua_data.get("PosID") or "") or None,
                data.get("Source") or ua.get("Source"),
                total,
                _int(data.get("RelativeSum") if data.get("RelativeSum") is not None else ua_data.get("RelativeSum")),
                _int(data.get("NumOfMembers")),
                data.get("CurrencyIsoCode"),
                event_at or _como_dt(data.get("TimeStamp")),
                _bool(pdata.get("Cancelled")), _bool(pdata.get("IsCancellation")),
            ))
            counts["tx"] = cur.rowcount
            puid = ua.get("PurchaseUID") or eid
            for n, it in enumerate(_purchase_items(data, ua_data)):
                if not isinstance(it, dict):
                    continue
                cur.execute(_SQL_ITEM, (
                    puid, str(it.get("LineID") or n), it.get("ItemCode"), it.get("ItemName"),
                    it.get("DepartmentCode"), it.get("DepartmentName"),
                    _int(it.get("Quantity")), _int(it.get("Price")), _int(it.get("GrossPrice")),
                    it.get("Action"),
                ))
            if mkey and branch:
                cur.execute(_SQL_FIRST_BRANCH, (branch, mkey))

        elif trigger.lower() == "redeem":
            benefits = ua_data.get("ExternalBenefits") or []
            for i, b in enumerate(benefits):
                cur.execute(_SQL_RED, (
                    f"{eid}:{i}", eid, mkey, branch,
                    str(ua.get("TransactionID") or "") or None,
                    b.get("AssetKey"), b.get("AssetName"), b.get("AssetType"),
                    _int(b.get("AppliedAmount")), b.get("Result"), event_at,
                ))
                counts["redemptions"] += cur.rowcount
    return counts
