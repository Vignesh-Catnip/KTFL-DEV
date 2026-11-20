"""
The whole journey of one invoice:

    PDF -> vision extraction (header + lines) -> GRN stamp -> SAP cross-check -> GST tax code
        -> classify (MATCHED / RECONCILE / BLOCKED) -> PostgreSQL

`process_pdf` is used by the upload worker; `recheck_invoice` re-runs only the SAP + GST + classification
part after a person corrected data on the dashboard.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import Invoice
from . import evidence as evmod, pdfio
from .gst import apply_tax_code
from .normalize import normalize
from .persist import (apply_state, audit, find_duplicate, header_from_invoice, save_new_invoice,
                      sync_lines)
from .sap import SapGateway, get_sap, sap_enrich
from .textutil import clean_grn, grn_is_valid, is_material_po, is_service_po, snap_gst_rate
from .validate import validate
from .vlm import VlmClient, get_vlm

log = logging.getLogger("clearmatch.pipeline")


@dataclass
class PipelineResult:
    outcome: str                 # CREATED / DUPLICATE / EXCEPTION
    invoice_id: int | None
    queue: str | None
    notes: list[str]
    duplicate_of: int | None = None
    reason: str = ""            # EXCEPTION: folder name (service_po / sac_99)
    message: str = ""


def check_against_sap(db: Session, h: dict, lines: list[dict], gw: SapGateway | None,
                      allow_grn_switch: bool = True) -> list[str]:
    """SAP cross-check -> GST tax code -> queue. Mutates h / lines. Returns notes."""
    # values re-derived from SAP on every run (never carried over from an earlier run)
    h["sap_tax_code"], h["grn_date"], h["po_date"], h["sap_hsn"] = "", "", "", ""
    h["grn_amount"], h["tax_code_match"], h["po_in_sap"] = None, None, False
    try:
        notes = sap_enrich(gw, h, lines, allow_grn_switch=allow_grn_switch)
    except Exception as exc:                                  # a SAP outage must not lose the invoice
        log.exception("SAP lookup failed")
        h["sap_checked"] = False
        notes = [f"SAP lookup error: {type(exc).__name__}: {exc}"]
    apply_tax_code(db, h, notes)
    from ..config import get_settings
    from .doctype import apply_doc_type
    apply_doc_type(db, h, get_settings().sap_client or None)         # Details-tab document type of the plant
    validate(h, lines)
    return notes


def _extras(h: dict) -> float:
    return float(h.get("round_off") or 0) + float(h.get("other_charges") or 0)


def _totals_ok(h: dict) -> bool:
    """subtotal + taxes (+ round-off / other charges) = total (INR 2 slack or 0.1 %)."""
    tot = float(h.get("total") or 0)
    if tot <= 0 or float(h.get("subtotal") or 0) <= 0:
        return False
    calc = float(h["subtotal"]) + float(h.get("cgst") or 0) + float(h.get("sgst") or 0) + float(h.get("igst") or 0) + _extras(h)
    return abs(calc - tot) <= max(2.0, tot * 0.001)


def _second_read_totals(vlm, path, h: dict, filename: str) -> None:
    """The first read of the amounts does not add up -> read the totals box again, cropped and in high
    resolution. The new numbers are used ONLY when they add up (a consistent read never replaces ... a consistent one)."""
    from ..config import get_settings
    if not get_settings().totals_pass or _totals_ok(h) or not hasattr(vlm, "read_totals"):
        return
    try:
        t = vlm.read_totals(pdfio.totals_images(path)) or {}
    except Exception:
        log.exception("%s: totals second read failed", filename)
        return
    cand = dict(h)
    cand.update({k: t.get(k, 0.0) for k in ("subtotal", "cgst", "sgst", "igst", "round_off", "other_charges", "total")})
    if _totals_ok(cand):
        log.info("%s: amounts re-read from the totals box (%s -> %s)", filename,
                 (h.get("subtotal"), h.get("total")), (cand["subtotal"], cand["total"]))
        for k in ("subtotal", "cgst", "sgst", "igst", "round_off", "other_charges", "total"):
            h[k] = cand[k]
        h["total_tax"] = round(h["cgst"] + h["sgst"] + h["igst"], 2)
        h["gst_rate_percent"] = snap_gst_rate(h["total_tax"] / h["subtotal"] * 100) if h["subtotal"] > 0 else None
        h["field_sources"].update({k: "VISION_TOTALS" for k in ("subtotal", "cgst", "sgst", "igst", "total")})


def _exception_reason(h: dict, lines: list[dict], ev: "evmod.Evidence") -> tuple[str, str] | None:
    """Only MATERIAL invoices are processed. Returns (folder, message) for an invoice that must go to the
    exception folder instead: service PO (81 / 82 / 83) or a service SAC (HSN starting 99).
    The reading must be corroborated by the OCR text (when OCR is available) - a phone number or a mis-read digit
    must never send a material invoice away. An uncorroborated reading is left to the SERVICE_PO_NOT_ALLOWED block."""
    pos = [p for p in ([h.get("po_number") or ""] + list(h.get("po_numbers_printed") or [])
                       + str(h.get("po_numbers_all") or "").split(",")) if p]
    svc = [p for p in pos if is_service_po(p.strip()) and p not in ev.phones and evmod.corroborates_po(ev, p)]
    if svc and not is_material_po(h.get("po_number") or ""):
        return "service_po", f"Service PO {svc[0]} (81/82/83) - only material invoices (PO 51/61) are processed"
    # SAC = the HSN of the main (highest value) row; a stray service row (loading charge 9965...) must not decide it
    main = str(h.get("hsn_sac") or "").strip()
    if not main and lines:
        main = str(max(lines, key=lambda l: float(l.get("amount") or 0)).get("hsn_sac") or "").strip()
    if re.match(r"^99\d{2,}", main) and evmod.corroborates_sac(ev, main):
        return "sac_99", f"SAC {main} (starts with 99 = service) - only material invoices are processed"
    return None


def process_pdf(db: Session, path: str | Path, filename: str | None = None, *,
                vlm: VlmClient | None = None, gw: SapGateway | None | bool = True,
                actor: str | None = None, source: str = "UPLOAD",
                progress: Callable[[str], None] | None = None) -> PipelineResult:
    path = Path(path)
    filename = filename or path.name
    vlm = vlm or get_vlm()
    gateway = get_sap() if gw is True else (gw or None)
    step = progress or (lambda s: None)

    step("Checking page orientation")
    try:
        pdfio.fix_orientation(path, getattr(vlm, "page_is_upright", None))
    except Exception:
        log.exception("page orientation check failed - reading the pages as they are")
    step("Checking printed text")
    ev = evmod.collect(path)                                   # independent OCR reading (empty when tesseract is missing)
    step("Reading invoice")
    images = pdfio.main_page_images(path)
    layer = pdfio.text_layer(path)
    if not layer and ev.available:                             # sideways / image-only scan: the OCR text (upright) replaces it
        layer = ev.text
    data = vlm.extract_invoice(images, layer)
    h, lines = normalize(data, filename)

    step("Reading GRN stamp")
    grn = clean_grn(vlm.read_grn(pdfio.grn_stamp_images(path)))
    if grn:
        h["grn_number"] = grn
        h["field_sources"]["grn_number"] = "VISION"

    # The file name is the GRN (e.g. "5000402140.pdf") - that is what the person who scanned it typed, so it is
    # more reliable than reading handwriting. A different VLM reading is kept only as a note, never used.
    stem = re.sub(r"[^\d]", "", Path(filename).stem)
    if grn_is_valid(stem):
        if h.get("grn_number") != stem:
            if grn_is_valid(h.get("grn_number", "")):
                log.info("%s: filename GRN %s overrides VLM GRN %s", filename, stem, h["grn_number"])
            else:
                log.info("%s: GRN from filename (%s) used (VLM read '%s')", filename, stem, h.get("grn_number"))
            h["grn_number"] = stem
            h["field_sources"]["grn_number"] = "FILENAME"
    # a number starting 50 is a goods-receipt number, never a PO (KTFL POs are 51 / 61 / 81 / 83 ...): the model
    # sometimes reads the handwritten GRN as the PO -> drop it, the PO is then looked up in SQL from GRN / invoice no.
    if h.get("po_number") and (h["po_number"].startswith("50") or h["po_number"] == h.get("grn_number")):
        log.info("%s: '%s' looks like a GRN, not a PO - ignored", filename, h["po_number"])
        h["po_number"] = ""
    h["po_numbers_printed"] = [p for p in h.get("po_numbers_printed", []) if not p.startswith("50")]

    # CIN (Company Identification Number) is NOT a PO. SLR Metaliks prints CIN on the invoice
    # in the same area as Order No. and the model picks it up as the PO.
    # CIN format: U or L, then 5 digits, 2 letters, 4 digits, 3 letters, 6 digits (21 chars total).
    _CIN_RE = re.compile(r"^[UL]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}$", re.IGNORECASE)
    if h.get("po_number") and _CIN_RE.match(h["po_number"]):
        log.info("%s: '%s' looks like a CIN number, not a PO - dropping", filename, h["po_number"])
        h["po_number"] = ""

    from ..config import get_settings as _gs
    from .prompts import VENDOR_HINTS
    for note in evmod.apply(h, ev, _gs().gstin_prefixes, tuple(VENDOR_HINTS)):
        log.info("%s: OCR evidence - %s", filename, note)
        h.setdefault("evidence_notes", []).append(note)

    # No material PO yet -> many vendors (SLR ...) print none and the PO is handwritten: look for it once, focused.
    if not is_material_po(h.get("po_number") or "") and hasattr(vlm, "read_handwritten_po"):
        step("Looking for a handwritten PO")
        hw_po = ""
        try:
            hw_po = vlm.read_handwritten_po(images)
        except Exception:
            log.exception("%s: handwritten PO look-up failed", filename)
        if hw_po and hw_po not in ev.phones:
            log.info("%s: handwritten PO %s", filename, hw_po)
            h["po_number"] = hw_po
            h["field_sources"]["po_number"] = "VISION_HANDWRITTEN"
            h["po_numbers_printed"] = [p for p in h.get("po_numbers_printed", []) if is_material_po(p)]

    exc = _exception_reason(h, lines, ev)
    if exc:
        log.info("%s: exception (%s) - %s", filename, exc[0], exc[1])
        audit(db, "EXCEPTION_" + exc[0].upper(), None, {"file": filename, "reason": exc[1]}, actor or "EXTRACTOR")
        return PipelineResult("EXCEPTION", None, None, [exc[1]], reason=exc[0], message=exc[1])

    _second_read_totals(vlm, path, h, filename)

    step("Checking SAP")
    notes = check_against_sap(db, h, lines, gateway)

    step("Saving")
    dup = find_duplicate(db, h)
    if dup is not None:
        audit(db, "DUPLICATE_SKIPPED", dup.id, {"file": filename, "invoice": h["invoice_number"],
                                                "reason": f"same invoice + PO as id {dup.id}"}, actor)
        return PipelineResult("DUPLICATE", dup.id, dup.queue, notes, duplicate_of=dup.id)

    inv = save_new_invoice(db, h, lines, source_path=str(path), page_count=pdfio.page_count(path))

    audit(db, f"EXTRACTED_{h['queue']}", inv.id,
          {"file": filename, "source": source, "model": get_vlm_name(), "queue": h["queue"],
           "issues": [i["message"] for i in h["issues"] if i["severity"] in ("BLOCK", "RECON")],
           "sap_notes": notes, "field_sources": h["field_sources"]}, actor or "EXTRACTOR", commit=False)
    db.commit()
    return PipelineResult("CREATED", inv.id, h["queue"], notes)


def get_vlm_name() -> str:
    from ..config import get_settings
    return get_settings().model_vlm


def recheck_invoice(db: Session, inv: Invoice, *, actor: str | None = None,
                    manual_fields: set[str] | None = None, gw: SapGateway | None | bool = True,
                    action: str = "RECHECKED") -> dict:
    """Re-run SAP + GST + classification on the stored invoice (after an edit, or when SAP was down)."""
    gateway = get_sap() if gw is True else (gw or None)
    h, lines = header_from_invoice(inv)
    for f in manual_fields or ():
        h["field_sources"][f] = "MANUAL"
    allow_switch = not ({"grn_number", "po_number"} & (manual_fields or set()))
    before = inv.queue
    notes = check_against_sap(db, h, lines, gateway, allow_grn_switch=allow_switch)
    apply_state(inv, h)
    sync_lines(inv, lines)
    audit(db, action, inv.id, {"queue_before": before, "queue_after": h["queue"],
                               "edited": sorted(manual_fields or []),
                               "issues": [i["message"] for i in h["issues"] if i["severity"] in ("BLOCK", "RECON")],
                               "sap_notes": notes}, actor, commit=False)
    db.commit()
    return h
