from __future__ import annotations

import csv
import io

from fastapi import Response

from ..models import Invoice
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
