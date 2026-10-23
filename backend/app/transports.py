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
    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw) if raw else {}, None
            except Exception:
                return r.status, {}, None
    except urllib.error.HTTPError as e:
        try:
            e.read()
        except Exception:
            pass
        return e.code, None, None
    except Exception as e:  # DNS, TLS, timeout, refused…
        return None, None, type(e).__name__


def http_json(method: str, url: str, headers: dict, payload: dict | None):
    data = json.dumps(payload).encode() if payload is not None else None
    return _request(method, url, headers, data)


def http_form(url: str, headers: dict, form: dict):
    data = urllib.parse.urlencode(form).encode()
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


def lead_variables(campaign: Campaign, lead: Lead) -> dict:
    """The per-call variables handed to the provider. The agent's single
    prompt addresses the lead through placeholders (e.g. {{lead_name}},
    {{bank_name}} in the seeded RPI prompt); both spellings are carried."""
    af = lead.agent_fields or {}
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
    }


def resolve_language(agent: AgentConfig, lead: Lead) -> str:
    """§9: the application layer resolves the call language from the
    agent's language rule before dispatch (state rule → default → the
    agent's STT language)."""
    rule = agent.language_rule or {}
    by_state = rule.get("by_state") or {}
    return by_state.get(lead.state) or rule.get("default") or agent.stt_language or "hi-IN"


def _zero_cost(mode: str, currency: str) -> dict:
    return {"telephony": 0, "stt": 0, "llm": 0, "tts": 0, "total": 0,
            "currency": currency, "mode": mode}


def _placement_failed(mode: str, currency: str, provider_label: str,
                      status, err) -> dict:
    detail = f"HTTP {status}" if status else f"network error ({err})"
    return {
        "final_state": "FAILED",
        "end_reason": "provider_error",
        "disposition": "",
        "confidence": 0.0,
        "summary": (f"{provider_label} rejected the call placement ({detail}) — "
                    "no call was placed."),
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


def build_vapi_payload(session: CallSession, agent: AgentConfig,
                       campaign: Campaign, lead: Lead,
                       vapi_fields: dict) -> dict:
    """The Vapi POST /call/phone body for one attempt (§8.1 start-call,
    managed flavour). Pure — unit-testable without HTTP.

    The assistant carries the agent's single prompt verbatim as its
    system message, an analysis plan whose structured-data schema asks
    for exactly the disposition bucket set parsed from that same prompt
    plus the agent's captured fields (§8.2 normalisation source), and
    the serverUrl the end-of-call report returns to, authenticated by
    the §8.1 shared secret (serverUrlSecret → x-vapi-secret header).
    """
    from .orchestrator import disposition_buckets  # deferred: import cycle

    language = resolve_language(agent, lead)
    model_provider, model_name = _split_provider(
        vapi_fields.get("default_model"), _FALLBACK_MODEL)

    properties: dict = {
        "disposition": {
            "type": "string",
            "enum": disposition_buckets(agent.prompt),
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

    assistant = {
        "model": {
            "provider": model_provider,
            "model": model_name,
            "messages": [{"role": "system", "content": agent.prompt or ""}],
        },
        "voice": vapi_voice(vapi_fields, agent, language),
        "transcriber": {"provider": "deepgram", "model": "nova-2",
                        "language": language.split("-")[0]},
        "firstMessageMode": "assistant-speaks-first",
        "serverUrl": f"{public_base()}/api/telephony/vapi/webhook",
        "serverUrlSecret": settings.WEBHOOK_SECRET,
        "analysisPlan": {
            "summaryPlan": {"enabled": True},
            "structuredDataSchema": {
                "type": "object",
                "properties": properties,
                "required": ["disposition"],
            },
        },
        "maxDurationSeconds": agent.max_duration_s or 300,
    }
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
        if role in ("assistant", "user") and isinstance(text, str) and text.strip():
            turns.append({"speaker": "agent" if role == "assistant" else "customer",
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
        "cost_breakdown": {
            "telephony": cb.get("transport", 0) or 0,
            "stt": cb.get("stt", 0) or 0,
            "llm": cb.get("llm", 0) or 0,
            "tts": cb.get("tts", 0) or 0,
            "total": cb.get("total", 0) or 0,
            "currency": "USD",
            "mode": "managed",
        },
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
        return _placement_failed("managed", "USD", "Vapi", status, err)


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


def plivo_stream_outcome(duration_s: int, stt_status: str = "",
                         stt_language: str = "", stt_detail: str = "",
                         transcript: list | None = None) -> dict:
    """§8.2 outcome for a call whose media stream opened and closed.

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
            "summary": (f"[SELF-HOSTED] Plivo call completed with live Sarvam "
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
            "summary": ("[SELF-HOSTED SPIKE] Plivo call connected and the media "
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
        "summary": ("[SELF-HOSTED SPIKE] Plivo call connected and the media "
                    "stream opened and closed cleanly. No speech was "
                    "generated or heard — the conversation brain (Sarvam "
                    "STT → LLM → TTS) lands in A7/A8."),
        "captured_fields": {},
        "duration_s": int(duration_s or 0),
        "recording_url": "",
        "transcript": [],
        "cost_breakdown": _zero_cost("self_hosted", "INR"),
    }


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
        return _placement_failed("self_hosted", "INR", "Plivo", status, err)
