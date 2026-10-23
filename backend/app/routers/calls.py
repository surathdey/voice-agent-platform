"""Dispatch & Calls API (A5) — design §8.1 / §8.2 / §11 row A5.

- POST /api/campaigns/{id}/dispatch — run the dispatcher for an ACTIVE
  campaign (draft / paused / completed campaigns do not dispatch). Mode
  routing and the stub transport live in app.orchestrator.
- GET  /api/campaigns/{id}/calls — this campaign's sessions + results,
  with attempt counts by state for the campaign page.
- GET  /api/campaigns/{id}/results — the campaign's RESULTS SUMMARY
  (A10): counts per disposition (buckets parsed from the campaign
  agent's own single prompt), attempts made, connect rate, leads by
  state — the numbers the campaign page's Results card renders.
- GET  /api/calls — the tenant's calls across campaigns (Calls page),
  optional ?campaign_id= / ?disposition= / ?review_required= filters.
- GET  /api/dashboard — the tenant home (A10): active campaigns, calls
  today (market-local), per-disposition totals across campaigns, and
  the human-review queue count.
- POST /api/results — the idempotent §8.2 result receiver: the callback
  the self-hosted orchestrator (and the normalised managed webhook) will
  deliver to in A6+. A duplicate delivery returns the stored result —
  exactly one CallResult per session, always.

Every query is tenant-scoped: another tenant's campaign/session is a 404.
All call views are assembled into the §8.2 contract shape (CallOut).
The results/dashboard read models are assembled from the SAME §8.2
records both modes land through finalize_attempt — managed and
self-hosted results are indistinguishable here by construction (A10
managed-mode parity).
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import market_time as mt
from ..db import get_db
from ..deps import get_current_user
from .. import stt as stt_module
from ..models import AgentConfig, CallResult, CallSession, Campaign, Lead, Tenant, User
from ..orchestrator import Dispatcher, disposition_buckets, receive_result
from ..schemas import (
    CallListOut,
    CallOut,
    CampaignResultsOut,
    DashboardOut,
    DispatchOut,
    ResultIn,
    ResultOut,
)

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
    campaign = db.get(Campaign, s.campaign_id) if s.campaign_id else None
    lead = db.get(Lead, s.lead_id) if s.lead_id else None
    # A11: a browser-test session references no campaign/lead rows — its
    # display name/number come from the test-lead snapshot instead.
    snapshot = s.test_lead or {}
    cost = (result.cost_breakdown or {}) if result else {}
    return CallOut(
        session_id=s.session_id,
        attempt_id=s.attempt_id,
        campaign_id=s.campaign_id,
        campaign_name=campaign.name if campaign else "",
        lead_id=s.lead_id,
        lead_name=(lead.name if lead else snapshot.get("name", "")),
        to_number=s.to_number or (lead.phone_number if lead else ""),
        provider_mode=s.provider_mode,
        channel=s.channel or "",
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


@router.get("/api/campaigns/{campaign_id}/results", response_model=CampaignResultsOut)
def campaign_results(campaign_id: str, user: User = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    """The campaign's results summary (A10). Counts per disposition use
    the bucket set parsed from the campaign agent's OWN single prompt —
    the same source the stub rotates through and the classifier is
    bound to — zero-filled, with any stored disposition outside the set
    appended so nothing is ever hidden."""
    c = _campaign_scoped(db, user, campaign_id)
    agent = db.get(AgentConfig, c.agent_id)
    buckets = disposition_buckets(agent.prompt if agent else "")

    sessions = (db.query(CallSession)
                .filter(CallSession.campaign_id == c.campaign_id).all())
    results = (db.query(CallResult)
               .join(CallSession, CallResult.session_id == CallSession.session_id)
               .filter(CallSession.campaign_id == c.campaign_id).all())
    session_state = {s.session_id: s.state for s in sessions}

    counts = {b: 0 for b in buckets}
    connected = 0
    review = 0
    total_duration = 0
    for r in results:
        counts[r.disposition or "(none)"] = counts.get(r.disposition or "(none)", 0) + 1
        if session_state.get(r.session_id) == "COMPLETED":
            connected += 1
        if r.review_required:
            review += 1
        total_duration += r.duration_s or 0

    lead_rows = (db.query(Lead.status)
                 .filter(Lead.campaign_id == c.campaign_id).all())
    leads_by_state: dict[str, int] = {}
    for (st,) in lead_rows:
        leads_by_state[st] = leads_by_state.get(st, 0) + 1
    now = datetime.utcnow()
    awaiting = (db.query(Lead)
                .filter(Lead.campaign_id == c.campaign_id,
                        Lead.status == "pending",
                        Lead.next_attempt_at.isnot(None),
                        Lead.next_attempt_at > now)
                .count())

    return CampaignResultsOut(
        campaign_id=c.campaign_id,
        agent_id=c.agent_id,
        buckets=buckets,
        counts_by_disposition=counts,
        attempts_made=len(sessions),
        results_count=len(results),
        connected=connected,
        connect_rate=(connected / len(results)) if results else 0.0,
        review_required=review,
        leads_total=len(lead_rows),
        leads_by_state=leads_by_state,
        awaiting_retry=awaiting,
        total_duration_s=total_duration,
    )


@router.get("/api/dashboard", response_model=DashboardOut)
def dashboard(user: User = Depends(get_current_user),
              db: Session = Depends(get_db)):
    """Tenant home (A10): the day at a glance, from the same §8.2
    records every mode produces. "Today" is the tenant's market-local
    day; dispositions are tenant-wide totals across campaigns."""
    tenant = db.get(Tenant, user.tenant_id)
    zone = mt.tenant_zone(tenant)
    today_local = mt.to_local(datetime.utcnow(), zone).date()
    day_start_utc = mt.local_day_start_utc(today_local, zone)

    campaigns = (db.query(Campaign)
                 .filter(Campaign.tenant_id == user.tenant_id).all())
    by_status: dict[str, int] = {}
    for c in campaigns:
        by_status[c.status] = by_status.get(c.status, 0) + 1

    calls_total = (db.query(CallSession)
                   .filter(CallSession.tenant_id == user.tenant_id).count())
    calls_today = (db.query(CallSession)
                   .filter(CallSession.tenant_id == user.tenant_id,
                           CallSession.started_at >= day_start_utc).count())

    results = (db.query(CallResult)
               .filter(CallResult.tenant_id == user.tenant_id).all())
    dispositions: dict[str, int] = {}
    review = 0
    for r in results:
        key = r.disposition or "(none)"
        dispositions[key] = dispositions.get(key, 0) + 1
        if r.review_required:
            review += 1

    return DashboardOut(
        timezone=mt.tenant_timezone_name(tenant),
        active_campaigns=by_status.get("active", 0),
        campaigns_by_status=by_status,
        calls_today=calls_today,
        calls_total=calls_total,
        results_total=len(results),
        dispositions=dispositions,
        review_queue_count=review,
    )


@router.get("/api/calls", response_model=CallListOut)
def list_calls(campaign_id: str = "", disposition: str = "",
               review_required: bool | None = None,
               limit: int = 500, offset: int = 0,
               user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    q = db.query(CallSession).filter(CallSession.tenant_id == user.tenant_id)
    if campaign_id:
        q = q.filter(CallSession.campaign_id == campaign_id)
    if disposition or review_required is not None:
        # Result-bearing filters: sessions without a §8.2 result cannot
        # match a disposition / review filter by definition.
        q = q.join(CallResult, CallResult.session_id == CallSession.session_id)
        if disposition:
            q = q.filter(CallResult.disposition == disposition)
        if review_required is not None:
            q = q.filter(CallResult.review_required == review_required)
    total = q.count()
    states = _counts(db, q.with_entities(CallSession.state))
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
