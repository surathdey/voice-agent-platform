"""Twilio telephony-leg cost, joined to a platform call.

Join, managed: session.provider_call_id (Vapi call id) -> Vapi GET
/call/{id} -> phoneCallProviderId (Twilio Call SID) -> Twilio REST GET
/Calls/{sid}.json -> price (Twilio reports the charge as a NEGATIVE
string; stored as a positive cost).
Join, self-hosted Twilio: session.provider_call_id IS the Twilio Call
SID already (the status handler stores Twilio's CallSid, "CA…") —
used directly, no Vapi hop (Oct 26, 2026: the old code resolved
only via Vapi, so self-hosted calls could never show a Twilio leg).

ensure_twilio_cost() is called from the shared call serializer. It
fills cost_breakdown["twilio"] + ["total_all_in"] and commits ONLY when
it newly completes something (SID and/or price). Failures never raise
into a read: the call shows Vapi-only with twilio status pending /
unavailable. Twilio prices land minutes after a call ends, so a fresh
call legitimately shows pending until its price exists.

Provider rules that bit here, kept as code comments so they survive:
- every Vapi HTTP call carries User-Agent VoiceAgentPlatform/1.0 —
  bare urllib gets Cloudflare 1010-blocked on api.vapi.ai.
- Twilio credentials: tenant twilio provider row first (fields
  auth_id/auth_token — the same names the Twilio transport uses),
  else platform .env TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN.
- in-place cost_breakdown mutation needs flag_modified or the commit
  writes nothing (JSON column change tracking).
"""

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request

UA = "VoiceAgentPlatform/1.0"


def _http_json(url, headers, timeout=15):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def resolve_twilio_creds(db, tenant_id):
    """(account_sid, auth_token) or (None, None). Never logged.

    Tenant row first (registry field names account_sid / auth_token,
    with the transport's legacy auth_id fallback — mirroring
    providers.twilio_credentials), else platform .env. Fixed
    Oct 26, 2026: the old code imported ProviderSettings (plural),
    which does not exist — models.py has ProviderSetting (singular)
    — so the tenant row was never read and every call fell back to
    .env. It also read auth_id as the primary SID, where the
    registry stores account_sid.
    """
    try:
        from .models import ProviderSetting
        row = (db.query(ProviderSetting)
               .filter(ProviderSetting.tenant_id == tenant_id,
                       ProviderSetting.provider == "twilio").first())
        f = (row.fields or {}) if row else {}
        sid = f.get("account_sid") or f.get("auth_id")
        if sid and f.get("auth_token"):
            return sid, f["auth_token"]
    except Exception:
        pass
    sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    tok = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    return (sid, tok) if sid and tok else (None, None)


def resolve_twilio_sid(db, session):
    """Twilio Call SID for a session, or None when it cannot be
    resolved yet. Self-hosted Twilio calls carry the SID directly:
    the status handler stores Twilio's CallSid as provider_call_id
    (starts with "CA"), so no Vapi hop is needed — and a Vapi hop
    would be wrong there (no Vapi record exists for a self-hosted
    call). Managed calls still resolve via their Vapi record."""
    if (session.provider_mode or "") == "self_hosted":
        sid = session.provider_call_id or ""
        if sid.startswith("CA"):
            return sid
        return None
    return _vapi_twilio_sid(db, session)


def _vapi_twilio_sid(db, session):
    """Twilio Call SID for a managed session, via its Vapi record."""
    try:
        from .providers import get_provider_config
        if not session.provider_call_id \
                or session.provider_call_id.startswith("stub_"):
            return None
        cfg = get_provider_config(
            session.tenant_id, session.provider_mode or "managed", db)
        key = (cfg.get("providers") or {}).get("vapi", {}) \
            .get("fields", {}).get("api_key", "")
        if not key:
            return None
        call = _http_json(
            f"https://api.vapi.ai/call/{session.provider_call_id}",
            {"Authorization": f"Bearer {key}"})
        return call.get("phoneCallProviderId") or None
    except Exception:
        return None


def _twilio_price(account_sid, token, twilio_sid):
    """(cost_abs, currency, status): completed, or pending when Twilio
    has not priced the call yet (price null). Raises on API failure."""
    cred = base64.b64encode(f"{account_sid}:{token}".encode()).decode()
    call = _http_json(
        f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}"
        f"/Calls/{twilio_sid}.json",
        {"Authorization": "Basic " + cred})
    price = call.get("price")
    unit = call.get("price_unit") or "USD"
    if price is None:
        return None, unit, "pending"
    try:
        return abs(float(price)), unit, "completed"
    except (TypeError, ValueError):
        return None, unit, "pending"


def ensure_twilio_cost(db, session, result, cost):
    """Fill cost['twilio'] + cost['total_all_in'] in place; commit only
    on newly completed data. Never raises. Returns the (same) dict."""
    if result is None or not cost or cost.get("mode") == "stub":
        return cost
    tw = cost.get("twilio") if isinstance(cost.get("twilio"), dict) else {}
    if tw.get("status") == "completed" and cost.get("total_all_in") is not None:
        return cost
    changed = False
    try:
        sid, tok = resolve_twilio_creds(db, session.tenant_id)
        if not sid or not tok:
            cost["twilio"] = {"status": "unavailable",
                              "note": "Twilio credentials not configured"}
            return cost
        tw_sid = tw.get("call_sid")
        if not tw_sid:
            tw_sid = resolve_twilio_sid(db, session)
            if not tw_sid:
                cost["twilio"] = {"status": "pending",
                                  "note": "Twilio call id not resolved yet"}
                return cost
            changed = True
        price, unit, status = _twilio_price(sid, tok, tw_sid)
        if status == "completed" and price is not None:
            vapi_total = float(cost.get("total") or 0.0)
            vapi_currency = cost.get("currency") or "USD"
            entry = {"call_sid": tw_sid, "cost": price, "currency": unit,
                     "status": "completed"}
            if unit == vapi_currency:
                entry["note"] = "Twilio telephony leg"
                cost["total_all_in"] = round(vapi_total + price, 4)
            else:
                entry["note"] = ("Currency differs from Vapi leg; "
                                 "not summed into total_all_in")
            cost["twilio"] = entry
            changed = True
        else:
            cost["twilio"] = {"call_sid": tw_sid, "status": "pending",
                              "note": "Twilio price not posted yet"}
            changed = changed or tw.get("call_sid") != tw_sid
    except Exception:
        return cost
    if changed:
        try:
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(result, "cost_breakdown")
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
    return cost


def hangup_twilio_call(db, session):
    """Complete a managed call via Twilio (the managed end-call net).

    Returns True when Twilio accepted the completion. Never raises:
    any failure returns False so the caller can still acknowledge the
    webhook (Vapi's phrase detection remains as a fallback). The SID
    resolves exactly like the cost path (Vapi record's
    phoneCallProviderId); credentials resolve like the cost path.
    """
    try:
        account_sid, token = resolve_twilio_creds(db, session.tenant_id)
        twilio_sid = resolve_twilio_sid(db, session)
        if not account_sid or not token or not twilio_sid:
            return False
        cred = base64.b64encode(f"{account_sid}:{token}".encode()).decode()
        data = urllib.parse.urlencode({"Status": "completed"}).encode()
        req = urllib.request.Request(
            f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{twilio_sid}.json",
            data=data, headers={"Authorization": f"Basic {cred}"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except Exception:
        return False
