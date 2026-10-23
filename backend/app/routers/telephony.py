"""Provider-facing telephony routes (A6) — no operator JWT here; these
are called by the providers, so each route carries its own auth:

- POST /api/telephony/vapi/webhook — Vapi server messages. Auth: the
  §8.1 shared secret, sent by Vapi as the `x-vapi-secret` header (the
  serverUrlSecret set on the assistant at dispatch). Fails closed: no
  WEBHOOK_SECRET configured → 503; wrong secret → 401. Only
  `end-of-call-report` messages finalize; every other message type is
  acknowledged. The report is normalised into §8.2 and landed through
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
"""
import asyncio
import base64
import hmac
import json
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from .. import conversation, orchestrator, stt, transports
from ..config import settings
from ..db import SessionLocal, get_db
from ..models import AgentConfig, CallSession, Campaign, Lead

router = APIRouter(tags=["telephony"])


def _session_or_404(db: Session, session_id: str) -> CallSession:
    s = db.get(CallSession, session_id)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return s


# ── Vapi (managed) ───────────────────────────────────────────────────────

@router.post("/api/telephony/vapi/webhook")
async def vapi_webhook(request: Request):
    secret = settings.WEBHOOK_SECRET
    if not secret:
        return JSONResponse(
            status_code=503,
            content={"received": False,
                     "error": "WEBHOOK_SECRET is not configured — the webhook fails closed."})
    provided = request.headers.get("x-vapi-secret", "")
    if not hmac.compare_digest(provided, secret):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad webhook secret")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body must be JSON")
    message = body.get("message") or body
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
            if engine is not None and engine.llm_status != "failed" \
                    and any(t.get("speaker") == "agent" for t in engine.turns):
                # The full loop ran — classify the real transcript
                # against the agent prompt's own disposition definitions.
                classification = await asyncio.to_thread(
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
                outcome = transports.plivo_stream_outcome(
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
                outcome = transports.plivo_stream_outcome(
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
                # discarded by the finalize path.
                outcome["cost_metrics"] = engine.metrics()
            orchestrator.finalize_attempt(db, s, outcome)
    finally:
        stt.end_live_view(session_id)
        db.close()
