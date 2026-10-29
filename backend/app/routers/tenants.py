"""Tenant API (A1 — minimal CRUD). A user sees only their own tenant.

Roles (C5): creating a tenant is Super Admin only (the open
provisioning endpoint is now locked down, as its own docstring
promised). The first Tenant Admin is created INVITE-based — pending
until they complete setup with the OTP + their own password — so
tenant creation never returns a password. Tenant settings (timezone,
from-numbers) are Tenant Admin work via PUT /me.
"""
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import get_current_user, require_super_admin, require_tenant_admin
from ..market_time import tenant_timezone_name, timezone_name_for_market
from ..models import Tenant, User, UserInvite
from ..schemas import TenantIn, TenantOut, TenantPatchIn
from ..security import hash_password

router = APIRouter(prefix="/api/tenants", tags=["tenants"])


def _out(t: Tenant) -> TenantOut:
    return TenantOut(
        tenant_id=t.tenant_id, name=t.name, market=t.market,
        timezone=tenant_timezone_name(t),
        provider_mode_default=t.provider_mode_default,
        from_numbers=t.from_numbers or [], concurrency_limit=t.concurrency_limit,
        recording_retention_days=t.recording_retention_days,
    )


@router.get("/me", response_model=TenantOut)
def my_tenant(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tenant = db.get(Tenant, user.tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tenant not found")
    return _out(tenant)


@router.get("/{tenant_id}", response_model=TenantOut)
def get_tenant(tenant_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # Tenant scoping: another tenant's record is indistinguishable from
    # missing — except for Super Admin, who reads tenant metadata
    # (not customer data) across tenants.
    if tenant_id != user.tenant_id and user.role != "super_admin":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tenant not found")
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tenant not found")
    return _out(tenant)


@router.put("/me", response_model=TenantOut)
def update_my_tenant(body: TenantPatchIn, user: User = Depends(require_tenant_admin),
                     db: Session = Depends(get_db)):
    """Tenant settings (C5): timezone and from-numbers. Tenant Admin
    only — Super Admin manages the platform, not tenant settings."""
    tenant = db.get(Tenant, user.tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tenant not found")
    if body.timezone is not None:
        tenant.timezone = body.timezone
    if body.from_numbers is not None:
        tenant.from_numbers = body.from_numbers
    db.commit()
    db.refresh(tenant)
    return _out(tenant)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_tenant(body: TenantIn, creator: User = Depends(require_super_admin),
                  db: Session = Depends(get_db)):
    """Provision a new tenant + its first Tenant Admin, invite-based.

    C5 lockdown: Super Admin only (previously open). The admin
    account is created PENDING with a setup invite — no password is
    generated or returned. The response carries the setup link the
    Super Admin forwards to the new Tenant Admin (email delivery is
    log-mode until a provider is wired) and the OTP under the
    INVITE_DEV_OTP test gate only.
    """
    tenant = Tenant(
        name=body.name, market=body.market,
        timezone=body.timezone or timezone_name_for_market(body.market),
        provider_mode_default=body.provider_mode_default,
        from_numbers=body.from_numbers, concurrency_limit=body.concurrency_limit,
    )
    db.add(tenant)
    db.flush()
    admin_email = f"admin@{tenant.tenant_id}.example"
    admin = User(
        tenant_id=tenant.tenant_id, email=admin_email, name="Tenant Admin",
        role="tenant_admin", password_hash="", is_active=False,
    )
    db.add(admin)
    db.flush()
    otp = f"{secrets.randbelow(900000) + 100000:06d}"
    setup_token = secrets.token_urlsafe(32)
    invite = UserInvite(
        tenant_id=tenant.tenant_id, email=admin_email, name="Tenant Admin",
        role="tenant_admin", channel="email", invited_by=creator.user_id,
        otp_hash=hash_password(otp), setup_token_hash=hash_password(setup_token),
        expires_at=datetime.utcnow() + timedelta(hours=24),
    )
    db.add(invite)
    db.commit()
    return {"tenant": _out(tenant).model_dump(), "admin_email": admin_email,
            "invite_id": invite.invite_id,
            "setup_url": f"/setup?invite={invite.invite_id}&token={setup_token}",
            "dev_otp": otp if settings.INVITE_DEV_OTP else None}
