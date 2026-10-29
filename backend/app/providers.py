"""Provider registry + credential resolution + Test connection (A4).

One registry describes every provider the platform can use, grouped by
orchestration mode:

    managed     → vapi (the managed platform) · twilio (optional)
    self_hosted → plivo (telephony) · exotel (telephony, alternative)
                  · twilio (telephony, alternative — see "pstn" selection)
                  · pstn (the self-hosted PSTN choice: plivo | twilio)
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

from .config import settings

from sqlalchemy.orm import Session

from .models import ProviderSetting

# ── Registry ──────────────────────────────────────────────────────────────
# field: name, label, secret (masked on read, write-only in the UI),
# required (needed before the provider counts as configured), env_attr
# (the Settings attribute holding the .env fallback value).
PROVIDERS = {
    "vapi": {
        "mode": "managed", "label": "Vapi — managed platform",
        "description": "Managed orchestration: telephony, STT, LLM and TTS behind one API. "
                       "Default model is the assistant (LLM) model; the voice profiles below "
                       "are the TTS side — one profile per language (provider, TTS model, "
                       "female + male voice IDs), pre-filled with the owner's verified RPI "
                       "voice setup and editable per tenant. The transport picks the profile "
                       "by the call's language and the agent's voice gender. The transcriber "
                       "profiles are the STT side (Deepgram via Vapi) — one provider + model "
                       "per language, likewise pre-filled and editable; Bengali ships nova-3 "
                       "because Vapi's nova-2 language enum has no Bengali. The Server "
                       "credential ID references a credential in your Vapi account (its "
                       "token = this platform's WEBHOOK_SECRET): when set, call payloads "
                       "authenticate Vapi's server messages with that credential instead "
                       "of your organization's default one.",
        "primary": True, "optional": False,
        "fields": [
            {"name": "api_key", "label": "API key", "secret": True, "required": True,
             "env_attr": "VAPI_API_KEY"},
            {"name": "default_model", "label": "Default model (assistant LLM)", "secret": False, "required": False,
             "env_attr": "VAPI_DEFAULT_MODEL"},
            {"name": "default_voice", "label": "Default voice (fallback)", "secret": False, "required": False,
             "env_attr": "VAPI_DEFAULT_VOICE"},
            {"name": "phone_number_id", "label": "Phone number ID", "secret": False, "required": False,
             "env_attr": "VAPI_PHONE_NUMBER_ID"},
            # ── Webhook auth (server credential) — an ID, not a
            # secret: the credential itself lives in the tenant's Vapi
            # account; its token equals the platform's WEBHOOK_SECRET.
            # When set, payloads reference it (assistant.server
            # .credentialId) so Vapi signs server messages with OUR
            # credential instead of the organization's default one.
            {"name": "server_credential_id", "label": "Server credential ID (webhook auth)", "secret": False, "required": False,
             "env_attr": "VAPI_SERVER_CREDENTIAL_ID"},
            # ── Voice profiles (A6 scope addition) — identifiers, not
            # secrets: plain fields, pre-filled from "default" below.
            {"name": "en_voice_provider", "label": "English · voice provider", "secret": False, "required": False,
             "env_attr": "VAPI_EN_VOICE_PROVIDER", "default": "11labs"},
            {"name": "en_tts_model", "label": "English · TTS model", "secret": False, "required": False,
             "env_attr": "VAPI_EN_TTS_MODEL", "default": "eleven_turbo_v2_5"},
            {"name": "en_voice_id_female", "label": "English · female voice ID", "secret": False, "required": False,
             "env_attr": "VAPI_EN_VOICE_ID_FEMALE", "default": "90ipbRoKi4CpHXvKVtl0"},
            {"name": "en_voice_id_male", "label": "English · male voice ID", "secret": False, "required": False,
             "env_attr": "VAPI_EN_VOICE_ID_MALE", "default": "pzxut4zZz4GImZNlqQ3H"},
            # ── Transcriber profiles (STT side) — provider + model per
            # language, same pre-filled pattern as the voice profiles.
            # Bengali ships nova-3: Vapi's nova-2 enum has no Bengali.
            {"name": "en_transcriber_provider", "label": "English · transcriber provider", "secret": False, "required": False,
             "env_attr": "VAPI_EN_TRANSCRIBER_PROVIDER", "default": "deepgram"},
            {"name": "en_transcriber_model", "label": "English · transcriber model", "secret": False, "required": False,
             "env_attr": "VAPI_EN_TRANSCRIBER_MODEL", "default": "nova-2"},
            {"name": "hi_voice_provider", "label": "Hindi · voice provider", "secret": False, "required": False,
             "env_attr": "VAPI_HI_VOICE_PROVIDER", "default": "11labs"},
            {"name": "hi_tts_model", "label": "Hindi · TTS model", "secret": False, "required": False,
             "env_attr": "VAPI_HI_TTS_MODEL", "default": "eleven_v3"},
            {"name": "hi_voice_id_female", "label": "Hindi · female voice ID", "secret": False, "required": False,
             "env_attr": "VAPI_HI_VOICE_ID_FEMALE", "default": "1qEiC6qsybMkmnNdVMbK"},
            {"name": "hi_voice_id_male", "label": "Hindi · male voice ID", "secret": False, "required": False,
             "env_attr": "VAPI_HI_VOICE_ID_MALE", "default": "zT03pEAEi0VHKciJODfn"},
            {"name": "hi_transcriber_provider", "label": "Hindi · transcriber provider", "secret": False, "required": False,
             "env_attr": "VAPI_HI_TRANSCRIBER_PROVIDER", "default": "deepgram"},
            {"name": "hi_transcriber_model", "label": "Hindi · transcriber model", "secret": False, "required": False,
             "env_attr": "VAPI_HI_TRANSCRIBER_MODEL", "default": "nova-2"},
            {"name": "bn_voice_provider", "label": "Bengali · voice provider", "secret": False, "required": False,
             "env_attr": "VAPI_BN_VOICE_PROVIDER", "default": "11labs"},
            {"name": "bn_tts_model", "label": "Bengali · TTS model", "secret": False, "required": False,
             "env_attr": "VAPI_BN_TTS_MODEL", "default": "eleven_v3"},
            {"name": "bn_voice_id_female", "label": "Bengali · female voice ID", "secret": False, "required": False,
             "env_attr": "VAPI_BN_VOICE_ID_FEMALE", "default": "611e0Wo6wEi3GcpkO5it"},
            {"name": "bn_voice_id_male", "label": "Bengali · male voice ID", "secret": False, "required": False,
             "env_attr": "VAPI_BN_VOICE_ID_MALE", "default": "GuSivqtqi1qfWxEuGhqa"},
            {"name": "bn_transcriber_provider", "label": "Bengali · transcriber provider", "secret": False, "required": False,
             "env_attr": "VAPI_BN_TRANSCRIBER_PROVIDER", "default": "deepgram"},
            {"name": "bn_transcriber_model", "label": "Bengali · transcriber model", "secret": False, "required": False,
             "env_attr": "VAPI_BN_TRANSCRIBER_MODEL", "default": "nova-3"},
            # ── Rate card (A12) — the tenant's contracted per-minute
            # telephony rate, used ONLY to estimate a call's cost when
            # Vapi's own end-of-call report carries no cost breakdown
            # (Vapi's reported figures always win). Empty = no estimate.
            {"name": "rate_telephony_per_min", "label": "Rate card · telephony (USD per minute)", "secret": False, "required": False,
             "env_attr": "VAPI_RATE_TELEPHONY_PER_MIN"},
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
            # ── Rate card — the tenant's contracted per-minute
            # telephony rate for self-hosted Twilio calls (app.costing
            # prices the telephony leg from it when the call's Twilio
            # SID is known; the provider-reported Twilio price on the
            # Calls page is separate and needs no rate). Empty = 0.
            {"name": "rate_telephony_per_min", "label": "Rate card · telephony, Twilio (INR per minute)", "secret": False, "required": False,
             "env_attr": "TWILIO_RATE_TELEPHONY_PER_MIN"},
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
            # ── Rate card (A12) — the tenant's contracted per-minute
            # telephony rate; per-call cost breakdowns are computed
            # from it at finalize (app.costing). Empty = costed at 0.
            {"name": "rate_telephony_per_min", "label": "Rate card · telephony (INR per minute)", "secret": False, "required": False,
             "env_attr": "PLIVO_RATE_TELEPHONY_PER_MIN"},
        ],
    },
    "pstn": {
        "mode": "self_hosted", "label": "Self-hosted PSTN provider",
        "description": "Which telephony adapter self-hosted dispatch uses: Plivo "
                       "(default) or Twilio (alternative, e.g. when Plivo "
                       "signup is unavailable in the tenant's region). "
                       "This is a SELECTION, not a credential: the chosen "
                       "provider's own credentials must still be configured. "
                       "When the selected provider is not configured, "
                       "dispatch falls back to Plivo (and fails honestly "
                       "there if Plivo is not configured either).",
        "primary": False, "optional": True,
        "fields": [
            {"name": "provider", "label": "Self-hosted PSTN provider (plivo | twilio)", "secret": False, "required": False,
             "env_attr": "SELF_HOSTED_PSTN_PROVIDER"},
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
            # ── Rate cards (A12) — the tenant's contracted rates for
            # the three Sarvam legs; per-call cost breakdowns are
            # computed from them at finalize (app.costing). STT is
            # metered on streamed-audio duration (Saaras returns no
            # per-call usage). The LLM is metered on TOKENS from the
            # usage Sarvam streams (Oct 26, 2026 — the old "no token
            # usage" claim was wrong; the per-minute LLM rate below
            # is only the fallback when no usage was captured). TTS
            # is metered on CHARACTERS in synthesis requests (Bulbul
            # returns no usage and bills per character — the
            # per-minute TTS rate below is only the fallback when no
            # character count was captured). Empty = costed at 0.
            {"name": "rate_stt_per_min", "label": "Rate card · STT — Saaras (INR per minute of call audio)", "secret": False, "required": False,
             "env_attr": "SARVAM_RATE_STT_PER_MIN"},
            {"name": "rate_llm_per_min", "label": "Rate card · LLM fallback (INR per minute of call duration)", "secret": False, "required": False,
             "env_attr": "SARVAM_RATE_LLM_PER_MIN"},
            {"name": "rate_llm_input_per_1m", "label": "Rate card · LLM input (INR per 1M prompt tokens)", "secret": False, "required": False,
             "env_attr": "SARVAM_RATE_LLM_INPUT_PER_1M"},
            {"name": "rate_llm_output_per_1m", "label": "Rate card · LLM output (INR per 1M completion tokens, reasoning billed as output)", "secret": False, "required": False,
             "env_attr": "SARVAM_RATE_LLM_OUTPUT_PER_1M"},
            {"name": "rate_llm_cached_per_1m", "label": "Rate card · LLM cached input (INR per 1M cached prompt tokens)", "secret": False, "required": False,
             "env_attr": "SARVAM_RATE_LLM_CACHED_PER_1M"},
            {"name": "rate_tts_per_min", "label": "Rate card · TTS fallback (INR per minute of synthesized audio played)", "secret": False, "required": False,
             "env_attr": "SARVAM_RATE_TTS_PER_MIN"},
            {"name": "rate_tts_per_1k_chars", "label": "Rate card · TTS — Bulbul (INR per 1,000 characters synthesized)", "secret": False, "required": False,
             "env_attr": "SARVAM_RATE_TTS_PER_1K_CHARS"},
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
    (tenant value, else .env fallback, else the registry "default" —
    used by the Vapi voice profiles, which ship pre-filled — else "");
    sources[name] is "tenant" | "env" | "default" | None.
    INTERNAL — values include raw secrets.
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
        elif f.get("default"):
            values[f["name"]], sources[f["name"]] = f["default"], "default"
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


def twilio_configured(db: Session, tenant_id: str) -> bool:
    """Whether the tenant can place Twilio calls: a Twilio account SID
    plus auth token, from the tenant row or the .env fallback.

    Field-spelling tolerance: the registry names the SID field
    "account_sid", but older tenant rows (and the cost module's reader)
    store it as "auth_id" — both spellings count. A disabled row never
    counts, matching _assess.
    """
    values, _sources, row = resolve_fields(db, tenant_id, "twilio")
    if row is not None and not row.enabled:
        return False
    raw = (row.fields or {}) if row else {}
    sid = (values.get("account_sid") or raw.get("auth_id") or "").strip()
    token = (values.get("auth_token") or "").strip()
    return bool(sid and token)


def twilio_credentials(db: Session, tenant_id: str) -> tuple[str, str]:
    """(account_sid, auth_token) for the Twilio transport / signature
    checks, or ("", "") when not configured. Internal — never logged."""
    if not twilio_configured(db, tenant_id):
        return "", ""
    values, _sources, row = resolve_fields(db, tenant_id, "twilio")
    raw = (row.fields or {}) if row else {}
    sid = (values.get("account_sid") or raw.get("auth_id") or "").strip()
    return sid, (values.get("auth_token") or "").strip()


def self_hosted_pstn(db: Session, tenant_id: str) -> str:
    """The tenant's self-hosted PSTN choice: "plivo" (default) or
    "twilio". Tenant row wins, then the .env fallback; anything else
    (unset, disabled row, unknown value) means Plivo — the default is
    never a surprise."""
    values, _sources, row = resolve_fields(db, tenant_id, "pstn")
    if row is not None and not row.enabled:
        return "plivo"
    choice = (values.get("provider") or "").strip().lower()
    return choice if choice in ("plivo", "twilio") else "plivo"


def get_provider_config(tenant_id: str, mode: str, db: Session | None = None) -> dict:
    """INTERNAL service API for the orchestrator (A5+): the effective
    provider configuration for one tenant + orchestration mode.

    Returns raw credential values — NEVER serialise this into an API
    response. Shape:
        {"tenant_id", "mode", "ready": bool, "missing": [provider keys],
         "providers": {key: {"configured": bool, "source": "tenant"|"env"|None,
                             "fields": {name: value}}}}
    `ready` means a real call could be placed in this mode: managed needs
    Vapi; self-hosted needs Sarvam AND one telephony provider (Plivo,
    Exotel or Twilio). When not ready, dispatch must run in stub mode.
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
            if twilio_configured(db, tenant_id) and "plivo" in missing:
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
    # "Set by someone" counts tenant/.env values only — registry defaults
    # (the pre-filled Vapi voice profiles) must not make a fresh tenant
    # look half-configured.
    any_set = any(values[f["name"]] and sources[f["name"]] in ("tenant", "env")
                  for f in spec["fields"])

    if provider == "pstn":
        # A selection, not a credential: never report "Credentials saved".
        # Unset means the Plivo default applies; an explicit choice names it.
        _choice = (values.get("provider") or "").strip().lower()
        if _choice in ("plivo", "twilio"):
            status, detail = "configured", f"Self-hosted PSTN provider is {_choice}."
        elif _choice:
            status, detail = "failed", f"Unknown PSTN provider {_choice!r} — expected 'plivo' or 'twilio'."
        else:
            status, detail = "configured", "No PSTN provider selected — self-hosted dispatch defaults to Plivo."
    elif row is not None and not row.enabled:
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
    # A product User-Agent is load-bearing: provider edges (Vapi proven
    # live 2026-10-23) reject urllib's default "Python-urllib/x.y" UA with
    # HTTP 403 before auth is even evaluated — same key, curl UA = 200,
    # urllib UA = 403.
    headers = {"User-Agent": "VoiceAgentPlatform/1.0", **(headers or {})}
    req = urllib.request.Request(url, method=method, data=data, headers=headers)
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
        # Via the configurable base URL (the documented seam the smoke
        # suite points at its fake) — previously hardcoded, which made
        # this probe the one call the fakes could not exercise.
        code, err = _http("GET", f"{settings.VAPI_BASE_URL.rstrip('/')}/call?limit=1",
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
    if provider == "pstn":
        # A selection, not a credential — nothing to call. The value
        # itself is the check: unset means the Plivo default applies.
        choice = (v.get("provider") or "").strip().lower()
        if not choice:
            return "connected", "No PSTN provider selected — self-hosted dispatch defaults to Plivo."
        if choice in ("plivo", "twilio"):
            return "connected", f"Self-hosted PSTN provider is {choice} (selection only — the provider's own credentials are tested separately)."
        return "failed", f"Unknown PSTN provider {choice!r} — expected 'plivo' or 'twilio'."
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
