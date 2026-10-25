"""Twilio telephony-leg cost, joined to a platform call.

Join: session.provider_call_id (Vapi call id) -> Vapi GET /call/{id}
-> phoneCallProviderId (Twilio Call SID) -> Twilio REST GET
/Calls/{sid}.json -> price (Twilio reports the charge as a NEGATIVE
string; stored as a positive cost).

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
import urllib.request

UA = "VoiceAgentPlatform/1.0"


def _http_json(url, headers, timeout=15):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def resolve_twilio_creds(db, tenant_id):
    """(account_sid, auth_token) or (None, None). Never logged."""
    try:
        from .models import ProviderSettings
        row = (db.query(ProviderSettings)
               .filter(ProviderSettings.tenant_id == tenant_id,
                       ProviderSettings.provider == "twilio").first())
        f = (row.fields or {}) if row else {}
        if f.get("auth_id") and f.get("auth_token"):
            return f["auth_id"], f["auth_token"]
    except Exception:
        pass
    sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    tok = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    return (sid, tok) if sid and tok else (None, None)


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
            tw_sid = _vapi_twilio_sid(db, session)
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
