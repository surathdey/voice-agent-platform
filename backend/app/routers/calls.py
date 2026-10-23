"""Dispatch & Calls API (A5) — design §8.1 / §8.2 / §11 row A5.

- POST /api/campaigns/{id}/dispatch — run the dispatcher for an ACTIVE
  campaign (draft / paused / completed campaigns do not dispatch). Mode
  routing and the stub transport live in app.orchestrator.
- GET  /api/campaigns/{id}/calls — this campaign's sessions + results,
  with attempt counts by state for the campaign page.
- GET  /api/calls — the tenant's calls across campaigns (Calls page),
  optional ?campaign_id= filter.
- POST /api/results — the idempotent §8.2 result receiver: the callback
  the self-hosted orchestrator (and the normalised managed webhook) will
  deliver to in A6+. A duplicate delivery returns the stored result —
  exactly one CallResult per session, always.

Every query is tenant-scoped: another tenant's campaign/session is a 404.
All call views are assembled into the §8.2 contract shape (CallOut).
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from .. import stt as stt_module
from ..models import CallResult, CallSession, Campaign, Lead, User
from ..orchestrator import Dispatcher, receive_result
from ..schemas import CallListOut, CallOut, DispatchOut, ResultIn, ResultOut

router = APIRouter(tags=["calls"])


def _campaign_scoped(db: Session, user: User, campaign_id: str) -> Campaign:
    c = (
        db.query(Campaign)
        .filter(Campaign.campaign_id == campaign_id,
                Campaign.tenant_id == user.tenant_id)
        .first()
    )
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found")
    return c


def _transcript_view(s: CallSession, result: CallResult | None) -> list:
    """The transcript for one call view: the persisted turns (finals are
    written to the session as they arrive, A7), plus — for a call still
    IN_CALL — the live partial segment from the in-memory STT view,
    marked "partial": true. The overlay is display-only; it is never
    written back."""
    turns = list(s.transcript or [])
    if result is None and s.state == "IN_CALL":
        partial = stt_module.live_partial(s.session_id)
        if partial:
            turns.append({"speaker": "customer", "text": partial,
                          "offset_s": 0.0, "partial": True})
    return turns


def _call_out(db: Session, s: CallSession) -> CallOut:
    result = (
        db.query(CallResult)
        .filter(CallResult.session_id == s.session_id)
        .first()
    )
    campaign = db.get(Campaign, s.campaign_id)
    lead = db.get(Lead, s.lead_id)
    cost = (result.cost_breakdown or {}) if result else {}
    return CallOut(
        session_id=s.session_id,
        attempt_id=s.attempt_id,
        campaign_id=s.campaign_id,
        campaign_name=campaign.name if campaign else "",
        lead_id=s.lead_id,
        lead_name=lead.name if lead else "",
        to_number=s.to_number or (lead.phone_number if lead else ""),
        provider_mode=s.provider_mode,
        # The transport that actually ran the call: stamped into the
        # result's cost_breakdown at finalize. Before a result exists,
        # a session carrying a real provider call id belongs to its
        # mode's real transport; a session without one (or with the
        # stub's id) has only ever been touched by the stub.
        transport=cost.get("mode") or (
            "stub" if (not s.provider_call_id or s.provider_call_id.startswith("stub_"))
            else s.provider_mode
        ),
        state=s.state,
        final_state=s.state if result else None,
        end_reason=s.end_reason or "",
        disposition=result.disposition if result else "",
        confidence=result.confidence if result else 0.0,
        summary=result.summary if result else "",
        captured_fields=(result.captured_fields or {}) if result else {},
        duration_s=result.duration_s if result else 0,
        recording_url=s.recording_url or "",
        transcript=_transcript_view(s, result),
        stt_status=s.stt_status or "",
        stt_language=s.stt_language or "",
        stt_detail=s.stt_detail or "",
        llm_status=s.llm_status or "",
        llm_detail=s.llm_detail or "",
        tts_status=s.tts_status or "",
        tts_detail=s.tts_detail or "",
        review_required=bool(result.review_required) if result else False,
        cost_breakdown=cost,
        started_at=s.started_at.isoformat() if s.started_at else None,
        answered_at=s.answered_at.isoformat() if s.answered_at else None,
        ended_at=s.ended_at.isoformat() if s.ended_at else None,
    )


def _counts(db: Session, query) -> dict:
    counts: dict[str, int] = {}
    for (state,) in query:
        counts[state] = counts.get(state, 0) + 1
    return counts


@router.post("/api/campaigns/{campaign_id}/dispatch", response_model=DispatchOut)
def run_dispatch(campaign_id: str, user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    c = _campaign_scoped(db, user, campaign_id)
    if c.status != "active":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Dispatch runs only on an active campaign — this one is {c.status}. "
            "Launch it first (draft → active).",
        )
    report = Dispatcher().dispatch(db, c)
    states = _counts(db, db.query(CallSession.state)
                     .filter(CallSession.campaign_id == c.campaign_id))
    return DispatchOut(**report, counts_by_state=states)


@router.get("/api/campaigns/{campaign_id}/calls", response_model=CallListOut)
def campaign_calls(campaign_id: str, limit: int = 500, offset: int = 0,
                   user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    c = _campaign_scoped(db, user, campaign_id)
    q = db.query(CallSession).filter(CallSession.campaign_id == c.campaign_id)
    total = q.count()
    states = _counts(db, db.query(CallSession.state)
                     .filter(CallSession.campaign_id == c.campaign_id))
    rows = (q.order_by(CallSession.started_at, CallSession.session_id)
             .offset(offset).limit(limit).all())
    return CallListOut(total=total, counts_by_state=states,
                       calls=[_call_out(db, s) for s in rows])


@router.get("/api/calls", response_model=CallListOut)
def list_calls(campaign_id: str = "", limit: int = 500, offset: int = 0,
               user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    q = db.query(CallSession).filter(CallSession.tenant_id == user.tenant_id)
    if campaign_id:
        q = q.filter(CallSession.campaign_id == campaign_id)
    total = q.count()
    tq = db.query(CallSession.state).filter(CallSession.tenant_id == user.tenant_id)
    if campaign_id:
        tq = tq.filter(CallSession.campaign_id == campaign_id)
    states = _counts(db, tq)
    rows = (q.order_by(CallSession.started_at, CallSession.session_id)
             .offset(offset).limit(limit).all())
    return CallListOut(total=total, counts_by_state=states,
                       calls=[_call_out(db, s) for s in rows])


@router.post("/api/results", response_model=ResultOut)
def post_result(body: ResultIn, user: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    """Idempotent §8.2 result receiver — keyed on session_id (attempt_id
    accepted as a fallback key). Duplicate deliveries are a no-op that
    returns the stored result with created=False."""
    if not body.session_id and not body.attempt_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "session_id (or attempt_id) is required")
    got = receive_result(db, user.tenant_id, body.model_dump())
    if got is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "No session of this tenant matches that session_id / attempt_id")
    session, _result, created = got
    return ResultOut(session_id=session.session_id,
                     attempt_id=session.attempt_id,
                     created=created,
                     call=_call_out(db, session))
