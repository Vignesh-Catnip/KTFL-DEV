from __future__ import annotations

import csv
import io

from datetime import datetime, timedelta, timezone

from fastapi import Response
from sqlalchemy import select

from ..models import AuditLog, Invoice
from ..services.validate import exception_type


def iso(d):
    return d.isoformat() if d else None


def invoice_row(i: Invoice) -> dict:
    open_ = [x for x in (i.issues or []) if x["severity"] in ("BLOCK", "RECON")]
    return {
        "id": i.id, "filename": i.filename, "vendor": i.vendor, "vendor_code": i.vendor_code,
        "invoice_number": i.invoice_number, "invoice_date": i.invoice_date, "po_number": i.po_number,
        "grn_number": i.grn_number, "plant_code": i.plant_code, "subtotal": i.subtotal, "total": i.total,
        "gst_tax_code": i.gst_tax_code, "gst_percent": i.gst_percent, "hsn_sac": i.hsn_sac, "sap_hsn": i.sap_hsn,
        "queue": i.queue, "status": i.status, "exception_type": exception_type(i.issues or []),
        "issue_count": len(open_), "top_issue": open_[0]["message"] if open_ else "",
        "variance_amount": i.variance_amount, "completeness": i.completeness, "multi_po_flag": bool(i.multi_po_flag),
        "resolution": i.resolution, "approved_variance": bool(i.approved_variance),
        "miro_status": i.miro_status, "miro_doc_number": i.miro_doc_number, "miro_message": i.miro_message,
        "miro_attempts": i.miro_attempts, "miro_updated_at": iso(i.miro_updated_at),
        "created_at": iso(i.created_at), "pushed_at": iso(i.pushed_at), "pushed_by": i.pushed_by,
    }


def csv_response(name: str, header: list[str], rows: list[list]) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(["" if v is None else str(v).replace("\n", " ") for v in r])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _aware(d: datetime | None) -> datetime | None:
    return d.replace(tzinfo=timezone.utc) if d is not None and d.tzinfo is None else d


def apply_effective(db, invs: list[Invoice], rows: list[dict]) -> None:
    """The robot in CHECK mode (dry run) never writes invoices.miro_status - its result is only in audit_logs.
    Show that result (status CHECKED / REVIEW / FAILED, flagged dry_run) until the invoice is changed again."""
    ids = [i.id for i in invs if not i.miro_status]
    if not ids:
        return
    latest: dict[int, AuditLog] = {}
    for a in db.scalars(select(AuditLog).where(AuditLog.invoice_id.in_(ids), AuditLog.action.like("MIRO_CHECK_%"))
                        .order_by(AuditLog.id.desc())):
        latest.setdefault(a.invoice_id, a)
    for inv, row in zip(invs, rows):
        a = latest.get(inv.id)
        if a is None or inv.miro_status:
            continue
        if _aware(inv.updated_at) and _aware(a.created_at) and _aware(inv.updated_at) > _aware(a.created_at) + timedelta(seconds=3):
            continue                                            # invoice changed after the check -> stale
        st = a.action.replace("MIRO_CHECK_", "")
        row["miro_status"] = "CHECKED" if st == "OK" else st
        row["miro_message"] = (a.detail or {}).get("message")
        row["miro_updated_at"] = iso(a.created_at)
        row["dry_run"] = True
