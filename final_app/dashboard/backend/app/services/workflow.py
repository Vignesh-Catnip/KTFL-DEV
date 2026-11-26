"""PDF folder workflow - every invoice PDF moves through these folders:

    invoices/
      1. input/           Drop PDFs here (WATCH_FOLDER points here)
      2. processing/      File lives here from extraction start until SAP post:
                            - while OCR extraction runs
                            - MATCHED (waiting for robot)
                            - RECONCILE (waiting for manual fix)
      3. output/          ONLY successfully posted invoices land here.
                          File is renamed on arrival: <GRN>_<Journal>_invoice.pdf
      4. exception/
            ocr_error/    Any extraction failure (OCR/VLM error, duplicate, service PO, corrupt PDF)
            sap_error/    Any SAP / robot failure (ABAP error, already posted, tax code error, etc.)

Rules:
  - Rename happens ONLY in output/ after a successful SAP post
  - exception/ files keep their original filename always
  - processing/ files keep their original filename always
  - input/ → processing/ on extraction start (no rename)
  - processing/ → output/ only after robot posts successfully (with rename)
  - processing/ → exception/sap_error/ if robot hits any SAP error
  - processing/ → exception/ocr_error/ if extraction fails or invoice is BLOCKED
"""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

from sqlalchemy import select

from ..config import get_settings
from ..database import session_factory
from ..models import Invoice, Job

log = logging.getLogger("clearmatch.workflow")


def folders() -> dict[str, Path | None]:
    cfg = get_settings()
    if not cfg.watch_folder:
        return {k: None for k in ("input", "processing", "output", "exception")}
    inp = Path(cfg.watch_folder)   # invoices/input/
    base = inp.parent              # invoices/
    return {
        "input":      inp,
        "processing": base / "processing",
        "output":     base / "output",
        "exception":  base / "exception",
    }


def ensure_folders() -> None:
    """Create all folders and known sub-folders upfront."""
    f = folders()
    for p in f.values():
        if p is not None:
            p.mkdir(parents=True, exist_ok=True)
    exc = f.get("exception")
    if exc:
        (exc / "ocr_error").mkdir(parents=True, exist_ok=True)
        (exc / "sap_error").mkdir(parents=True, exist_ok=True)


def move(src: Path | str | None, dest_dir: Path, new_name: str | None = None) -> Path | None:
    """Move (and optionally rename) a file into dest_dir.
    Never overwrites — a clash gets a timestamp prefix. Returns the new path."""
    if not src:
        return None
    src = Path(src)
    if not src.is_file():
        return None
    name = new_name or src.name
    if src.parent.resolve() == dest_dir.resolve() and src.name == name:
        return src
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / name
    if target.exists():
        target = dest_dir / f"{int(time.time())}_{name}"
    shutil.move(str(src), str(target))
    return target


def final_name(inv: Invoice, current: Path) -> str:
    """Name for a posted invoice: <GRN>_<MIRO Journal no>_invoice.pdf
    Used ONLY when moving to output/ after a successful SAP post."""
    doc = (inv.miro_doc_number or "").strip()
    if not doc:
        return current.name
    grn = (inv.grn_number or inv.invoice_number or "NOGRN").strip()
    safe = lambda v: "".join(c if c.isalnum() or c in "-." else "-" for c in v)   # noqa: E731
    return f"{safe(grn)}_{safe(doc)}_invoice{current.suffix.lower() or '.pdf'}"


# ------------------------------------------------------------------ placement rules

def place_after_extraction(job: Job, outcome: str, queue: str | None) -> None:
    """Extraction finished:
       CREATED + MATCHED / RECONCILE → processing/   (stays here until robot posts)
       CREATED + BLOCKED             → exception/ocr_error/  (mandatory data missing)
       DUPLICATE                     → exception/ocr_error/  (already extracted)
    File is NOT renamed."""
    f = folders()
    if f["input"] is None or not job.folder_path:
        return
    if outcome == "DUPLICATE" or queue == "BLOCKED":
        dest = f["exception"] / "ocr_error"
    else:
        dest = f["processing"]   # MATCHED / RECONCILE stay in processing until posted
    new = move(job.folder_path, dest)
    if new:
        job.folder_path = str(new)


def place_failed(job: Job) -> None:
    """Extraction completely failed (OCR/VLM error, corrupt PDF, timeout, etc.):
       → exception/ocr_error/  (no rename)"""
    f = folders()
    if f["input"] is None or not job.folder_path:
        return
    new = move(job.folder_path, f["exception"] / "ocr_error")
    if new:
        job.folder_path = str(new)


def place_exception(job: Job, reason: str) -> None:
    """Pipeline exception (service PO 81/82/83, SAC 99xx, or any extraction-level issue):
       → exception/ocr_error/  (no rename)"""
    f = folders()
    if f["input"] is None or not job.folder_path:
        return
    new = move(job.folder_path, f["exception"] / "ocr_error")
    if new:
        job.folder_path = str(new)


def place_posted(job: Job, inv: Invoice) -> None:
    """Robot successfully posted invoice in SAP:
       processing/ → output/ with rename to <GRN>_<Journal>_invoice.pdf"""
    f = folders()
    if f["input"] is None or not job.folder_path:
        return
    cur = Path(job.folder_path)
    name = final_name(inv, cur)
    new = move(cur, f["output"], name)
    if new:
        job.folder_path = str(new)
        log.info("Posted invoice %s moved to output: %s", inv.id, new)


def place_posted_by_id(inv) -> None:
    """Called by the robot after a successful SAP post.
    Finds the job by invoice id and moves the PDF to output/ with rename."""
    f = folders()
    if f["input"] is None:
        return
    with session_factory()() as db:
        from sqlalchemy import select as _select
        from ..models import Job as _Job
        job = db.scalar(_select(_Job).where(_Job.invoice_id == inv.id).order_by(_Job.id.desc()).limit(1))
        if job and job.folder_path:
            cur = Path(job.folder_path)
            if cur.is_file():
                name = final_name(inv, cur)
                new = move(cur, f["output"], name)
                if new:
                    job.folder_path = str(new)
                    db.commit()
                    log.info("Posted invoice %s → output/%s", inv.id, name)


def place_sap_error(job: Job) -> None:
    """Robot hit a SAP error (ABAP, already posted, tax code, any SAP issue):
       processing/ → exception/sap_error/  (no rename)"""
    f = folders()
    if f["input"] is None or not job.folder_path:
        return
    new = move(job.folder_path, f["exception"] / "sap_error")
    if new:
        job.folder_path = str(new)


def move_to_sap_error(inv: Invoice, reason: str = "sap_error") -> None:
    """Called by the robot integration when a SAP error occurs.
    Moves the invoice PDF from wherever it is to exception/sap_error/. No rename."""
    f = folders()
    if f["input"] is None:
        return
    with session_factory()() as db:
        from sqlalchemy import select as _select
        from ..models import Job as _Job
        job = db.scalar(_select(_Job).where(_Job.invoice_id == inv.id).order_by(_Job.id.desc()).limit(1))
        if job and job.folder_path:
            cur = Path(job.folder_path)
            if cur.is_file():
                new = move(cur, f["exception"] / "sap_error")
                if new:
                    job.folder_path = str(new)
                    db.commit()
                    log.info("Invoice %s PDF → exception/sap_error (%s): %s", inv.id, reason, new)


def sweep() -> int:
    """Called periodically by the watcher.
    After a successful SAP post: move processing/ → output/ with rename.
    Returns number of files moved."""
    f = folders()
    if f["input"] is None:
        return 0
    moved = 0
    with session_factory()() as db:
        rows = db.execute(
            select(Job, Invoice)
            .join(Invoice, Invoice.id == Job.invoice_id)
            .where(Job.folder_path.is_not(None), Job.status == "DONE")
        ).all()
        for job, inv in rows:
            cur = Path(job.folder_path)
            # Only move files that are still in processing/ (not already in output/ or exception/)
            if f["exception"] and str(cur).startswith(str(f["exception"])):
                continue
            if f["output"] and str(cur).startswith(str(f["output"])):
                continue
            # Only posted/parked invoices go to output/ (with rename)
            if inv.miro_status in ("POSTED", "PARKED"):
                name = final_name(inv, cur)
                try:
                    new = move(cur, f["output"], name)
                    if new:
                        job.folder_path = str(new)
                        moved += 1
                except OSError:
                    log.info("could not move %s - will retry", cur.name)
        db.commit()
    return moved
