"""Provider registry + credential resolution + Test connection (A4).

One registry describes every provider the platform can use, grouped by
orchestration mode:

    managed     → vapi (the managed platform) · twilio (optional)
    self_hosted → plivo (telephony) · exotel (telephony, alternative)
                  · sarvam (STT / LLM / TTS)

Credential resolution per field: the tenant's ProviderSetting row wins; a
field the tenant has not set falls back to the platform .env value;
TELEPHONY_MODE=stub (config) is the global fallback when nothing is
configured anywhere — such providers run simulated and report status
"stub".

SECURITY CONTRACT (design §7/§10, A4 acceptance):
- Secret values live only in provider_settings.fields (server-side) and in
  the return value of get_provider_config(), which is INTERNAL — it must
  never be serialised into an API response.
- Every API-facing view is built by provider_view(), which emits only
  masked secrets (•••• + last 4) plus plain values for non-secret fields
  (ids / sids / subdomains — identifiers, not credentials).
- Nothing in this module logs field values; test-result messages are built
  from HTTP status codes and exception class names only.
"""
import base64
import json
import time
import urllib.error
import urllib.request
from datetime import datetime

from sqlalchemy.orm import Session

from .config import settings
from .models import ProviderSetting

# ── Registry ──────────────────────────────────────────────────────────────
# field: name, label, secret (masked on read, write-only in the UI),
# required (needed before the provider counts as configured), env_attr
# (the Settings attribute holding the .env fallback value).
PROVIDERS = {
    "vapi": {
        "mode": "managed", "label": "Vapi — managed platform",
        "description": "Managed orchestration: telephony, STT, LLM and TTS behind one API. "
                       "Default model + voice are the tenant defaults the managed dispatcher "
                       "uses at dispatch (agents carry their own model/voice for self-hosted mode).",
        "primary": True, "optional": False,
        "fields": [
            {"name": "api_key", "label": "API key", "secret": True, "required": True,
             "env_attr": "VAPI_API_KEY"},
            {"name": "default_model", "label": "Default model", "secret": False, "required": False,
             "env_attr": "VAPI_DEFAULT_MODEL"},
            {"name": "default_voice", "label": "Default voice", "secret": False, "required": False,
             "env_attr": "VAPI_DEFAULT_VOICE"},
        ],
    },
    "twilio": {
        "mode": "managed", "label": "Twilio",
        "description": "Optional telephony / WhatsApp account used alongside the managed platform.",
        "primary": False, "optional": True,
        "fields": [
            {"name": "account_sid", "label": "Account SID", "secret": False, "required": True,
             "env_attr": "TWILIO_ACCOUNT_SID"},
            {"name": "auth_token", "label": "Auth token", "secret": True, "required": True,
             "env_attr": "TWILIO_AUTH_TOKEN"},
        ],
    },
    "plivo": {
        "mode": "self_hosted", "label": "Plivo — telephony",
        "description": "Self-hosted telephony (primary). India account + DLT live on the tenant's own entity.",
        "primary": True, "optional": False,
        "fields": [
            {"name": "auth_id", "label": "Auth ID", "secret": False, "required": True,
             "env_attr": "PLIVO_AUTH_ID"},
            {"name": "auth_token", "label": "Auth token", "secret": True, "required": True,
             "env_attr": "PLIVO_AUTH_TOKEN"},
        ],
    },
    "exotel": {
        "mode": "self_hosted", "label": "Exotel — telephony (alternative)",
        "description": "Alternative self-hosted telephony behind the same adapter interface as Plivo.",
        "primary": False, "optional": True,
        "fields": [
            {"name": "sid", "label": "Account SID", "secret": False, "required": True,
             "env_attr": "EXOTEL_SID"},
            {"name": "api_key", "label": "API key", "secret": True, "required": True,
             "env_attr": "EXOTEL_API_KEY"},
            {"name": "api_token", "label": "API token", "secret": True, "required": True,
             "env_attr": "EXOTEL_API_TOKEN"},
            {"name": "subdomain", "label": "API subdomain", "secret": False, "required": False,
             "env_attr": "EXOTEL_SUBDOMAIN"},
        ],
    },
    "sarvam": {
        "mode": "self_hosted", "label": "Sarvam — STT / LLM / TTS",
        "description": "Saaras STT, Sarvam LLM and Bulbul TTS for the self-hosted orchestrator.",
        "primary": True, "optional": False,
        "fields": [
            {"name": "api_key", "label": "API key", "secret": True, "required": True,
             "env_attr": "SARVAM_API_KEY"},
        ],
    },
}

PROVIDERS_BY_MODE = {
    mode: [k for k, v in PROVIDERS.items() if v["mode"] == mode]
    for mode in ("managed", "self_hosted")
}


def mask(secret: str) -> str:
    """Masked form for API responses: bullets + last 4 characters only."""
    if not secret:
        return ""
    return "••••" + secret[-4:] if len(secret) >= 4 else "••••"


def _env_value(field: dict) -> str:
    return getattr(settings, field["env_attr"], "") or ""


def get_row(db: Session, tenant_id: str, provider: str) -> ProviderSetting | None:
    return (
        db.query(ProviderSetting)
        .filter(ProviderSetting.tenant_id == tenant_id, ProviderSetting.provider == provider)
        .first()
    )


def resolve_fields(db: Session, tenant_id: str, provider: str):
    """Resolve every field of one provider for a tenant.

    Returns (values, sources, row): values[name] is the effective value
    (tenant value, else .env fallback, else ""); sources[name] is
    "tenant" | "env" | None. INTERNAL — values include raw secrets.
    """
    spec = PROVIDERS[provider]
    row = get_row(db, tenant_id, provider)
    tenant_fields = (row.fields or {}) if row else {}
    values, sources = {}, {}
    for f in spec["fields"]:
        tv = tenant_fields.get(f["name"]) or ""
        if tv:
            values[f["name"]], sources[f["name"]] = tv, "tenant"
        elif _env_value(f):
            values[f["name"]], sources[f["name"]] = _env_value(f), "env"
        else:
            values[f["name"]], sources[f["name"]] = "", None
    return values, sources, row


def _assess(db: Session, tenant_id: str, provider: str):
    """(values, sources, row, missing_required, configured) for one provider."""
    spec = PROVIDERS[provider]
    values, sources, row = resolve_fields(db, tenant_id, provider)
    if row is not None and not row.enabled:
        return values, sources, row, [], False
    missing = [f["name"] for f in spec["fields"] if f["required"] and not values[f["name"]]]
    return values, sources, row, missing, not missing


def get_provider_config(tenant_id: str, mode: str, db: Session | None = None) -> dict:
    """INTERNAL service API for the orchestrator (A5+): the effective
    provider configuration for one tenant + orchestration mode.

    Returns raw credential values — NEVER serialise this into an API
    response. Shape:
        {"tenant_id", "mode", "ready": bool, "missing": [provider keys],
         "providers": {key: {"configured": bool, "source": "tenant"|"env"|None,
                             "fields": {name: value}}}}
    `ready` means a real call could be placed in this mode: managed needs
    Vapi; self-hosted needs Sarvam AND one telephony provider (Plivo or
    Exotel). When not ready, dispatch must run in stub mode.
    """
    own = db is None
    if own:
        from .db import SessionLocal
        db = SessionLocal()
    try:
        providers, missing = {}, []
        for key in PROVIDERS_BY_MODE.get(mode, []):
            values, sources, _row, miss, configured = _assess(db, tenant_id, key)
            providers[key] = {
                "configured": configured,
                "source": ("tenant" if "tenant" in sources.values()
                           else "env" if "env" in sources.values() else None),
                "fields": values,
            }
            if PROVIDERS[key]["primary"] and not configured:
                missing.append(key)
        if mode == "self_hosted":
            # Telephony may come from either adapter; both missing = not ready
            # (already covered by plivo being primary, so drop the duplicate).
            if providers.get("exotel", {}).get("configured") and "plivo" in missing:
                missing.remove("plivo")
        return {"tenant_id": tenant_id, "mode": mode, "ready": not missing,
                "missing": missing, "providers": providers}
    finally:
        if own:
            db.close()


def mode_readiness(db: Session, tenant_id: str, mode: str):
    """(ready, missing_labels) for campaign flagging — no secret values."""
    cfg = get_provider_config(tenant_id, mode, db=db)
    labels = [PROVIDERS[k]["label"] for k in cfg["missing"]]
    return cfg["ready"], labels


def env_provider_status() -> dict:
    """Per-mode configured flags from the .env fallback ONLY (for /api/health,
    which has no tenant context). Booleans only — never values."""
    out = {}
    for mode, keys in PROVIDERS_BY_MODE.items():
        out[mode] = {}
        for key in keys:
            spec = PROVIDERS[key]
            out[mode][key] = all(
                _env_value(f) for f in spec["fields"] if f["required"]
            )
    return out


# ── API-facing view (masked — the only shape that leaves the server) ──────

def provider_view(db: Session, tenant_id: str, provider: str) -> dict:
    spec = PROVIDERS[provider]
    values, sources, row, missing, configured = _assess(db, tenant_id, provider)
    any_set = any(values[f["name"]] for f in spec["fields"])

    if row is not None and not row.enabled:
        status, detail = "not_configured", "Disabled in tenant settings — re-enable to use this provider."
    elif missing and any_set:
        labels = [f["label"] for f in spec["fields"] if f["name"] in missing]
        status, detail = "not_configured", f"Incomplete — still missing: {', '.join(labels)}."
    elif not configured:
        if spec["optional"]:
            status, detail = "not_configured", "Optional provider — not configured."
        else:
            status, detail = ("stub",
                              "No credentials configured — this provider runs in stub mode; calls are simulated.")
    elif row is not None and row.last_test_status == "connected":
        status, detail = "connected", row.last_test_message or "Test connection passed."
    elif row is not None and row.last_test_status == "failed":
        status, detail = "failed", row.last_test_message or "Last test connection failed."
    else:
        status, detail = "configured", "Credentials saved — run Test connection to verify them."

    fields_out = []
    for f in spec["fields"]:
        v = values[f["name"]]
        fields_out.append({
            "name": f["name"], "label": f["label"], "secret": f["secret"],
            "required": f["required"], "is_set": bool(v), "source": sources[f["name"]],
            "value": None if f["secret"] else (v or None),
            "masked": mask(v) if f["secret"] and v else None,
        })
    return {
        "provider": provider, "mode": spec["mode"], "label": spec["label"],
        "description": spec["description"], "optional": spec["optional"],
        "configured": configured,
        "source": ("tenant" if "tenant" in sources.values()
                   else "env" if "env" in sources.values() else None),
        "enabled": (row.enabled if row is not None else True),
        "status": status, "status_detail": detail,
        "fields": fields_out,
        "last_test_status": (row.last_test_status if row else "") or "",
        "last_test_message": (row.last_test_message if row else "") or "",
        "last_test_at": (row.last_test_at.isoformat() if row and row.last_test_at else None),
    }


# ── Test connection ───────────────────────────────────────────────────────
# Lightweight REAL checks, one per provider, stdlib HTTP only. A provider
# with no credentials anywhere is never called: the result is an honest
# "stub". A failed check is reported as failed — success is never faked.
# Messages contain status codes / error class names only, never secrets and
# never raw provider response bodies.

def _basic_auth(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def _http(method: str, url: str, headers: dict | None = None,
          data: bytes | None = None, timeout: int = 6):
    """Returns (status_code, None) or (None, error_label) — never raises."""
    req = urllib.request.Request(url, method=method, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read(512)
            return r.status, None
    except urllib.error.HTTPError as e:
        e.read(512) if hasattr(e, "read") else None
        return e.code, None
    except Exception as e:  # DNS, TLS, timeout, refused…
        return None, type(e).__name__


def _verdict(provider_label: str, code, err) -> tuple[str, str]:
    if err:
        return "failed", f"Could not reach {provider_label} ({err}). Check network / provider status."
    if code is not None and 200 <= code < 300:
        return "connected", f"{provider_label} accepted the credentials (HTTP {code})."
    if code in (401, 403):
        return "failed", f"{provider_label} rejected the credentials (HTTP {code})."
    return "failed", f"{provider_label} returned HTTP {code} — credentials not verified."


def _check(provider: str, v: dict) -> tuple[str, str]:
    if provider == "vapi":
        code, err = _http("GET", "https://api.vapi.ai/call?limit=1",
                          {"Authorization": f"Bearer {v['api_key']}"})
        return _verdict("Vapi", code, err)
    if provider == "twilio":
        code, err = _http(
            "GET", f"https://api.twilio.com/2010-04-01/Accounts/{v['account_sid']}.json",
            {"Authorization": _basic_auth(v["account_sid"], v["auth_token"])})
        return _verdict("Twilio", code, err)
    if provider == "plivo":
        code, err = _http(
            "GET", f"https://api.plivo.com/v1/Account/{v['auth_id']}/",
            {"Authorization": _basic_auth(v["auth_id"], v["auth_token"])})
        return _verdict("Plivo", code, err)
    if provider == "exotel":
        host = (v.get("subdomain") or "api.exotel.com").replace("https://", "").replace("http://", "").strip("/")
        code, err = _http(
            "GET", f"https://{host}/v1/Accounts/{v['sid']}",
            {"Authorization": _basic_auth(v["api_key"], v["api_token"])})
        return _verdict("Exotel", code, err)
    if provider == "sarvam":
        body = json.dumps({
            "input": "Hello", "source_language_code": "en-IN",
            "target_language_code": "hi-IN", "model": "sarvam-translate:v1",
        }).encode()
        code, err = _http("POST", "https://api.sarvam.ai/translate",
                          {"api-subscription-key": v["api_key"],
                           "Content-Type": "application/json"}, data=body)
        return _verdict("Sarvam", code, err)
    return "failed", f"No connection check implemented for {provider}."


def test_connection(db: Session, tenant_id: str, provider: str) -> dict:
    """Run the Test-connection action for one provider of one tenant.

    Persists the outcome on the tenant's ProviderSetting row when one
    exists. With nothing configured, reports stub honestly and performs no
    network call.
    """
    values, _sources, row, _missing, configured = _assess(db, tenant_id, provider)
    label = PROVIDERS[provider]["label"]
    started = time.time()
    if not configured:
        result_status = "stub"
        message = (f"Stub mode — no {label} credentials configured for this tenant "
                   "(tenant settings or .env). Nothing was called; calls would be simulated.")
    else:
        result_status, message = _check(provider, values)
    latency_ms = int((time.time() - started) * 1000)
    now = datetime.utcnow()
    if row is not None:
        row.last_test_status = result_status
        row.last_test_message = message
        row.last_test_at = now
        db.commit()
    return {
        "provider": provider, "status": result_status, "message": message,
        "checked_at": now.isoformat(), "latency_ms": latency_ms,
        "persisted": row is not None,
    }
