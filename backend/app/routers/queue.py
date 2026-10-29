"""Human Queue + approvals (Epic C, C5 — design §16, UI-review Q2/Q4).

The queue is the set of call results flagged review_required. Every
approval is time-bound (APPROVAL_WINDOW_HOURS from queueing) and
audited (who + when + decision on the result row itself).

Who may approve what:
- Settlement-agent items: Team Leader ONLY (settlement authority =
  process TL, UI-review Q4) — the TL of the case's team, or any TL
  in the tenant when the case is unassigned.
- Any other item: Tenant Admin, or the Team Leader of the case's
  team (any TL in the tenant when unassigned).
- Caller: never approves. Super Admin: never approves (no tenant
  customer data, ever).

Approving past the deadline is refused (410): the item stays
queued and overdue — it is never silently dropped. An "escalated"
decision keeps the item queued (it still needs a human); approved
and rejected resolve it.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import (
    case_scope,
    get_current_user,
    team_caller_ids,
    ROLE_CALLER,
    ROLE_SUPER_ADMIN,
    ROLE_TEAM_LEADER,
    ROLE_TENANT_ADMIN,
)
from ..models import AgentConfig, CallResult, CallSession, Campaign, Lead, User
from ..schemas import ApproveIn

router = APIRouter(prefix="/api/queue", tags=["queue"])


def _item_out(db: Session, r: CallResult, s: CallSession) -> dict:
    campaign = db.get(Campaign, s.campaign_id) if s.campaign_id else None
    agent = db.get(AgentConfig, campaign.agent_id) if campaign else None
    lead = db.get(Lead, s.lead_id) if s.lead_id else None
    return {
        "result_id": r.result_id,
        "session_id": r.session_id,
        "campaign_id": s.campaign_id,
        "campaign_name": campaign.name if campaign else "",
        "agent_stage": agent.stage if agent else "",
        "is_settlement": bool(agent and agent.stage == "Settlement"),
        "lead_id": s.lead_id,
        "lead_name": lead.name if lead else "",
        "assigned_caller_id": lead.assigned_caller_id if lead else None,
        "disposition": r.disposition,
        "summary": r.summary,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "approval_expires_at": r.approval_expires_at.isoformat() if r.approval_expires_at else None,
        "approval_decision": r.approval_decision,
        "approved_by": r.approved_by,
        "approved_at": r.approved_at.isoformat() if r.approved_at else None,
    }


def _scoped_results(user, db: Session):
    """review_required results visible to this role (tenant-scoped;
    Super Admin is refused before this runs)."""
    scope = case_scope(user, db)  # "ALL" | [caller ids] | raises for super_admin
    query = (db.query(CallResult, CallSession)
             .join(CallSession, CallResult.session_id == CallSession.session_id)
             .filter(CallResult.review_required.is_(True),
                     CallResult.tenant_id == user.tenant_id))
    if scope == "ALL":
        return query.all()
    # Team Leader: team's items + unassigned items (they supervise
    # the pool they allocate from). Caller: own items only.
    rows = query.all()
    visible = []
    for r, s in rows:
        lead = db.get(Lead, s.lead_id) if s.lead_id else None
        assigned = lead.assigned_caller_id if lead else None
        if user.role == ROLE_TEAM_LEADER and (assigned is None or assigned in scope):
            visible.append((r, s))
        elif user.role == ROLE_CALLER and assigned in scope:
            visible.append((r, s))
    return visible


@router.get("")
def list_queue(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role == ROLE_SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Super Admin sees no tenant customer data")
    return [_item_out(db, r, s) for r, s in _scoped_results(user, db)]


@router.post("/{result_id}/approve")
def approve_item(result_id: str, body: ApproveIn,
                 user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if body.decision not in ("approved", "rejected", "escalated"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "decision must be approved, rejected, or escalated")
    if user.role in (ROLE_CALLER, ROLE_SUPER_ADMIN):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Callers and Super Admin never approve queue items")
    r = db.get(CallResult, result_id)
    if r is None or r.tenant_id != user.tenant_id or not r.review_required:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "Queue item not found (or already resolved)")
    s = db.get(CallSession, r.session_id)
    campaign = db.get(Campaign, s.campaign_id) if s and s.campaign_id else None
    agent = db.get(AgentConfig, campaign.agent_id) if campaign else None
    is_settlement = bool(agent and agent.stage == "Settlement")
    lead = db.get(Lead, s.lead_id) if s and s.lead_id else None
    assigned = lead.assigned_caller_id if lead else None
    if is_settlement and user.role != ROLE_TEAM_LEADER:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Settlement approvals belong to the Team Leader (Q4)")
    if user.role == ROLE_TEAM_LEADER:
        team_ids = team_caller_ids(user, db)
        if assigned is not None and assigned not in team_ids:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "Team Leaders approve only their team's items")
    if r.approval_expires_at is not None and datetime.utcnow() > r.approval_expires_at:
        raise HTTPException(status.HTTP_410_GONE,
                            "Approval window expired — the item stays queued and overdue")
    r.approval_decision = body.decision
    r.approved_by = user.user_id
    r.approved_at = datetime.utcnow()
    if body.decision in ("approved", "rejected"):
        r.review_required = False
    db.commit()
    db.refresh(r)
    return _item_out(db, r, s)
