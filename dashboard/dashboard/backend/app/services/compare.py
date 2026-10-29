"""Builds the 3-way comparison (invoice / GRN / PO) shown on the reconciliation screen."""
from __future__ import annotations

from ..models import Invoice
from .textutil import norm_hsn, to_float
from .validate import exception_type, hsn_agree


def _row(field: str, label: str, inv, grn, po, status: str, note: str = "") -> dict:
    return {"field": field, "label": label, "invoice": "" if inv is None else str(inv),
            "grn": "" if grn is None else str(grn), "po": "" if po is None else str(po),
            "status": status, "note": note}


def build_compare(inv: Invoice) -> dict:
    snap = inv.sap_snapshot or {}
    po_hdr = snap.get("po") or {}
    po_items = snap.get("po_items") or []
    gr_rows = snap.get("gr_rows") or []
    grn_plant = next((str(r.get("WERKS")).strip() for r in gr_rows if r.get("WERKS")), "")
    po_plant = next((str(r.get("WERKS")).strip() for r in po_items if r.get("WERKS")), "")
    po_net = round(sum(to_float(r.get("NETWR")) for r in po_items), 2) or None
    grn_val = inv.grn_amount
    open_codes = {i["code"]: i for i in (inv.issues or []) if i["severity"] in ("BLOCK", "RECON")}
    bad_fields = {i["field"] for i in open_codes.values() if i.get("field")}

    def st(field, has_sap=True):
        if field in bad_fields:
            return "MISSING" if any(i["severity"] == "BLOCK" and i["field"] == field for i in open_codes.values()) else "MISMATCH"
        return "OK" if has_sap else "NA"

    inv_hsn = norm_hsn(inv.hsn_sac)
    sap_hsn = inv.sap_hsn or ""
    hsn_state = ("MISSING" if not inv_hsn else "MISSING" if not sap_hsn and inv.sap_checked
                 else "MISMATCH" if inv.hsn_match is False else "OK")
    rows = [
        _row("vendor_code", "Vendor code", inv.vendor_code, str(po_hdr.get("LIFNR", "")).lstrip("0") or None,
             str(po_hdr.get("LIFNR", "")).lstrip("0") or None, st("vendor_code")),
        _row("po_number", "PO number", inv.po_number, snap.get("po") and po_hdr.get("EBELN"), po_hdr.get("EBELN"),
             st("po_number")),
        _row("grn_number", "GRN number", inv.grn_number, snap.get("grn") if inv.grn_in_sap else "",
             "", st("grn_number")),
        _row("plant_code", "Plant", inv.plant_code, grn_plant, po_plant, st("plant_code")),
        _row("subtotal", "Taxable value", f"{inv.subtotal:,.2f}", f"{grn_val:,.2f}" if grn_val else "",
             f"{po_net:,.2f}" if po_net else "", st("subtotal")),
        _row("gst_tax_code", "GST tax code", f"{inv.gst_tax_code or ''} ({inv.gst_percent or 0:g}%)",
             inv.sap_tax_code, inv.sap_tax_code, st("gst_tax_code")),
        _row("hsn_sac", "HSN / SAC", inv_hsn, "", sap_hsn, hsn_state, "HSN is read from the invoice only"),
        _row("invoice_date", "Invoice date", inv.invoice_date, inv.grn_date, inv.po_date, st("invoice_date"),
             "second column = GRN date, third = PO date"),
    ]
    lines = [{
        "id": l.id, "item": l.item, "quantity": l.quantity, "uom": l.uom, "rate": l.rate, "amount": l.amount,
        "hsn_sac": l.hsn_sac, "gr_quantity": l.gr_quantity, "gr_amount": l.gr_amount, "gr_row": l.gr_row,
        "line_status": l.line_status or "NA",
    } for l in inv.lines]
    return {"rows": rows, "lines": lines,
            "grn_rows": gr_rows, "po_items": po_items,
            "exception_type": exception_type(inv.issues or [])}
