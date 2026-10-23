"""Auth routes: login with the seeded demo user (or any tenant user)."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Tenant, User
from ..schemas import LoginIn, LoginOut, UserOut
from ..security import create_token, verify_password

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
            name=user.name, role=user.role,
        ),
        tenant_name=tenant.name if tenant else "",
    )
