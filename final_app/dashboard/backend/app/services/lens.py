"""'Lens' - the person draws a box on the digital invoice, we read the number inside it and offer it as PO or GRN.

The box is read twice: tesseract (printed digits, exact) and the vision model (handwriting). Every candidate is
classified (GRN = 10 digits starting 50, PO = 10 digits starting 51/61, service PO 81/82/83) and - for PO / GRN -
checked against SAP, so the person sees "found in SAP" BEFORE the value is put into the field.
"""
from __future__ import annotations

import re

from .textutil import clean_grn, clean_po, grn_is_valid, is_material_po, is_service_po, is_valid_po

_RUN = re.compile(r"\d(?:[\d \-/.]{4,}\d)")


def classify(v: str) -> str:
    if grn_is_valid(v):
        return "GRN"
    if is_material_po(v):
        return "PO"
    if is_valid_po(v) and is_service_po(v):
        return "SERVICE_PO"
    return "OTHER"


def numbers_in_text(text: str) -> list[str]:
    """Digit groups of 6+ digits. '5100 002448' and '51-00002448' are one number."""
    out = []
    for m in _RUN.finditer(text or ""):
        d = re.sub(r"\D", "", m.group(0))
        if len(d) >= 6:
            out.append(d)
    return out


def _fix(d: str) -> str:
    """Known handwriting / OCR repairs for KTFL numbers (9000... -> 5000..., 610000xxxxx -> 61000xxxxx)."""
    for fn in (clean_grn, clean_po):
        r = fn(d)
        if r and r != d and classify(r) in ("GRN", "PO"):
            return r
    return d


def build_candidates(ocr_text: str, ai_numbers: list[str]) -> list[dict]:
    """Merge the two readings. Valid GRN/PO first, then by agreement of both readers."""
    found: dict[str, dict] = {}
    for src, nums in (("OCR", numbers_in_text(ocr_text)), ("AI", [re.sub(r"\D", "", n) for n in ai_numbers])):
        for raw in nums:
            if len(raw) < 6:
                continue
            v = _fix(raw)
            c = found.setdefault(v, {"value": v, "kind": classify(v), "sources": []})
            if src not in c["sources"]:
                c["sources"].append(src)
            if v != raw:
                c["corrected_from"] = raw
    order = {"GRN": 0, "PO": 0, "SERVICE_PO": 1, "OTHER": 2}
    return sorted(found.values(), key=lambda c: (order[c["kind"]], -len(c["sources"])))


def sap_check(gw, header: dict, lines: list[dict], value: str, kind: str, sap_enrich) -> dict:
    """Does SAP know this PO / GRN? Uses the same enrichment as the dashboard's 'Re-check SAP' button."""
    if gw is None or kind not in ("GRN", "PO", "SERVICE_PO"):
        return {"checked": False}
    h = dict(header)
    if kind == "GRN":
        h["grn_number"] = value
    else:
        h["po_number"] = value
    try:
        sap_enrich(gw, h, [dict(l) for l in lines], allow_grn_switch=False)
    except Exception:
        return {"checked": False}
    found = bool(h.get("grn_in_sap")) if kind == "GRN" else bool(h.get("po_in_sap"))
    return {"checked": True, "in_sap": found, "po_number": h.get("po_number", ""), "grn_number": h.get("grn_number", ""),
            "plant_code": h.get("plant_code", ""), "vendor_code": h.get("sap_vendor_code") or h.get("vendor_code", ""),
            "po_date": h.get("po_date", ""), "grn_date": h.get("grn_date", "")}
