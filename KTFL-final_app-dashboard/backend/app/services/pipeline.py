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
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import Invoice
from . import pdfio
from .gst import apply_tax_code
from .normalize import normalize
from .monitor import event, timed
from .persist import (apply_state, audit, find_duplicate, header_from_invoice, save_new_invoice,
                      sync_lines)
from .sap import SapGateway, get_sap, sap_enrich
from .textutil import clean_grn, grn_is_valid
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
    from .normalize import flag_duplicate_lines
    h["possible_duplicate_lines"] = flag_duplicate_lines(lines)
    h["sap_tax_code"], h["grn_date"], h["po_date"], h["sap_hsn"] = "", "", "", ""
    h["grn_amount"], h["tax_code_match"], h["po_in_sap"] = None, None, False
    try:
        notes = sap_enrich(gw, h, lines, allow_grn_switch=allow_grn_switch)
    except Exception as exc:                                  # a SAP outage must not lose the invoice
        log.exception("SAP lookup failed")
        h["sap_checked"] = False
        notes = [f"SAP lookup error: {type(exc).__name__}: {exc}"]
    h["sap_snapshot"] = {**(h.get("sap_snapshot") or {}), "page_info": h.get("page_info") or {}}
    apply_tax_code(db, h, notes)
    from ..config import get_settings
    from .doctype import apply_doc_type
    apply_doc_type(db, h, get_settings().sap_client or None)         # Details-tab document type of the plant
    validate(h, lines)
    return notes


def _save_runs(db: Session, runs: list[dict], *, job_id, invoice_id, filename) -> None:
    """Store the raw model answers (approved table extraction_runs). A failure here must never lose the invoice."""
    if not runs:
        return
    try:
        from ..models import ExtractionRun
        cols = {c.name for c in ExtractionRun.__table__.columns}
        for r in runs:
            row = ExtractionRun(job_id=job_id, invoice_id=invoice_id, filename=(filename or "")[:255],
                                **{k: v for k, v in r.items() if k in cols})
            db.add(row)
        db.commit()
    except Exception:
        db.rollback()
        log.exception("could not store extraction_runs")


def process_pdf(db: Session, path: str | Path, filename: str | None = None, *,
                vlm: VlmClient | None = None, gw: SapGateway | None | bool = True,
                actor: str | None = None, source: str = "UPLOAD",
                progress: Callable[[str], None] | None = None, job_id: int | None = None) -> PipelineResult:
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
    step("Reading invoice")
    with timed("render_pages", job_id=job_id, file=filename):
        images = pdfio.main_page_images(path)
    n_pages = pdfio.page_count(path)
    with timed("extract_invoice", job_id=job_id, file=filename, pages=len(images), pages_total=n_pages,
               model=getattr(getattr(vlm, "cfg", None), "model_vlm", None)):
        data = vlm.extract_invoice(images, pdfio.text_layer(path))
    runs = (data.pop("_runs", None) or []) if isinstance(data, dict) else []
    h, lines = normalize(data, filename)
    if n_pages > len(images):                                 # never silently drop pages
        h["page_info"] = {**(h.get("page_info") or {}), "pages_total": n_pages, "pages_skipped": n_pages - len(images)}

    step("Reading GRN stamp")
    grn = clean_grn(vlm.read_grn(pdfio.grn_stamp_images(path)))
    if grn:
        h["grn_number"] = grn
        h["field_sources"]["grn_number"] = "VISION"

    # Filename GRN fallback: if the PDF is named exactly like a GRN (e.g. "5000402140.pdf"),
    # and the VLM either missed it or read only a partial number, use the filename.
    stem = re.sub(r"[^\d]", "", Path(filename).stem)
    if grn_is_valid(stem):
        if not grn_is_valid(h.get("grn_number", "")):
            log.info("%s: GRN from filename (%s) used (VLM read '%s')", filename, stem, h.get("grn_number"))
            h["grn_number"] = stem
            h["field_sources"]["grn_number"] = "FILENAME"
        elif h.get("grn_number") != stem:
            log.info("%s: filename GRN %s differs from VLM GRN %s - keeping VLM", filename, stem, h["grn_number"])
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

    step("Checking SAP")
    with timed("sap_validation", job_id=job_id, file=filename):
        notes = check_against_sap(db, h, lines, gateway)

    step("Saving")
    dup = find_duplicate(db, h)
    if dup is not None:
        _save_runs(db, runs, job_id=job_id, invoice_id=None, filename=filename)
        audit(db, "DUPLICATE_SKIPPED", dup.id, {"file": filename, "invoice": h["invoice_number"],
                                                "reason": f"same invoice + PO as id {dup.id}"}, actor)
        return PipelineResult("DUPLICATE", dup.id, dup.queue, notes, duplicate_of=dup.id)

    inv = save_new_invoice(db, h, lines, source_path=str(path), page_count=n_pages)
    db.flush()
    _save_runs(db, runs, job_id=job_id, invoice_id=inv.id, filename=filename)

    # SAC 99xx check: material invoices (non-subcon) with a service HSN starting 99 should be
    # flagged and their PDF copied to an exception subfolder for human review.
    _sac = (h.get("hsn_sac") or "").strip()
    _line_hsns = [str(l.get("hsn_sac") or "").strip() for l in lines]
    _has_sac99 = _sac.startswith("99") or any(s.startswith("99") for s in _line_hsns)
    # Only flag if this looks like a material invoice (PO starting 51/61, not 81/82/83)
    _po = (h.get("po_number") or "").strip()
    _is_material = bool(re.match(r"^(51|61)\d{8}", _po)) or not bool(re.match(r"^(81|82|83)\d{8}", _po))
    if _has_sac99 and _is_material:
        exc_dir = path.parent / "exceptions" / "sac99"
        exc_dir.mkdir(parents=True, exist_ok=True)
        dest = exc_dir / path.name
        try:
            shutil.copy2(str(path), str(dest))
            log.warning("SAC 99xx on material invoice %s — PDF copied to %s", filename, dest)
            inv.issues = list(inv.issues or []) + [{"code": "SAC_SERVICE", "severity": "RECON", "category": "DATA", "field": "hsn_sac", "message": "SAC code starts with 99 (service HSN) on a material invoice — verify HSN with vendor", "invoice_value": _sac or (_line_hsns[0] if _line_hsns else ""), "sap_value": "", "variance": None}]
            if inv.queue == "MATCHED":
                inv.queue = "RECONCILE"
                inv.status = "SAC_SERVICE"
        except Exception:
            log.exception("Could not copy SAC-99 PDF to exception folder")

    _in = sum(r.get("prompt_eval_count") or 0 for r in runs)
    _out = sum(r.get("eval_count") or 0 for r in runs)
    _ms = sum(r.get("total_duration_ms") or 0 for r in runs)
    event("invoice_done", job_id=job_id, invoice_id=inv.id, queue=h["queue"], pages_total=n_pages,
          pages_processed=len((h.get("page_info") or {}).get("pages_read", [])) or None,
          prompt_version=(h.get("page_info") or {}).get("prompt_version"), input_tokens=_in, output_tokens=_out,
          ollama_ms=_ms)
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
    # a value a person typed is no longer "unread"
    h["unread"] = [k for k in h.get("unread", []) if h["field_sources"].get(k) != "MANUAL"]
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
