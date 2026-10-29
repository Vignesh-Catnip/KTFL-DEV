"""Error Flow, Auto Flow (SAP posting log), Audit trail, Jobs / uploads, stats and health."""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..models import AuditLog, Invoice, Job, User
from ..security import current_user, require
from ..services import vlm, worker
from ..services.persist import audit
from ..services.sap import sap_status
from .common import csv_response, invoice_row, iso

router = APIRouter(prefix="/api", tags=["flows"])


# ------------------------------------------------------------------ Error flow
@router.get("/exceptions")
def exceptions(category: str | None = None, db: Session = Depends(get_db), _: User = Depends(current_user)):
    """Every invoice that is not clean: with its open issues, line-level mismatches first."""
    rows = db.scalars(select(Invoice).where(Invoice.queue.in_(("RECONCILE", "BLOCKED")))
                      .order_by(Invoice.id.desc())).all()
    out = []
    for i in rows:
        r = invoice_row(i)
        r["issues"] = [x for x in (i.issues or []) if x["severity"] in ("BLOCK", "RECON")]
        r["bad_lines"] = [{"item": l.item, "quantity": l.quantity, "gr_quantity": l.gr_quantity,
                           "amount": l.amount, "gr_amount": l.gr_amount, "status": l.line_status}
                          for l in i.lines if l.line_status not in (None, "OK")]
        if category and r["exception_type"] != category:
            continue
        out.append(r)
    types: dict[str, int] = {}
    for r in out:
        types[r["exception_type"]] = types.get(r["exception_type"], 0) + 1
    return {"total": len(out), "by_type": types, "items": out}


# ------------------------------------------------------------------ Auto flow (robot results)
def _posting_query(status: str | None):
    stmt = select(Invoice).where(Invoice.queue == "MATCHED")
    if status == "WAITING":
        stmt = stmt.where(Invoice.miro_status.is_(None))
    elif status:
        stmt = stmt.where(Invoice.miro_status == status)
    return stmt.order_by(func.coalesce(Invoice.miro_updated_at, Invoice.pushed_at, Invoice.created_at).desc())


@router.get("/posting")
def posting(status: str | None = None, db: Session = Depends(get_db), _: User = Depends(current_user)):
    all_ = db.execute(select(Invoice.miro_status, func.count()).where(Invoice.queue == "MATCHED")
                      .group_by(Invoice.miro_status)).all()
    counts = {(s or "WAITING"): n for s, n in all_}
    items = [invoice_row(i) for i in db.scalars(_posting_query(status).limit(500))]
    return {"counts": counts, "items": items}


@router.get("/posting/export.csv")
def posting_csv(status: str | None = None, db: Session = Depends(get_db), _: User = Depends(current_user)):
    rows = [[i.id, i.invoice_number, i.vendor, i.po_number, i.grn_number, i.total, i.miro_status or "WAITING",
             i.miro_doc_number, i.miro_attempts, i.miro_message, iso(i.miro_updated_at)]
            for i in db.scalars(_posting_query(status))]
    return csv_response("sap_posting_log.csv", ["Id", "Invoice", "Vendor", "PO", "GRN", "Total", "SAP status",
                                                "SAP document", "Attempts", "Message", "Updated"], rows)


@router.post("/posting/{invoice_id}/requeue")
def requeue(invoice_id: int, db: Session = Depends(get_db), user: User = Depends(require("approver"))):
    inv = db.get(Invoice, invoice_id)
    if inv is None:
        raise HTTPException(404, "Invoice not found")
    if inv.miro_status not in ("FAILED", "REVIEW", "CHECKED"):
        raise HTTPException(409, f"Status {inv.miro_status or 'WAITING'} cannot be re-queued "
                                 "(POSTED / PARKED / UNKNOWN need a check in SAP first)")
    old = inv.miro_status
    inv.miro_status, inv.miro_attempts, inv.miro_message = None, 0, None
    audit(db, "MIRO_REQUEUED", inv.id, {"previous": old}, user.username)
    return {"ok": True}


# ------------------------------------------------------------------ audit
def _audit_stmt(action: str | None, invoice_id: int | None):
    stmt = select(AuditLog).order_by(AuditLog.id.desc())
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if invoice_id:
        stmt = stmt.where(AuditLog.invoice_id == invoice_id)
    return stmt


@router.get("/audit")
def audit_list(action: str | None = None, invoice_id: int | None = None, limit: int = 200,
               db: Session = Depends(get_db), _: User = Depends(current_user)):
    return [{"id": a.id, "invoice_id": a.invoice_id, "action": a.action, "actor": a.actor,
             "at": iso(a.created_at), "detail": a.detail}
            for a in db.scalars(_audit_stmt(action, invoice_id).limit(min(limit, 1000)))]


@router.get("/audit/export.csv")
def audit_csv(db: Session = Depends(get_db), _: User = Depends(current_user)):
    rows = [[a.id, iso(a.created_at), a.actor, a.action, a.invoice_id, a.detail]
            for a in db.scalars(_audit_stmt(None, None).limit(20000))]
    return csv_response("audit_trail.csv", ["Id", "Time", "User", "Action", "Invoice id", "Detail"], rows)


# ------------------------------------------------------------------ jobs / upload
@router.post("/jobs/upload", status_code=202)
async def upload(files: list[UploadFile] = File(...), db: Session = Depends(get_db),
                 user: User = Depends(require("approver"))):
    cfg = get_settings()
    if not cfg.allow_upload:
        raise HTTPException(403, "Browser upload is switched off - drop the PDF in the watch folder")
    tmp = Path(cfg.upload_dir) / "_incoming"
    tmp.mkdir(parents=True, exist_ok=True)
    jobs = []
    for f in files:
        name = Path(f.filename or "invoice.pdf").name
        data = await f.read()
        if not name.lower().endswith(".pdf") or not data.startswith(b"%PDF"):
            raise HTTPException(422, f"{name}: only PDF files are accepted")
        if len(data) > cfg.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, f"{name}: larger than {cfg.max_upload_mb} MB")
        p = tmp / f"{time.time_ns()}.pdf"
        p.write_bytes(data)
        job = worker.enqueue(db, p, name, "UPLOAD", user.username)
        p.unlink(missing_ok=True)
        jobs.append({"id": job.id, "filename": job.filename})
    return {"jobs": jobs}


@router.get("/jobs")
def jobs(limit: int = 100, db: Session = Depends(get_db), _: User = Depends(current_user)):
    return [{"id": j.id, "filename": j.filename, "status": j.status, "stage": j.stage, "source": j.source,
             "invoice_id": j.invoice_id, "queue": j.queue, "error": j.error, "by": j.created_by,
             "created_at": iso(j.created_at), "finished_at": iso(j.finished_at)}
            for j in db.scalars(select(Job).order_by(Job.id.desc()).limit(min(limit, 500)))]


@router.post("/jobs/{job_id}/retry")
def retry(job_id: int, db: Session = Depends(get_db), user: User = Depends(require("approver"))):
    j = db.get(Job, job_id)
    if j is None:
        raise HTTPException(404, "Job not found")
    if j.status != "FAILED":
        raise HTTPException(409, "Only failed jobs can be retried")
    j.status, j.error, j.stage = "QUEUED", None, None
    db.commit()
    worker._wake.set()
    return {"ok": True}


# ------------------------------------------------------------------ stats / health
@router.get("/stats")
def stats(db: Session = Depends(get_db), _: User = Depends(current_user)):
    q = dict(db.execute(select(Invoice.queue, func.count()).group_by(Invoice.queue)).all())
    m = {(k or "WAITING"): n for k, n in db.execute(
        select(Invoice.miro_status, func.count()).where(Invoice.queue == "MATCHED")
        .group_by(Invoice.miro_status)).all()}
    value = dict(db.execute(select(Invoice.queue, func.coalesce(func.sum(Invoice.total), 0))
                            .group_by(Invoice.queue)).all())
    plants = [{"plant": p or "-", "count": n} for p, n in db.execute(
        select(Invoice.plant_code, func.count()).group_by(Invoice.plant_code).order_by(func.count().desc()))]
    pending_jobs = db.scalar(select(func.count()).select_from(Job).where(Job.status.in_(("QUEUED", "RUNNING"))))
    return {"queues": {k: q.get(k, 0) for k in ("MATCHED", "RECONCILE", "BLOCKED")},
            "value": {k: round(float(value.get(k, 0)), 2) for k in ("MATCHED", "RECONCILE", "BLOCKED")},
            "posting": m, "plants": plants, "pending_jobs": pending_jobs,
            "total": sum(q.values()),
            "straight_through_pct": round(100 * q.get("MATCHED", 0) / max(1, sum(q.values())), 1)}


@router.get("/config")
def app_config(_: User = Depends(current_user)):
    cfg = get_settings()
    return {"allow_upload": cfg.allow_upload, "watch_folder": cfg.watch_folder or None,
            "poll_seconds": cfg.watcher_poll_seconds, "min_completeness": cfg.min_completeness,
            "worker_enabled": cfg.worker_enabled}


@router.get("/health")
def health():
    ok, detail = vlm.get_vlm().health() if hasattr(vlm.get_vlm(), "health") else (True, "n/a")
    return {"api": "ok", "sap": sap_status(), "ollama": {"ok": ok, "detail": detail}}
