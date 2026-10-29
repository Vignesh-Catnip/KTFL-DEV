"""
Decides which dashboard queue an invoice belongs to.

    BLOCKED    mandatory data is missing, or SAP cannot be matched at all
               (no invoice no / date / vendor / PO / GRN / plant / GST tax code, PO or GRN not in SAP,
               invoice linked to several POs)
    RECONCILE  every field is present but something does not agree with SAP
               (line quantity / amount, GRN value, tax code, HSN, arithmetic ...)
    MATCHED    everything present and everything agrees with SAP  -> robot posts it automatically

HSN rule: the invoice's own HSN is the only HSN ever stored. SAP's HSN is compared with it:
    not printed on the invoice -> RECONCILE   not in SAP -> RECONCILE   different -> RECONCILE
"""
from __future__ import annotations

import re

from ..config import get_settings
from ..models import (QUEUE_BLOCKED, QUEUE_MATCHED, QUEUE_RECONCILE, STATUS_MANUAL, STATUS_MATCHED,
                      STATUS_REVIEW)
from .textutil import (GSTIN_RE, GST_STANDARD_RATES, grn_is_valid, is_valid_po, norm_hsn)

BLOCK, RECON, INFO, WAIVED = "BLOCK", "RECON", "INFO", "WAIVED"

MANDATORY_FIELDS = ("invoice_number", "invoice_date", "vendor", "po_number", "grn_number", "subtotal", "total",
                    "gst_tax_code", "gstin", "hsn_sac")      # 10 fields that must come from the invoice itself
FIELD_MISSING_CODES = {"MISSING_INVOICE_NUMBER", "MISSING_INVOICE_DATE", "MISSING_VENDOR", "MISSING_PO", "MISSING_GRN",
                       "INVALID_GRN", "MISSING_TAXABLE_VALUE", "MISSING_TOTAL", "MISSING_GST_TAX_CODE", "MISSING_GSTIN"}

# category -> label used by the Error-flow screen
CATEGORY_LABEL = {
    "MISSING": "Data missing", "QUANTITY": "Quantity mismatch", "PRICE": "Price / amount variance",
    "TAX": "Tax code", "HSN": "HSN / SAC", "SAP": "SAP match", "DATA": "Invoice data",
}


def issue(code: str, severity: str, category: str, message: str, field: str = "",
          invoice_value="", sap_value="", variance: float | None = None, **extra) -> dict:
    d = {"code": code, "severity": severity, "category": category, "field": field,
         "message": message, "invoice_value": "" if invoice_value is None else str(invoice_value),
         "sap_value": "" if sap_value is None else str(sap_value), "variance": variance}
    d.update(extra)
    return d


def hsn_agree(a: str, b: str) -> bool:
    """4 / 6 / 8 digit codes of the same family agree (7228 == 72284000). Needs >= 4 common digits."""
    a, b = norm_hsn(a), norm_hsn(b)
    n = min(len(a), len(b))
    return n >= 4 and a[:n] == b[:n]


def check_hsn(h: dict, lines: list[dict]) -> list[dict]:
    out: list[dict] = []
    inv = {norm_hsn(h.get("hsn_sac"))} | {norm_hsn(l.get("hsn_sac")) for l in lines}
    inv.discard("")
    sap = [x.strip() for x in (h.get("sap_hsn") or "").split(",") if x.strip()]
    h["hsn_match"] = None
    if not inv:
        out.append(issue("HSN_MISSING_INVOICE", RECON, "HSN",
                         "HSN/SAC is not printed on the invoice (it is never taken from SAP)",
                         "hsn_sac", "", ", ".join(sap)))
        return out
    if not h.get("sap_checked") or not h.get("po_in_sap"):
        return out                                           # nothing to compare with yet
    if not sap:
        h["hsn_match"] = False
        out.append(issue("HSN_MISSING_SAP", RECON, "HSN",
                         "HSN/SAC is not maintained for this PO in SAP (EKPO.STAWN is empty)",
                         "hsn_sac", ", ".join(sorted(inv)), ""))
        return out
    bad = sorted(x for x in inv if not any(hsn_agree(x, s) for s in sap))
    h["hsn_match"] = not bad
    if bad:
        out.append(issue("HSN_MISMATCH", RECON, "HSN",
                         f"HSN {', '.join(bad)} on invoice does not match SAP HSN {', '.join(sap)}",
                         "hsn_sac", ", ".join(bad), ", ".join(sap)))
    return out


def validate(h: dict, lines: list[dict]) -> dict:
    """Fills h['queue'], h['status'], h['issues'], h['validation_status'], h['validation_errors'],
    h['variance_amount']. Returns h."""
    cfg = get_settings()
    iss: list[dict] = []
    sap_on = bool(h.get("sap_checked"))

    # ---------------------------------------------------------------- mandatory data -> BLOCKED
    def need(key: str, label: str, code: str):
        if not h.get(key):
            iss.append(issue(code, BLOCK, "MISSING", f"{label} is missing", key))

    need("invoice_number", "Invoice number", "MISSING_INVOICE_NUMBER")
    need("invoice_date", "Invoice date", "MISSING_INVOICE_DATE")
    need("vendor", "Vendor name", "MISSING_VENDOR")
    need("gstin", "Vendor GSTIN", "MISSING_GSTIN")
    if not is_valid_po(h.get("po_number", "")):
        iss.append(issue("MISSING_PO", BLOCK, "MISSING",
                         "PO number is missing" if not h.get("po_number")
                         else f"PO '{h['po_number']}' is not a valid 10-digit number", "po_number",
                         h.get("po_number", "")))
    if not h.get("grn_number"):
        iss.append(issue("MISSING_GRN", BLOCK, "MISSING", "GRN number is missing", "grn_number"))
    elif not grn_is_valid(h["grn_number"]):
        iss.append(issue("INVALID_GRN", BLOCK, "MISSING",
                         f"GRN '{h['grn_number']}' is not a valid 10-digit number starting with 50",
                         "grn_number", h["grn_number"]))
    if h.get("subtotal", 0) <= 0:
        iss.append(issue("MISSING_TAXABLE_VALUE", BLOCK, "MISSING", "Taxable value is zero", "subtotal"))
    if h.get("total", 0) <= 0:
        iss.append(issue("MISSING_TOTAL", BLOCK, "MISSING", "Invoice total is zero", "total"))
    if not h.get("gst_tax_code"):
        iss.append(issue("MISSING_GST_TAX_CODE", BLOCK, "MISSING",
                         "GST tax code could not be determined from the invoice GST %", "gst_tax_code"))
    # plant code comes from SQL (GRN / PO plant) - not demanded from the invoice and not part of the % rule
    # MIRO document type (Details tab) is the document type of that plant - a person chooses when it is not unique
    if sap_on and h.get("plant_code") and not h.get("doc_type"):
        opts = h.get("doc_type_options") or []
        if opts:
            iss.append(issue("DOC_TYPE_CHOOSE", RECON, "DATA", f"Plant {h['plant_code']} has several document types "
                             f"({', '.join(o['doc_type'] + ' ' + (o['description'] or '') for o in opts)}) - choose one",
                             "doc_type", "", ", ".join(o["doc_type"] for o in opts)))
        else:
            iss.append(issue("NO_DOC_TYPE", RECON, "DATA", f"No MIRO document type for plant {h['plant_code']} in the "
                             f"master data - add it under Document types", "doc_type"))

    # ---------------------------------------------------------------- SAP availability
    if not sap_on:
        iss.append(issue("SAP_NOT_CHECKED", RECON, "SAP",
                         "SAP could not be reached - the invoice has not been cross-checked yet"))
    else:
        if h.get("po_number") and is_valid_po(h["po_number"]) and not h.get("po_in_sap"):
            iss.append(issue("PO_NOT_IN_SAP", BLOCK, "MISSING",
                             f"PO {h['po_number']} not found in SAP (EKKO)", "po_number", h["po_number"]))
        if h.get("grn_number") and not h.get("grn_in_sap"):
            iss.append(issue("GRN_NOT_IN_SAP", BLOCK, "MISSING",
                             f"GRN {h['grn_number']} not found in SAP (MSEG) by stamp, invoice no, PO or lines",
                             "grn_number", h["grn_number"]))
    if h.get("multi_po_flag"):
        iss.append(issue("MULTI_PO", INFO, "SAP", h.get("red_flag_reason") or "Invoice linked to several POs",
                         "po_number", h.get("po_list", "")))

    # ---------------------------------------------------------------- values vs SAP -> RECONCILE
    for m in h.get("sap_mismatches", []):
        iss.append(issue(m.get("code", "FIELD_MISMATCH"), RECON, "SAP", m["message"], m.get("field", ""),
                         m.get("invoice_value", ""), m.get("sap_value", "")))

    variance_total = 0.0
    for m in h.get("line_mismatches", []):
        cat = "QUANTITY" if m["code"] in ("QTY_MISMATCH", "LINE_NOT_IN_GR") else "PRICE"
        iss.append(issue(m["code"], RECON, cat, m["message"], "lines", m.get("invoice_value", ""),
                         m.get("sap_value", ""), m.get("variance"), line_index=m.get("line_index")))
        variance_total += abs(to_num(m.get("variance")))

    sub, tax, total = h.get("subtotal", 0.0), h.get("total_tax", 0.0), h.get("total", 0.0)
    grn_amt = h.get("grn_amount")
    if grn_amt and sub and abs(grn_amt - sub) > max(cfg.round_tolerance, sub * cfg.amount_tolerance):
        iss.append(issue("GRN_VALUE_MISMATCH", RECON, "PRICE",
                         f"Invoice taxable value {sub:,.2f} vs SAP goods-receipt value {grn_amt:,.2f}",
                         "subtotal", f"{sub:,.2f}", f"{grn_amt:,.2f}", round(sub - grn_amt, 2)))
        if not h.get("line_mismatches"):
            variance_total = abs(sub - grn_amt)
    if h.get("tax_code_match") is False:
        iss.append(issue("TAX_CODE_MISMATCH", RECON, "TAX",
                         f"Invoice GST {h.get('gst_percent')}% maps to tax code {h['gst_tax_code']} but "
                         f"SAP has {h['sap_tax_code']}", "gst_tax_code", h["gst_tax_code"], h["sap_tax_code"]))

    iss += check_hsn(h, lines)

    # ---------------------------------------------------------------- arithmetic on the invoice
    c, s, g = h.get("cgst", 0.0), h.get("sgst", 0.0), h.get("igst", 0.0)
    rt = cfg.round_tolerance
    if (c + s + g) > 0 and abs((c + s + g) - tax) > rt:
        iss.append(issue("TAX_SPLIT_MISMATCH", RECON, "DATA",
                         f"CGST+SGST+IGST {round(c + s + g, 2)} differs from total tax {tax}", "total_tax"))
    if sub > 0 and total > 0 and abs((sub + tax) - total) > rt:
        iss.append(issue("TOTAL_MISMATCH", RECON, "DATA",
                         f"Taxable {sub} + tax {tax} differs from invoice total {total}", "total"))
    if c > 0 and abs(c - s) > 0.5:
        iss.append(issue("CGST_SGST_DIFFER", RECON, "DATA", f"CGST {c} differs from SGST {s}", "cgst"))
    if c > 0 and g > 0:
        iss.append(issue("MIXED_GST", RECON, "DATA", "Both CGST/SGST and IGST charged on one invoice", "igst"))
    if h.get("gst_percent") is not None and h["gst_percent"] not in GST_STANDARD_RATES:
        iss.append(issue("NON_STANDARD_GST", RECON, "TAX", f"Non-standard GST rate {h['gst_percent']}%",
                         "gst_percent", h["gst_percent"]))
    if h.get("invoice_date") and not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", h["invoice_date"]):
        iss.append(issue("DATE_UNPARSED", RECON, "DATA", f"Invoice date '{h['invoice_date']}' not understood",
                         "invoice_date", h["invoice_date"]))
    if h.get("gstin") and not GSTIN_RE.match(h["gstin"]):
        iss.append(issue("GSTIN_FORMAT", RECON, "DATA", f"Vendor GSTIN '{h['gstin']}' looks wrong", "gstin",
                         h["gstin"]))
    if not lines:
        iss.append(issue("NO_LINES", RECON, "DATA", "No line items could be read from the invoice", "lines"))
    else:
        line_sum = round(sum(l.get("amount", 0) or 0 for l in lines), 2)
        if sub > 0 and abs(line_sum - sub) > max(rt, sub * cfg.amount_tolerance):
            iss.append(issue("LINES_TOTAL_MISMATCH", RECON, "DATA",
                             f"Line items add up to {line_sum:,.2f} but taxable value is {sub:,.2f}", "lines"))
    if h.get("confidence") and h["confidence"] < 60:
        iss.append(issue("LOW_CONFIDENCE", RECON, "DATA", f"Low AI confidence ({h['confidence']}%)"))

    # ---------------------------------------------------------------- an approved deviation waives RECON
    if h.get("approved_variance"):
        for i in iss:
            if i["severity"] == RECON and i["category"] != "MISSING":     # missing data can never be waived
                i["severity"] = WAIVED

    # ---------------------------------------------------------------- how much of the invoice was read?
    # 10 mandatory fields. Everything read but a few gaps -> a person completes it in RECONCILE;
    # too much missing (< min_completeness) -> BLOCKED. "Not in SAP" and multi-PO always stay BLOCKED.
    missing = [i for i in iss if i["severity"] == BLOCK and i["code"] in FIELD_MISSING_CODES]
    gone = {i["field"] for i in missing}
    if not norm_hsn(h.get("hsn_sac")) and not any(norm_hsn(l.get("hsn_sac")) for l in (h.get("lines") or [])):
        gone.add("hsn_sac")                      # HSN not read (its own RECON issue is raised in the HSN check)
    done = max(0, len(MANDATORY_FIELDS) - len(gone))
    h["completeness"] = round(done / len(MANDATORY_FIELDS), 2)
    # without a PO there is nothing to check in SQL -> stays BLOCKED however much else was read
    if missing and h["completeness"] >= cfg.min_completeness - 1e-9 and is_valid_po(h.get("po_number", "")):
        for i in missing:
            i["severity"] = RECON
            i["message"] += f" ({int(h['completeness'] * 100)}% of the invoice was read - please complete it)"

    blocked = any(i["severity"] == BLOCK for i in iss)
    recon = any(i["severity"] == RECON for i in iss)
    queue = QUEUE_BLOCKED if blocked else (QUEUE_RECONCILE if recon else QUEUE_MATCHED)
    status = STATUS_MATCHED if queue == QUEUE_MATCHED else STATUS_REVIEW

    h["issues"] = iss
    h["queue"], h["status"], h["validation_status"] = queue, status, status
    h["variance_amount"] = round(variance_total, 2) if variance_total else 0.0
    h["validation_errors"] = {                                # legacy shape (Excel export, older tools)
        "status": status,
        "red_flag": [i["message"] for i in iss if i["code"] == "MULTI_PO"],       # information only
        "missing": [i["message"] for i in iss if i["severity"] == BLOCK and i["code"] != "MULTI_PO"],
        "mismatch": [i["message"] for i in iss if i["severity"] == RECON and i["category"] != "DATA"],
        "doubt": [i["message"] for i in iss if i["severity"] == RECON and i["category"] == "DATA"],
    }
    return h


def to_num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def exception_type(issues: list[dict]) -> str:
    """Single label for the Error-flow list: the most serious open issue decides."""
    open_ = [i for i in issues if i["severity"] in (BLOCK, RECON)]
    if not open_:
        return ""
    if any(i["severity"] == BLOCK for i in open_):
        return "Blocked"
    if any(i["category"] == "MISSING" for i in open_):
        return "Data missing"
    for cat, label in (("QUANTITY", "Quantity mismatch"), ("PRICE", "Price variance"),
                       ("TAX", "Tax / HSN"), ("HSN", "Tax / HSN")):
        if any(i["category"] == cat for i in open_):
            return label
    return "Other mismatch"
