"""Provider-facing telephony routes (A6) — no operator JWT here; these
are called by the providers, so each route carries its own auth:

- POST /api/telephony/vapi/webhook — Vapi server messages. Auth: the
  §8.1 shared secret, sent by Vapi as the `x-vapi-secret` header (the
  serverUrlSecret set on the assistant at dispatch). Fails closed: no
  WEBHOOK_SECRET configured → 503; wrong secret → 401. An auth failure
  also logs a header fingerprint at WARNING — header names, value
  lengths and sha256 prefixes only, never values — so what a provider
  actually sent can be identified from the log.
  `end-of-call-report` messages finalize. `transcript` messages run
  the managed end-call net (see _handle_vapi_transcript): an
  assistant FINAL turn that is a complete approved closing ends the
  underlying Twilio leg, because Vapi's endCallPhrases alone proved
  unreliable. Every other message type is acknowledged. The report is normalised into §8.2 and landed through
  orchestrator.receive_result → finalize_attempt — idempotent, so a
  duplicate delivery never creates a second result.
- GET|POST /api/telephony/plivo/answer/{session_id} — Plivo fetches the
  answer document when the callee picks up: Plivo XML opening the
  bidirectional media stream to the placeholder stream endpoint (§8.3).
- POST /api/telephony/plivo/status/{session_id} — Plivo status callbacks
  (form-encoded). Plivo Signature V2 is verified whenever the signature
  header is present and the tenant's token is resolvable (§10). Terminal
  statuses finalize through the same path.
- POST /api/telephony/plivo/stream-status/{session_id} — stream status
  callbacks from the Stream element; recorded, never finalize from here.
- WS  /api/telephony/plivo/stream/{session_id} — the media-stream
  endpoint. Consumes Plivo's stream events (start / media / stop),
  marks the session IN_CALL on start, and when the stream closes
  finalizes the attempt exactly once through finalize_attempt.
  **A7: the STT leg lives inside this handler** — a Sarvam Saaras
  streaming session is opened per call (app.stt) with the lead's
  resolved language, Plivo media frames are forwarded to it, final
  transcript segments are persisted to the session as they arrive, and
  the finalize outcome carries the real accumulated transcript. With
  no Sarvam config the leg does not exist (stt_status not_configured)
  and the handler behaves exactly like the A6 spike.
  **A8: the conversation brain lives here too** — when Sarvam is
  configured, a ConversationEngine (app.conversation) runs beside the
  STT leg: it speaks the opening first (LLM turn from the agent's
  single prompt), replies to each STT final (LLM → sentence-chunked
  Bulbul TTS → playAudio frames back into this stream), and ends the
  call on the model's end_call action or a guard. At close the engine
  drains, the transcript is classified against the prompt's own
  DISPOSITIONS (app.conversation.classify_call), and the attempt
  finalizes with the real conversation. If the LLM leg failed, the
  outcome degrades honestly to the STT-shaped record plus a review
  flag — never a faked conversation or disposition.
  **A9: barge-in + silence live here as wiring** — the STT leg's
  partial callback feeds the engine's speech-activity signal (a
  partial while the agent speaks cuts its audio; the engine then
  asks this handler to flush the provider's buffered audio with
  Plivo's `clearAudio` message, carrying the stream id Plivo
  supplied in its events when it did), and the engine's silence
  state machine (nudge, then polite close with end_reason no_input)
  runs inside its wait loop. The handler's shape is unchanged.
- Twilio mirrors the Plivo section one-for-one (same finalize path,
  same engine wiring, same 160-byte/20 ms pacing clock): GET|POST
  /api/telephony/twilio/answer/{session_id} returns TwiML with a
  bidirectional <Stream> to the Twilio WebSocket endpoint; POST
  /api/telephony/twilio/status/{session_id} takes Twilio's
  form-encoded status callbacks (X-Twilio-Signature verified with
  HMAC-SHA1 whenever the header is present and the tenant's token
  is resolvable — Twilio's scheme is SHA1, not Plivo's SHA256);
  POST /api/telephony/twilio/stream-status/{session_id} is an
  acknowledgement only; WS /api/telephony/twilio/stream/{session_id}
  consumes Twilio media-stream events (connected/start/media/mark/
  stop), sends agent audio back as Twilio media messages, and
  sends Twilio's `clear` message (not Plivo's clearAudio) for
  barge-in. The two stream handlers share one finalize helper,
  _finalize_stream_session, so Plivo's behaviour is unchanged.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from .. import conversation, orchestrator, stt, transports, twilio_cost
from ..config import settings
from ..db import SessionLocal, get_db
from ..models import AgentConfig, CallSession, Campaign, Lead

router = APIRouter(tags=["telephony"])

log = logging.getLogger(__name__)


def _sha8(value: str) -> str:
    """First 8 hex of the value's sha256 — enough to tell two secrets
    apart in a log line, never enough to recover one."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _session_or_404(db: Session, session_id: str) -> CallSession:
    s = db.get(CallSession, session_id)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return s


# Managed end-call net (Oct 26, 2026): Vapi's endCallPhrases do not
# reliably end a managed call — 3 of 5 live managed calls on Oct 26
# stayed up after a complete closing until the customer hung up.
# So the platform ends the call itself: when a Vapi transcript server
# message carries an assistant FINAL turn that IS a complete approved
# closing (the agent prompt's CLOSINGS, the deterministic fallbacks,
# or any turn ending with an end-call phrase), the net
# completes the underlying Twilio leg. The end-of-call-report that
# follows still finalizes the attempt through the normal path.
# Guards: managed sessions only; never on partial transcripts; never
# on customer text; never twice per session; never raises — a hangup
# failure only logs, and Vapi's phrase detection remains as fallback.
_NET_FIRED: set[str] = set()

# Net v4 (Oct 26-27): Vapi streams one spoken turn as several FINAL
# segments. The Oct 26 PTP retest split its closing phrase across two
# segments ("आपका दिन" + "शुभ हो।"), so no single segment ended with
# the phrase and the call stayed open until the customer hung up.
# The net therefore accumulates assistant-final segments per session
# (one turn's text) and matches on the accumulated text; a
# customer-final turn clears the buffer, so a new agent turn starts
# fresh. Matching stays ends-with only, so a phrase in the middle of
# accumulated text never fires, and the buffer is capped.
_NET_BUFFER: dict[str, str] = {}


def _bare_norm(text: str) -> str:
    """Bare-phrase normalisation: closing-match norm, then trailing
    sentence punctuation stripped — the spoken turn usually ends with
    a danda or period that the bare phrase itself does not carry."""
    return conversation._norm_text(text).rstrip("।.!?").strip()


def _bind_session(db: Session, message: dict):
    """Resolve the CallSession for a Vapi message: metadata session_id
    first, else the CallSession with this provider_call_id."""
    call = message.get("call") or {}
    metadata = call.get("metadata") or message.get("metadata") or {}
    session_id = metadata.get("session_id", "")
    provider_call_id = call.get("id", "") or message.get("callId", "")
    session = None
    if session_id:
        session = db.get(CallSession, session_id)
    if session is None and provider_call_id:
        session = (
            db.query(CallSession)
            .filter(CallSession.provider_call_id == provider_call_id)
            .first()
        )
    return session


def _bn_skeleton(s: str) -> str:
    """Bengali/Devanagari closing-coda skeleton: vowel signs stripped,
    aspirated থ/थ folded to ত/त, nasal ং/ं folded to ন/न. STT drops
    aspiration and vowel signs first, so "থাকবেন" and its mangled
    renderings ("তক বেং", "তক বেহ") collapse to the same skeleton."""
    s = "".join(s.split())
    s = s.translate(str.maketrans({"থ": "ত", "थ": "त", "ং": "ন", "ं": "न"}))
    return "".join(ch for ch in s if ch not in
                   "া ি ী ু ূ ৃ ে ৈ ো ৌ ্ ा ि ी ु ू ृ े ै ो ौ ्".replace(" ", ""))


def _garbled_bn_closing(buffer_text: str) -> bool:
    """Garble-tolerant Bengali-closing match (Oct 27, Surath-approved).
    The multi transcriber renders a correctly spoken Bengali closing
    ("ভালো থাকবেন") as mangled text ("ভালো তক বেং", "ভালো তক বেহ")
    — neither Vapi's endCallPhrase nor the exact/spaceless match fires
    on it, so the call stays open (switch call ses_6d5c2eed67094556:
    both "ভালো থাকবে" and "ভালো থাকবেন" spoken, neither ended it).
    Rule: the accumulated turn's TAIL, after the last ভালো/বালো/
    भालो/बालो anchor, must be a short coda (<= 2 tokens, <= 15 chars)
    whose skeleton starts with তক/तक and contains ব/ब/ভ/भ, or is
    exactly তক/तक (bare থাক, aspiration-dropped). The ভ/ব and भ/ब
    alternations are the same STT confusion class: the Bengali
    Settlement call (ses_1596cf4562a649cc) evaluated a 12-char
    closing-like segment without firing, while the finalized
    11-char "ভালো তাকবে।" matches — the live segment evidently
    carried the ভ/ব variant, which the first version's anchor missed.
    Ends-with only, same as the rest of the net, so a ভালো-phrase
    mid-turn never fires (its coda is the rest of the turn, over the
    length gate). Bengali family only — Hindi closings render
    cleanly and stay exact-matched."""
    norm = _bare_norm(buffer_text)
    best, best_len = -1, 0
    for anchor in ("ভালো", "বালো", "भालो", "बालो"):
        i = norm.rfind(anchor)
        if i > best:
            best, best_len = i, len(anchor)
    if best < 0:
        return False
    coda = norm[best + best_len:].strip().rstrip("।.!?")
    if not coda or len(coda) > 15 or len(coda.split()) > 2:
        return False
    skel = _bn_skeleton(coda)
    return skel in ("তক", "तक") or (
        skel.startswith(("তক", "तक"))
        and ("ব" in skel or "ब" in skel or "ভ" in skel or "भ" in skel))


def _net_match(buffer_text: str, closings: list[str]) -> str:
    """Match one turn's accumulated assistant text. Returns
    "full-closing", the matched bare phrase, "garbled-bn-closing", or
    "". Ends-with only, so a phrase mid-turn never fires. The spaceless
    comparison covers a phrase split mid-word across two segments."""
    if conversation.is_closing_text(buffer_text, closings):
        return "full-closing"
    norm = _bare_norm(buffer_text)
    flat = "".join(norm.split())
    for phrase in transports.END_CALL_PHRASES:
        bare = _bare_norm(phrase)
        if norm.endswith(bare) or flat.endswith("".join(bare.split())):
            return phrase
    if _garbled_bn_closing(buffer_text):
        return "garbled-bn-closing"
    return ""


def _handle_vapi_transcript(message: dict) -> dict:
    """Act on one Vapi transcript server message. Always 200-safe."""
    try:
        if message.get("transcriptType") != "final":
            return {"received": True, "handled": False, "reason": "not final"}
        text = (message.get("transcript") or message.get("text") or "").strip()
        if not text:
            return {"received": True, "handled": False, "reason": "empty transcript"}
        if message.get("role") in ("user", "customer"):
            # A customer turn ends the agent's turn: clear its buffer
            # so the next agent turn accumulates fresh. (Vapi calls
            # customer speech role "user"; "customer" kept defensively.)
            db = SessionLocal()
            try:
                session = _bind_session(db, message)
                if session is not None:
                    _NET_BUFFER.pop(session.session_id, None)
            finally:
                db.close()
            return {"received": True, "handled": False, "reason": "customer turn"}
        if message.get("role") != "assistant":
            return {"received": True, "handled": False, "reason": "not assistant"}
        db = SessionLocal()
        try:
            session = _bind_session(db, message)
            if session is None:
                # DIAGNOSTIC (Oct 26-27, remove after the ending
                # investigation closes): reason-only, no transcript text.
                log.info("managed net skip reason=unknown-session")
                return {"received": True, "handled": False, "reason": "unknown session"}
            if (session.provider_mode or "") != "managed":
                log.info("managed net skip session=%s reason=not-managed",
                         session.session_id)
                return {"received": True, "handled": False, "reason": "not managed"}
            if session.state in ("COMPLETED", "FAILED"):
                log.info("managed net skip session=%s reason=already-final",
                         session.session_id)
                return {"received": True, "handled": False, "reason": "already final"}
            if session.session_id in _NET_FIRED:
                log.info("managed net skip session=%s reason=already-fired",
                         session.session_id)
                return {"received": True, "handled": False, "reason": "net already fired"}
            agent = None
            if session.campaign_id:
                campaign = db.get(Campaign, session.campaign_id)
                if campaign is not None:
                    agent = db.get(AgentConfig, campaign.agent_id)
            if agent is None and (session.test_lead or {}).get("agent_id"):
                agent = db.get(AgentConfig, session.test_lead["agent_id"])
            prompt = agent.prompt if agent is not None else ""
            closings = (conversation.prompt_closings(prompt)
                        + list(conversation.FALLBACK_CLOSINGS.values()))
            # DIAGNOSTIC (Oct 26-27, remove after the ending
            # investigation closes): timestamped arrival line per
            # evaluated assistant-final turn — length only, never text.
            # A matching turn is followed by the fire line below; an
            # evaluated turn with no fire line after it did not match.
            # Net v4: accumulate this turn's segments and match on the
            # accumulated text — a closing phrase split across two
            # segments (Oct 26 PTP retest) must still fire.
            buf = _NET_BUFFER.get(session.session_id, "")
            buf = (buf + " " + text).strip() if buf else text
            if len(buf) > 1000:
                buf = buf[-1000:]
            if len(_NET_BUFFER) > 10000:
                _NET_BUFFER.pop(next(iter(_NET_BUFFER)))
            _NET_BUFFER[session.session_id] = buf
            log.info("managed net eval session=%s len=%d buflen=%d",
                     session.session_id, len(text), len(buf))
            match = _net_match(buf, closings)
            matched = (match == "full-closing")
            bare = match if match not in ("", "full-closing") else ""
            if not matched and not bare:
                return {"received": True, "handled": False, "reason": "not a closing"}
            if len(_NET_FIRED) > 10000:
                _NET_FIRED.clear()
            _NET_FIRED.add(session.session_id)
            _NET_BUFFER.pop(session.session_id, None)
            ok = twilio_cost.hangup_twilio_call(db, session)
            log.info("managed end-call net session=%s matched=%.60s hangup=%s",
                     session.session_id, (matched or bare), ok)
            return {"received": True, "handled": True, "hangup": ok,
                    "session_id": session.session_id}
        finally:
            db.close()
    except Exception:
        log.exception("managed end-call net failed")
        return {"received": True, "handled": False, "reason": "net error"}


# ── Vapi (managed) ───────────────────────────────────────────────────────

@router.post("/api/telephony/vapi/webhook")
async def vapi_webhook(request: Request):
    # Read the body once, up front: the auth-failure fingerprint below
    # needs its size, and Starlette caches the bytes so the JSON parse
    # after auth sees exactly the same body as before.
    raw = await request.body()
    secret = settings.WEBHOOK_SECRET
    if not secret:
        return JSONResponse(
            status_code=503,
            content={"received": False,
                     "error": "WEBHOOK_SECRET is not configured — the webhook fails closed."})
    provided = request.headers.get("x-vapi-secret", "")
    if not hmac.compare_digest(provided, secret):
        # Fingerprint, never values: which headers arrived, how long
        # the secret-shaped ones are, and sha256 prefixes that tell
        # "same secret" from "different secret" at a glance.
        auth = request.headers.get("authorization", "")
        log.warning(
            "vapi webhook auth failed: headers=%s "
            "x-vapi-secret: present=%s len=%d sha8=%s "
            "authorization: present=%s scheme=%s "
            "expected sha8=%s content_type=%s body_bytes=%d",
            sorted(k.lower() for k in request.headers.keys()),
            bool(provided), len(provided), _sha8(provided),
            bool(auth), (auth.split(None, 1)[0] if auth else "-"),
            _sha8(secret),
            request.headers.get("content-type", "-"),
            len(raw))
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad webhook secret")
    try:
        body = json.loads(raw)
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body must be JSON")
    message = body.get("message") or body
    if message.get("type") == "transcript":
        return _handle_vapi_transcript(message)
    if message.get("type") != "end-of-call-report":
        return {"received": True, "handled": False,
                "reason": f"message type {message.get('type')!r} is not a result"}

    binding, outcome = transports.map_vapi_report(message)
    db = SessionLocal()
    try:
        session = None
        if binding["session_id"]:
            session = db.get(CallSession, binding["session_id"])
        if session is None and binding["provider_call_id"]:
            session = (
                db.query(CallSession)
                .filter(CallSession.provider_call_id == binding["provider_call_id"])
                .first()
            )
        if session is None:
            # Not one of ours (or a provider test ping) — acknowledge so
            # the provider does not retry-storm, and say why.
            return {"received": True, "handled": False, "reason": "unknown session"}
        got = orchestrator.receive_result(
            db, session.tenant_id,
            {"session_id": session.session_id, "provider_mode": "managed", **outcome})
        if got is None:
            return {"received": True, "handled": False, "reason": "session vanished"}
        _s, _r, created = got
        return {"received": True, "handled": True, "created": created,
                "session_id": session.session_id}
    finally:
        db.close()


# ── Plivo (self_hosted) ──────────────────────────────────────────────────

@router.api_route("/api/telephony/plivo/answer/{session_id}", methods=["GET", "POST"])
def plivo_answer(session_id: str, db: Session = Depends(get_db)):
    _session_or_404(db, session_id)
    return Response(content=transports.plivo_answer_xml(session_id),
                    media_type="application/xml")


@router.post("/api/telephony/plivo/status/{session_id}")
async def plivo_status(session_id: str, request: Request,
                       db: Session = Depends(get_db)):
    session = _session_or_404(db, session_id)
    form = {k: v for k, v in (await request.form()).items()}

    signature = request.headers.get("x-plivo-signature-v2")
    if signature:
        from .. import providers as prov

        cfg = prov.get_provider_config(session.tenant_id, "self_hosted", db=db)
        token = cfg["providers"]["plivo"]["fields"].get("auth_token", "")
        url = f"{transports.public_base()}/api/telephony/plivo/status/{session_id}"
        if token and not transports.verify_plivo_signature(url, form, token, signature):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Bad Plivo signature")

    call_uuid = form.get("CallUUID")
    if call_uuid:
        session.provider_call_id = call_uuid
    if form.get("RecordUrl"):
        session.recording_url = form["RecordUrl"]

    call_status = (form.get("CallStatus") or "").strip().lower()
    if call_status == "ringing" and session.state == "QUEUED":
        session.state = "DIALING"
        db.commit()
    elif call_status == "in-progress":
        if session.state in ("QUEUED", "DIALING"):
            session.state = "IN_CALL"
        if session.answered_at is None:
            session.answered_at = datetime.utcnow()
        db.commit()
    else:
        outcome = transports.plivo_terminal_outcome(call_status, form)
        if outcome is not None:
            orchestrator.finalize_attempt(db, session, outcome)
        else:
            db.commit()
    return {"received": True, "session_id": session_id, "call_status": call_status}


@router.post("/api/telephony/plivo/stream-status/{session_id}")
async def plivo_stream_status(session_id: str, request: Request,
                              db: Session = Depends(get_db)):
    """Stream-element status callbacks — acknowledged and recorded on
    the session only as state hints; finalization belongs to the stream
    handler / hangup callback (both idempotent)."""
    _session_or_404(db, session_id)
    return {"received": True, "session_id": session_id}


async def _finalize_stream_session(db, session_id, pipeline, engine, live,
                                   language, api_key, outcome_fn) -> None:
    """Shared stream-close finalize for the Plivo and Twilio handlers —
    moved verbatim from plivo_stream. The ONLY provider-dependent piece
    is outcome_fn (transports.plivo_stream_outcome or
    transports.twilio_stream_outcome): the degraded / no-brain shapes
    carry that provider's label; the full-conversation classification
    path is provider-agnostic."""
    if pipeline is not None:
        await pipeline.stop()
    if engine is not None:
        await engine.finish()
    s = db.get(CallSession, session_id)
    if s is not None:
        anchor = s.answered_at or s.started_at
        duration = int((datetime.utcnow() - anchor).total_seconds()) if anchor else 0
        status_now = (live or {}).get("status", "") or s.stt_status or ""
        detail_now = (live or {}).get("detail", "") or ""
        if status_now == "connecting":
            # The stream ended before Saaras ever opened — recorded
            # honestly as a failed STT leg, never as "active".
            status_now = "failed"
            detail_now = "STT session did not open before the stream ended"
        if pipeline is not None:
            s.stt_status = status_now
            s.stt_detail = detail_now[:500]
        legs = (engine.legs() if engine is not None else {
            "llm_status": "not_configured", "llm_detail": "",
            "tts_status": "not_configured", "tts_detail": ""})
        s.llm_status = legs["llm_status"]
        s.llm_detail = legs["llm_detail"][:500]
        s.tts_status = legs["tts_status"]
        s.tts_detail = legs["tts_detail"][:500]
        db.commit()
        # Telemetry for the next silent-call investigation (session_id
        # and counts only — never transcript text or phone numbers):
        # agent_turns + tts_frames + first_audio_s distinguish "the
        # brain never spoke" from "audio was pumped but unheard".
        _m = engine.metrics() if engine is not None else {}
        log.info("stream finalize session=%s agent_turns=%s tts_frames=%s "
                 "first_audio_s=%s holding=%s llm=%s tts=%s",
                 session_id, len(engine.turns) if engine is not None else 0,
                 _m.get("tts_frames", 0), _m.get("first_audio_s"),
                 _m.get("holding_played", False),
                 legs["llm_status"], legs["tts_status"])
        # Turn-loop tracing summary (stall fix): one compact line per
        # turn — stage budget without digging the per-event lines.
        if engine is not None:
            for _t in getattr(engine, "turn_timings", []):
                _sents = ";".join(
                    f"{_s.get('tts', '?')}s/{_s.get('status', '?')}"
                    for _s in _t.get("sentences", []))
                log.info("trace session=%s turn=%s outcome=%s dur=%.2f "
                         "llm_first=%s llm_end=%s tool=%s sents=[%s]",
                         session_id, _t.get("turn"), _t.get("outcome"),
                         _t.get("duration", 0), _t.get("llm_first_token"),
                         _t.get("llm_end"), _t.get("tool", ""), _sents)
        if engine is not None and engine.llm_status != "failed" \
                and any(t.get("speaker") == "agent" for t in engine.turns):
            # The full loop ran — classify the real transcript
            # against the agent prompt's own disposition definitions.
            classification, classify_usage = await asyncio.to_thread(
                conversation.classify_call, api_key, engine.model,
                engine.rendered_prompt, engine.captured_fields,
                engine.turns)
            outcome = conversation.conversation_outcome(
                duration, engine.turns,
                engine.end_reason or "stream_closed",
                classification,
                stt_status=status_now, stt_detail=detail_now,
                llm_status=engine.llm_status,
                tts_status=engine.tts_status)
        elif engine is not None:
            # The brain was configured but its LLM leg failed: the
            # call degrades to the STT-shaped record (A7 shapes) —
            # the transcript is whatever STT captured, no
            # classification was possible, and the result is
            # flagged for review rather than dressed up.
            outcome = outcome_fn(
                duration,
                stt_status=status_now,
                stt_language=language,
                stt_detail=detail_now,
                transcript=(live or {}).get("finals", []),
            )
            outcome["summary"] += (
                " The conversation brain (Sarvam LLM) failed for this "
                f"call ({engine.llm_detail or 'unknown error'}) — no "
                "reply was spoken and no classification was possible; "
                "flagged for review.")
            outcome["review_required"] = True
        else:
            outcome = outcome_fn(
                duration,
                stt_status=status_now if pipeline is not None else "not_configured",
                stt_language=language,
                stt_detail=detail_now,
                transcript=(live or {}).get("finals", []),
            )
        if engine is not None:
            # A12: the engine's cost inputs (synthesized-audio
            # frames actually pumped) ride the outcome into
            # finalize, where app.costing prices the legs from the
            # tenant's rate card. Not a §8.2 field — consumed and
            # discarded by the finalize path. The classifier call's
            # own token usage joins the LLM leg under
            # llm_usage_classify (it is a Sarvam LLM call for this
            # call, so it belongs in the call's LLM cost).
            outcome["cost_metrics"] = engine.metrics()
            if engine.llm_status != "failed" \
                    and any(t.get("speaker") == "agent"
                            for t in engine.turns):
                outcome["cost_metrics"]["llm_usage_classify"] = \
                    classify_usage
        orchestrator.finalize_attempt(db, s, outcome)


@router.websocket("/api/telephony/plivo/stream/{session_id}")
async def plivo_stream(websocket: WebSocket, session_id: str):
    """Media-stream endpoint with the A7 STT leg and the A8 conversation
    brain inside (see module doc).

    Plivo speaks first: a `start` event (stream + call ids, custom
    parameters), then `media` frames (base64 μ-law audio), then `stop`.
    On `start` — the call is answered — the Saaras session opens with
    the lead's resolved language, media frames are forwarded to it, and
    the conversation engine speaks the opening line and replies to each
    final (audio returning as playAudio frames). On close, the engine
    drains, the transcript is classified, and the attempt is finalized
    once through orchestrator.finalize_attempt. Leg failures never
    crash the stream: they are recorded on the session and degrade the
    outcome honestly.
    """
    await websocket.accept()
    db = SessionLocal()
    pipeline: stt.SaarasSttSession | None = None
    engine: conversation.ConversationEngine | None = None
    live: dict | None = None

    # Outbound playback clock (A9): Plivo plays media in real time —
    # one 160-byte μ-law frame is 20 ms of audio — so frames are PACED
    # to that clock instead of being dumped into the provider's buffer
    # as fast as the socket takes them. The first frame of a burst
    # still leaves instantly (the reply-latency target), but after that
    # "sent" tracks "heard": the engine's speaking window spans the
    # reply's true playback duration, which is what gives barge-in a
    # real window to cut and makes the transcript's "words actually
    # spoken" exact. An idle gap restarts the clock (a new burst).
    playback = {"next_at": 0.0}

    async def _send_audio(frame: bytes) -> None:
        """One outbound audio frame to the call, in Plivo's playAudio
        shape (μ-law 8 kHz, base64 payload), paced to the playback
        clock above."""
        now = time.monotonic()
        if playback["next_at"] < now:
            playback["next_at"] = now
        delay = playback["next_at"] - now
        if delay > 0:
            await asyncio.sleep(delay)
        playback["next_at"] += (len(frame) / 160.0) * 0.02
        await websocket.send_text(json.dumps({
            "event": "playAudio",
            "media": {"contentType": "audio/x-mulaw", "sampleRate": 8000,
                      "payload": base64.b64encode(frame).decode()},
        }))

    # A9 barge-in flush: when the engine cuts a reply mid-sentence it
    # asks for Plivo's clearAudio so the provider drops the audio it
    # has already buffered. Best-effort by design — the authoritative
    # cut is the engine stopping its own frame pump; this only clears
    # what Plivo queued beyond that. The stream id goes in when Plivo
    # supplied one in its stream events (captured by the pump below).
    stream_id_holder: dict = {}

    async def _clear_audio() -> None:
        msg: dict = {"event": "clearAudio"}
        if stream_id_holder.get("stream_id"):
            msg["stream_id"] = stream_id_holder["stream_id"]
        await websocket.send_text(json.dumps(msg))

    # The engine's end-call action sets this event instead of closing
    # the socket itself: closing a Starlette WebSocket from a second
    # task while the handler is blocked in receive_text() does not
    # reliably reach the client. The handler races its receive pump
    # against the event and ends the stream by returning, which closes
    # the socket deterministically (the A6/A7 close pattern).
    engine_end = asyncio.Event()

    async def _close_stream() -> None:
        engine_end.set()

    try:
        session = db.get(CallSession, session_id)
        if session is None:
            await websocket.close(code=4404)
            return

        # Resolve the call's language + the tenant's Sarvam config once,
        # up front: the STT leg and the conversation brain exist only
        # when Sarvam is configured (A4 — one key serves all three
        # legs). The resolved language is recorded either way.
        campaign = db.get(Campaign, session.campaign_id)
        agent = db.get(AgentConfig, campaign.agent_id) if campaign else None
        lead = db.get(Lead, session.lead_id)
        language = stt.saaras_language(
            transports.resolve_language(agent, lead) if agent and lead else "hi-IN")
        configured, api_key = stt.stt_config(session.tenant_id, db)
        session.stt_language = language
        if configured:
            session.stt_status = "connecting"
            live = stt.new_live_view(session_id, language, "connecting")
            pipeline = stt.SaarasSttSession(session_id, api_key, language, live)
            if agent is not None and campaign is not None and lead is not None:
                engine = conversation.ConversationEngine(
                    session_id=session_id, agent=agent, campaign=campaign,
                    lead=lead, language=language, api_key=api_key,
                    send_audio=_send_audio, close_stream=_close_stream,
                    clear_audio=_clear_audio)
                pipeline.on_final = engine.enqueue_final
                pipeline.on_partial = engine.note_speech_activity
        else:
            session.stt_status = "not_configured"
            live = stt.new_live_view(session_id, language, "not_configured")
        db.commit()

        async def _pump() -> None:
            """The receive loop: Plivo stream events in, media frames
            forwarded to the STT leg. Ends on `stop`, on disconnect, or
            when cancelled because the engine ended the call."""
            try:
                while True:
                    raw = await websocket.receive_text()
                    try:
                        event = json.loads(raw)
                    except Exception:
                        continue
                    kind = event.get("event")
                    sid_in = event.get("stream_id") or (
                        event.get("start") or {}).get("streamId")
                    if sid_in:
                        stream_id_holder["stream_id"] = sid_in
                    if kind == "start":
                        s = db.get(CallSession, session_id)
                        if s is not None and s.state in ("QUEUED", "DIALING"):
                            s.state = "IN_CALL"
                            if s.answered_at is None:
                                s.answered_at = datetime.utcnow()
                            db.commit()
                        if pipeline is not None:
                            await pipeline.start()
                        if engine is not None:
                            await engine.start()
                    elif kind == "media":
                        if pipeline is not None:
                            payload = (event.get("media") or {}).get("payload") or ""
                            try:
                                audio = base64.b64decode(payload)
                            except Exception:
                                audio = b""
                            if audio:
                                await pipeline.feed(audio)
                    elif kind == "stop":
                        break
            except Exception:
                pass  # disconnect / transport error: the stream is over either way

        pump = asyncio.ensure_future(_pump())
        waiters = [pump]
        if engine is not None:
            waiters.append(asyncio.ensure_future(engine_end.wait()))
        _done, _pending = await asyncio.wait(
            waiters, return_when=asyncio.FIRST_COMPLETED)
        for _t in _pending:
            _t.cancel()
        if pump in _done and not pump.cancelled():
            pump.exception()  # retrieve (the pump swallows its own errors)

        await _finalize_stream_session(
            db, session_id, pipeline, engine, live, language, api_key,
            transports.plivo_stream_outcome)
    finally:
        stt.end_live_view(session_id)
        db.close()


# ── Twilio (self_hosted, alternative PSTN) ───────────────────────────────
# Same boundary as the Plivo section, through Twilio's shapes:
# - GET|POST /api/telephony/twilio/answer/{session_id} — TwiML with a
#   <Start><Stream> opening the bidirectional media stream below.
# - POST /api/telephony/twilio/status/{session_id} — Twilio status
#   callbacks (form-encoded). The X-Twilio-Signature header (HMAC-SHA1)
#   is verified whenever it is present and the tenant's token is
#   resolvable — the same "verify when present" pattern as Plivo.
#   Terminal statuses finalize through the same path.
# - POST /api/telephony/twilio/stream-status/{session_id} — Stream
#   status callbacks; acknowledged only, never finalize from here.
# - WS  /api/telephony/twilio/stream/{session_id} — the media-stream
#   endpoint. Twilio speaks first (connected, then start with the
#   streamSid + media format), then media frames (base64 μ-law 8 kHz —
#   fed to the Saaras STT leg VERBATIM, exactly like Plivo's frames;
#   no transcoding), then stop. The STT leg, conversation brain,
#   barge-in (Twilio "clear" event) and silence machine are the same
#   wiring as the Plivo handler; only the event parsing, the outbound
#   media-message shape and the finalize outcome label differ. The
#   close path is the shared _finalize_stream_session helper with the
#   Twilio outcome flavour.

@router.api_route("/api/telephony/twilio/answer/{session_id}", methods=["GET", "POST"])
def twilio_answer(session_id: str, db: Session = Depends(get_db)):
    _session_or_404(db, session_id)
    return Response(content=transports.twilio_answer_xml(session_id),
                    media_type="application/xml")


@router.post("/api/telephony/twilio/status/{session_id}")
async def twilio_status(session_id: str, request: Request,
                        db: Session = Depends(get_db)):
    session = _session_or_404(db, session_id)
    form = {k: v for k, v in (await request.form()).items()}

    signature = request.headers.get("x-twilio-signature")
    if signature:
        from .. import providers as prov

        _sid, token = prov.twilio_credentials(db, session.tenant_id)
        url = f"{transports.public_base()}/api/telephony/twilio/status/{session_id}"
        if token and not transports.verify_twilio_signature(url, form, token, signature):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Bad Twilio signature")

    call_sid = form.get("CallSid")
    if call_sid:
        session.provider_call_id = call_sid
    if form.get("RecordingUrl"):
        session.recording_url = form["RecordingUrl"]

    call_status = (form.get("CallStatus") or "").strip().lower()
    if call_status in ("initiated", "ringing") and session.state == "QUEUED":
        session.state = "DIALING"
        db.commit()
    elif call_status == "in-progress":
        if session.state in ("QUEUED", "DIALING"):
            session.state = "IN_CALL"
        if session.answered_at is None:
            session.answered_at = datetime.utcnow()
        db.commit()
    else:
        outcome = transports.twilio_terminal_outcome(call_status, form)
        if outcome is not None:
            orchestrator.finalize_attempt(db, session, outcome)
        else:
            db.commit()
    return {"received": True, "session_id": session_id, "call_status": call_status}


@router.post("/api/telephony/twilio/stream-status/{session_id}")
async def twilio_stream_status(session_id: str, request: Request,
                               db: Session = Depends(get_db)):
    """Stream status callbacks — acknowledged and recorded on the
    session only as state hints; finalization belongs to the stream
    handler / hangup callback (both idempotent)."""
    _session_or_404(db, session_id)
    return {"received": True, "session_id": session_id}


@router.websocket("/api/telephony/twilio/stream/{session_id}")
async def twilio_stream(websocket: WebSocket, session_id: str):
    """Media-stream endpoint for Twilio — the Plivo handler's twin.

    Twilio's event flow: "connected", then "start" (streamSid +
    mediaFormat + customParameters), then "media" frames (base64
    μ-law 8 kHz, fed to the STT leg verbatim), then "stop". Outbound
    agent audio returns as "media" messages (base64 μ-law) on the same
    stream, paced to the same 20 ms / 160-byte playback clock as Plivo
    (Twilio plays streamed audio in real time too). Barge-in clears
    Twilio's buffer with a "clear" event. On close the shared finalize
    helper runs with the Twilio outcome flavour. Leg failures degrade
    exactly like the Plivo handler's.
    """
    await websocket.accept()
    db = SessionLocal()
    pipeline: stt.SaarasSttSession | None = None
    engine: conversation.ConversationEngine | None = None
    live: dict | None = None

    playback = {"next_at": 0.0}
    stream_id_holder: dict = {}

    async def _send_audio(frame: bytes) -> None:
        """One outbound audio frame to the call, in Twilio's media
        shape (μ-law 8 kHz, base64 payload), paced to the playback
        clock — one 160-byte frame is 20 ms of audio."""
        now = time.monotonic()
        if playback["next_at"] < now:
            playback["next_at"] = now
        delay = playback["next_at"] - now
        if delay > 0:
            await asyncio.sleep(delay)
        playback["next_at"] += (len(frame) / 160.0) * 0.02
        await websocket.send_text(json.dumps({
            "event": "media",
            "streamSid": stream_id_holder.get("stream_id", ""),
            "media": {"payload": base64.b64encode(frame).decode()},
        }))

    async def _clear_audio() -> None:
        # Twilio's clear event needs the streamSid; without one yet,
        # skip it — the authoritative cut is the engine stopping its
        # own frame pump, as on Plivo.
        if stream_id_holder.get("stream_id"):
            await websocket.send_text(json.dumps({
                "event": "clear",
                "streamSid": stream_id_holder["stream_id"],
            }))

    engine_end = asyncio.Event()

    async def _close_stream() -> None:
        engine_end.set()

    try:
        session = db.get(CallSession, session_id)
        if session is None:
            await websocket.close(code=4404)
            return

        campaign = db.get(Campaign, session.campaign_id)
        agent = db.get(AgentConfig, campaign.agent_id) if campaign else None
        lead = db.get(Lead, session.lead_id)
        language = stt.saaras_language(
            transports.resolve_language(agent, lead) if agent and lead else "hi-IN")
        configured, api_key = stt.stt_config(session.tenant_id, db)
        session.stt_language = language
        if configured:
            session.stt_status = "connecting"
            live = stt.new_live_view(session_id, language, "connecting")
            pipeline = stt.SaarasSttSession(session_id, api_key, language, live)
            if agent is not None and campaign is not None and lead is not None:
                engine = conversation.ConversationEngine(
                    session_id=session_id, agent=agent, campaign=campaign,
                    lead=lead, language=language, api_key=api_key,
                    send_audio=_send_audio, close_stream=_close_stream,
                    clear_audio=_clear_audio)
                pipeline.on_final = engine.enqueue_final
                pipeline.on_partial = engine.note_speech_activity
        else:
            session.stt_status = "not_configured"
            live = stt.new_live_view(session_id, language, "not_configured")
        db.commit()

        async def _pump() -> None:
            """The receive loop: Twilio stream events in, media frames
            forwarded to the STT leg. Ends on `stop`, on disconnect, or
            when cancelled because the engine ended the call."""
            try:
                while True:
                    raw = await websocket.receive_text()
                    try:
                        event = json.loads(raw)
                    except Exception:
                        continue
                    kind, sid_in, audio = transports.parse_twilio_stream_event(event)
                    if sid_in:
                        stream_id_holder["stream_id"] = sid_in
                    if kind == "start":
                        s = db.get(CallSession, session_id)
                        if s is not None and s.state in ("QUEUED", "DIALING"):
                            s.state = "IN_CALL"
                            if s.answered_at is None:
                                s.answered_at = datetime.utcnow()
                            db.commit()
                        if pipeline is not None:
                            await pipeline.start()
                        if engine is not None:
                            await engine.start()
                    elif kind == "media":
                        if pipeline is not None and audio:
                            await pipeline.feed(audio)
                    elif kind == "stop":
                        break
            except Exception:
                pass  # disconnect / transport error: the stream is over either way

        pump = asyncio.ensure_future(_pump())
        waiters = [pump]
        if engine is not None:
            waiters.append(asyncio.ensure_future(engine_end.wait()))
        _done, _pending = await asyncio.wait(
            waiters, return_when=asyncio.FIRST_COMPLETED)
        for _t in _pending:
            _t.cancel()
        if pump in _done and not pump.cancelled():
            pump.exception()  # retrieve (the pump swallows its own errors)

        await _finalize_stream_session(
            db, session_id, pipeline, engine, live, language, api_key,
            transports.twilio_stream_outcome)
    finally:
        stt.end_live_view(session_id)
        db.close()
