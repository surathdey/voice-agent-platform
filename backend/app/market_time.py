"""Market-local time (A10) — the tenant's market timezone is the clock
the whole schedule runs on.

Every timestamp the platform stores is a naive UTC datetime (the A1–A9
convention). Schedule questions — "is now inside the calling window?",
"how many calls started today?", "when is this lead's retry due?" —
are MARKET questions, though: a 09:00–19:00 window for an India tenant
means 09:00–19:00 in India. A5 evaluated windows in UTC; A10 moves all
of it here, evaluated in the tenant's timezone (tenants.timezone, set
from the market at provisioning — migration 008).
"""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

# Market → IANA timezone. The tenant row carries the resolved value, so
# a tenant provisioned for an unusual market can be corrected without a
# code change; this map is only the provisioning default.
MARKET_TIMEZONES = {
    "IN": "Asia/Kolkata",
    "NA": "America/Toronto",
}
DEFAULT_TIMEZONE = "Asia/Kolkata"


def timezone_name_for_market(market: str) -> str:
    return MARKET_TIMEZONES.get((market or "").upper(), DEFAULT_TIMEZONE)


def tenant_timezone_name(tenant) -> str:
    """The tenant's market timezone name (stored value wins; the market
    map is the fallback for rows created before migration 008)."""
    stored = getattr(tenant, "timezone", None) if tenant is not None else None
    if stored:
        return stored
    return timezone_name_for_market(getattr(tenant, "market", "") or "")


def tenant_zone(tenant) -> ZoneInfo:
    try:
        return ZoneInfo(tenant_timezone_name(tenant))
    except Exception:
        return ZoneInfo(DEFAULT_TIMEZONE)


def to_local(dt_utc: datetime, zone: ZoneInfo) -> datetime:
    """A stored naive-UTC datetime → aware datetime in `zone`."""
    return dt_utc.replace(tzinfo=timezone.utc).astimezone(zone)


def to_utc_naive(dt_local: datetime) -> datetime:
    """An aware local datetime → the naive-UTC storage convention."""
    return dt_local.astimezone(timezone.utc).replace(tzinfo=None)


def local_day_start_utc(day, zone: ZoneInfo) -> datetime:
    """Naive UTC of local midnight at the start of `day` (a date) in zone."""
    return to_utc_naive(datetime.combine(day, time.min, tzinfo=zone))


_DAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3,
              "fri": 4, "sat": 5, "sun": 6}


def parse_calling_days(text: str):
    """Parse the schedule's calling-days text into weekday numbers
    (Mon=0 … Sun=6): "Mon – Sat", "Mon-Fri", "Mon, Wed, Fri", "Daily".

    Returns None when the text cannot be understood — the schedule then
    imposes no day restriction (an operator's free-text note must never
    silently block a campaign). Ranges wrap (Fri – Mon = Fri,Sat,Sun,Mon).
    """
    raw = (text or "").strip().lower()
    if not raw:
        return None
    if raw in ("daily", "every day", "all days", "mon - sun", "mon – sun"):
        return set(range(7))
    days: set[int] = set()
    for part in raw.replace("–", "-").replace("—", "-").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            a, b = a.strip()[:3], b.strip()[:3]
            if a not in _DAY_INDEX or b not in _DAY_INDEX:
                return None
            i, j = _DAY_INDEX[a], _DAY_INDEX[b]
            span = [(i + k) % 7 for k in range((j - i) % 7 + 1)]
            days.update(span)
        else:
            key = part[:3]
            if key not in _DAY_INDEX:
                return None
            days.add(_DAY_INDEX[key])
    return days or None
