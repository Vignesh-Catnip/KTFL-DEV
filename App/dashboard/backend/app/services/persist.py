"""Moving the working state (header dict + line dicts) in and out of the database."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AuditLog, Invoice, InvoiceLine, InvoiceMaster
from .normalize import new_header
from .textutil import norm_key

# header key -> Invoice column (same name unless listed)
_COLUMN_MAP = {"total_tax": "tax", "plant_code": "plant_code"}
_DIRECT = (
    "filename", "vendor", "invoice_number", "invoice_date", "po_number", "gstin", "hsn_sac", "vendor_code",
    "tax_code", "currency", "subtotal", "cgst", "sgst", "igst", "total", "grn_number",
    "handwritten_references", "plant_code", "plant_source", "po_date", "grn_date", "grn_amount", "po_list",
    "multi_po_flag", "red_flag_reason", "gst_percent", "gst_percent_component", "gst_nature", "gst_tax_code",
    "gst_tax_code_desc", "sap_tax_code", "tax_code_match", "validation_status", "validation_errors",
    "field_sources", "status", "queue", "issues", "company_code", "sap_hsn", "hsn_match", "variance_amount",
    "sap_checked", "completeness", "grn_in_sap", "sap_snapshot", "approved_variance",
)


def make_dedupe_key(h: dict) -> str | None:
    inv = norm_key(h.get("invoice_number"))
    if not inv:
        return None
    po = norm_key(h.get("po_number")) or norm_key(h.get("vendor"))
    return f"{inv}::{po}"


def find_duplicate(db: Session, h: dict, exclude_id: int | None = None) -> Invoice | None:
    key = make_dedupe_key(h)
    if not key:
        return None
    q = select(Invoice).where(Invoice.dedupe_key == key)
    if exclude_id:
        q = q.where(Invoice.id != exclude_id)
    return db.scalar(q)


def audit(db: Session, action: str, invoice_id: int | None = None, detail: dict | None = None,
          actor: str | None = None, commit: bool = True) -> AuditLog:
    row = AuditLog(invoice_id=invoice_id, action=action, detail=detail or {}, actor=actor)
    db.add(row)
    if commit:
        db.commit()
    return row


def apply_state(inv: Invoice, h: dict) -> None:
    """Copy the working header onto the ORM row."""
    for key in _DIRECT:
        if key in h:
            setattr(inv, key, h[key])
    inv.tax = h.get("total_tax", inv.tax or 0)
    inv.business_place = h.get("plant_code") or h.get("sap_plant") or inv.business_place   # posting plant for the robot
    inv.confidence = (h.get("confidence") or 0) / 100
    inv.dedupe_key = make_dedupe_key(h)
    for k in ("po_number", "vendor_code", "plant_code", "hsn_sac", "po_date", "grn_date", "tax_code",
              "gst_tax_code", "sap_tax_code", "gst_nature", "sap_hsn", "company_code"):
        if getattr(inv, k) == "":
            setattr(inv, k, None)
    problems = [i["message"] for i in (h.get("issues") or []) if i.get("severity") in ("BLOCK", "RECON")]
    inv.error_message = "; ".join(problems) or None


def sync_lines(inv: Invoice, lines: list[dict]) -> None:
    """Make inv.lines equal `lines`, re-using existing rows (by position) so line ids stay stable."""
    existing = list(inv.lines)
    for i, l in enumerate(lines):
        row = existing[i] if i < len(existing) else None
        if row is None:
            row = InvoiceLine(item="")
            inv.lines.append(row)
        row.item = (l.get("item") or "")[:255]
        row.quantity = l.get("quantity") or 0
        row.rate = l.get("rate") or 0
        row.amount = l.get("amount") or 0
        row.hsn_sac = l.get("hsn_sac") or None
        row.po_number = l.get("po_number") or None
        row.po_item = l.get("po_item") or None
        row.plant_code = l.get("plant_code") or inv.plant_code
        row.uom = l.get("uom") or None
        row.source = l.get("source") or "VISION"
        row.tax_code = inv.gst_tax_code or ""
        row.gr_quantity = l.get("gr_quantity")
        row.gr_amount = l.get("gr_amount")
        row.gr_row = l.get("gr_row")
        row.line_status = l.get("line_status")
    for row in existing[len(lines):]:
        inv.lines.remove(row)


def save_new_invoice(db: Session, h: dict, lines: list[dict], source_path: str | None = None,
                     page_count: int | None = None) -> Invoice:
    inv = Invoice(filename=h.get("filename") or "")
    apply_state(inv, h)
    inv.source_path = source_path
    inv.page_count = page_count
    inv.created_at = datetime.now(timezone.utc)
    db.add(inv)
    db.flush()
    sync_lines(inv, lines)
    if h.get("invoice_number"):
        exists = db.scalar(select(InvoiceMaster).where(InvoiceMaster.invoice_number == h["invoice_number"]))
        if exists is None:
            db.add(InvoiceMaster(invoice_number=h["invoice_number"], vendor=h.get("vendor"),
                                 vendor_code=h.get("vendor_code"), po_number=h.get("po_number"),
                                 invoice_id=inv.id))
    db.flush()
    return inv


def header_from_invoice(inv: Invoice) -> tuple[dict, list[dict]]:
    """Rebuild the working state from a stored invoice (used to re-check against SAP after an edit)."""
    h = new_header(inv.filename)
    h.update({
        "vendor": inv.vendor or "", "invoice_number": inv.invoice_number or "",
        "invoice_date": inv.invoice_date or "", "po_number": inv.po_number or "", "gstin": inv.gstin or "",
        "hsn_sac": inv.hsn_sac or "", "vendor_code": inv.vendor_code or "",
        "plant_code": inv.plant_code or "", "plant_source": inv.plant_source or "",
        "company_code": inv.company_code or "", "currency": inv.currency or "INR",
        "subtotal": inv.subtotal or 0.0, "cgst": inv.cgst or 0.0, "sgst": inv.sgst or 0.0,
        "igst": inv.igst or 0.0, "total_tax": inv.tax or 0.0, "total": inv.total or 0.0,
        "grn_number": inv.grn_number or "", "handwritten_references": inv.handwritten_references or "",
        "confidence": round((inv.confidence or 0) * 100, 1), "tax_code": inv.tax_code or "",
        "field_sources": dict(inv.field_sources or {}), "approved_variance": bool(inv.approved_variance),
        "po_numbers_printed": [],
    })
    lines = [{
        "item": l.item, "quantity": l.quantity or 0.0, "uom": l.uom or "", "rate": l.rate or 0.0,
        "hsn_sac": l.hsn_sac or "", "amount": l.amount or 0.0, "po_number": l.po_number or "",
        "po_item": l.po_item or "", "plant_code": l.plant_code or "", "source": l.source or "VISION",
    } for l in inv.lines]
    return h, lines
