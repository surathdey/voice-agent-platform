"""Provider Settings API (A4) — design §7 “Provider settings”, §8.5, §11 A4.

Per-tenant credentials for every provider of both orchestration modes.
Secrets are write-only: PUT accepts values, every read returns masked
forms (app.providers.provider_view), and no response ever contains a full
secret — enforced by smoke tests using sentinel keys. Tenant settings
override the platform .env; TELEPHONY_MODE=stub is the global fallback
when nothing is configured (see app.providers).
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import providers as prov
from ..db import get_db
from ..deps import get_current_user
from ..models import ProviderSetting, User
from ..schemas import ProviderOut, ProviderSettingIn, ProviderTestOut

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _spec(provider: str) -> dict:
    spec = prov.PROVIDERS.get(provider)
    if spec is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown provider '{provider}'")
    return spec


@router.get("/providers", response_model=list[ProviderOut])
def list_providers(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """All providers, both modes, for the caller's tenant — masked view."""
    return [prov.provider_view(db, user.tenant_id, key) for key in prov.PROVIDERS]


@router.get("/providers/{provider}", response_model=ProviderOut)
def get_provider(provider: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _spec(provider)
    return prov.provider_view(db, user.tenant_id, provider)


@router.put("/providers/{provider}", response_model=ProviderOut)
def save_provider(provider: str, body: ProviderSettingIn,
                  user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    spec = _spec(provider)
    known = {f["name"]: f for f in spec["fields"]}
    unknown = sorted(set(body.fields) - set(known))
    if unknown:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"Unknown field(s) for {provider}: {unknown}")
    row = prov.get_row(db, user.tenant_id, provider)
    if row is None:
        row = ProviderSetting(tenant_id=user.tenant_id, provider=provider, fields={})
        db.add(row)
    before = dict(row.fields or {})
    stored = dict(before)
    for name, value in body.fields.items():
        if not isinstance(value, str):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                f"Field '{name}' must be a string")
        if known[name]["secret"] and value == "":
            continue  # write-only secret: blank means "keep the stored value"
        stored[name] = value
    creds_changed = stored != before
    row.fields = stored
    if body.enabled is not None:
        creds_changed = creds_changed or body.enabled != row.enabled
        row.enabled = body.enabled
    if creds_changed:
        # A previous Test-connection verdict no longer describes these credentials.
        row.last_test_status = ""
        row.last_test_message = ""
        row.last_test_at = None
    db.commit()
    db.refresh(row)
    return prov.provider_view(db, user.tenant_id, provider)


@router.delete("/providers/{provider}", response_model=ProviderOut)
def delete_provider(provider: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Remove the tenant's own settings for a provider (falls back to .env / stub)."""
    _spec(provider)
    row = prov.get_row(db, user.tenant_id, provider)
    if row is not None:
        db.delete(row)
        db.commit()
    return prov.provider_view(db, user.tenant_id, provider)


@router.post("/providers/{provider}/test", response_model=ProviderTestOut)
def test_provider(provider: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Test connection: an honest lightweight real check when credentials
    exist; a clear stub report when none are configured."""
    _spec(provider)
    return prov.test_connection(db, user.tenant_id, provider)
