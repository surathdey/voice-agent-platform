"""Browser test harness (A11) — talk to an agent from the console.

- POST /api/browser-tests (operator JWT) — create a browser-test
  session: an agent (tenant-scoped), optionally a campaign of that
  agent for context (its product + lead list) and one of its saved
  leads, or hand-typed fictitious lead values. The session is created
  with channel "browser-test", campaign_id / lead_id NULL, and the
  test-lead SNAPSHOT on the session row — a snapshot, never a lead
  row, so a test can never consume a campaign lead's attempts or earn
  it a retry (orchestrator.finalize_attempt additionally skips lead
  routing for this channel outright).
- WS /api/telephony/browser/stream/{session_id}?token=<JWT> — the
  media endpoint (browsers cannot set WebSocket headers, so the
  operator token rides as a query parameter; the session must belong
  to the token's tenant — anything else closes 4404, the tenant
  isolation rule in WebSocket form).

The stream handler is a TRANSPORT, not a second conversation: it runs
the same Saaras STT session (app.stt), the same ConversationEngine
(app.conversation) and the same finalize path (orchestrator
.finalize_attempt) as the Plivo stream — only the wire differs:

- Inbound: the browser ships PCM16 little-endian at 16 kHz (binary
  messages). The handler converts each 20 ms chunk to μ-law at 8 kHz
  (app.audio_codec) and feeds the STT session exactly the frames the
  Plivo path feeds it.
- Outbound: the engine pumps μ-law frames; the handler paces them on
  the same 20 ms-per-frame playback clock as the Plivo handler (so
  the engine's "words actually spoken" accounting keeps its meaning),
  decodes each frame to PCM16 and sends it as a binary message for
  Web Audio playback.
- Barge-in: the engine's interrupt is the SAME one A9 built (STT
  partial → engine stops its pump, abandons unsent sentences). The
  transport difference is the flush: Plivo gets a `clearAudio`
  message (best-effort — the provider's buffer state is unknowable);
  here the handler sends an `interrupt` event and the page drops its
  own Web Audio queue and stops the currently scheduled source —
  a stronger, exact cut, because the playback buffer lives in the
  page. Audio already played cannot be recalled in either world.
- Transcript: a watcher task mirrors the engine's turns (finals and
  agent replies, in order) plus the STT live partial into `transcript`
  / `partial` events, so the page shows the conversation line by line
  as it happens. The durable record is the same session transcript
  every other call gets.

The finished test lands through finalize_attempt with the §8.2 shape,
its summary prefixed [BROWSER TEST], and appears in the Calls list and
the tenant dashboard counts with channel "browser-test" — the same
result surface as any call, marked as a test, attached to no campaign.

If Sarvam is not configured for the tenant the legs do not exist and
there is nothing to test with: the stream reports `not_configured`
and the session finalizes FAILED with that reason in its summary —
failure reported as failure, never a simulated conversation.
"""
import asyncio
import json
import time
import uuid
from datetime import datetime

import jwt as pyjwt
from fastapi import APIRouter, Depends, HTTPException, WebSocket, status
from sqlalchemy.orm import Session

from .. import audio_codec, conversation, orchestrator, stt, transports
from ..db import SessionLocal, get_db
from ..deps import get_current_user
from ..models import AgentConfig, CallResult, CallSession, Campaign, Lead, User
from ..schemas import PRODUCTS, BrowserTestIn, BrowserTestOut
from ..security import decode_token

router = APIRouter(tags=["browser-test"])

CHANNEL = "browser-test"
# One inbound conversion unit: 20 ms of PCM16 @16 kHz (640 bytes) →
# one 160-byte μ-law frame @8 kHz, the Plivo media frame size.
_PCM_CHUNK = 640


# ── Session creation (operator API) ──────────────────────────────────────

def _tenant_agent(db: Session, user: User, agent_id: str) -> AgentConfig:
    agent = (
        db.query(AgentConfig)
        .filter(AgentConfig.agent_id == agent_id,
                AgentConfig.tenant_id == user.tenant_id)
        .first()
    )
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    return agent


def _tenant_campaign(db: Session, user: User, campaign_id: str) -> Campaign:
    c = (
        db.query(Campaign)
        .filter(Campaign.campaign_id == campaign_id,
                Campaign.tenant_id == user.tenant_id)
        .first()
    )
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found")
    return c


@router.post("/api/browser-tests", response_model=BrowserTestOut,
             status_code=status.HTTP_201_CREATED)
def create_browser_test(body: BrowserTestIn, user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    agent = _tenant_agent(db, user, body.agent_id)

    campaign = None
    if body.campaign_id:
        campaign = _tenant_campaign(db, user, body.campaign_id)
        if campaign.agent_id != agent.agent_id:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "That campaign is bound to a different agent — pick the "
                "campaign's own agent, or no campaign.")

    # The test-lead snapshot: picked lead > hand-typed values > blanks.
    snap = {"agent_id": agent.agent_id, "name": "", "phone_number": "",
            "bank_name": "", "state": "", "agent_fields": {},
            "product": campaign.product if campaign else "other",
            "source_campaign_id": campaign.campaign_id if campaign else "",
            "source_campaign_name": campaign.name if campaign else "",
            "source_lead_id": ""}
    if body.lead_id:
        if campaign is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "lead_id requires campaign_id — a saved lead belongs to a campaign.")
        lead = (
            db.query(Lead)
            .filter(Lead.lead_id == body.lead_id,
                    Lead.campaign_id == campaign.campaign_id,
                    Lead.tenant_id == user.tenant_id)
            .first()
        )
        if lead is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")
        snap.update({
            "name": lead.name or "", "phone_number": lead.phone_number or "",
            "bank_name": lead.bank_name or "", "state": lead.state or "",
            "agent_fields": dict(lead.agent_fields or {}),
            "source_lead_id": lead.lead_id,
        })
    elif body.lead is not None:
        typed = body.lead
        fields: dict = {}
        if typed.product_identifier:
            itype = (PRODUCTS.get(snap["product"]) or {}).get(
                "identifier_type", "account_number")
            fields = {"product_identifier": typed.product_identifier,
                      "product_identifier_type": itype}
        snap.update({
            "name": typed.name or "", "phone_number": typed.phone_number or "",
            "bank_name": typed.bank_name or "", "state": typed.state or "",
            "agent_fields": fields,
        })

    session = CallSession(
        tenant_id=user.tenant_id,
        campaign_id=None,           # not a campaign attempt — see module doc
        lead_id=None,               # the snapshot carries the test lead
        attempt_id=f"att_btest_{uuid.uuid4().hex[:16]}",
        provider_mode="self_hosted",  # the harness runs the self-hosted loop
        channel=CHANNEL,
        test_lead=snap,
        state="QUEUED",
        to_number=snap["phone_number"],
        from_number="",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return BrowserTestOut(
        session_id=session.session_id,
        channel=CHANNEL,
        state=session.state,
        agent_id=agent.agent_id,
        agent_name=agent.name,
        lead_name=snap["name"],
        to_number=snap["phone_number"],
        stream_path=f"/api/telephony/browser/stream/{session.session_id}",
    )


# ── The media stream (browser transport for the same engine) ─────────────

def _user_from_token(db: Session, token: str) -> User | None:
    if not token:
        return None
    try:
        payload = decode_token(token)
    except pyjwt.PyJWTError:
        return None
    user = db.get(User, payload.get("sub", ""))
    if user is None or not user.is_active or user.tenant_id != payload.get("tenant_id"):
        return None
    return user


@router.websocket("/api/telephony/browser/stream/{session_id}")
async def browser_stream(websocket: WebSocket, session_id: str):
    await websocket.accept()
    db = SessionLocal()
    pipeline: stt.SaarasSttSession | None = None
    engine: conversation.ConversationEngine | None = None
    live: dict | None = None

    send_lock = asyncio.Lock()

    async def _send_json(obj: dict) -> None:
        async with send_lock:
            await websocket.send_text(json.dumps(obj))

    # Outbound playback clock — the same one the Plivo handler keeps
    # (20 ms per 160-byte μ-law frame): frames are paced, not dumped,
    # so the engine's speaking window is the operator's hearing window
    # and barge-in has a real window to cut. An idle gap restarts it.
    playback = {"next_at": 0.0}

    async def _send_audio(frame: bytes) -> None:
        """One outbound frame to the browser: paced on the μ-law clock,
        then decoded to PCM16 @8 kHz (binary message) for Web Audio —
        browsers cannot play μ-law (app.audio_codec)."""
        now = time.monotonic()
        if playback["next_at"] < now:
            playback["next_at"] = now
        delay = playback["next_at"] - now
        if delay > 0:
            await asyncio.sleep(delay)
        playback["next_at"] += (len(frame) / 160.0) * 0.02
        pcm = audio_codec.mulaw_to_pcm16(frame)
        async with send_lock:
            await websocket.send_bytes(pcm)

    async def _clear_audio() -> None:
        """Barge-in flush, browser flavour: the page drops its queued
        Web Audio buffers and stops the sounding source. Stronger than
        Plivo's clearAudio (which is best-effort against an unknowable
        provider buffer) — the buffer here lives in the page, so the
        cut is exact for everything not yet played."""
        await _send_json({"event": "interrupt"})

    # The engine's end-call action ends the stream by setting this
    # event; the handler races its receive pump against it and returns
    # (the A6–A9 close pattern — closing from a second task is
    # unreliable under Starlette).
    engine_end = asyncio.Event()

    async def _close_stream() -> None:
        engine_end.set()

    try:
        session = db.get(CallSession, session_id)
        if session is None or (session.channel or "") != CHANNEL:
            await websocket.close(code=4404)
            return
        user = _user_from_token(db, websocket.query_params.get("token", ""))
        if user is None:
            await websocket.close(code=4401)
            return
        if user.tenant_id != session.tenant_id:
            await websocket.close(code=4404)  # tenant isolation = not found
            return
        if session.state != "QUEUED":
            await _send_json({"event": "error", "reason": "session_not_startable",
                              "message": f"This test session is {session.state} — "
                                         "create a new browser test to run it again."})
            await websocket.close(code=4409)
            return

        snap = session.test_lead or {}
        agent = db.get(AgentConfig, snap.get("agent_id", ""))
        if agent is None or agent.tenant_id != session.tenant_id:
            await websocket.close(code=4404)
            return

        configured, api_key = stt.stt_config(session.tenant_id, db)
        if not configured:
            # No Sarvam → no STT / LLM / TTS legs → nothing to test
            # with. Report the failure as a result, honestly.
            outcome = {
                "final_state": "FAILED",
                "end_reason": "not_configured",
                "disposition": "",
                "confidence": 0.0,
                "summary": ("[BROWSER TEST] The test could not start: Sarvam "
                            "is not configured for this tenant, so the "
                            "conversation legs (STT / LLM / TTS) do not "
                            "exist. Configure Sarvam under Settings → "
                            "Providers and run a new test."),
                "captured_fields": {},
                "duration_s": 0,
                "recording_url": "",
                "transcript": [],
                "cost_breakdown": transports._zero_cost("self_hosted", "INR"),
                "review_required": False,
            }
            session.started_at = datetime.utcnow()
            orchestrator.finalize_attempt(db, session, outcome)
            await _send_json({"event": "error", "reason": "not_configured",
                              "message": outcome["summary"]})
            await websocket.close()
            return

        # The engine's call context: the real campaign when one was
        # picked (read-only — only its product feeds the prompt
        # variables), else a transient stand-in; the lead is a
        # transient object built from the snapshot. Neither is ever
        # written: the engine snapshots plain values at construction.
        campaign_ctx = None
        if snap.get("source_campaign_id"):
            c = db.get(Campaign, snap["source_campaign_id"])
            if c is not None and c.tenant_id == session.tenant_id:
                campaign_ctx = c
        campaign_obj = campaign_ctx or Campaign(product=snap.get("product") or "other")
        lead_obj = Lead(name=snap.get("name", ""),
                        phone_number=snap.get("phone_number", ""),
                        bank_name=snap.get("bank_name", ""),
                        state=snap.get("state", ""),
                        agent_fields=dict(snap.get("agent_fields") or {}))

        language = stt.saaras_language(
            transports.resolve_language(agent, lead_obj))
        now = datetime.utcnow()
        session.state = "IN_CALL"
        session.provider_call_id = f"browser_{session_id}"
        session.started_at = now
        session.answered_at = now
        session.stt_language = language
        session.stt_status = "connecting"
        db.commit()
        live = stt.new_live_view(session_id, language, "connecting")
        pipeline = stt.SaarasSttSession(session_id, api_key, language, live)
        engine = conversation.ConversationEngine(
            session_id=session_id, agent=agent, campaign=campaign_obj,
            lead=lead_obj, language=language, api_key=api_key,
            send_audio=_send_audio, close_stream=_close_stream,
            clear_audio=_clear_audio)
        pipeline.on_final = engine.enqueue_final
        pipeline.on_partial = engine.note_speech_activity

        # ── live transcript watcher ────────────────────────────────
        # Mirrors the engine's turns (agent + customer, in order) and
        # the STT live partial into events for the page. Display only —
        # the durable transcript is the session record, as always.
        watch = {"turns_sent": 0, "partial_sent": "", "stop": False}

        async def _flush_transcript(final: bool = False) -> None:
            turns = list(engine.turns) if engine is not None else []
            while watch["turns_sent"] < len(turns):
                turn = turns[watch["turns_sent"]]
                watch["turns_sent"] += 1
                await _send_json({"event": "transcript", "turn": turn})
            partial = (live or {}).get("partial", "") or ""
            if partial != watch["partial_sent"]:
                watch["partial_sent"] = partial
                await _send_json({"event": "partial", "text": partial})

        async def _watch() -> None:
            while not watch["stop"]:
                try:
                    await _flush_transcript()
                except Exception:
                    return  # the socket is gone — the pump will end too
                await asyncio.sleep(0.1)

        watcher = asyncio.ensure_future(_watch())

        async def _pump() -> None:
            """Inbound: binary messages are PCM16 @16 kHz microphone
            audio — converted chunk by chunk into the μ-law frames the
            STT session eats. A text {"type": "end"} is the operator's
            End button. Ends on disconnect or when cancelled."""
            pcm_buf = bytearray()
            try:
                while True:
                    msg = await websocket.receive()
                    if msg.get("type") == "websocket.disconnect":
                        break
                    data = msg.get("bytes")
                    if data:
                        pcm_buf += data
                        while len(pcm_buf) >= _PCM_CHUNK:
                            chunk = bytes(pcm_buf[:_PCM_CHUNK])
                            del pcm_buf[:_PCM_CHUNK]
                            if pipeline is not None:
                                await pipeline.feed(
                                    audio_codec.pcm16_16k_to_mulaw8k(chunk))
                        continue
                    text = msg.get("text")
                    if text:
                        try:
                            ctrl = json.loads(text)
                        except Exception:
                            continue
                        if ctrl.get("type") == "end":
                            break
            except Exception:
                pass  # disconnect / transport error: the test is over

        await pipeline.start()
        await _send_json({"event": "ready", "session_id": session_id,
                          "agent_name": agent.name, "language": language,
                          "lead_name": snap.get("name", "")})
        await engine.start()

        pump = asyncio.ensure_future(_pump())
        _done, _pending = await asyncio.wait(
            [pump, asyncio.ensure_future(engine_end.wait())],
            return_when=asyncio.FIRST_COMPLETED)
        for _t in _pending:
            _t.cancel()
        if pump in _done and not pump.cancelled():
            pump.exception()  # retrieve (the pump swallows its own errors)

        if pipeline is not None:
            await pipeline.stop()
        if engine is not None:
            await engine.finish()
        watch["stop"] = True
        try:
            await asyncio.wait_for(watcher, timeout=2)
        except BaseException:
            watcher.cancel()
        try:
            await _flush_transcript(final=True)
        except Exception:
            pass

        s = db.get(CallSession, session_id)
        if s is not None:
            anchor = s.answered_at or s.started_at
            duration = int((datetime.utcnow() - anchor).total_seconds()) if anchor else 0
            status_now = (live or {}).get("status", "") or s.stt_status or ""
            detail_now = (live or {}).get("detail", "") or ""
            if status_now == "connecting":
                status_now = "failed"
                detail_now = "STT session did not open before the stream ended"
            if pipeline is not None:
                s.stt_status = status_now
                s.stt_detail = detail_now[:500]
            legs = engine.legs()
            s.llm_status = legs["llm_status"]
            s.llm_detail = legs["llm_detail"][:500]
            s.tts_status = legs["tts_status"]
            s.tts_detail = legs["tts_detail"][:500]
            db.commit()
            if engine.llm_status != "failed" \
                    and any(t.get("speaker") == "agent" for t in engine.turns):
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
            else:
                # Same honest degradation as the Plivo path: the STT
                # record stands, no classification was possible, the
                # result is flagged for review.
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
            # Mark the outcome as what it is: a browser test that ran
            # the self-hosted legs — never a Plivo phone call.
            summary = outcome.get("summary") or ""
            summary = summary.replace(
                "[SELF-HOSTED] Plivo call", "[BROWSER TEST] Browser call", 1)
            if summary.startswith("[SELF-HOSTED] "):
                summary = "[BROWSER TEST] " + summary[len("[SELF-HOSTED] "):]
            elif not summary.startswith("[BROWSER TEST]"):
                summary = "[BROWSER TEST] " + summary
            outcome["summary"] = summary.strip()
            # A12: the engine's cost inputs ride the outcome into
            # finalize (see routers/telephony.py) — the TTS leg is
            # priced on the synthesized audio actually pumped; the
            # browser channel's telephony leg is labelled, never costed.
            # The classifier call's token usage joins the LLM leg, as
            # on the phone path.
            outcome["cost_metrics"] = engine.metrics()
            if engine.llm_status != "failed" \
                    and any(t.get("speaker") == "agent"
                            for t in engine.turns):
                outcome["cost_metrics"]["llm_usage_classify"] = \
                    classify_usage
            orchestrator.finalize_attempt(db, s, outcome)
            result = (
                db.query(CallResult)
                .filter(CallResult.session_id == session_id)
                .first()
            )
            try:
                await _send_json({
                    "event": "ended",
                    "final_state": s.state,
                    "end_reason": s.end_reason or "",
                    "disposition": result.disposition if result else "",
                })
            except Exception:
                pass
        try:
            await websocket.close()
        except Exception:
            pass
    finally:
        stt.end_live_view(session_id)
        db.close()
