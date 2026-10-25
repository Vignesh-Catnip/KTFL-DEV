from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import AuditLog, Invoice, QUEUES, User
from ..schemas import InvoiceEdit, ResolveIn
from ..security import current_user, require
from ..services import pdfio
from ..services.compare import build_compare
from ..services.persist import audit, find_duplicate, header_from_invoice
from ..services.pipeline import recheck_invoice
from .common import invoice_row, iso

router = APIRouter(prefix="/api/invoices", tags=["invoices"])

# edit field -> (Invoice column, header key)
_EDIT_COL = {"total_tax": "tax"}


def _get(db: Session, invoice_id: int) -> Invoice:
    inv = db.get(Invoice, invoice_id)
    if inv is None:
        raise HTTPException(404, "Invoice not found")
    return inv


@router.get("")
def list_invoices(queue: str | None = None, q: str | None = None, plant: str | None = None,
                  miro: str | None = None, page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=200),
                  db: Session = Depends(get_db), _: User = Depends(current_user)):
    stmt = select(Invoice)
    if queue:
        if queue.upper() not in QUEUES:
            raise HTTPException(422, "queue must be MATCHED, RECONCILE or BLOCKED")
        stmt = stmt.where(Invoice.queue == queue.upper())
    if plant:
        stmt = stmt.where(Invoice.plant_code == plant)
    if miro:
        stmt = stmt.where(Invoice.miro_status.is_(None) if miro == "NONE" else Invoice.miro_status == miro)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Invoice.invoice_number.ilike(like), Invoice.vendor.ilike(like),
                              Invoice.po_number.ilike(like), Invoice.grn_number.ilike(like)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(Invoice.id.desc()).offset((page - 1) * size).limit(size)).all()
    return {"total": total, "page": page, "size": size, "items": [invoice_row(i) for i in rows]}


@router.get("/{invoice_id}")
def detail(invoice_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    inv = _get(db, invoice_id)
    row = invoice_row(inv)
    row.update({
        "gstin": inv.gstin, "currency": inv.currency, "cgst": inv.cgst, "sgst": inv.sgst, "igst": inv.igst,
        "tax": inv.tax, "po_date": inv.po_date, "grn_date": inv.grn_date, "grn_amount": inv.grn_amount,
        "sap_tax_code": inv.sap_tax_code, "hsn_match": inv.hsn_match, "sap_checked": inv.sap_checked,
        "grn_in_sap": inv.grn_in_sap, "po_list": inv.po_list, "red_flag_reason": inv.red_flag_reason,
        "issues": inv.issues or [], "field_sources": inv.field_sources or {},
        "page_count": inv.page_count, "resolution_note": inv.resolution_note, "resolved_by": inv.resolved_by,
        "resolved_at": iso(inv.resolved_at), "confidence": inv.confidence,
        "handwritten_references": inv.handwritten_references, "company_code": inv.company_code,
        "miro_selected_rows": inv.miro_selected_rows,
        "compare": build_compare(inv),
        "history": [{"action": a.action, "actor": a.actor, "at": iso(a.created_at), "detail": a.detail}
                    for a in db.scalars(select(AuditLog).where(AuditLog.invoice_id == inv.id)
                                        .order_by(AuditLog.id.desc()).limit(50))],
    })
    return row


@router.patch("/{invoice_id}")
def edit(invoice_id: int, body: InvoiceEdit, db: Session = Depends(get_db),
         user: User = Depends(require("approver"))):
    inv = _get(db, invoice_id)
    if inv.miro_status in ("POSTED", "PARKED"):
        raise HTTPException(409, f"Invoice already {inv.miro_status} in SAP - it cannot be edited")
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(422, "Nothing to change")
    before = {}
    for k, v in changes.items():
        col = _EDIT_COL.get(k, k)
        before[k] = getattr(inv, col)
        if isinstance(v, str):
            v = v.strip()
        setattr(inv, col, v)
    if find_duplicate(db, header_from_invoice(inv)[0] | {"invoice_number": inv.invoice_number,
                                                          "po_number": inv.po_number}, exclude_id=inv.id):
        db.rollback()
        raise HTTPException(409, "Another invoice with the same invoice number and PO already exists")
    recheck_invoice(db, inv, actor=user.username, manual_fields=set(changes), action="EDITED")
    audit(db, "FIELDS_EDITED", inv.id, {"before": before, "after": changes}, user.username)
    return {"queue": inv.queue, "status": inv.status}


@router.post("/{invoice_id}/recheck")
def recheck(invoice_id: int, db: Session = Depends(get_db), user: User = Depends(require("approver"))):
    inv = _get(db, invoice_id)
    recheck_invoice(db, inv, actor=user.username)
    return {"queue": inv.queue, "status": inv.status}


@router.post("/{invoice_id}/resolve")
def resolve(invoice_id: int, body: ResolveIn, db: Session = Depends(get_db),
            user: User = Depends(require("approver"))):
    inv = _get(db, invoice_id)
    if inv.queue == "BLOCKED":
        raise HTTPException(409, "A blocked invoice has missing data - correct the data first")
    inv.resolution, inv.resolution_note = body.resolution, body.note
    inv.resolved_by, inv.resolved_at = user.username, datetime.now(timezone.utc)
    inv.approved_variance = body.approve_variance
    recheck_invoice(db, inv, actor=user.username, action="RESOLVED")
    audit(db, "RESOLUTION_SAVED", inv.id, {"resolution": body.resolution, "note": body.note,
                                           "approve_variance": body.approve_variance}, user.username)
    return {"queue": inv.queue, "status": inv.status}


@router.post("/{invoice_id}/push")
def push(invoice_id: int, db: Session = Depends(get_db), user: User = Depends(require("approver"))):
    """Hand the invoice to the MIRO robot (it picks status=MATCHED rows that have no miro_status)."""
    inv = _get(db, invoice_id)
    if inv.queue != "MATCHED" or inv.status != "MATCHED":
        raise HTTPException(409, "Only a fully matched invoice can be pushed to SAP MIRO")
    if inv.miro_status in ("POSTED", "PARKED", "IN_PROGRESS", "UNKNOWN"):
        raise HTTPException(409, f"Invoice is already {inv.miro_status} in SAP")
    inv.miro_status, inv.miro_message = None, None
    inv.pushed_by, inv.pushed_at = user.username, datetime.now(timezone.utc)
    audit(db, "PUSHED_TO_MIRO", inv.id, {}, user.username)
    return {"ok": True, "message": "Queued - the MIRO robot will pick it up on its next run"}


@router.get("/{invoice_id}/page/{page}.png")
def page_image(invoice_id: int, page: int, zoom: float = 1.6, db: Session = Depends(get_db),
               _: User = Depends(current_user)):
    inv = _get(db, invoice_id)
    path = pdfio.locate_pdf(inv.filename, inv.source_path)
    if path is None:
        raise HTTPException(404, "Original PDF not found on the server")
    if page < 1 or page > pdfio.page_count(path):
        raise HTTPException(404, "No such page")
    return Response(pdfio.render_page_png(path, page, min(max(zoom, 0.5), 3.0)), media_type="image/png",
                    headers={"Cache-Control": "private, max-age=3600"})


@router.get("/{invoice_id}/file")
def original(invoice_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    inv = _get(db, invoice_id)
    path = pdfio.locate_pdf(inv.filename, inv.source_path)
    if path is None:
        raise HTTPException(404, "Original PDF not found on the server")
    return FileResponse(Path(path), media_type="application/pdf", filename=inv.filename)
