from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import DocTypeMaster, User
from ..security import current_user, require
from ..services.persist import audit

router = APIRouter(prefix="/api/doc-types", tags=["doc-types"])


class DocTypeIn(BaseModel):
    client: str = Field("150", max_length=10)
    doc_type: str = Field(min_length=1, max_length=10)
    number_range: str | None = Field(None, max_length=10)
    description: str | None = Field(None, max_length=120)
    plant_code: str = Field(min_length=1, max_length=20)


def _row(r: DocTypeMaster) -> dict:
    return {"id": r.id, "client": r.client, "doc_type": r.doc_type, "number_range": r.number_range,
            "description": r.description, "plant_code": r.plant_code}


@router.get("")
def list_doc_types(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return [_row(r) for r in db.scalars(select(DocTypeMaster).order_by(DocTypeMaster.plant_code, DocTypeMaster.doc_type))]


@router.post("")
def add_doc_type(body: DocTypeIn, db: Session = Depends(get_db), user: User = Depends(require("admin"))):
    dt, plant = body.doc_type.strip().upper(), body.plant_code.strip()
    if db.scalar(select(DocTypeMaster).where(DocTypeMaster.client == body.client, DocTypeMaster.doc_type == dt,
                                             DocTypeMaster.plant_code == plant)):
        raise HTTPException(409, "This document type is already defined for the plant")
    r = DocTypeMaster(client=body.client, doc_type=dt, number_range=body.number_range,
                      description=body.description, plant_code=plant)
    db.add(r)
    audit(db, "DOC_TYPE_ADDED", None, _row(r) | {"id": None}, user.username)
    return _row(r)


@router.delete("/{row_id}")
def delete_doc_type(row_id: int, db: Session = Depends(get_db), user: User = Depends(require("admin"))):
    r = db.get(DocTypeMaster, row_id)
    if r is None:
        raise HTTPException(404, "Not found")
    audit(db, "DOC_TYPE_DELETED", None, _row(r), user.username, commit=False)
    db.delete(r)
    db.commit()
    return {"ok": True}
