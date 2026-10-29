"""Auth dependencies — every protected route resolves the current user and
their tenant. Tenant scoping rule (A1): queries are ALWAYS filtered by the
current user's tenant_id; there is no code path that reads across tenants."""
import jwt as pyjwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .db import get_db
from .models import User
from .security import decode_token

bearer = HTTPBearer(auto_error=False)


def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        payload = decode_token(creds.credentials)
    except pyjwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    user = db.get(User, payload.get("sub", ""))
    if user is None or not user.is_active or user.tenant_id != payload.get("tenant_id"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown user")
    return user


# Epic C, C5 (design §16): exactly one role per user, and a user
# belongs to exactly one scope (platform or one tenant).
ROLE_SUPER_ADMIN = "super_admin"
ROLE_TENANT_ADMIN = "tenant_admin"
ROLE_TEAM_LEADER = "team_leader"
ROLE_CALLER = "caller"
ALL_ROLES = (ROLE_SUPER_ADMIN, ROLE_TENANT_ADMIN, ROLE_TEAM_LEADER, ROLE_CALLER)


def _forbid():
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Role does not permit this action")


def require_super_admin(user: User = Depends(get_current_user)) -> User:
    """Platform scope only: catalog authoring, tenant creation,
    Tenant Admin account creation, platform settings. A Super Admin
    NEVER touches tenant customer data — no leads, calls, results,
    campaigns, approvals, or exports (those endpoints forbid the role
    outright rather than scoping it to an empty set)."""
    if user.role != ROLE_SUPER_ADMIN:
        _forbid()
    return user


def require_tenant_admin(user: User = Depends(get_current_user)) -> User:
    """Tenant Admin only — deliberately NOT Super Admin: the
    Super Admin's no-customer-data rule means campaign, lead, call,
    and export work belongs to the Tenant Admin alone."""
    if user.role != ROLE_TENANT_ADMIN:
        _forbid()
    return user


def require_tenant_admin_or_super(user: User = Depends(get_current_user)) -> User:
    """Tenant configuration work both roles may do: adoption and
    account management. (Adoption carries no customer data, and
    Super Admin performs initial tenant setup.)"""
    if user.role not in (ROLE_TENANT_ADMIN, ROLE_SUPER_ADMIN):
        _forbid()
    return user


def require_team_leader(user: User = Depends(get_current_user)) -> User:
    """Team Leader only — case allocation is theirs alone: the
    Tenant Admin does NOT allocate (decided 2026-10-29)."""
    if user.role != ROLE_TEAM_LEADER:
        _forbid()
    return user


def require_catalog_admin(user: User = Depends(get_current_user)) -> User:
    """Catalog write gate (Epic C, C2 seam, tightened by C5).

    Super Admin only: the prompt editor lives on the platform side
    and nowhere else. Tenants get new prompt text by ADOPTING a new
    catalog version, never by editing a prompt in place."""
    if user.role != ROLE_SUPER_ADMIN:
        _forbid()
    return user


def require_catalog_reader(user: User = Depends(get_current_user)) -> User:
    """Catalog reads: Super Admin (authoring side) and Tenant Admin
    (adoption side). Team Leaders and Callers never see the catalog."""
    if user.role not in (ROLE_SUPER_ADMIN, ROLE_TENANT_ADMIN):
        _forbid()
    return user


def team_caller_ids(user: User, db: Session) -> list[str]:
    """User IDs of the Callers on a Team Leader's team (C5)."""
    from .models import Team, User as UserModel
    team_ids = [t.team_id for t in
                db.query(Team).filter(Team.leader_user_id == user.user_id).all()]
    if not team_ids:
        return []
    return [u.user_id for u in
            db.query(UserModel).filter(UserModel.team_id.in_(team_ids)).all()]


def case_scope(user: User, db: Session) -> str | list[str] | None:
    """Which cases a role may see (C5 scoping, applied by every
    leads/calls/queue list and every per-case action).

    Returns "ALL" for Tenant Admin (whole tenant), the Caller's own
    id for a Caller, the team's Caller ids for a Team Leader (who
    ALSO sees the unallocated pool — allocation starts there), and
    raises 403 for Super Admin (no tenant customer data, ever).
    """
    if user.role == ROLE_TENANT_ADMIN:
        return "ALL"
    if user.role == ROLE_CALLER:
        return [user.user_id]
    if user.role == ROLE_TEAM_LEADER:
        return team_caller_ids(user, db)
    _forbid()
