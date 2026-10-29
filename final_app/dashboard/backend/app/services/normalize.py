"""Turn the vision model's JSON into the working 'header' dict + line list used by every later step."""
from __future__ import annotations

import re

from ..config import get_settings
from .textutil import (clean, clean_po, fix_amounts, fix_gstin, format_date, is_valid_po, to_float)


def new_header(filename: str = "") -> dict:
    """Every key the pipeline reads, with safe defaults (so SAP / validation never hit KeyError)."""
    return {
        "filename": filename, "vendor": "", "invoice_number": "", "invoice_date": "",
        "po_number": "", "po_numbers_printed": [], "gstin": "", "hsn_sac": "", "vendor_code": "",
        "plant_code": "", "plant_source": "", "company_code": "", "currency": "INR",
        "subtotal": 0.0, "cgst": 0.0, "sgst": 0.0, "igst": 0.0, "total_tax": 0.0, "total": 0.0,
        "gst_rate_printed": 0.0, "grn_number": "", "handwritten_references": "", "confidence": 0.0,
        "tax_code": "", "po_date": "", "grn_date": "", "grn_amount": None,
        "po_list": "", "multi_po_flag": False, "red_flag_reason": "",
        "gst_percent": None, "gst_percent_component": None, "gst_nature": "",
        "gst_tax_code": "", "gst_tax_code_desc": "", "sap_tax_code": "", "tax_code_match": None,
        "sap_hsn": "", "hsn_match": None,
        "completeness": None, "sap_checked": False, "grn_in_sap": False, "po_in_sap": False,
        "sap_mismatches": [], "line_mismatches": [],
        "field_sources": {}, "sap_snapshot": {}, "variance_amount": None,
        "approved_variance": False,
    }


def normalize(data: dict, filename: str):
    """Vision JSON -> (header, lines). HSN is kept ONLY if the invoice itself prints it."""
    cfg = get_settings()
    data = data if isinstance(data, dict) else {}
    sub, cgst, sgst, igst, tax, total = fix_amounts(
        to_float(data.get("subtotal")), to_float(data.get("cgst")), to_float(data.get("sgst")),
        to_float(data.get("igst")), to_float(data.get("total_tax")), to_float(data.get("total")))

    hsn_inv = re.sub(r"[^\d]", "", clean(data.get("hsn_sac")))
    seen, lines = set(), []
    for l in data.get("lines") or []:
        if not isinstance(l, dict):
            continue
        item = clean(l.get("item") or l.get("description"))
        amt = to_float(l.get("amount"))
        if not item:
            continue
        key = (item.lower()[:40], round(amt))
        if key in seen:
            continue
        seen.add(key)
        lines.append({"item": item, "quantity": to_float(l.get("quantity")), "uom": clean(l.get("uom")),
                      "rate": to_float(l.get("rate")),
                      "hsn_sac": re.sub(r"[^\d]", "", clean(l.get("hsn_sac"))) or hsn_inv,
                      "amount": amt, "po_number": "", "po_item": "", "plant_code": "",
                      "source": "VISION"})

    hw = data.get("handwritten_references") or ""
    if isinstance(hw, list):
        hw = ", ".join(map(str, hw))

    gstin = fix_gstin(clean(data.get("gstin")).upper().replace(" ", ""))
    if any(gstin.startswith(p) for p in cfg.gstin_prefixes):
        gstin = ""                                         # the buyer's own GSTIN is never the vendor's

    po_all = [clean_po(p) for p in re.split(r"[,;/\s]+", clean(data.get("po_numbers_all"))) if p]

    h = new_header(filename)
    h.update({
        "vendor": clean(data.get("vendor")),
        "invoice_number": clean(data.get("invoice_number")),
        "invoice_date": format_date(clean(data.get("invoice_date"))),
        "po_number": clean_po(data.get("po_number")),
        "po_numbers_printed": sorted({p for p in po_all if is_valid_po(p)}),
        "gstin": gstin, "hsn_sac": hsn_inv,
        "vendor_code": clean(data.get("vendor_code")),
        "plant_code": re.sub(r"[^\w]", "", clean(data.get("plant_hint")))[:20],
        "currency": clean(data.get("currency")) or "INR",
        "subtotal": round(sub, 2), "cgst": round(cgst, 2), "sgst": round(sgst, 2),
        "igst": round(igst, 2), "total_tax": round(tax, 2), "total": round(total, 2),
        "gst_rate_printed": to_float(data.get("gst_rate_percent")),
        "handwritten_references": clean(hw),
        "confidence": round(max(0, min(1, to_float(data.get("confidence")))) * 100, 1),
    })
    if h["plant_code"]:
        h["plant_source"] = "VISION"
    h["field_sources"] = {
        k: "VISION" for k, v in h.items()
        if k in ("vendor", "invoice_number", "invoice_date", "po_number", "gstin", "hsn_sac",
                 "vendor_code", "plant_code", "currency", "subtotal", "cgst", "sgst", "igst",
                 "total_tax", "total") and v not in ("", None, 0, 0.0, [], False, {})}
    return h, lines
