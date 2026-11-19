"""Turn the vision model's JSON into the working 'header' dict + line list used by every later step."""
from __future__ import annotations

import re

from ..config import get_settings
from .textutil import (clean, clean_po, fix_amounts, fix_gstin, repair_gstin, split_double_gstin,
                       format_date, is_valid_po, to_float)


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
        "approved_variance": False, "unread": [], "possible_duplicate_lines": [], "page_info": {},
        "parse_errors": {}, "raw_values": {}, "round_off": 0.0, "other_charges": 0.0, "due_date": "",
    }


def flag_duplicate_lines(lines: list[dict]) -> list[tuple[int, int]]:
    """Identical printed rows are NEVER removed (two identical rows can be two real entries). They are only listed so
    validate() can ask a person to confirm."""
    seen: dict[tuple, int] = {}
    pairs: list[tuple[int, int]] = []
    for i, l in enumerate(lines):
        key = ((l.get("item") or "").casefold(), round(float(l.get("amount") or 0), 2),
               round(float(l.get("quantity") or 0), 3))
        if key in seen:
            pairs.append((seen[key] + 1, i + 1))
        else:
            seen[key] = i
    return pairs


def normalize(data: dict, filename: str):
    """Model/adapted JSON -> (header, lines). Approved rules: nothing is calculated, filled, merged or copied here.
    An unreadable amount keeps value 0.0 ONLY because the stored columns are NOT NULL; it is recorded in
    h['unread'] (and persisted as field_sources[...] = 'UNREAD') so every later step treats it as null."""
    cfg = get_settings()
    data = data if isinstance(data, dict) else {}
    unread = set(data.get("unread") or [])
    legacy = "unread" not in data                 # older callers / fakes hand over plain numbers: 0 means 0

    def num(key):
        v = data.get(key)
        if v is None:
            unread.add(key)
            return 0.0
        return float(v)

    sub, cgst, sgst, igst, tax, total = (num(k) for k in ("subtotal", "cgst", "sgst", "igst", "total_tax", "total"))
    if legacy:
        unread.clear()
    # taxes: a tax row that is not printed is "not charged" only when another tax is readable (IGST-only / CGST+SGST
    # only invoices). All three unreadable -> the tax is unread.
    if all(k in unread for k in ("cgst", "sgst", "igst")):
        unread.add("total_tax")
    else:
        unread.discard("total_tax")
        unread -= {"cgst", "sgst", "igst"}

    hsn_inv = re.sub(r"[^\d]", "", clean(data.get("hsn_sac")))
    lines = []
    for l in data.get("lines") or []:
        if not isinstance(l, dict):
            continue
        item = clean(l.get("item") or l.get("description"))
        lu = list(l.get("unread") or [])
        amt = l.get("amount")
        qty = l.get("quantity")
        rate = l.get("rate")
        if not item and (amt in (None, 0, 0.0)) and qty in (None, 0, 0.0):
            continue                                # an entirely empty object is not a printed row
        lines.append({"item": item, "quantity": float(qty) if qty is not None else 0.0, "uom": clean(l.get("uom")),
                      "rate": float(rate) if rate is not None else 0.0,
                      "hsn_sac": re.sub(r"[^\d]", "", clean(l.get("hsn_sac"))),      # the row's OWN HSN only
                      "amount": float(amt) if amt is not None else 0.0, "po_number": "", "po_item": "",
                      "plant_code": "", "source": "VISION", "unread": lu,
                      "page": l.get("page"), "line_no": clean(l.get("line_no")),
                      "material_code": clean(l.get("material_code")),
                      "discount": l.get("discount"), "raw_text": clean(l.get("raw_text"))})

    # repeated rows are NEVER removed; identical consecutive-or-not rows are only flagged for a person
    dup_pairs = flag_duplicate_lines(lines)

    hw = data.get("handwritten_references") or ""
    if isinstance(hw, list):
        hw = ", ".join(map(str, hw))

    gstin_raw = clean(data.get("gstin")).upper().replace(" ", "")
    gstin = repair_gstin(fix_gstin(split_double_gstin(gstin_raw)))
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
        "round_off": data.get("round_off") if data.get("round_off") is not None else 0.0,
        "other_charges": data.get("other_charges") if data.get("other_charges") is not None else 0.0,
        "gst_rate_printed": float(data.get("gst_rate_percent") or 0.0),
        "unread": sorted(unread), "possible_duplicate_lines": dup_pairs,
        "page_info": data.get("_page_info") or {}, "parse_errors": data.get("parse_errors") or {},
        "raw_values": data.get("raw_values") or {}, "due_date": clean(data.get("due_date")),
        "handwritten_references": clean(hw),
        "confidence": round(max(0, min(1, to_float(data.get("confidence")))) * 100, 1),
    })
    if h["plant_code"]:
        h["plant_source"] = "VISION"
    h["field_sources"] = {
        k: "VISION" for k, v in h.items()
        if k in ("vendor", "invoice_number", "invoice_date", "po_number", "gstin", "hsn_sac",
                 "vendor_code", "plant_code", "currency", "subtotal", "cgst", "sgst", "igst",
                 "total_tax", "total") and v not in ("", None, [], False, {}) and k not in unread
        and not (v in (0, 0.0) and k not in ("cgst", "sgst", "igst", "total_tax"))}
    for k in unread:
        if k in ("subtotal", "cgst", "sgst", "igst", "total_tax", "total"):
            h["field_sources"][k] = "UNREAD"
    return h, lines
    return h, lines
