"""
Pure business logic - no SAP, no DB. Fully unit-tested.

  * SAP number / date formatting and parsing
  * deciding which MIRO item rows belong to the invoice's GRN
  * cross-checking the selected rows against the invoice from the database
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime


class MiroDataError(Exception):
    """The invoice data and SAP do not agree -> invoice goes to REVIEW, never retried blindly."""
    ROBOT_SUPPRESS_NAME = True


# ---------------------------------------------------------------- numbers / dates

def parse_sap_number(text: str, decimal_sep: str = ".") -> float:
    """'1,23,456.78' / '1.234,56' / '12.000-' / '' -> float. SAP puts the minus sign at the end."""
    s = str(text or "").strip().replace(" ", "")
    if not s:
        return 0.0
    neg = s.endswith("-") or s.startswith("-")
    s = s.strip("-")
    thousands = "," if decimal_sep == "." else "."
    s = s.replace(thousands, "").replace(decimal_sep, ".")
    s = re.sub(r"[^0-9.]", "", s)
    if not s or s == ".":
        return 0.0
    val = float(s)
    return -val if neg else val


def format_sap_amount(value: float, decimal_sep: str = ".") -> str:
    """Amount as the user would type it - no thousands separator: 619763.14 -> '619763.14' / '619763,14'."""
    s = f"{float(value):.2f}"
    return s.replace(".", ",") if decimal_sep == "," else s


DATE_INPUT_FORMATS = ("%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d.%m.%y", "%d/%m/%y",
                      "%d-%m-%y", "%d-%b-%Y", "%d-%b-%y", "%d %b %Y", "%d %B %Y")


def format_sap_date(raw: str, out_format: str = "%d.%m.%Y") -> str:
    raw = str(raw or "").strip()
    if not raw:
        raise MiroDataError("Invoice date is empty")
    for f in DATE_INPUT_FORMATS:
        try:
            dt = datetime.strptime(raw, f)
            if dt.year < 100:
                dt = dt.replace(year=dt.year + 2000)
            return dt.strftime(out_format)
        except ValueError:
            continue
    raise MiroDataError(f"Invoice date '{raw}' is not a valid date")


def norm_doc(v) -> str:
    """Document numbers: strip spaces and leading zeros so '005000343863' == '5000343863'."""
    return re.sub(r"\s+", "", str(v or "")).lstrip("0")


# ---------------------------------------------------------------- MIRO rows

@dataclass
class MiroRow:
    index: int              # absolute row index in the MIRO table (0-based)
    po: str = ""
    po_item: str = ""
    ref_doc: str = ""       # Reference Document (GRN / material document)
    amount: float = 0.0
    quantity: float = 0.0
    text: str = ""


@dataclass
class SelectionPlan:
    rows: list = field(default_factory=list)       # MiroRow objects to select
    selected_amount: float = 0.0
    selected_quantity: float = 0.0
    other_grns: list = field(default_factory=list)  # GRNs present in the table we did NOT select
    warnings: list = field(default_factory=list)

    @property
    def indexes(self) -> list:
        return [r.index for r in self.rows]


def plan_selection(rows: list, invoice: dict, amount_tolerance: float = 1.0) -> SelectionPlan:
    """
    Decide which MIRO rows to tick.

    Rule: a row is taken when its Reference Document == the invoice's GRN number
          (and, when the row shows a PO, that PO == the invoice PO).
    Checks (raise MiroDataError -> REVIEW):
          * GRN number missing
          * no row in MIRO carries that GRN
          * selected GR value differs from the invoice taxable value by > tolerance
          * invoice line count / quantity cannot be covered by the GRN rows
    """
    grn = norm_doc(invoice.get("grn_number"))
    po = norm_doc(invoice.get("po_number"))
    if not grn:
        raise MiroDataError("GRN number is empty in the database - cannot pick MIRO lines")

    data_rows = [r for r in rows if norm_doc(r.ref_doc) or norm_doc(r.po)]
    if not data_rows:
        raise MiroDataError(f"MIRO shows no items for PO {invoice.get('po_number')} "
                            f"(PO fully invoiced, wrong PO, or GR not posted)")

    plan = SelectionPlan()
    for r in data_rows:
        if norm_doc(r.ref_doc) != grn:
            continue
        if po and norm_doc(r.po) and norm_doc(r.po) != po:
            plan.warnings.append(f"Row {r.index + 1}: GRN {grn} but PO {r.po} (expected {po}) - skipped")
            continue
        plan.rows.append(r)

    plan.other_grns = sorted({r.ref_doc.strip() for r in data_rows
                              if norm_doc(r.ref_doc) and norm_doc(r.ref_doc) != grn})

    if not plan.rows:
        seen = ", ".join(plan.other_grns) or "none"
        raise MiroDataError(f"GRN {invoice.get('grn_number')} not found in MIRO item list "
                            f"(Reference Documents shown: {seen})")

    plan.selected_amount = round(sum(r.amount for r in plan.rows), 2)
    plan.selected_quantity = round(sum(r.quantity for r in plan.rows), 3)

    subtotal = float(invoice.get("subtotal") or 0)
    if subtotal > 0 and abs(plan.selected_amount - subtotal) > amount_tolerance:
        raise MiroDataError(
            f"GRN {invoice.get('grn_number')} rows total {plan.selected_amount:,.2f} but invoice "
            f"taxable value is {subtotal:,.2f} (diff {plan.selected_amount - subtotal:,.2f}, "
            f"tolerance {amount_tolerance})")

    lines = [l for l in invoice.get("lines") or [] if float(l.get("amount") or 0) or float(l.get("quantity") or 0)]
    if lines:
        inv_qty = round(sum(float(l.get("quantity") or 0) for l in lines), 3)
        if inv_qty and plan.selected_quantity and abs(inv_qty - plan.selected_quantity) > 0.001:
            plan.warnings.append(f"Quantity: invoice lines {inv_qty:g} vs GRN rows "
                                 f"{plan.selected_quantity:g} (UoM may differ) - amount matched, continuing")
    return plan


# ---------------------------------------------------------------- SAP messages

DOC_NO_RE = re.compile(r"\b(\d{10})\b")


def extract_document_number(status_text: str) -> str:
    """'Document no. 5105600123 created' -> '5105600123'."""
    m = DOC_NO_RE.search(status_text or "")
    return m.group(1) if m else ""


DUPLICATE_HINTS = ("already entered", "already been entered", "duplicate", "bereits erfasst")


def is_duplicate_message(text: str) -> bool:
    t = (text or "").lower()
    return any(h in t for h in DUPLICATE_HINTS)
