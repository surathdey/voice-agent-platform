"""Orchestrator skeleton (A5) — dispatch service + stub transport + the
single finalize path every mode lands on.

Design anchors:
- §8.1 start-call contract: a call is created per ATTEMPT, keyed by an
  idempotency `attempt_id` from the application layer; a second dispatch
  for the same attempt never creates a second session (unique constraint
  on (tenant_id, attempt_id) + deterministic attempt ids, below).
- §8.2 CallResult contract: every mode produces exactly one result in the
  same shape. ALL transports — the stub today, Vapi (managed) and the
  self-hosted pipeline later — land through `finalize_attempt()`, the
  idempotent terminal write. A second finalize for the same session is a
  no-op and returns the existing result.
- Mode routing happens HERE, at the dispatch boundary: the campaign's
  provider_mode is resolved through A4's get_provider_config(). A real
  transport is used only when the global TELEPHONY_MODE equals the
  campaign's mode AND the tenant's provider for that mode is ready AND a
  transport for the mode is registered. With TELEPHONY_MODE=stub (the
  default) every dispatch runs simulated and says so: results are
  stamped cost_breakdown.mode = "stub" and sessions carry a stub
  provider_call_id. Nothing is ever disguised as a real call.
- A6 registers the real transports (app.transports): Vapi (managed) and
  Plivo (self_hosted). A real transport's run() PLACES the call and
  returns None — the attempt stays in flight (DIALING) and its §8.2
  result lands later through finalize_attempt, delivered by the Vapi
  end-of-call webhook or the Plivo stream / status callbacks
  (app.routers.telephony). The stub still completes synchronously
  through the same finalize path.

The dispatcher admits attempts per the campaign's schedule — calling
window, calling days and start date, ALL evaluated in the tenant's
MARKET-LOCAL time (app.market_time; A5 evaluated the window in UTC —
fixed in A10) — and the daily cap (sessions started on the market-local
day count against it). It processes admitted attempts through a worker
loop in batches of the tenant's concurrency_limit. The worker runs
synchronously inside the dispatch call, behind this service boundary,
so later chunks can move it to a background loop without changing the
API.

Retries (A10): an attempt that ends NO_ANSWER, BUSY or FAILED is not
the lead's end. finalize_attempt re-queues the lead (status back to
"pending") with next_attempt_at = the attempt's end + the campaign's
retry gap (the different-time-of-day shift applied in market-local time
when configured); dispatch admits it again only once that time has
passed, as a NEW attempt — a new session with its own deterministic
attempt_id (…_2, …_3) and its own single result. Exactly-one-result
stays per ATTEMPT and absolute: finalize_attempt remains the single
terminal writer and a duplicate delivery is still a no-op. A lead
whose attempts run out is "exhausted"; a lead that reached a terminal
conversation outcome (COMPLETED, VOICEMAIL) is done ("attempted").
"""
import hashlib
import re
from datetime import date, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import market_time as mt
from . import providers as prov
from .config import settings
from .models import (
    AgentConfig,
    CallResult,
    CallSession,
    Campaign,
    Lead,
    Tenant,
)

# Session states (design §6): QUEUED → DIALING → IN_CALL → terminal.
# Terminal states are the §8.2 final_state values.
TERMINAL_STATES = {"COMPLETED", "NO_ANSWER", "BUSY", "FAILED", "VOICEMAIL"}

# Attempt outcomes that earn the lead another attempt (A10): the call
# never became a conversation. COMPLETED and VOICEMAIL are final for
# the lead — the campaign's max-attempts cap still bounds the retries.
RETRYABLE_FINAL_STATES = {"NO_ANSWER", "BUSY", "FAILED"}

# Used ONLY when an agent's prompt carries no parseable DISPOSITIONS
# section — the buckets normally come from the prompt itself (single
# source of truth, owner decision 2026-10-23).
FALLBACK_BUCKETS = ["COMPLETED", "NO_ANSWER", "CALLBACK_REQUESTED"]

_BUCKET_LINE = re.compile(r"^-\s+([A-Z][A-Z0-9_]{2,})\s*(?::|$)")
_SECTION_HEAD = re.compile(r"^[A-Z][A-Z /&-]{2,}$")


def disposition_buckets(prompt: str) -> list[str]:
    """Extract the disposition bucket set from the agent's single prompt.

    The buckets live in the prompt's DISPOSITIONS section as lines like
    ``- RPC_CONFIRMED: the person on the line confirmed …``. Order is
    preserved — the stub's round-robin walks them in prompt order.
    """
    buckets: list[str] = []
    in_section = False
    for raw in (prompt or "").splitlines():
        line = raw.strip()
        if line.upper().startswith("DISPOSITIONS"):
            in_section = True
            continue
        if in_section:
            m = _BUCKET_LINE.match(line)
            if m:
                if m.group(1) not in buckets:
                    buckets.append(m.group(1))
                continue
            if _SECTION_HEAD.match(line) and line.upper() not in ("DISPOSITIONS",):
                break  # next section (e.g. CLOSINGS) — buckets are done
    return buckets or list(FALLBACK_BUCKETS)


def _stub_duration_s(attempt_id: str) -> int:
    """Deterministic pseudo-duration (25–124 s) from the attempt id."""
    digest = int(hashlib.md5(attempt_id.encode()).hexdigest()[:8], 16)
    return 25 + digest % 100


def _stub_captured_fields(agent: AgentConfig, disposition: str, buckets: list[str]) -> dict:
    """Placeholder captured values, typed per the agent's field schema."""
    out = {}
    for f in agent.captured_fields or []:
        name, ftype = f.get("name"), f.get("type", "string")
        if not name:
            continue
        if ftype == "boolean":
            # The first bucket is the "confirmed/reached" outcome in the
            # prompt's own ordering (e.g. RPC_CONFIRMED for RPI).
            out[name] = disposition == buckets[0]
        elif ftype == "number":
            out[name] = 0
        elif ftype == "date":
            out[name] = None
        else:
            out[name] = "[STUB] simulated value"
    return out


class StubTransport:
    """Simulates the full call lifecycle — no provider is ever called.

    run() moves the session QUEUED → DIALING → IN_CALL → COMPLETED and
    returns an outcome dict in the §8.2 shape, ready for finalize_attempt.
    Dispositions round-robin deterministically across the agent prompt's
    bucket set, seeded by how many results the campaign already has, so
    repeated runs continue the rotation instead of restarting it.
    """

    name = "stub"

    def run(self, db: Session, session: CallSession, agent: AgentConfig,
            campaign: Campaign, seq: int, run_start: datetime) -> dict:
        buckets = disposition_buckets(agent.prompt)
        already = (
            db.query(CallResult)
            .join(CallSession, CallResult.session_id == CallSession.session_id)
            .filter(CallSession.campaign_id == campaign.campaign_id)
            .count()
        )
        disposition = buckets[already % len(buckets)]
        duration = _stub_duration_s(session.attempt_id)

        # Timestamps are offset by sequence so a run's sessions order
        # deterministically by started_at (their processing order).
        started = run_start + timedelta(milliseconds=2 * seq)
        session.provider_call_id = f"stub_{session.session_id}"
        session.state = "IN_CALL"  # DIALING → IN_CALL collapse in the stub
        session.started_at = started
        session.answered_at = started + timedelta(milliseconds=1)
        db.commit()

        return {
            "final_state": "COMPLETED",
            "end_reason": "agent_end_call",
            "disposition": disposition,
            "confidence": 0.9,
            "summary": (
                f"[STUB] Simulated call via the stub transport "
                f"({campaign.provider_mode} mode) — no provider was called. "
                f"Disposition: {disposition}."
            ),
            "captured_fields": _stub_captured_fields(agent, disposition, buckets),
            "duration_s": duration,
            "recording_url": "",
            "transcript": [
                {"speaker": "agent",
                 "text": "[STUB] Simulated opening line — the agent's prompt "
                         "would be spoken here by the real transport.",
                 "offset_s": 0.0},
                {"speaker": "customer",
                 "text": "[STUB] Simulated customer reply (stub transport — "
                         "no real call took place).",
                 "offset_s": 2.5},
            ],
            "cost_breakdown": {
                "telephony": 0, "stt": 0, "llm": 0, "tts": 0,
                "total": 0, "currency": "INR",
                "mode": "stub", "simulated": True,
            },
        }


# Transports registered per mode. The stub is the fallback, not a
# registry entry: resolve_transport() returns it whenever a real
# transport may not run. A6 registers the real Vapi (managed) and Plivo
# (self_hosted) transports from app.transports.
TRANSPORTS: dict[str, object] = {}


def _register_real_transports() -> None:
    from .transports import PlivoTransport, VapiTransport

    TRANSPORTS.setdefault("managed", VapiTransport())
    TRANSPORTS.setdefault("self_hosted", PlivoTransport())


_register_real_transports()


def resolve_transport(db: Session, tenant_id: str, mode: str):
    """(transport, transport_name, provider_ready) for one dispatch.

    Real transport only when ALL hold: TELEPHONY_MODE equals the
    campaign's mode (stub mode, or a mode mismatch, never silently runs
    a real provider), A4 says the tenant's provider for this mode is
    ready, and a transport for the mode is registered. Otherwise the
    stub — honestly labelled, never disguised as a real call.
    """
    cfg = prov.get_provider_config(tenant_id, mode, db=db)
    ready = bool(cfg["ready"])
    if settings.TELEPHONY_MODE == mode and ready and mode in TRANSPORTS:
        return TRANSPORTS[mode], mode, True
    return StubTransport(), StubTransport.name, ready


def _hhmm_minutes(value: str, fallback: int) -> int:
    """'HH:MM' → minutes after midnight (fallback when unparseable)."""
    try:
        h, _, m = (value or "").partition(":")
        return int(h) * 60 + int(m)
    except (TypeError, ValueError):
        return fallback


def _retry_due(db: Session, campaign: Campaign | None, ended_utc: datetime) -> datetime:
    """When a lead whose attempt just ended retryably may be attempted
    again (naive UTC, the storage convention).

    Base rule: the attempt's end + the campaign's retry gap in days.
    With `retry_different_time` set, the due time is additionally moved
    to the OPPOSITE end of the campaign's calling window from the
    attempt that just ended (a morning attempt retries late in the
    window, an afternoon attempt early), computed in the tenant's
    market-local time — retrying a customer at the same hour they did
    not answer rarely changes the outcome. If the shift would land at
    or before the attempt's end (gap 0, late attempt), it rolls to the
    next local day at the shifted time.
    """
    rules = (campaign.retry_rules or {}) if campaign else {}
    try:
        gap_days = max(0, int(rules.get("retry_gap_days", 1)))
    except (TypeError, ValueError):
        gap_days = 1
    due = ended_utc + timedelta(days=gap_days)
    if rules.get("retry_different_time") and campaign is not None:
        tenant = db.get(Tenant, campaign.tenant_id)
        zone = mt.tenant_zone(tenant)
        sched = campaign.schedule or {}
        win_start = _hhmm_minutes(sched.get("window_start") or "09:00", 9 * 60)
        win_end = _hhmm_minutes(sched.get("window_end") or "19:00", 19 * 60)
        if win_end <= win_start:  # window wraps midnight — keep it simple
            return due
        ended_local = mt.to_local(ended_utc, zone)
        midpoint = (win_start + win_end) / 2
        if ended_local.hour * 60 + ended_local.minute < midpoint:
            target = max(win_start, win_end - 30)
        else:
            target = min(win_end, win_start + 30)
        due_local = mt.to_local(due, zone).replace(
            hour=target // 60, minute=target % 60, second=0, microsecond=0)
        if due_local <= ended_local:
            due_local = due_local + timedelta(days=1)
        due = mt.to_utc_naive(due_local)
    return due


def finalize_attempt(db: Session, session: CallSession, outcome: dict):
    """The one terminal write every mode lands on (§8.2).

    Idempotent: if a CallResult already exists for the session, it is
    returned unchanged and NOTHING is written (no second result, no
    session mutation, no extra lead attempt count).
    Returns (result, created).
    """
    existing = (
        db.query(CallResult)
        .filter(CallResult.session_id == session.session_id)
        .first()
    )
    if existing is not None:
        return existing, False

    session.state = outcome.get("final_state") or "COMPLETED"
    session.end_reason = outcome.get("end_reason") or ""
    session.ended_at = datetime.utcnow()
    if outcome.get("recording_url"):
        session.recording_url = outcome["recording_url"]
    if outcome.get("transcript") is not None:
        session.transcript = outcome["transcript"]
    if session.answered_at is None and session.state == "COMPLETED":
        session.answered_at = session.ended_at

    result = CallResult(
        tenant_id=session.tenant_id,
        session_id=session.session_id,
        disposition=outcome.get("disposition") or "",
        confidence=float(outcome.get("confidence") or 0.0),
        review_required=bool(outcome.get("review_required") or False),
        summary=outcome.get("summary") or "",
        captured_fields=outcome.get("captured_fields") or {},
        duration_s=int(outcome.get("duration_s") or 0),
        cost_breakdown=outcome.get("cost_breakdown") or {},
    )
    db.add(result)

    # A11: a browser-test session is NOT a campaign attempt. It carries
    # no lead (lead_id is NULL) — and even if a reference were ever
    # recorded, finalizing a test must never consume the lead's attempts
    # or re-queue it: the lead block below is skipped on the channel.
    lead = db.get(Lead, session.lead_id) if session.lead_id else None
    if lead is not None and (session.channel or "") == "browser-test":
        lead = None
    if lead is not None:
        lead.attempt_count = (lead.attempt_count or 0) + 1
        campaign = db.get(Campaign, session.campaign_id)
        max_attempts = ((campaign.retry_rules or {}).get("max_attempts")
                        if campaign else None) or 3
        if session.state in RETRYABLE_FINAL_STATES and lead.attempt_count < max_attempts:
            # Re-queue (A10): the lead returns to the dispatchable pool,
            # due again after the campaign's retry gap. Its next attempt
            # is a NEW session (next attempt number) with its own result
            # — this attempt's result above is final and untouched.
            lead.status = "pending"
            lead.next_attempt_at = _retry_due(
                db, campaign, session.ended_at or datetime.utcnow())
        elif session.state in RETRYABLE_FINAL_STATES:
            # Out of attempts: a final state — dispatch never picks it up.
            lead.status = "exhausted"
            lead.next_attempt_at = None
        else:
            # A conversation outcome (COMPLETED) or VOICEMAIL: the lead
            # is done and leaves the pending pool for good.
            lead.status = "attempted"
            lead.next_attempt_at = None

    try:
        db.commit()
    except IntegrityError:
        # Lost a race with a duplicate delivery — the unique constraint on
        # call_results.session_id is the backstop for exactly-one-result.
        db.rollback()
        existing = (
            db.query(CallResult)
            .filter(CallResult.session_id == session.session_id)
            .first()
        )
        return existing, False
    db.refresh(result)
    return result, True


def receive_result(db: Session, tenant_id: str, payload: dict):
    """Idempotent §8.2 result receiver (the callback a real orchestrator
    or the managed platform's normalised webhook will POST to in A6+).

    Keyed on session_id within the tenant. A duplicate delivery returns
    the stored result untouched. Returns (session, result, created) or
    None when the session is not this tenant's.
    """
    session = (
        db.query(CallSession)
        .filter(CallSession.session_id == payload.get("session_id"),
                CallSession.tenant_id == tenant_id)
        .first()
    )
    if session is None and payload.get("attempt_id"):
        session = (
            db.query(CallSession)
            .filter(CallSession.attempt_id == payload["attempt_id"],
                    CallSession.tenant_id == tenant_id)
            .first()
        )
    if session is None:
        return None
    outcome = {
        "final_state": payload.get("final_state"),
        "end_reason": payload.get("end_reason"),
        "disposition": payload.get("disposition"),
        "confidence": payload.get("confidence"),
        "review_required": payload.get("review_required"),
        "summary": payload.get("summary"),
        "captured_fields": payload.get("captured_fields"),
        "duration_s": payload.get("duration_s"),
        "recording_url": payload.get("recording_url"),
        "transcript": payload.get("transcript"),
        "cost_breakdown": payload.get("cost_breakdown"),
    }
    result, created = finalize_attempt(db, session, outcome)
    return session, result, created


def _within_window(schedule: dict, now_local: datetime) -> bool:
    """Calling-window check against a MARKET-LOCAL datetime (A10 — A5
    compared UTC wall-clock against the market's window)."""
    start = (schedule or {}).get("window_start") or "00:00"
    end = (schedule or {}).get("window_end") or "23:59"
    t = now_local.strftime("%H:%M")
    if start <= end:
        return start <= t <= end
    return t >= start or t <= end  # window wraps midnight


def _schedule_block(schedule: dict, now_local: datetime) -> str | None:
    """Why dispatch may not run at `now_local` (market-local), or None
    when the schedule admits attempts: start date not yet reached
    (market-local date), today not one of the calling days, or outside
    the calling window. The returned reason is operator-facing."""
    sched = schedule or {}
    start_date = (sched.get("start_date") or "").strip()
    if start_date:
        try:
            first_day = date.fromisoformat(start_date)
        except ValueError:
            first_day = None
        if first_day is not None and now_local.date() < first_day:
            return f"the campaign's start date is {start_date}"
    days = mt.parse_calling_days(sched.get("calling_days") or "")
    if days is not None and now_local.weekday() not in days:
        return (f"today is not one of the campaign's calling days "
                f"({sched.get('calling_days')})")
    if not _within_window(sched, now_local):
        return (f"outside the calling window "
                f"{sched.get('window_start') or '—'} – {sched.get('window_end') or '—'}")
    return None


def _attempt_id(campaign: Campaign, lead: Lead, attempt_no: int) -> str:
    """Deterministic idempotency key (§8.1): the same (campaign, lead,
    attempt number) always yields the same attempt_id, so re-dispatching
    can never create a second session for the same attempt."""
    return f"att_{campaign.campaign_id[4:]}_{lead.lead_id[4:]}_{attempt_no}"


class Dispatcher:
    """Admits attempts for one campaign and works them to terminal state.

    Enqueue: one CallSession (QUEUED) per admissible lead, idempotent by
    attempt_id. Admissible = campaign ACTIVE (the router enforces it),
    schedule open in market-local time, lead pending and under its
    attempt cap, daily cap not reached, and — for re-queued leads — the
    retry gap elapsed. Worker: drains the queue in batches of the
    tenant's concurrency_limit through the resolved transport into
    finalize.
    """

    def dispatch(self, db: Session, campaign: Campaign) -> dict:
        tenant = db.get(Tenant, campaign.tenant_id)
        zone = mt.tenant_zone(tenant)
        tz_name = mt.tenant_timezone_name(tenant)
        transport, transport_name, provider_ready = resolve_transport(
            db, campaign.tenant_id, campaign.provider_mode)
        report = {
            "campaign_id": campaign.campaign_id,
            "transport": transport_name,
            "stub": transport_name == StubTransport.name,
            "provider_mode": campaign.provider_mode,
            "provider_ready": provider_ready,
            "timezone": tz_name,
            "outside_window": False,
            "created": 0,
            "already_dispatched": 0,
            "retry_waiting": 0,
            "finalized": 0,
            "placed": 0,
            "message": "",
        }

        # Schedule admission is market-local (A10): the tenant's clock,
        # not UTC, decides window / calling days / start date.
        now_utc = datetime.utcnow()
        now_local = mt.to_local(now_utc, zone)
        block = _schedule_block(campaign.schedule or {}, now_local)
        if block is not None:
            report["outside_window"] = True
            report["message"] = (
                f"No attempts admitted — {block} "
                f"(schedule evaluated in {tz_name}, the tenant's market time).")
            return report

        # Daily cap: sessions started on the market-local day count.
        daily_cap = (campaign.schedule or {}).get("daily_cap") or 0
        started_today = 0
        if daily_cap:
            day_start_utc = mt.local_day_start_utc(now_local.date(), zone)
            started_today = (
                db.query(CallSession)
                .filter(CallSession.campaign_id == campaign.campaign_id,
                        CallSession.started_at >= day_start_utc)
                .count()
            )
            remaining_cap = max(0, daily_cap - started_today)
        else:
            remaining_cap = None  # 0 in the schedule means "no cap set"

        max_attempts = (campaign.retry_rules or {}).get("max_attempts") or 3
        pool = (
            db.query(Lead)
            .filter(Lead.campaign_id == campaign.campaign_id,
                    Lead.status == "pending",
                    Lead.attempt_count < max_attempts)
            .order_by(Lead.created_at, Lead.lead_id)
            .all()
        )
        # A re-queued lead is admissible only once its retry gap has
        # elapsed; the rest are reported, not silently dropped.
        leads = [l for l in pool
                 if l.next_attempt_at is None or l.next_attempt_at <= now_utc]
        report["retry_waiting"] = len(pool) - len(leads)

        queued: list[CallSession] = []
        for lead in leads:
            if remaining_cap is not None and len(queued) >= remaining_cap:
                break
            attempt_id = _attempt_id(campaign, lead, (lead.attempt_count or 0) + 1)
            exists = (
                db.query(CallSession)
                .filter(CallSession.tenant_id == campaign.tenant_id,
                        CallSession.attempt_id == attempt_id)
                .first()
            )
            if exists is not None:
                report["already_dispatched"] += 1
                continue
            session = CallSession(
                tenant_id=campaign.tenant_id,
                campaign_id=campaign.campaign_id,
                lead_id=lead.lead_id,
                attempt_id=attempt_id,
                provider_mode=campaign.provider_mode,
                state="QUEUED",
                to_number=lead.phone_number,
                from_number=(tenant.from_numbers or [""])[0] if tenant else "",
            )
            db.add(session)
            queued.append(session)
        db.commit()
        for s in queued:
            db.refresh(s)
        report["created"] = len(queued)

        # Worker loop — batches of the tenant's concurrency limit. A
        # transport returns an outcome dict (finalize now — the stub, or
        # a placement the provider rejected) or None (a real call was
        # placed; its result lands later via webhook / stream through
        # the same finalize path).
        agent = db.get(AgentConfig, campaign.agent_id)
        limit = max(1, (tenant.concurrency_limit if tenant else 20) or 20)
        run_start = datetime.utcnow()
        seq = 0
        for i in range(0, len(queued), limit):
            for session in queued[i:i + limit]:
                outcome = transport.run(db, session, agent, campaign, seq, run_start)
                if outcome is None:
                    report["placed"] += 1
                else:
                    _result, created = finalize_attempt(db, session, outcome)
                    if created:
                        report["finalized"] += 1
                seq += 1

        if report["placed"]:
            report["message"] = (
                f"Placed {report['placed']} call(s) via the {transport_name} "
                "transport — results land as the provider reports them "
                "(end-of-call webhook / media stream), through the same "
                "finalize path as every other mode.")
        elif report["created"]:
            report["message"] = (
                f"Dispatched {report['created']} attempt(s) via the "
                f"{transport_name} transport"
                + (" — simulated, no provider was called."
                   if report["stub"] else "."))
        elif report["already_dispatched"]:
            report["message"] = ("All admissible leads already have an attempt "
                                  "for their current attempt number — nothing new dispatched.")
        elif report["retry_waiting"]:
            report["message"] = (
                f"{report['retry_waiting']} lead(s) are re-queued but their "
                "retry gap has not elapsed yet — nothing new dispatched.")
        else:
            report["message"] = "No pending leads to dispatch."
        if report["retry_waiting"] and (report["created"] or report["placed"]):
            report["message"] += (
                f" ({report['retry_waiting']} more lead(s) are waiting for "
                "their retry gap to elapse.)")
        return report
