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
import json
import urllib.request

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .. import market_time as mt
from .. import providers as prov
from ..db import get_db
from ..deps import (
    case_scope,
    get_current_user,
    require_tenant_admin,
    team_caller_ids,
    ROLE_CALLER,
    ROLE_SUPER_ADMIN,
    ROLE_TEAM_LEADER,
)
from .. import stt as stt_module
from ..models import AgentConfig, CallResult, CallSession, Campaign, Lead, Tenant, User
from ..orchestrator import Dispatcher, campaign_prompt, disposition_buckets, receive_result
from ..schemas import (
    CallListOut,
    CallOut,
    CampaignResultsOut,
    DashboardOut,
    DispatchOut,
    DispositionPatchIn,
    ResultIn,
    ResultOut,
)

router = APIRouter(tags=["calls"])


def _campaign_scoped(db: Session, user: User, campaign_id: str) -> Campaign:
    """Tenant-scoped campaign + C5 visibility (same rule as
    campaigns._get_scoped): Super Admin sees no tenant customer data;
    a Caller sees only campaigns holding their own cases; Tenant
    Admin and Team Leader see any campaign of the tenant."""
    c = (
        db.query(Campaign)
        .filter(Campaign.campaign_id == campaign_id,
                Campaign.tenant_id == user.tenant_id)
        .first()
    )
    if c is None or user.role == ROLE_SUPER_ADMIN:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found")
    if user.role == ROLE_CALLER:
        own = (
            db.query(Lead)
            .filter(Lead.campaign_id == c.campaign_id,
                    Lead.assigned_caller_id == user.user_id)
            .first()
        )
        if own is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found")
    return c


def _scope_sessions(user: User, db: Session, query):
    """Apply C5 case scoping to a CallSession query. Tenant Admin:
    unchanged. Team Leader: sessions on their team's cases plus
    sessions on unallocated cases (the pool they allocate from).
    Caller: sessions on their own cases only. Sessions without a
    lead (browser tests) are not cases and drop out of scoped views.
    Super Admin never reaches here (403 at the endpoint)."""
    scope = case_scope(user, db)
    if scope == "ALL":
        return query
    query = query.join(Lead, CallSession.lead_id == Lead.lead_id)
    if user.role == ROLE_TEAM_LEADER:
        return query.filter((Lead.assigned_caller_id.in_(scope)) |
                            (Lead.assigned_caller_id.is_(None)))
    return query.filter(Lead.assigned_caller_id == user.user_id)


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
    # Twilio leg (all-in cost): cached into cost_breakdown on first
    # view; failures never break the read (see twilio_cost).
    try:
        from .. import twilio_cost as _tw
        _tw.ensure_twilio_cost(db, s, result, cost)
    except Exception:
        pass
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


def _cost_summary(pairs) -> list:
    """Aggregate stored cost_breakdowns into per-currency totals (A12).

    `pairs` yields (CallResult, final_state). Currencies are NEVER
    mixed: each currency gets its own entry {currency, total, calls,
    connected_calls, cost_per_connected}. Stub results are excluded —
    their zeros are labelled simulated, and simulated money must not
    dilute real cost-per-connected figures.
    """
    by_currency: dict[str, dict] = {}
    for result, final_state in pairs:
        cb = result.cost_breakdown or {}
        if cb.get("mode") == "stub":
            continue
        currency = cb.get("currency") or ""
        if not currency:
            continue
        entry = by_currency.setdefault(currency, {
            "currency": currency, "total": 0.0, "calls": 0,
            "connected_calls": 0, "cost_per_connected": 0.0})
        entry["total"] += float(cb.get("total") or 0)
        entry["calls"] += 1
        if final_state == "COMPLETED":
            entry["connected_calls"] += 1
    out = []
    for currency in sorted(by_currency):
        entry = by_currency[currency]
        entry["total"] = round(entry["total"], 4)
        if entry["connected_calls"]:
            entry["cost_per_connected"] = round(
                entry["total"] / entry["connected_calls"], 4)
        out.append(entry)
    return out


@router.post("/api/campaigns/{campaign_id}/dispatch", response_model=DispatchOut)
def run_dispatch(campaign_id: str, user: User = Depends(require_tenant_admin),
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
    q = _scope_sessions(user, db, db.query(CallSession)
                        .filter(CallSession.campaign_id == c.campaign_id))
    total = q.count()
    states = _counts(db, q.with_entities(CallSession.state))
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
    # C2: the summary's bucket set follows the campaign's PINNED prompt,
    # not the agent's live prompt — a pinned campaign's dispositions
    # must appear in its own summary even after a later adoption.
    buckets = disposition_buckets(campaign_prompt(c, agent) if agent else "")

    # C5: the summary aggregates only the viewer's cases (Tenant
    # Admin: the whole campaign; Team Leader: team's + unallocated;
    # Caller: own). The session set drives everything below — results
    # join to these sessions, never to the whole campaign.
    sessions = _scope_sessions(user, db, db.query(CallSession)
                               .filter(CallSession.campaign_id == c.campaign_id)).all()
    session_ids = [s.session_id for s in sessions]
    results = (db.query(CallResult)
               .filter(CallResult.session_id.in_(session_ids)).all()) if session_ids else []
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

    lead_query = db.query(Lead).filter(Lead.campaign_id == c.campaign_id)
    scope = case_scope(user, db)
    if scope != "ALL":
        if user.role == ROLE_CALLER:
            lead_query = lead_query.filter(Lead.assigned_caller_id == user.user_id)
        else:  # Team Leader: team's + unallocated
            lead_query = lead_query.filter((Lead.assigned_caller_id.in_(scope)) |
                                           (Lead.assigned_caller_id.is_(None)))
    lead_rows = [(l.status,) for l in lead_query.all()]
    leads_by_state: dict[str, int] = {}
    for (st,) in lead_rows:
        leads_by_state[st] = leads_by_state.get(st, 0) + 1
    now = datetime.utcnow()
    awaiting = (lead_query
                .filter(Lead.status == "pending",
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
        costs=_cost_summary(
            (r, session_state.get(r.session_id)) for r in results),
    )


@router.get("/api/dashboard", response_model=DashboardOut)
def dashboard(user: User = Depends(get_current_user),
              db: Session = Depends(get_db)):
    """Tenant home (A10): the day at a glance, from the same §8.2
    records every mode produces. "Today" is the tenant's market-local
    day; dispositions are tenant-wide totals across campaigns.

    C5: Super Admin gets no dashboard (no tenant customer data).
    Campaign counts stay tenant-wide for Team Leaders (supervision
    metadata) but shrink to own campaigns for Callers; every call /
    result / review number is scoped to the viewer's cases."""
    if user.role == ROLE_SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Super Admin sees no tenant customer data")
    tenant = db.get(Tenant, user.tenant_id)
    zone = mt.tenant_zone(tenant)
    today_local = mt.to_local(datetime.utcnow(), zone).date()
    day_start_utc = mt.local_day_start_utc(today_local, zone)

    campaign_query = db.query(Campaign).filter(Campaign.tenant_id == user.tenant_id)
    if user.role == ROLE_CALLER:
        own_campaigns = (db.query(Lead.campaign_id)
                         .filter(Lead.assigned_caller_id == user.user_id))
        campaign_query = campaign_query.filter(Campaign.campaign_id.in_(own_campaigns))
    campaigns = campaign_query.all()
    by_status: dict[str, int] = {}
    for c in campaigns:
        by_status[c.status] = by_status.get(c.status, 0) + 1

    scoped_sessions = _scope_sessions(
        user, db, db.query(CallSession).filter(CallSession.tenant_id == user.tenant_id))
    scoped_ids = [s.session_id for s in scoped_sessions.all()]
    calls_total = len(scoped_ids)
    calls_today = (db.query(CallSession)
                   .filter(CallSession.session_id.in_(scoped_ids),
                           CallSession.started_at >= day_start_utc).count()) if scoped_ids else 0

    results = (db.query(CallResult)
               .filter(CallResult.session_id.in_(scoped_ids)).all()) if scoped_ids else []
    dispositions: dict[str, int] = {}
    review = 0
    for r in results:
        key = r.disposition or "(none)"
        dispositions[key] = dispositions.get(key, 0) + 1
        if r.review_required:
            review += 1
    result_states = {
        s.session_id: s.state
        for s in db.query(CallSession)
        .filter(CallSession.session_id.in_(scoped_ids)).all()
    } if scoped_ids else {}

    return DashboardOut(
        timezone=mt.tenant_timezone_name(tenant),
        active_campaigns=by_status.get("active", 0),
        campaigns_by_status=by_status,
        calls_today=calls_today,
        calls_total=calls_total,
        results_total=len(results),
        dispositions=dispositions,
        review_queue_count=review,
        costs=_cost_summary(
            (r, result_states.get(r.session_id)) for r in results),
    )


@router.get("/api/calls", response_model=CallListOut)
def list_calls(campaign_id: str = "", disposition: str = "",
               review_required: bool | None = None,
               limit: int = 500, offset: int = 0,
               user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    if user.role == ROLE_SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Super Admin sees no tenant customer data")
    q = db.query(CallSession).filter(CallSession.tenant_id == user.tenant_id)
    if campaign_id:
        # Lists return empty, not 404: the tenant filter above already
        # excludes a foreign campaign's sessions (cross-tenant fuzz
        # asserts total 0 here). Single-item access 404s elsewhere.
        q = q.filter(CallSession.campaign_id == campaign_id)
    q = _scope_sessions(user, db, q)
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


@router.get("/api/calls/{session_id}/recording")
def call_recording(session_id: str, download: int = 0,
                   user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Stream one call's recording to the browser.

    The stored recording_url is the provider's PRIVATE object URL — a
    browser that opens it directly gets the provider's Authorization
    error (live-seen: R2 InvalidArgument, an empty 0:00 player, a
    failed download). So the browser never touches that URL: this
    endpoint fetches a FRESH short-lived signed URL from the provider
    with the tenant's own API key and streams the audio back
    same-origin, under the operator's JWT. Signed URLs expire, so they
    are never stored or handed to the browser — each playback or
    download re-signs.

    S3 archival (012) changes the preference, not the contract: when
    the session carries an archived copy in the tenant's own private
    bucket, that copy streams first (durable, no provider round-trip);
    any S3 failure falls through to the provider path below, so an
    archived row never plays worse than an unarchived one."""
    s = (db.query(CallSession)
         .filter(CallSession.session_id == session_id,
                 CallSession.tenant_id == user.tenant_id).first())
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Call not found")
    # C5: a recording is customer data — Super Admin never streams
    # one, and a Team Leader / Caller streams only their cases'
    # recordings (a foreign call is indistinguishable from missing).
    if user.role == ROLE_SUPER_ADMIN:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Call not found")
    if user.role in (ROLE_TEAM_LEADER, ROLE_CALLER):
        lead = db.get(Lead, s.lead_id) if s.lead_id else None
        assigned = lead.assigned_caller_id if lead else None
        if user.role == ROLE_CALLER and assigned != user.user_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Call not found")
        if user.role == ROLE_TEAM_LEADER:
            if assigned is not None and assigned not in team_caller_ids(user, db):
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Call not found")
    if not s.provider_call_id or s.provider_call_id.startswith("stub_"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No recording for this call")
    camp = db.get(Campaign, s.campaign_id) if s.campaign_id else None
    if camp is not None and camp.provider_mode != "managed":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No recording for this call")
    disp = ("attachment" if download else "inline") + f'; filename="{session_id}.wav"'
    # S3 first (012): the archived copy, when present, is the durable
    # one. The key is tenant-scoped at archive time; re-check the
    # prefix here so a row can never stream another tenant's object.
    # get_object is eager (headers + body handle), so a missing key or
    # missing credentials fails HERE and falls through to Vapi —
    # instead of breaking mid-stream after the response started.
    if (s.recording_s3_key and s.recording_s3_bucket
            and s.recording_s3_key.startswith(f"recordings/{s.tenant_id}/")):
        try:
            from .. import s3_recordings

            _client = s3_recordings._s3_client()
            _body = _client.get_object(
                Bucket=s.recording_s3_bucket,
                Key=s.recording_s3_key)["Body"]

            def _stream_s3():
                try:
                    while True:
                        chunk = _body.read(64 * 1024)
                        if not chunk:
                            break
                        yield chunk
                finally:
                    _body.close()

            return StreamingResponse(_stream_s3(), media_type="audio/wav",
                                     headers={"Content-Disposition": disp})
        except Exception:
            pass  # fall through to the provider path below
    try:
        cfg = prov.get_provider_config(s.tenant_id, "managed", db=db)
        api_key = cfg["providers"]["vapi"]["fields"].get("api_key", "")
    except Exception:
        api_key = ""
    if not api_key:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            "Voice provider not configured")
    try:
        req = urllib.request.Request(
            f"https://api.vapi.ai/call/{s.provider_call_id}",
            headers={"Authorization": f"Bearer {api_key}",
                     "User-Agent": "VoiceAgentPlatform/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            call = json.load(r)
    except Exception:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            "Could not reach the voice provider")
    art = call.get("artifact") or {}
    url = art.get("presignedMonoUrl") or art.get("presignedStereoUrl") or ""
    if not url:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            "No playable recording for this call")

    def _stream():
        req2 = urllib.request.Request(url, headers={"User-Agent": "VoiceAgentPlatform/1.0"})
        with urllib.request.urlopen(req2, timeout=60) as resp:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(_stream(), media_type="audio/wav",
                             headers={"Content-Disposition": disp})


@router.patch("/api/calls/{session_id}/disposition", response_model=CallOut)
def update_disposition(session_id: str, body: DispositionPatchIn,
                       user: User = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    """Manually correct a call's disposition (C5). Who may correct
    what: Tenant Admin any case in the tenant; Team Leader their
    team's cases; Caller their own cases only; Super Admin never
    (no tenant customer data). The correction is audited (who +
    when on the result row), and the new disposition must be one of
    the campaign agent's own buckets — an unknown bucket is NEVER
    stored as a disposition (the same rule the classifier obeys).
    """
    if user.role == ROLE_SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Super Admin sees no tenant customer data")
    s = (db.query(CallSession)
         .filter(CallSession.session_id == session_id,
                 CallSession.tenant_id == user.tenant_id).first())
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Call not found")
    lead = db.get(Lead, s.lead_id) if s.lead_id else None
    assigned = lead.assigned_caller_id if lead else None
    if user.role == ROLE_CALLER and assigned != user.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Call not found")
    if user.role == ROLE_TEAM_LEADER and assigned is not None \
            and assigned not in team_caller_ids(user, db):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Call not found")
    r = (db.query(CallResult)
         .filter(CallResult.session_id == s.session_id).first())
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "No result to correct for this call")
    campaign = db.get(Campaign, s.campaign_id) if s.campaign_id else None
    agent = db.get(AgentConfig, campaign.agent_id) if campaign else None
    buckets = disposition_buckets(campaign_prompt(campaign, agent) if agent else "")
    if buckets and body.disposition not in buckets:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"disposition must be one of the agent's buckets: {sorted(buckets)}")
    r.disposition = body.disposition
    r.disposition_updated_by = user.user_id
    r.disposition_updated_at = datetime.utcnow()
    db.commit()
    db.refresh(r)
    return _call_out(db, s)


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
