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
