"""Real transports (A6) — the telephony spike behind the A5 boundary.

Two transports register alongside the stub in app.orchestrator.TRANSPORTS:

- **VapiTransport (managed)** — builds the Vapi start-call payload from
  the agent's SINGLE prompt (as the assistant's system message — Vapi
  substitutes the {{variables}} from assistantOverrides.variableValues),
  the tenant's Vapi defaults (model / voice / phone number, A4) and the
  lead's variables; POSTs it to Vapi and returns None — the call is
  PLACED, not finished. The result arrives later on the end-of-call
  webhook (app.routers.telephony), is normalised into the §8.2 shape by
  map_vapi_report() and lands through orchestrator.receive_result →
  finalize_attempt, exactly like the stub's results. Idempotent: a
  duplicate webhook delivery is a no-op there.
- **PlivoTransport (self_hosted)** — skeleton to the same boundary:
  creates the Plivo outbound call with an answer URL on this app. The
  answer route returns Plivo XML opening a bidirectional media stream to
  the placeholder stream endpoint, which — in this chunk — speaks
  nothing and, when it closes, finalizes the attempt through the same
  path with an honestly-labelled spike result. The conversation brain
  (Sarvam STT → LLM → Bulbul TTS) plugs into that stream endpoint in
  A7/A8; nothing here pretends a conversation happened.
- **TwilioTransport (self_hosted, alternative PSTN)** — the same
  boundary through Twilio: creates the call via Twilio's REST API with
  an answer URL returning TwiML that opens a bidirectional media
  stream to the Twilio stream endpoint. Selected per tenant by the
  "pstn" provider setting (plivo | twilio, default plivo) when the
  tenant's Twilio credentials are configured; Plivo stays the default
  and its code path is untouched.

Honesty gates (unchanged from A5): a real transport runs ONLY when
TELEPHONY_MODE equals the campaign's mode AND A4 says the tenant's
provider for that mode is ready (see orchestrator.resolve_transport).
With TELEPHONY_MODE=stub the stub transport runs even when keys exist.
A placement the provider rejects is finalised FAILED with the reason —
never dressed up as a call.

ALL provider HTTP goes through http_json() / http_form() below — the one
seam. Provider base URLs are configuration (VAPI_BASE_URL /
PLIVO_BASE_URL), which is how the A6 smoke suite points both transports
at a local fake provider: no real provider is ever called in tests.
Error messages carry HTTP status codes and exception class names only —
never response bodies (they could echo data back) and never secrets.
"""
import base64
import hashlib
import hmac
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from sqlalchemy.orm import Session

from . import providers as prov
from .config import settings
from .models import AgentConfig, CallSession, Campaign, Lead

# Platform fallbacks when the tenant has not set Vapi defaults (A4) —
# tenant settings override these; they exist so a tenant with only an
# API key can still place a call.
_FALLBACK_MODEL = ("openai", "gpt-4o")
_FALLBACK_VOICE = ("vapi", "Elliot")


# ── The HTTP seam ────────────────────────────────────────────────────────

def _request(method: str, url: str, headers: dict, data: bytes | None,
             timeout: int = 15):
    """Returns (status, parsed_json_or_None, error_label). Never raises;
    error_label is an exception class name only."""
    # Product User-Agent — provider edges reject urllib's default UA with
    # 403 before auth (proven live against Vapi, 2026-10-23).
    headers = {"User-Agent": "VoiceAgentPlatform/1.0", **headers}
    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw) if raw else {}, None
            except Exception:
                return r.status, {}, None
    except urllib.error.HTTPError as e:
        # Keep the provider's error body: its message is the diagnosis
        # (live lesson 2026-10-23 — a bare "HTTP 400" cost an hour;
        # Vapi's body named the exact invalid field).
        try:
            raw = e.read()
            parsed = json.loads(raw) if raw else {}
        except Exception:
            parsed = {}
        return e.code, parsed, None
    except Exception as e:  # DNS, TLS, timeout, refused…
        return None, None, type(e).__name__


def http_json(method: str, url: str, headers: dict, payload: dict | None):
    data = json.dumps(payload).encode() if payload is not None else None
    return _request(method, url, headers, data)


def http_form(url: str, headers: dict, form: dict):
    # doseq=True so list values encode as repeated parameters (Twilio's
    # StatusCallbackEvent needs one parameter per event; a single
    # space-separated value is ignored by Twilio and no callbacks arrive).
    data = urllib.parse.urlencode(form, doseq=True).encode()
    headers = {**headers, "Content-Type": "application/x-www-form-urlencoded"}
    return _request("POST", url, headers, data)


def _basic_auth(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


# ── Shared helpers ───────────────────────────────────────────────────────

def public_base() -> str:
    return (settings.PUBLIC_BASE_URL or "http://localhost:8000").rstrip("/")


def ws_base() -> str:
    base = public_base()
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):]
    if base.startswith("http://"):
        return "ws://" + base[len("http://"):]
    return base


_LOAN_TYPE_LABELS = {"vehicle_loan": "two-wheeler loan"}


def _loan_type_label(product):
    """Display name for a campaign product. Unknown products pass through
    unchanged, so future products (credit card and others) plug in here
    without a code change to the variable itself."""
    return _LOAN_TYPE_LABELS.get(product or "", product or "")


def lead_variables(campaign: Campaign, lead: Lead) -> dict:
    """The per-call variables handed to the provider. The agent's single
    prompt addresses the lead through placeholders (e.g. {{lead_name}},
    {{bank_name}} in the seeded RPI prompt); both spellings are carried."""
    af = lead.agent_fields or {}
    _acct = (
        af.get("account_no", "") or af.get("account_number", "")
        or (af.get("product_identifier", "") if af.get("product_identifier_type") == "account_number" else "")
    )
    _acct = str(_acct).strip()
    _rc = str(af.get("product_identifier", "") or "").strip()
    return {
        "name": lead.name or "",
        "lead_name": lead.name or "",
        "bank_name": lead.bank_name or "",
        "bank": lead.bank_name or "",
        "state": lead.state or "",
        "phone_number": lead.phone_number or "",
        "product": campaign.product or "",
        "product_identifier": af.get("product_identifier", "") or "",
        "product_identifier_type": af.get("product_identifier_type", "") or "",
        # Pre-computed numbers (2026-10-27): the model repeatedly failed
        # to EXTRACT these from the raw values on live calls — it spoke
        # 9876 (first four) as the "last four digits" of 987654 in
        # Hindi and English, and the full 987654 in Bengali — despite
        # an explicit prompt guardrail. So the platform computes them
        # and the prompt reads the ready value instead of computing:
        # account_last4 is the final four characters, and
        # product_identifier_spelled is the RC already split into
        # single characters ("W B 0 2 A B 1 2 3 4").
        "account_last4": _acct[-4:] if len(_acct) >= 4 else _acct,
        "product_identifier_spelled": " ".join(_rc),
        "account_number": _acct,
        "principal": af.get("principal", "") or "",
        "loan_type": af.get("loan_type", "") or _loan_type_label(campaign.product),
        "due_date": af.get("due_date", "") or "",
        "amount_due": af.get("amount_due", "") or "",
        "outstanding": af.get("outstanding", "") or "",
        "promise_date": af.get("promise_date", "") or "",
        "promised_amount": af.get("promised_amount", "") or "",
        "settlement_cap_pct": (campaign.settlement_authority_pct
                               if getattr(campaign, "settlement_authority_pct", None) is not None else ""),
    }


def resolve_language(agent: AgentConfig, lead: Lead) -> str:
    """§9: the application layer resolves the call language from the
    agent's language rule before dispatch (state rule → default → the
    agent's STT language). A per-lead preferred_language in agent_fields
    (en / hi / bn, or a full xx-IN code) wins over the rule — additive
    only: leads without it resolve exactly as before."""
    fields = (lead.agent_fields or {}) if lead else {}
    override = (fields.get("preferred_language") or "").strip()
    if override:
        code = override.split("-")[0].strip().lower()
        if code in ("en", "hi", "bn"):
            return f"{code}-IN"
    rule = agent.language_rule or {}
    by_state = rule.get("by_state") or {}
    return by_state.get(lead.state) or rule.get("default") or agent.stt_language or "hi-IN"


def _zero_cost(mode: str, currency: str) -> dict:
    return {"telephony": 0, "stt": 0, "llm": 0, "tts": 0, "total": 0,
            "currency": currency, "mode": mode}


def _placement_failed(mode: str, currency: str, provider_label: str,
                      status, err, provider_message=None) -> dict:
    detail = f"HTTP {status}" if status else f"network error ({err})"
    summary = (f"{provider_label} rejected the call placement ({detail}) — "
               "no call was placed.")
    if provider_message:
        # The provider's own words (Vapi returns a string or a list of
        # validation strings) — sanitised to plain text, length-capped.
        if isinstance(provider_message, (list, tuple)):
            text = "; ".join(str(m) for m in provider_message)
        else:
            text = str(provider_message)
        text = " ".join(text.split())[:400]
        if text:
            summary += f" {provider_label} said: {text}"
    return {
        "final_state": "FAILED",
        "end_reason": "provider_error",
        "disposition": "",
        "confidence": 0.0,
        "summary": summary,
        "captured_fields": {},
        "duration_s": 0,
        "recording_url": "",
        "transcript": [],
        "cost_breakdown": _zero_cost(mode, currency),
    }


# ── Managed transport: Vapi ──────────────────────────────────────────────

def _split_provider(value: str, fallback: tuple[str, str]) -> tuple[str, str]:
    """Tenant defaults are plain text (A4): "name" or "provider/name"."""
    value = (value or "").strip()
    if not value:
        return fallback
    if "/" in value:
        p, m = value.split("/", 1)
        return p.strip() or fallback[0], m.strip() or fallback[1]
    return fallback[0], value


def vapi_voice(fields: dict, agent: AgentConfig, language: str) -> dict:
    """Pick the Vapi voice for one call from the tenant's per-language
    voice profiles (A6): profile by the call's resolved language prefix
    (en / hi / bn), voice ID by the AGENT's configured voice gender; the
    TTS model rides along in the voice object — distinct from the
    assistant's LLM model (default_model). Languages without a profile
    fall back to the tenant's legacy default voice, then the platform
    fallback."""
    prefix = (language or "").split("-")[0].strip().lower()
    gender = (getattr(agent, "voice_gender", "") or "female").strip().lower()
    if gender not in ("female", "male"):
        gender = "female"
    voice_id = (fields.get(f"{prefix}_voice_id_{gender}") or "").strip()
    if voice_id:
        voice = {"provider": (fields.get(f"{prefix}_voice_provider") or "11labs").strip(),
                 "voiceId": voice_id}
        tts_model = (fields.get(f"{prefix}_tts_model") or "").strip()
        if tts_model:
            voice["model"] = tts_model
        return voice
    provider, vid = _split_provider(fields.get("default_voice"), _FALLBACK_VOICE)
    return {"provider": provider, "voiceId": vid}


def _vapi_transcriber(fields: dict, language: str) -> dict:
    """Deepgram transcriber for one call, from the tenant's per-language
    transcriber profiles (Settings → Providers → Vapi): provider + model
    by the call's resolved language prefix (en / hi / bn); the language
    code itself is derived from the resolved language. The fields arrive
    resolved (tenant → .env → registry default), so the fallbacks here
    only guard a hand-built fields dict — and they are the live-proven
    shipped defaults (2026-10-23): Bengali needs nova-3, because Vapi's
    nova-2 language enum contains NO Bengali — a bn placement on nova-2
    is rejected HTTP 400 before anything else runs; Hindi and English
    are in the nova-2 enum and stay there.
    NOTE 2026-10-26: language="multi" was tried here for mid-call
    switching and REVERTED the same night — it garbled Hindi
    transcription on a live call (ses_0606c033f56a4177: mangled
    opening, "No, no, no", HOSTILE) and was pulled before the Oct 28
    demo. A/B 2026-10-27 (Surath's P1 order, demo-eve): multi was
    re-enabled for a controlled live A/B — and FAILED the control:
    the Hindi control call (ses_af2fd27fc6374274) looped its intro
    because multi on nova-2 mangled Hindi input into Latin-script
    garbage ("haan haan" never registered, HOSTILE mislabel again).
    nova-2 + multi is OUT (failed twice). REVERTED to monolingual
    2026-10-27 00:26 for platform safety; variant nova-3 + multi
    RUNNING since 00:27 on Surath's go (see below) — the verdict
    decides whether it stays or monolingual returns before the demo."""
    code = (language or "hi").split("-")[0].strip().lower() or "hi"
    provider = (fields.get(f"{code}_transcriber_provider") or "").strip() or "deepgram"
    model = (fields.get(f"{code}_transcriber_model") or "").strip()
    if not model:
        model = "nova-3" if code == "bn" else "nova-2"
    # VARIANT 2026-10-27 (Surath's P1 order): nova-3 + multi. nova-2 +
    # multi mangled Hindi input itself on both attempts, so the
    # variant forces Deepgram's newer model with multilingual
    # detection. A/B-only: the verdict decides whether this stays or
    # monolingual returns before the demo.
    if provider == "deepgram":
        return {"provider": provider, "model": "nova-3", "language": "multi"}
    return {"provider": provider, "model": model, "language": code}


# Phrases Vapi listens for to end a managed call (shared with the
# managed end-call net in routers/telephony.py — the net also ends
# the call when the assistant speaks one of these alone after a
# closing, so the two lists must never drift apart).
END_CALL_PHRASES = [
    "ভালো থাকবেন",
    "ভালো থাকবে",
    "ভালো থাক",
    "आपका दिन शुभ हो",
    "Have a good day",
    "চিন্তা করবেন না",
    "चिंता न करें",
    "don't worry about this right now",
]


def _today_str(tz_name):
    """'27 October 2026' in the campaign's timezone (UTC fallback).
    Never raises — a bad timezone name falls back to UTC."""
    from datetime import datetime, timezone
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name) if tz_name else timezone.utc
    except Exception:
        tz = timezone.utc
    try:
        now = datetime.now(tz)
    except Exception:
        now = datetime.now(timezone.utc)
    return f"{now.day} {now.strftime('%B %Y')}"


def build_vapi_payload(session: CallSession, agent: AgentConfig,
                       campaign: Campaign, lead: Lead,
                       vapi_fields: dict) -> dict:
    """The Vapi POST /call/phone body for one attempt (§8.1 start-call,
    managed flavour). Pure — unit-testable without HTTP.

    The assistant carries the campaign's PINNED prompt verbatim (C2;
    pre-C2 campaigns fall back to the agent's live prompt) as its
    system message, an analysis plan whose structured-data schema asks
    for exactly the disposition bucket set parsed from that same prompt
    plus the agent's captured fields (§8.2 normalisation source), and
    the server binding the end-of-call report returns to: the legacy
    serverUrl/serverUrlSecret pair (the URL is honoured for routing —
    live-proven) plus the assistant's `server` object carrying the
    same URL and the webhook authentication. Precedence on that
    object: the tenant's server credential ID when one is configured
    (Vapi's current credential-based auth — the credential's token is
    what Vapi transmits as the X-Vapi-Secret header); otherwise the
    §8.1 shared secret inline (the legacy serverUrlSecret field's
    secret is stored/echoed only, never transmitted).
    """
    from .orchestrator import campaign_prompt, disposition_buckets  # deferred: import cycle

    # C2 pinning: the campaign's pinned prompt is the assistant's
    # system message (and the disposition enum's source). Pre-C2
    # campaigns fall back to the agent's live prompt.
    prompt = campaign_prompt(campaign, agent)
    language = resolve_language(agent, lead)
    # Language grounding (managed): the agent's prompt lists one opening
    # line per language and says "in the call's language", but never
    # states which language this call is — so the model guessed (it
    # picked the first-listed English template on a Hindi call,
    # 2026-10-26). Ground the OPENING only, per call, from the same
    # resolved language that already picks voice + transcriber:
    # West Bengal -> Bengali, default -> Hindi, preferred_language
    # wins. Scoped to the opening on purpose: the prompt's LANGUAGE
    # section still governs mid-call switching, so a customer who
    # answers in another language still gets switched to.
    _LANG_NAMES = {"hi": "Hindi", "bn": "Bengali", "en": "English"}
    _lang_code = (language or "hi").split("-")[0].strip().lower() or "hi"
    _lang_name = _LANG_NAMES.get(_lang_code, _lang_code)
    # Date grounding (managed): the model has no current-date context,
    # so a confirmed "15th November" was read back as 2023 on live
    # calls in all three languages (2026-10-27) even after the prompt
    # gained a speak-the-year rule — the rule fixed the FORM (a year
    # was spoken) but not the VALUE. State today per the campaign's
    # timezone (UTC fallback) so year-bearing read-backs land in the
    # right year. Placed BEFORE the language bracket so the message
    # still ends with the grounding line the tests assert.
    _today = _today_str(getattr(campaign, "timezone", None))
    system_content = (prompt or "") + (
        f"\n\n[Today is {_today}. When you confirm a date, always "
        f"include the year.]\n"
        f"[The call's language is {_lang_name}. Begin the call in "
        f"{_lang_name}, including the opening line.]"
    )
    model_provider, model_name = _split_provider(
        vapi_fields.get("default_model"), _FALLBACK_MODEL)

    properties: dict = {
        "disposition": {
            "type": "string",
            "enum": disposition_buckets(prompt),
            "description": "Exactly one disposition bucket, as defined in "
                           "the DISPOSITIONS section of the system prompt.",
        }
    }
    json_types = {"string": "string", "number": "number",
                  "boolean": "boolean", "date": "string"}
    for f in agent.captured_fields or []:
        name = f.get("name")
        if name and name != "disposition":
            properties[name] = {
                "type": json_types.get(f.get("type", "string"), "string"),
                "description": f.get("description", "") or "",
            }

    webhook_url = f"{public_base()}/api/telephony/vapi/webhook"
    assistant = {
        "model": {
            "provider": model_provider,
            "model": model_name,
            "messages": [{"role": "system", "content": system_content}],
            # NO tools on purpose — above all no endCall tool. Live
            # A/B 2026-10-24: without any tool, the RPI v3 test call
            # spoke its opening and the full verbatim closing (the call
            # then stayed up — the hangup problem); WITH the endCall
            # tool added, the model short-circuited BOTH test calls to
            # a bare one-word farewell and never spoke the approved
            # closings at all. A tool at closing time competes with the
            # spoken closing and wins. Hangup is therefore driven by
            # endCallPhrases below, never by a model-invoked tool.
        },
        "voice": vapi_voice(vapi_fields, agent, language),
        "transcriber": _vapi_transcriber(vapi_fields, language),
        "firstMessageMode": "assistant-speaks-first",
        "serverUrl": webhook_url,
        "serverUrlSecret": settings.WEBHOOK_SECRET,
        # Explicit serverMessages: Vapi's default server set does NOT
        # include "transcript" (it is client-side by default), so
        # without this the managed end-call net in
        # routers/telephony.py never sees a closing. end-of-call-report
        # stays because the finalize path depends on it; nothing else
        # in the default set is consumed by the platform.
        "serverMessages": ["end-of-call-report", "transcript"],
        "analysisPlan": {
            "summaryPlan": {"enabled": True},
            "structuredDataSchema": {
                "type": "object",
                "properties": properties,
                "required": ["disposition"],
            },
        },
        "maxDurationSeconds": agent.max_duration_s or 300,
        # Hangup mechanism: Vapi ends the call when the assistant speaks
        # one of these phrases. Each phrase occurs ONLY as the final
        # words of an approved closing in the agent prompts (the RPI
        # closings in en / hi / bn), so the call can end only by the
        # agent speaking a complete approved closing — never by a
        # shortcut farewell. Covers the CONFIRMED, CPC, DENIAL,
        # DENIAL-REPEATED, CALLBACK, HOSTILE and DECEASED closings in
        # all three languages. DNC closings end with apology phrases
        # that also occur mid-closing elsewhere, so they are
        # deliberately NOT listed; a DNC call ends via the customer's
        # hangup or the silence timeout. "ভালো থাকবে" is the same
        # closing word with its final nasal dropped — the rendering
        # the Bengali closings actually produced on live calls
        # (2026-10-25, twice on PTP retest v2) — so it is listed
        # alongside the full form; it occurs only at closings too.
        "endCallPhrases": END_CALL_PHRASES,
    }
    credential_id = (vapi_fields.get("server_credential_id") or "").strip()
    if credential_id:
        # Credential-based server auth — Vapi's current model (docs:
        # server-url/server-authentication): the `server` object
        # references a credential in the tenant's Vapi account whose
        # token equals our WEBHOOK_SECRET, and that token is what
        # Vapi sends as the X-Vapi-Secret header on server messages.
        # Referencing OUR credential matters: without an explicit one,
        # Vapi's "credential fallback" signs with the ORGANIZATION's
        # DEFAULT server credential — a foreign token our webhook
        # rightly rejects (live-proven 2026-10-23: the auth-failure
        # fingerprint showed a 64-char x-vapi-secret, sha8 ≠ ours,
        # while the dashboard warned the fallback was active). Per
        # the docs, credentialId REPLACES inline auth — no secret key
        # is sent alongside it.
        assistant["server"] = {"url": webhook_url,
                               "credentialId": credential_id}
    elif settings.WEBHOOK_SECRET:
        # Inline-secret fallback (no credential ID configured): the
        # §8.1 secret on the `server` object — per Vapi's docs this
        # inline secret is likewise transmitted as the X-Vapi-Secret
        # header; the legacy serverUrlSecret above is stored/echoed
        # but not transmitted (live-proven 2026-10-23: every real
        # server message 401'd while an identical manual probe with
        # the header passed). Skipped entirely when no secret is
        # configured, so stub/local payloads are unchanged.
        assistant["server"] = {"url": webhook_url,
                               "secret": settings.WEBHOOK_SECRET}
    payload: dict = {
        "assistant": assistant,
        "assistantOverrides": {
            "variableValues": lead_variables(campaign, lead),
        },
        "customer": {"number": session.to_number or lead.phone_number,
                     "name": lead.name or ""},
        "metadata": {
            "session_id": session.session_id,
            "attempt_id": session.attempt_id,
            "tenant_id": session.tenant_id,
        },
    }
    if vapi_fields.get("phone_number_id"):
        payload["phoneNumberId"] = vapi_fields["phone_number_id"]
    return payload


_ENDED_REASON_MAP = {
    "assistant-ended-call": ("COMPLETED", "agent_end_call"),
    "customer-ended-call": ("COMPLETED", "customer_hangup"),
    "customer-did-not-answer": ("NO_ANSWER", "no_answer"),
    "customer-did-not-say-anything": ("COMPLETED", "customer_hangup"),
    "customer-busy": ("BUSY", "busy"),
    "voicemail": ("VOICEMAIL", "amd_voicemail"),
    "exceeded-max-duration": ("COMPLETED", "max_duration"),
    "manually-canceled": ("FAILED", "cancelled"),
    "pipeline-error": ("FAILED", "provider_error"),
    "assistant-error": ("FAILED", "provider_error"),
    "phone-call-provider-closed-websocket": ("FAILED", "provider_error"),
    "twilio-error": ("FAILED", "provider_error"),
    "vonage-error": ("FAILED", "provider_error"),
}


def _parse_iso(ts: str):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _vapi_transcript(message: dict) -> list:
    artifact = message.get("artifact") or {}
    msgs = artifact.get("messages") or []
    turns = []
    for m in msgs:
        role = m.get("role")
        text = m.get("content") or m.get("message") or ""
        # Vapi voice calls report the assistant's turns with role "bot"
        # (persisted assistants use "assistant"); both are agent speech.
        # The "system" turn is the prompt and is never transcript.
        if role in ("assistant", "bot", "user") and isinstance(text, str) and text.strip():
            turns.append({"speaker": "agent" if role in ("assistant", "bot") else "customer",
                          "text": text, "offset_s": 0.0})
    if turns:
        return turns
    raw = artifact.get("transcript") or message.get("transcript") or ""
    for line in str(raw).splitlines():
        line = line.strip()
        if line.startswith("AI:"):
            turns.append({"speaker": "agent", "text": line[3:].strip(), "offset_s": 0.0})
        elif line.startswith("User:"):
            turns.append({"speaker": "customer", "text": line[5:].strip(), "offset_s": 0.0})
    return turns


def map_vapi_report(message: dict):
    """Normalise a Vapi end-of-call-report into the §8.2 outcome shape.

    Returns (binding, outcome): binding carries the ids that locate our
    session (metadata first, provider call id as fallback); outcome is
    the dict orchestrator.receive_result / finalize_attempt consume.
    Pure — unit-testable without HTTP.
    """
    call = message.get("call") or {}
    metadata = call.get("metadata") or message.get("metadata") or {}
    binding = {
        "session_id": metadata.get("session_id", ""),
        "attempt_id": metadata.get("attempt_id", ""),
        "provider_call_id": call.get("id", "") or message.get("callId", ""),
    }

    ended = message.get("endedReason", "") or ""
    if ended in _ENDED_REASON_MAP:
        final_state, end_reason = _ENDED_REASON_MAP[ended]
    elif "error" in ended:
        final_state, end_reason = "FAILED", "provider_error"
    else:
        final_state, end_reason = "COMPLETED", (ended[:60] or "provider_end")

    analysis = message.get("analysis") or {}
    structured = dict(analysis.get("structuredData") or {})
    disposition = structured.pop("disposition", "") or ""

    started, ended_at = _parse_iso(message.get("startedAt", "")), _parse_iso(message.get("endedAt", ""))
    if started and ended_at:
        duration_s = max(0, int((ended_at - started).total_seconds()))
    else:
        duration_s = int(message.get("durationSeconds") or 0)

    artifact = message.get("artifact") or {}
    cb = message.get("costBreakdown") or {}
    summary = message.get("summary") or analysis.get("summary") or ""

    # Managed-mode parity (A10): the self-hosted path flags a connected
    # call whose classification produced no disposition (A8 classifier
    # failure → review_required). The managed path must behave the
    # same: a call Vapi completed but whose analysis carried no
    # disposition is flagged for human review — never silently stored
    # as an unclassified success, and never given an invented bucket.
    review_required = final_state == "COMPLETED" and not disposition
    if review_required and not summary:
        summary = ("Managed call completed, but the provider's analysis "
                   "returned no disposition — flagged for human review.")
    breakdown = {
        "telephony": cb.get("transport", 0) or 0,
        "stt": cb.get("stt", 0) or 0,
        "llm": cb.get("llm", 0) or 0,
        "tts": cb.get("tts", 0) or 0,
        "total": cb.get("total", 0) or 0,
        "currency": "USD",
        "mode": "managed",
    }
    if cb:
        # A12: the provider metered this call itself — finalize keeps
        # these figures verbatim (app.costing). A report WITHOUT a
        # cost breakdown gets no stamp: finalize then estimates
        # telephony from the tenant's rate card and marks the other
        # legs "not reported by the provider".
        breakdown["cost_source"] = "provider"
    outcome = {
        "final_state": final_state,
        "end_reason": end_reason,
        "disposition": disposition,
        "confidence": 0.0,
        "review_required": review_required,
        "summary": summary,
        "captured_fields": structured,
        "duration_s": duration_s,
        "recording_url": artifact.get("recordingUrl") or message.get("recordingUrl") or "",
        "transcript": _vapi_transcript(message),
        "cost_breakdown": breakdown,
    }
    return binding, outcome


class VapiTransport:
    """Managed transport: place the call on Vapi, finish by webhook.

    run() returns None once the call is placed — the §8.2 result lands
    later through the webhook (see routers/telephony.py). A placement
    Vapi rejects returns a FAILED outcome for the finalize path.
    """

    name = "managed"

    def run(self, db: Session, session: CallSession, agent: AgentConfig,
            campaign: Campaign, seq: int, run_start: datetime):
        lead = db.get(Lead, session.lead_id)
        cfg = prov.get_provider_config(session.tenant_id, "managed", db=db)
        fields = cfg["providers"]["vapi"]["fields"]
        payload = build_vapi_payload(session, agent, campaign, lead, fields)
        status, body, err = http_json(
            "POST", f"{settings.VAPI_BASE_URL.rstrip('/')}/call/phone",
            {"Authorization": f"Bearer {fields.get('api_key', '')}",
             "Content-Type": "application/json"},
            payload)
        session.started_at = datetime.utcnow()
        if status and 200 <= status < 300 and body and body.get("id"):
            session.provider_call_id = body["id"]
            session.state = "DIALING"
            db.commit()
            return None  # placed — the webhook finalizes this attempt
        db.commit()
        return _placement_failed(
            "managed", "USD", "Vapi", status, err,
            body.get("message") if isinstance(body, dict) else None)


# ── Self-hosted transport: Plivo (skeleton to the same boundary) ────────

def plivo_answer_xml(session_id: str) -> str:
    """The answer-URL document (§8.3): opens the bidirectional media
    stream to our placeholder stream endpoint, session-bound both by the
    URL path and by a stream Parameter. No <Speak> — in this chunk the
    stream speaks nothing; the brain arrives in A7/A8."""
    stream_url = f"{ws_base()}/api/telephony/plivo/stream/{session_id}"
    status_url = f"{public_base()}/api/telephony/plivo/stream-status/{session_id}"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        f'  <Stream bidirectional="true" audioTrack="inbound" '
        f'contentType="audio/x-mulaw;rate=8000" keepCallAlive="true" '
        f'statusCallbackUrl="{status_url}" statusCallbackMethod="POST">'
        f"{stream_url}"
        f'<Parameter name="session_id" value="{session_id}"/></Stream>\n'
        "</Response>"
    )


def _self_hosted_stream_outcome(provider_label: str, duration_s: int, stt_status: str = "",
                                stt_language: str = "", stt_detail: str = "",
                                transcript: list | None = None) -> dict:
    """§8.2 outcome for a self-hosted call whose media stream opened and
    closed, shared by the Plivo and Twilio flavours — only the provider
    label in the summary strings differs.

    Three honest shapes, by how the A7 STT leg went (stt_status is the
    record on the session row; the stream handler passes it through):

    - "active" — Saaras transcribed the call: the transcript is the REAL
      accumulated final segments; the summary says STT is live and that
      no speech was generated (the reply brain is A8).
    - "failed" — the STT session failed to open or dropped: whatever
      finals were captured stand, headed by a [TRANSCRIPT PARTIAL]
      marker turn, and the spike label stays — the transport path ran,
      the ear did not.
    - "" / "not_configured" — the original A6 spike outcome, unchanged:
      no Sarvam config, no STT leg, transcript empty.
    """
    if stt_status == "active":
        turns = list(transcript or [])
        return {
            "final_state": "COMPLETED",
            "end_reason": "stream_closed",
            "disposition": "",
            "confidence": 0.0,
            "summary": (f"[SELF-HOSTED] {provider_label} call completed with live Sarvam "
                        f"Saaras transcription ({stt_language or 'language n/a'}) "
                        f"— {len(turns)} final segment(s) captured in the "
                        "transcript. No speech was generated — the reply "
                        "brain (LLM → Bulbul TTS) lands in A8."),
            "captured_fields": {},
            "duration_s": int(duration_s or 0),
            "recording_url": "",
            "transcript": turns,
            "cost_breakdown": _zero_cost("self_hosted", "INR"),
        }
    if stt_status == "failed":
        reason = stt_detail or "unknown error"
        turns = [{
            "speaker": "system",
            "text": (f"[TRANSCRIPT PARTIAL — Sarvam STT failed ({reason}). "
                     "Only the segments captured before the failure are "
                     "shown.]"),
            "offset_s": 0.0,
        }] + list(transcript or [])
        return {
            "final_state": "COMPLETED",
            "end_reason": "stream_closed",
            "disposition": "",
            "confidence": 0.0,
            "summary": ("[SELF-HOSTED SPIKE] "
                        f"{provider_label} call connected and the media "
                        f"stream opened and closed cleanly, but the Sarvam STT "
                        f"session failed ({reason}) — the transcript is "
                        "partial. No speech was generated — the conversation "
                        "brain (LLM → TTS) lands in A8."),
            "captured_fields": {},
            "duration_s": int(duration_s or 0),
            "recording_url": "",
            "transcript": turns,
            "cost_breakdown": _zero_cost("self_hosted", "INR"),
        }
    return {
        "final_state": "COMPLETED",
        "end_reason": "stream_closed",
        "disposition": "",
        "confidence": 0.0,
        "summary": ("[SELF-HOSTED SPIKE] "
                    f"{provider_label} call connected and the media "
                    "stream opened and closed cleanly. No speech was "
                    "generated or heard — the conversation brain (Sarvam "
                    "STT → LLM → TTS) lands in A7/A8."),
        "captured_fields": {},
        "duration_s": int(duration_s or 0),
        "recording_url": "",
        "transcript": [],
        "cost_breakdown": _zero_cost("self_hosted", "INR"),
    }


def plivo_stream_outcome(duration_s: int, stt_status: str = "",
                         stt_language: str = "", stt_detail: str = "",
                         transcript: list | None = None) -> dict:
    """Plivo flavour of the shared self-hosted stream outcome — the
    exact A6/A7 shape, unchanged (provider label "Plivo")."""
    return _self_hosted_stream_outcome(
        "Plivo", duration_s, stt_status, stt_language, stt_detail, transcript)


def twilio_stream_outcome(duration_s: int, stt_status: str = "",
                          stt_language: str = "", stt_detail: str = "",
                          transcript: list | None = None) -> dict:
    """Twilio flavour of the shared self-hosted stream outcome — the
    same three honest shapes, provider label "Twilio"."""
    return _self_hosted_stream_outcome(
        "Twilio", duration_s, stt_status, stt_language, stt_detail, transcript)


# Plivo CallStatus → (§8.2 final_state, end_reason) for terminal states.
_PLIVO_TERMINAL = {
    "completed": ("COMPLETED", "call_completed"),
    "no-answer": ("NO_ANSWER", "no_answer"),
    "timeout": ("NO_ANSWER", "no_answer"),
    "busy": ("BUSY", "busy"),
    "cancel": ("FAILED", "cancelled"),
    "failed": ("FAILED", "provider_error"),
    "machine": ("VOICEMAIL", "amd_voicemail"),
}


def plivo_terminal_outcome(call_status: str, form: dict) -> dict | None:
    """§8.2 outcome for a terminal Plivo status callback, or None when
    the status is not terminal (ringing / in-progress are state updates,
    handled by the router)."""
    key = (call_status or "").strip().lower()
    if key not in _PLIVO_TERMINAL:
        return None
    final_state, end_reason = _PLIVO_TERMINAL[key]
    duration = 0
    for field in ("Duration", "BillDuration"):
        try:
            duration = int(form.get(field) or 0)
            break
        except (TypeError, ValueError):
            continue
    summary = {
        "COMPLETED": ("[SELF-HOSTED SPIKE] Plivo reported the call completed "
                      "(hangup callback) before the media stream finalized it."),
        "NO_ANSWER": "Plivo reported the call was not answered.",
        "BUSY": "Plivo reported the number was busy.",
        "FAILED": f"Plivo reported the call ended as {key}.",
        "VOICEMAIL": "Plivo's answering-machine detection reported a machine.",
    }[final_state]
    return {
        "final_state": final_state,
        "end_reason": end_reason,
        "disposition": "",
        "confidence": 0.0,
        "summary": summary,
        "captured_fields": {},
        "duration_s": duration,
        "recording_url": form.get("RecordUrl", "") or "",
        "transcript": [],
        "cost_breakdown": _zero_cost("self_hosted", "INR"),
    }


def verify_plivo_signature(url: str, params: dict, auth_token: str,
                           signature: str) -> bool:
    """Plivo Signature V2 check for callbacks (§10): base64 HMAC-SHA256
    over the callback URL + the sorted form parameters concatenated."""
    if not auth_token or not signature:
        return False
    base = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), base.encode(), hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature)


class PlivoTransport:
    """Self-hosted telephony spike: place the call via Plivo's REST API
    with our answer URL; the call finalizes through the stream endpoint /
    status callbacks (routers/telephony.py) — the same finalize path.

    run() returns None once Plivo accepts the call. A tenant whose
    self-hosted readiness comes from Exotel only is honestly FAILED:
    the Exotel adapter is not part of this spike.
    """

    name = "self_hosted"

    def run(self, db: Session, session: CallSession, agent: AgentConfig,
            campaign: Campaign, seq: int, run_start: datetime):
        cfg = prov.get_provider_config(session.tenant_id, "self_hosted", db=db)
        plivo = cfg["providers"]["plivo"]
        if not plivo["configured"]:
            return {
                "final_state": "FAILED",
                "end_reason": "provider_error",
                "disposition": "",
                "confidence": 0.0,
                "summary": ("Self-hosted dispatch is configured via Exotel only — "
                            "the A6 spike ships the Plivo transport; no call was placed."),
                "captured_fields": {},
                "duration_s": 0,
                "recording_url": "",
                "transcript": [],
                "cost_breakdown": _zero_cost("self_hosted", "INR"),
            }
        fields = plivo["fields"]
        answer_url = f"{public_base()}/api/telephony/plivo/answer/{session.session_id}"
        form = {
            "from": session.from_number or "",
            "to": session.to_number or "",
            "answer_url": answer_url,
            "answer_method": "POST",
            "status_callback": f"{public_base()}/api/telephony/plivo/status/{session.session_id}",
            "status_callback_method": "POST",
            "record": "true",
        }
        status, body, err = http_form(
            f"{settings.PLIVO_BASE_URL.rstrip('/')}/v1/Account/{fields['auth_id']}/Call/",
            {"Authorization": _basic_auth(fields["auth_id"], fields["auth_token"])},
            form)
        session.started_at = datetime.utcnow()
        if status and 200 <= status < 300 and body and body.get("request_uuid"):
            session.provider_call_id = body["request_uuid"]
            session.state = "DIALING"
            db.commit()
            return None  # placed — stream / status callbacks finalize it
        db.commit()
        return _placement_failed(
            "self_hosted", "INR", "Plivo", status, err,
            (body.get("message") or body.get("error")) if isinstance(body, dict) else None)


# ── Self-hosted transport: Twilio (Plivo alternative) ───────────────────

def twilio_answer_xml(session_id: str) -> str:
    """The answer-URL document for Twilio (§8.3, Twilio flavour): a
    <Connect><Stream> opening the bidirectional media stream to our
    Twilio stream endpoint, session-bound by the URL path and by a
    stream Parameter. <Connect> is REQUIRED here, not <Start>: a
    <Start><Stream> does not block the call, so with nothing after it
    Twilio ends the call the moment the TwiML ends — the first two
    pilots cut instantly on pickup for exactly this reason. <Connect>
    holds the call open while the stream runs, and closing the stream
    ends the call, matching the Plivo behaviour. The stream URL must be
    wss:// in production (ws_base() derives wss from an https
    PUBLIC_BASE_URL); a local http base yields ws://, which Twilio
    rejects at call time, so live Twilio calls need the public https
    base. No <Say> — the brain speaks through the stream, never
    through TwiML speech."""
    stream_url = f"{ws_base()}/api/telephony/twilio/stream/{session_id}"
    status_url = f"{public_base()}/api/telephony/twilio/stream-status/{session_id}"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        f'  <Connect><Stream url="{stream_url}" '
        f'statusCallback="{status_url}" statusCallbackMethod="POST">'
        f'<Parameter name="session_id" value="{session_id}"/></Stream></Connect>\n'
        "</Response>"
    )


def parse_twilio_stream_event(event: dict) -> tuple[str, str, bytes]:
    """Parse one Twilio media-stream WebSocket message into
    (kind, stream_sid, audio_bytes) — pure, unit-testable.

    Kinds: "connected" | "start" | "media" | "mark" | "stop" |
    "unknown". Audio is the base64 μ-law 8 kHz payload of a "media"
    event, decoded; an undecodable payload yields b"" (the stream
    handler skips empty audio, same as the Plivo pump). Twilio's media
    format is μ-law at 8 kHz — the SAME format the Saaras STT leg is
    fed on the Plivo path — so no transcoding happens anywhere: the
    decoded bytes are fed to pipeline.feed() verbatim, and the engine's
    160-byte μ-law frames go back as Twilio media messages verbatim
    (base64 only at the WebSocket boundary, as on Plivo)."""
    if not isinstance(event, dict):
        return "unknown", "", b""
    kind = (event.get("event") or "").strip().lower()
    start = event.get("start") or {}
    sid = event.get("streamSid") or start.get("streamSid") or ""
    if kind == "media":
        payload = (event.get("media") or {}).get("payload") or ""
        try:
            audio = base64.b64decode(payload, validate=True)
        except Exception:
            audio = b""
        return "media", sid, audio
    if kind in ("connected", "start", "mark", "stop"):
        return kind, sid, b""
    return "unknown", sid, b""


# Twilio CallStatus → (§8.2 final_state, end_reason) for terminal states.
_TWILIO_TERMINAL = {
    "completed": ("COMPLETED", "call_completed"),
    "busy": ("BUSY", "busy"),
    "failed": ("FAILED", "provider_error"),
    "no-answer": ("NO_ANSWER", "no_answer"),
    "canceled": ("FAILED", "cancelled"),
}


def twilio_terminal_outcome(call_status: str, form: dict) -> dict | None:
    """§8.2 outcome for a terminal Twilio status callback, or None when
    the status is not terminal (queued / initiated / ringing /
    in-progress are state updates, handled by the router). A completed
    call whose AnsweredBy says a machine answered maps to VOICEMAIL —
    the Twilio equivalent of Plivo's machine status."""
    key = (call_status or "").strip().lower()
    if key not in _TWILIO_TERMINAL:
        return None
    final_state, end_reason = _TWILIO_TERMINAL[key]
    if key == "completed" and (form.get("AnsweredBy") or "").strip().lower().startswith("machine"):
        final_state, end_reason = "VOICEMAIL", "amd_voicemail"
    duration = 0
    try:
        duration = int(form.get("CallDuration") or 0)
    except (TypeError, ValueError):
        duration = 0
    summary = {
        "COMPLETED": ("[SELF-HOSTED] Twilio reported the call completed "
                      "(hangup callback) before the media stream finalized it."),
        "NO_ANSWER": "Twilio reported the call was not answered.",
        "BUSY": "Twilio reported the number was busy.",
        "FAILED": f"Twilio reported the call ended as {key}.",
        "VOICEMAIL": "Twilio's answering-machine detection reported a machine.",
    }[final_state]
    return {
        "final_state": final_state,
        "end_reason": end_reason,
        "disposition": "",
        "confidence": 0.0,
        "summary": summary,
        "captured_fields": {},
        "duration_s": duration,
        "recording_url": form.get("RecordingUrl", "") or "",
        "transcript": [],
        "cost_breakdown": _zero_cost("self_hosted", "INR"),
    }


def verify_twilio_signature(url: str, params: dict, auth_token: str,
                            signature: str) -> bool:
    """Twilio request-signature check (Twilio's scheme): base64
    HMAC-SHA1 over the callback URL + the sorted form parameters
    concatenated. (Plivo's scheme is SHA256 — the two must not be
    confused.)"""
    if not auth_token or not signature:
        return False
    base = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), base.encode(), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature)


class TwilioTransport:
    """Self-hosted telephony via Twilio — the Plivo alternative.

    Same boundary as PlivoTransport: run() places the call via Twilio's
    REST API with our answer URL; the call finalizes through the Twilio
    stream endpoint / status callbacks (routers/telephony.py) into the
    same finalize path. run() returns None once Twilio accepts the
    call. A tenant without Twilio credentials is honestly FAILED —
    never silently placed on Plivo.
    """

    name = "self_hosted"

    def run(self, db: Session, session: CallSession, agent: AgentConfig,
            campaign: Campaign, seq: int, run_start: datetime):
        sid, token = prov.twilio_credentials(db, session.tenant_id)
        if not sid or not token:
            return {
                "final_state": "FAILED",
                "end_reason": "provider_error",
                "disposition": "",
                "confidence": 0.0,
                "summary": ("Self-hosted PSTN is set to Twilio but no Twilio "
                            "credentials are configured for this tenant — "
                            "no call was placed."),
                "captured_fields": {},
                "duration_s": 0,
                "recording_url": "",
                "transcript": [],
                "cost_breakdown": _zero_cost("self_hosted", "INR"),
            }
        answer_url = f"{public_base()}/api/telephony/twilio/answer/{session.session_id}"
        form = {
            "From": session.from_number or "",
            "To": session.to_number or "",
            "Url": answer_url,
            "Method": "POST",
            "StatusCallback": f"{public_base()}/api/telephony/twilio/status/{session.session_id}",
            # Repeated parameters: Twilio ignores a single space-separated
            # value, which is why the first two pilots got zero callbacks.
            "StatusCallbackEvent": ["initiated", "ringing", "answered", "completed"],
            "StatusCallbackMethod": "POST",
            "Record": "true",
        }
        status, body, err = http_form(
            f"{settings.TWILIO_BASE_URL.rstrip('/')}/2010-04-01/Accounts/{sid}/Calls.json",
            {"Authorization": _basic_auth(sid, token)},
            form)
        session.started_at = datetime.utcnow()
        if status and 200 <= status < 300 and body and body.get("sid"):
            session.provider_call_id = body["sid"]
            session.state = "DIALING"
            db.commit()
            return None  # placed — stream / status callbacks finalize it
        db.commit()
        return _placement_failed(
            "self_hosted", "INR", "Twilio", status, err,
            (body.get("message") or body.get("code")) if isinstance(body, dict) else None)
