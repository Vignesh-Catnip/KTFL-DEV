"""PDF folder workflow - every invoice PDF moves through four folders, so a person can see where it is:

    1. input        (WATCH_FOLDER)      drop the PDFs here
    2. processing                       while the agent reads it and checks it against SQL
    3. output\\matched | reconcile | blocked   extracted - waiting for the robot / for a person (follows the dashboard queue)
    4. finish\\posted | parked | duplicate     the robot posted / parked it in SAP (duplicates go here too)
       error                            the PDF could not be read at all

The dashboard keeps its own copy of every PDF in UPLOAD_DIR (that is what the viewer shows); the files in these
folders are moved, never deleted. Folder names can be changed in .env (PROCESSING_FOLDER, OUTPUT_FOLDER, ...).
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
        return {k: None for k in ("input", "processing", "output", "finish", "error")}
    inp = Path(cfg.watch_folder)
    base = inp.parent
    pick = lambda v, d: Path(v) if v.strip() else base / d            # noqa: E731
    return {"input": inp, "processing": pick(cfg.processing_folder, "processing"),
            "output": pick(cfg.output_folder, "output"), "finish": pick(cfg.finish_folder, "finish"),
            "error": pick(cfg.error_folder, "error")}


def ensure_folders() -> None:
    for p in folders().values():
        if p is not None:
            p.mkdir(parents=True, exist_ok=True)


def move(src: Path | str | None, dest_dir: Path, new_name: str | None = None) -> Path | None:
    """Move (and optionally rename) a file into dest_dir (never overwrites - a clash gets a timestamp prefix).
    Returns the new path."""
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
    """Name of the PDF once SAP gave a document number:  <GRN no>_<MIRO document no>_invoice.pdf
    (no document number yet -> the file keeps its name)."""
    doc = (inv.miro_doc_number or "").strip()
    if not doc:
        return current.name
    grn = (inv.grn_number or inv.invoice_number or "NOGRN").strip()
    safe = lambda v: "".join(c if c.isalnum() or c in "-." else "-" for c in v)      # noqa: E731
    return f"{safe(grn)}_{safe(doc)}_invoice{current.suffix.lower() or '.pdf'}"


def _output_dir(f: dict, queue: str | None) -> Path:
    return f["output"] / (queue or "unknown").lower()


def place_after_extraction(job: Job, outcome: str, queue: str | None) -> None:
    """Called by the worker when extraction finished: processing -> output\\<queue> (duplicate -> finish\\duplicate)."""
    f = folders()
    if f["input"] is None or not job.folder_path:
        return
    dest = f["finish"] / "duplicate" if outcome == "DUPLICATE" else _output_dir(f, queue)
    new = move(job.folder_path, dest)
    if new:
        job.folder_path = str(new)


def place_failed(job: Job) -> None:
    f = folders()
    if f["input"] is None or not job.folder_path:
        return
    new = move(job.folder_path, f["error"])
    if new:
        job.folder_path = str(new)


def sweep() -> int:
    """Keep the output / finish folders in line with the dashboard:
       invoice queue changed (e.g. Reconcile -> Matched after a correction) -> output\\<new queue>
       robot posted / parked it -> finish\\posted | finish\\parked.  Returns the number of files moved."""
    f = folders()
    if f["input"] is None:
        return 0
    moved = 0
    with session_factory()() as db:
        rows = db.execute(select(Job, Invoice).join(Invoice, Invoice.id == Job.invoice_id)
                          .where(Job.folder_path.is_not(None), Job.status == "DONE")).all()
        for job, inv in rows:
            cur = Path(job.folder_path)
            name = None
            if inv.miro_status in ("POSTED", "PARKED"):
                want = f["finish"] / inv.miro_status.lower()
                name = final_name(inv, cur)
            else:
                want = _output_dir(f, inv.queue)
            try:
                if cur.parent.resolve() != want.resolve() or (name and cur.name != name):
                    new = move(cur, want, name)
                    if new:
                        job.folder_path = str(new)
                        moved += 1
            except OSError:
                log.info("could not move %s - will retry", cur.name)
        db.commit()
    return moved
