"""Tenant API (A1 — minimal CRUD). A user sees only their own tenant;
creating a tenant also creates its first admin user (returned once)."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from ..models import Tenant, User
from ..schemas import TenantIn, TenantOut
from ..security import hash_password

router = APIRouter(prefix="/api/tenants", tags=["tenants"])


def _out(t: Tenant) -> TenantOut:
    return TenantOut(
        tenant_id=t.tenant_id, name=t.name, market=t.market,
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
    # Tenant scoping: another tenant's record is indistinguishable from missing.
    if tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tenant not found")
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tenant not found")
    return _out(tenant)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_tenant(body: TenantIn, db: Session = Depends(get_db)):
    """Provision a new tenant + its first admin login.

    Open in this build chunk (operator provisioning); lock down with an
    agency-admin role before production use.
    """
    tenant = Tenant(
        name=body.name, market=body.market,
        provider_mode_default=body.provider_mode_default,
        from_numbers=body.from_numbers, concurrency_limit=body.concurrency_limit,
    )
    db.add(tenant)
    db.flush()
    admin_email = f"admin@{tenant.tenant_id}.example"
    admin = User(
        tenant_id=tenant.tenant_id, email=admin_email, name="Tenant Admin",
        role="admin", password_hash=hash_password("demo-password"),
    )
    db.add(admin)
    db.commit()
    return {"tenant": _out(tenant).model_dump(), "admin_email": admin_email,
            "admin_password": "demo-password"}
