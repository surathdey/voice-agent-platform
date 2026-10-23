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
  and the handler behaves exactly like the A6 spike. A8 adds the
  LLM → TTS reply legs beside the STT leg.
"""
import base64
import hmac
import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from .. import orchestrator, stt, transports
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
    """Media-stream endpoint with the A7 STT leg inside (see module doc).

    Plivo speaks first: a `start` event (stream + call ids, custom
    parameters), then `media` frames (base64 μ-law audio), then `stop`.
    On `start` — the call is answered — the Saaras session opens with
    the lead's resolved language and media frames are forwarded to it;
    finals land on session.transcript as they arrive. On close, the
    attempt is finalized once through orchestrator.finalize_attempt
    with the STT-aware stream outcome (transports.plivo_stream_outcome).
    STT failures never crash the stream: they are recorded on the
    session and the call finalizes with a partial transcript.
    """
    await websocket.accept()
    db = SessionLocal()
    pipeline: stt.SaarasSttSession | None = None
    live: dict | None = None
    try:
        session = db.get(CallSession, session_id)
        if session is None:
            await websocket.close(code=4404)
            return

        # Resolve the call's language + the tenant's Sarvam config once,
        # up front: the STT leg exists only when Sarvam is configured
        # (A4). The resolved language is recorded either way.
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
        else:
            session.stt_status = "not_configured"
            live = stt.new_live_view(session_id, language, "not_configured")
        db.commit()

        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    event = json.loads(raw)
                except Exception:
                    continue
                kind = event.get("event")
                if kind == "start":
                    s = db.get(CallSession, session_id)
                    if s is not None and s.state in ("QUEUED", "DIALING"):
                        s.state = "IN_CALL"
                        if s.answered_at is None:
                            s.answered_at = datetime.utcnow()
                        db.commit()
                    if pipeline is not None:
                        await pipeline.start()
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

        if pipeline is not None:
            await pipeline.stop()
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
                db.commit()
            orchestrator.finalize_attempt(db, s, transports.plivo_stream_outcome(
                duration,
                stt_status=status_now if pipeline is not None else "not_configured",
                stt_language=language,
                stt_detail=detail_now,
                transcript=(live or {}).get("finals", []),
            ))
    finally:
        stt.end_live_view(session_id)
        db.close()
