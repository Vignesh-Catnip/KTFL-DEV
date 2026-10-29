from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User
from ..schemas import LoginIn, UserIn
from ..security import current_user, hash_password, make_token, require, verify_password
from ..services.persist import audit

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    u = db.scalar(select(User).where(User.username == body.username.strip()))
    if u is None or not u.active or not verify_password(body.password, u.password_hash):
        raise HTTPException(401, "Wrong user name or password")
    audit(db, "LOGIN", None, {}, u.username)
    return {"token": make_token(u), "user": {"username": u.username, "name": u.full_name, "role": u.role}}


@router.get("/me")
def me(u: User = Depends(current_user)):
    return {"username": u.username, "name": u.full_name, "role": u.role}


@router.get("/users")
def users(db: Session = Depends(get_db), _: User = Depends(require("admin"))):
    return [{"id": u.id, "username": u.username, "name": u.full_name, "role": u.role, "active": u.active}
            for u in db.scalars(select(User).order_by(User.id))]


@router.post("/users", status_code=201)
def create_user(body: UserIn, db: Session = Depends(get_db), admin: User = Depends(require("admin"))):
    if body.role not in ("viewer", "approver", "admin"):
        raise HTTPException(422, "role must be viewer, approver or admin")
    if db.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(409, "User already exists")
    db.add(User(username=body.username, full_name=body.full_name, role=body.role,
                password_hash=hash_password(body.password)))
    audit(db, "USER_CREATED", None, {"username": body.username, "role": body.role}, admin.username, commit=False)
    db.commit()
    return {"ok": True}
