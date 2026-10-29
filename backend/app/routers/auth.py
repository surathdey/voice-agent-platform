"""Auth routes: login with the seeded demo user (or any tenant user),
plus the C5 invite-setup endpoint (single-use link + OTP -> the user
sets their own password and the pending account activates)."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Tenant, User, UserInvite
from ..schemas import LoginIn, LoginOut, SetupIn, UserOut
from ..security import create_token, hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=LoginOut)
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == body.email).first()
    if user is None or not user.is_active or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    tenant = db.get(Tenant, user.tenant_id)
    return LoginOut(
        access_token=create_token(user),
        user=UserOut(
            user_id=user.user_id, tenant_id=user.tenant_id, email=user.email,
            name=user.name, role=user.role, team_id=user.team_id,
            phone=user.phone or "",
        ),
        tenant_name=tenant.name if tenant else "",
    )


@router.post("/setup", response_model=LoginOut)
def setup_from_invite(body: SetupIn, db: Session = Depends(get_db)):
    """Complete an invite: the single-use setup token from the link
    PLUS the OTP from the invite channel, then the user's own
    password. Both factors are verified against stored (salted)
    hashes; the invite is single-use (used_at) and expiring
    (expires_at). A failure here is indistinguishable (401) whether
    the invite, the token, the OTP, or the expiry is wrong — no
    oracle for either factor."""
    invite = db.get(UserInvite, body.invite_id)
    if (invite is None or invite.used_at is not None
            or datetime.utcnow() > invite.expires_at
            or not verify_password(body.token, invite.setup_token_hash)
            or not verify_password(body.otp, invite.otp_hash)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "Invalid or expired invite")
    user = db.query(User).filter(User.email == invite.email).first()
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "Invalid or expired invite")
    user.password_hash = hash_password(body.password)
    user.is_active = True
    user.team_id = invite.team_id
    user.phone = invite.phone or user.phone
    invite.used_at = datetime.utcnow()
    db.commit()
    db.refresh(user)
    tenant = db.get(Tenant, user.tenant_id)
    return LoginOut(
        access_token=create_token(user),
        user=UserOut(
            user_id=user.user_id, tenant_id=user.tenant_id, email=user.email,
            name=user.name, role=user.role, team_id=user.team_id,
            phone=user.phone or "",
        ),
        tenant_name=tenant.name if tenant else "",
    )
