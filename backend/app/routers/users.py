"""Account management (Epic C, C5 — design §16).

Who creates whom, enforced here (not just documented):
- Super Admin: invites Tenant Admins, Team Leaders, and Callers
  (with an explicit tenant_id), and creates teams at setup.
- Tenant Admin: invites Callers into their own tenant only, manages
  those Caller accounts day to day (team, active flag, name).
- Team Leader / Caller: no account management at all.

Creation is invite-only: there is deliberately NO direct "create
user with a password" endpoint. An invite is a single-use expiring
setup link + an OTP over the invite channel (email first, SMS
fallback); the user sets their own password at setup. One account
per person — no shared logins, because the who + when audit is
meaningless with shared accounts.
"""
import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import (
    get_current_user,
    require_tenant_admin_or_super,
    team_caller_ids,
    ROLE_CALLER,
    ROLE_SUPER_ADMIN,
    ROLE_TEAM_LEADER,
    ROLE_TENANT_ADMIN,
)
from ..models import Team, User, UserInvite
from ..schemas import (
    InviteIn,
    InviteOut,
    TeamIn,
    TeamOut,
    UserOut,
    UserPatchIn,
)
from ..security import hash_password

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/users", tags=["users"])
teams_router = APIRouter(prefix="/api/teams", tags=["teams"])

INVITABLE_ROLES = (ROLE_TENANT_ADMIN, ROLE_TEAM_LEADER, ROLE_CALLER)
SETUP_LINK_TTL_HOURS = 24


def _user_out(u: User) -> UserOut:
    return UserOut(
        user_id=u.user_id, tenant_id=u.tenant_id, email=u.email,
        name=u.name, role=u.role, team_id=u.team_id, phone=u.phone or "",
    )


def _team_out(db: Session, t: Team) -> TeamOut:
    members = (db.query(User).filter(User.team_id == t.team_id,
                                     User.role == ROLE_CALLER).count())
    return TeamOut(
        team_id=t.team_id, tenant_id=t.tenant_id, name=t.name,
        leader_user_id=t.leader_user_id, member_count=members,
    )


def _resolve_tenant_for_invite(creator: User, body: InviteIn) -> str:
    """The tenant an invite lands in. Super Admin names it; Tenant
    Admin invites only into their own tenant (a body tenant_id that
    disagrees is rejected, not silently overridden)."""
    if creator.role == ROLE_SUPER_ADMIN:
        if not body.tenant_id:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Super Admin invites require tenant_id")
        return body.tenant_id
    if body.tenant_id is not None and body.tenant_id != creator.tenant_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Tenant Admin invites only into their own tenant")
    return creator.tenant_id


@router.get("/me", response_model=UserOut)
def my_profile(user: User = Depends(get_current_user)):
    return _user_out(user)


@router.get("", response_model=list[UserOut])
def list_users(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Staff directory (not customer data). Super Admin: everyone.
    Tenant Admin: own tenant. Team Leader: their team's Callers.
    Caller: forbidden — a Caller sees no other accounts."""
    if user.role == ROLE_SUPER_ADMIN:
        rows = db.query(User).order_by(User.email).all()
    elif user.role == ROLE_TENANT_ADMIN:
        rows = (db.query(User).filter(User.tenant_id == user.tenant_id)
                .order_by(User.email).all())
    elif user.role == ROLE_TEAM_LEADER:
        ids = team_caller_ids(user, db)
        rows = (db.query(User).filter(User.user_id.in_(ids))
                .order_by(User.email).all()) if ids else []
    else:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Callers cannot list accounts")
    return [_user_out(u) for u in rows]


@router.post("/invites", response_model=InviteOut, status_code=status.HTTP_201_CREATED)
def create_invite(body: InviteIn, creator: User = Depends(require_tenant_admin_or_super),
                  db: Session = Depends(get_db)):
    if body.role not in INVITABLE_ROLES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Invites create tenant_admin, team_leader, or caller — "
                            "never super_admin (bootstrapped, see seed.py)")
    if creator.role == ROLE_TENANT_ADMIN and body.role != ROLE_CALLER:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Tenant Admin invites Callers only")
    if body.channel not in ("email", "sms"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "channel must be email or sms")
    tenant_id = _resolve_tenant_for_invite(creator, body)
    if body.team_id is not None:
        team = (db.query(Team).filter(Team.team_id == body.team_id,
                                      Team.tenant_id == tenant_id).first())
        if team is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Team not found in this tenant")
    existing = db.query(User).filter(User.email == body.email).first()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "An account with this email already exists")
    from datetime import datetime, timedelta
    otp = f"{secrets.randbelow(900000) + 100000:06d}"
    setup_token = secrets.token_urlsafe(32)
    user = User(
        tenant_id=tenant_id, email=body.email, name=body.name,
        role=body.role, password_hash="", is_active=False,
        team_id=body.team_id, phone=body.phone,
    )
    db.add(user)
    db.flush()
    invite = UserInvite(
        tenant_id=tenant_id, email=body.email, name=body.name,
        role=body.role, team_id=body.team_id, phone=body.phone,
        channel=body.channel, invited_by=creator.user_id,
        otp_hash=hash_password(otp), setup_token_hash=hash_password(setup_token),
        expires_at=datetime.utcnow() + timedelta(hours=SETUP_LINK_TTL_HOURS),
    )
    db.add(invite)
    db.commit()
    # Log-mode delivery until an email/SMS provider is wired: the
    # dispatch is recorded, the OTP value is NOT logged. The inviter
    # forwards the setup link (returned below) when the channel is
    # unavailable — the OTP itself travels only over its channel.
    logger.info("invite %s created for %s via %s (invited by %s)",
                invite.invite_id, body.email, body.channel, creator.email)
    return InviteOut(
        invite_id=invite.invite_id, tenant_id=tenant_id, email=body.email,
        name=body.name, role=body.role, team_id=body.team_id,
        channel=body.channel,
        setup_url=f"/setup?invite={invite.invite_id}&token={setup_token}",
        expires_at=invite.expires_at.isoformat(),
        dev_otp=otp if settings.INVITE_DEV_OTP else None,
    )


@router.get("/invites", response_model=list[InviteOut])
def list_invites(user: User = Depends(require_tenant_admin_or_super),
                 db: Session = Depends(get_db)):
    """Pending + used invites. Super Admin: all tenants; Tenant
    Admin: own tenant. The OTP and setup token are never listed —
    only hashes are stored, so there is nothing to leak."""
    query = db.query(UserInvite)
    if user.role == ROLE_TENANT_ADMIN:
        query = query.filter(UserInvite.tenant_id == user.tenant_id)
    rows = query.order_by(UserInvite.created_at.desc()).all()
    return [InviteOut(
        invite_id=r.invite_id, tenant_id=r.tenant_id, email=r.email,
        name=r.name, role=r.role, team_id=r.team_id, channel=r.channel,
        setup_url="", expires_at=r.expires_at.isoformat() if r.expires_at else None,
    ) for r in rows]


@router.patch("/{user_id}", response_model=UserOut)
def update_user(user_id: str, body: UserPatchIn,
                actor: User = Depends(require_tenant_admin_or_super),
                db: Session = Depends(get_db)):
    """Day-to-day account management. Super Admin: any account.
    Tenant Admin: Caller accounts in their own tenant only (name,
    team, active flag — role never changes through this endpoint;
    a role change is deactivate + re-invite)."""
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if actor.role == ROLE_TENANT_ADMIN:
        if target.tenant_id != actor.tenant_id or target.role != ROLE_CALLER:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "Tenant Admin manages Caller accounts in their own tenant only")
    if body.team_id is not None:
        team = (db.query(Team).filter(Team.team_id == body.team_id,
                                      Team.tenant_id == target.tenant_id).first())
        if team is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Team not found in this tenant")
        target.team_id = body.team_id
    if body.name is not None:
        target.name = body.name
    if body.is_active is not None:
        if target.user_id == actor.user_id and body.is_active is False:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Cannot deactivate your own account")
        target.is_active = body.is_active
    db.commit()
    db.refresh(target)
    return _user_out(target)


@teams_router.post("", response_model=TeamOut, status_code=status.HTTP_201_CREATED)
def create_team(body: TeamIn, actor: User = Depends(require_tenant_admin_or_super),
                db: Session = Depends(get_db)):
    """Teams are org structure, not customer data: Super Admin
    creates them at tenant setup (with an explicit tenant_id),
    Tenant Admin manages them day to day in their own tenant.
    The leader, when named, must already be a Team Leader in the
    same tenant."""
    if actor.role == ROLE_SUPER_ADMIN:
        if not body.tenant_id:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Super Admin team creation requires tenant_id")
        tenant_id = body.tenant_id
    else:
        if body.tenant_id is not None and body.tenant_id != actor.tenant_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "Tenant Admin creates teams in their own tenant only")
        tenant_id = actor.tenant_id
    if body.leader_user_id is not None:
        leader = db.get(User, body.leader_user_id)
        if (leader is None or leader.tenant_id != tenant_id
                or leader.role != ROLE_TEAM_LEADER):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Leader must be a Team Leader in this tenant")
    team = Team(tenant_id=tenant_id, name=body.name,
                leader_user_id=body.leader_user_id)
    db.add(team)
    db.commit()
    db.refresh(team)
    return _team_out(db, team)


@teams_router.get("", response_model=list[TeamOut])
def list_teams(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Super Admin: all teams. Tenant Admin: own tenant. Team
    Leader: teams they lead. Caller: forbidden."""
    if user.role == ROLE_SUPER_ADMIN:
        rows = db.query(Team).order_by(Team.name).all()
    elif user.role == ROLE_TENANT_ADMIN:
        rows = (db.query(Team).filter(Team.tenant_id == user.tenant_id)
                .order_by(Team.name).all())
    elif user.role == ROLE_TEAM_LEADER:
        rows = (db.query(Team).filter(Team.leader_user_id == user.user_id)
                .order_by(Team.name).all())
    else:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Callers cannot list teams")
    return [_team_out(db, t) for t in rows]


@teams_router.patch("/{team_id}", response_model=TeamOut)
def update_team(team_id: str, body: TeamIn,
                actor: User = Depends(require_tenant_admin_or_super),
                db: Session = Depends(get_db)):
    team = db.get(Team, team_id)
    if team is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Team not found")
    if actor.role == ROLE_TENANT_ADMIN and team.tenant_id != actor.tenant_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your tenant's team")
    if body.name:
        team.name = body.name
    if body.leader_user_id is not None:
        leader = db.get(User, body.leader_user_id)
        if (leader is None or leader.tenant_id != team.tenant_id
                or leader.role != ROLE_TEAM_LEADER):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Leader must be a Team Leader in this tenant")
        team.leader_user_id = body.leader_user_id
    db.commit()
    db.refresh(team)
    return _team_out(db, team)
