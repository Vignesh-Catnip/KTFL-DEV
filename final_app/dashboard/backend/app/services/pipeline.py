"""
The whole journey of one invoice:

    PDF -> vision extraction (header + lines) -> GRN stamp -> SAP cross-check -> GST tax code
        -> classify (MATCHED / RECONCILE / BLOCKED) -> PostgreSQL

`process_pdf` is used by the upload worker; `recheck_invoice` re-runs only the SAP + GST + classification
part after a person corrected data on the dashboard.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import Invoice
from . import pdfio
from .gst import apply_tax_code
from .normalize import normalize
from .persist import (apply_state, audit, find_duplicate, header_from_invoice, save_new_invoice,
                      sync_lines)
from .sap import SapGateway, get_sap, sap_enrich
from .textutil import clean_grn
from .validate import validate
from .vlm import VlmClient, get_vlm

log = logging.getLogger("clearmatch.pipeline")


@dataclass
class PipelineResult:
    outcome: str                 # CREATED / DUPLICATE
    invoice_id: int | None
    queue: str | None
    notes: list[str]
    duplicate_of: int | None = None


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


def process_pdf(db: Session, path: str | Path, filename: str | None = None, *,
                vlm: VlmClient | None = None, gw: SapGateway | None | bool = True,
                actor: str | None = None, source: str = "UPLOAD",
                progress: Callable[[str], None] | None = None) -> PipelineResult:
    path = Path(path)
    filename = filename or path.name
    vlm = vlm or get_vlm()
    gateway = get_sap() if gw is True else (gw or None)
    step = progress or (lambda s: None)

    step("Reading invoice")
    images = pdfio.main_page_images(path)
    data = vlm.extract_invoice(images, pdfio.text_layer(path))
    h, lines = normalize(data, filename)

    step("Reading GRN stamp")
    grn = clean_grn(vlm.read_grn(pdfio.grn_stamp_images(path)))
    if grn:
        h["grn_number"] = grn
        h["field_sources"]["grn_number"] = "VISION"

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
