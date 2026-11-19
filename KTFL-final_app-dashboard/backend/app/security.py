"""Password hashing (PBKDF2), JWT tokens and role checks. Roles: viewer < approver < admin."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .models import User

ROLES = {"viewer": 1, "approver": 2, "admin": 3}
_ITER = 240_000
_bearer = HTTPBearer(auto_error=False)


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, _ITER)
    return f"pbkdf2${_ITER}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, it, salt, dk = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), base64.b64decode(salt), int(it))
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except Exception:
        return False


def make_token(user: User) -> str:
    cfg = get_settings()
    exp = datetime.now(timezone.utc) + timedelta(minutes=cfg.jwt_expire_minutes)
    return jwt.encode({"sub": user.username, "role": user.role, "exp": exp}, cfg.jwt_secret, cfg.jwt_algorithm)


def seed_admin(db: Session) -> None:
    cfg = get_settings()
    if not cfg.admin_password:
        return
    if db.scalar(select(User).where(User.username == cfg.admin_username)) is None:
        db.add(User(username=cfg.admin_username, full_name="Administrator", role="admin",
                    password_hash=hash_password(cfg.admin_password)))
        db.commit()


def current_user(cred: HTTPAuthorizationCredentials | None = Depends(_bearer),
                 db: Session = Depends(get_db)) -> User:
    if cred is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
    cfg = get_settings()
    try:
        data = jwt.decode(cred.credentials, cfg.jwt_secret, algorithms=[cfg.jwt_algorithm])
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired - sign in again")
    user = db.scalar(select(User).where(User.username == data.get("sub")))
    if user is None or not user.active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not active")
    return user


def require(role: str):
    def dep(user: User = Depends(current_user)) -> User:
        if ROLES.get(user.role, 0) < ROLES[role]:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"{role} role required")
        return user
    return dep
