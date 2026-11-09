"""Background extraction: a job queue (table extraction_jobs) plus an optional watched folder."""
from __future__ import annotations

import logging
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from ..config import get_settings
from ..database import session_factory
from ..models import Job
from .persist import audit
from . import pdfio, robot, workflow
from .pipeline import process_pdf

log = logging.getLogger("clearmatch.worker")
_stop = threading.Event()
_threads: list[threading.Thread] = []
_wake = threading.Event()


def _friendly_error(exc: Exception, raw: str) -> str:
    """Convert a technical exception into a human-readable extraction error message."""
    msg = str(exc).lower()
    cls = type(exc).__name__

    if "timeout" in msg or "timed out" in msg:
        return "Extraction Failed – The model took too long to respond. Please retry."
    if "connectionerror" in cls or "connection refused" in msg or "connect" in msg:
        return "Extraction Failed – Cannot reach the AI model (Ollama). Check that the model server is running."
    if "json" in msg or "parse" in msg or "decode" in msg:
        return "Extraction Failed – PDF/Image is unclear or the model returned unreadable output. Please retry with a clearer scan."
    if "image" in msg or "pdf" in msg or "corrupt" in msg or "invalid" in msg:
        return "Extraction Failed – PDF/Image could not be read. The file may be corrupted or password-protected."
    if "memory" in msg or "out of memory" in msg:
        return "Extraction Failed – Server ran out of memory processing this file. Try a smaller or simpler PDF."
    if "permission" in msg or "access" in msg:
        return "Extraction Failed – File access error. The PDF could not be read from storage."
    # generic fallback — still better than a raw traceback
    return f"Extraction Failed – {cls}: {str(exc)[:200]}"


def re_enqueue_failed(db, job_id: int, user: str | None = None) -> "Job":
    """Re-queue a FAILED job for extraction retry. Returns the updated job."""
    job = db.get(Job, job_id)
    if job is None:
        raise ValueError(f"Job {job_id} not found")
    if job.status != "FAILED":
        raise ValueError(f"Job {job_id} is not in FAILED state (current: {job.status})")
    # Move the file back from error folder to upload dir so it can be re-processed
    cfg = get_settings()
    stored = Path(job.stored_path) if job.stored_path else None
    if stored is None or not stored.is_file():
        # Try to find the file in the error folder
        f = workflow.folders()
        if f["error"]:
            candidate = f["error"] / (stored.name if stored else job.filename)
            if candidate.is_file():
                dest_dir = Path(cfg.upload_dir)
                dest_dir.mkdir(parents=True, exist_ok=True)
                new_path = dest_dir / f"{int(time.time() * 1000)}_{job.filename}"
                import shutil as _shutil
                _shutil.copyfile(str(candidate), str(new_path))
                job.stored_path = str(new_path)
    job.status = "QUEUED"
    job.error = None
    job.stage = "Queued for retry"
    job.started_at = None
    job.finished_at = None
    db.commit()
    _wake.set()
    return job


def now():
    return datetime.now(timezone.utc)


def enqueue(db, src: Path, filename: str, source: str = "UPLOAD", user: str | None = None,
            folder_path: str | None = None) -> Job:
    cfg = get_settings()
    dest_dir = Path(cfg.upload_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{int(time.time() * 1000)}_{Path(filename).name}"
    if Path(src) != dest:
        shutil.copyfile(src, dest)
    job = Job(filename=Path(filename).name, stored_path=str(dest), source=source, created_by=user,
              folder_path=folder_path)
    db.add(job)
    db.commit()
    _wake.set()
    return job


def run_job(job_id: int) -> None:
    SessionLocal = session_factory()
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if job is None or job.status != "QUEUED":
            return
        job.status, job.started_at, job.stage = "RUNNING", now(), "Starting"
        db.commit()

        log.info("extraction started: job %s  %s  (%s)", job_id, job.filename, job.source)

        def progress(stage):
            job.stage = stage
            db.commit()
        try:
            res = process_pdf(db, job.stored_path, job.filename, actor=job.created_by, source=job.source,
                              progress=progress)
            job.status = "DUPLICATE" if res.outcome == "DUPLICATE" else "DONE"
            job.invoice_id, job.queue, job.stage = res.invoice_id, res.queue, "Finished"
            log.info("extraction finished: job %s  %s  -> %s / %s  invoice_id=%s", job_id, job.filename,
                     res.outcome, res.queue, res.invoice_id)
            workflow.place_after_extraction(job, res.outcome, res.queue)      # processing -> output\\<queue>
            if res.outcome == "CREATED" and res.queue == "MATCHED":
                robot.schedule_auto()                   # matched invoices go to the MIRO robot by themselves
        except Exception as exc:
            db.rollback()
            log.exception("job %s failed", job_id)
            job = db.get(Job, job_id)
            # Produce a meaningful error message instead of a generic one
            raw_err = f"{type(exc).__name__}: {exc}"
            friendly = _friendly_error(exc, raw_err)
            job.status, job.error, job.stage = "FAILED", friendly[:1000], "Failed"
            workflow.place_failed(job)                                           # processing -> error
            audit(db, "EXTRACTION_FAILED", None, {"file": job.filename, "error": job.error,
                                                   "raw_error": raw_err[:500]}, job.created_by)
        job.finished_at = now()
        db.commit()


def _loop():
    SessionLocal = session_factory()
    while not _stop.is_set():
        with SessionLocal() as db:
            jid = db.scalar(select(Job.id).where(Job.status == "QUEUED").order_by(Job.id).limit(1))
        if jid:
            run_job(jid)
            continue
        _wake.wait(3)
        _wake.clear()


def _settled(p: Path, seconds: int) -> bool:
    """True when the file has not changed for `seconds` (the user may still be copying it in)."""
    try:
        st = p.stat()
        return st.st_size > 0 and time.time() - st.st_mtime >= seconds
    except OSError:
        return False


def scan_folder(folder: Path, settle: int = 0) -> int:
    """Input -> processing: queue every settled PDF or image (jpg/png/tif...) of `folder` and move it to the processing folder.
    Returns how many were queued."""
    n = 0
    SessionLocal = session_factory()
    f = workflow.folders()
    processing = f["processing"] if f["input"] is not None and f["input"] == folder else folder.parent / "processing"
    wanted = (".pdf",) + pdfio.IMAGE_EXTS
    files = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in wanted)
    for pdf in files:
        if not _settled(pdf, settle):
            continue
        try:
            moved = workflow.move(pdf, processing)                       # 1. input -> 2. processing
            if moved is None:
                continue
            try:
                with SessionLocal() as db:
                    enqueue(db, moved, pdf.name, "WATCH", "WATCHER", folder_path=str(moved))
            except Exception:
                workflow.move(moved, folder)                              # could not queue -> back to input, retry later
                raise
            n += 1
        except PermissionError:
            log.info("%s is still open in another program - will retry", pdf.name)
        except Exception:
            log.exception("could not pick up %s", pdf)
    return n


def _watch():
    cfg = get_settings()
    folder = Path(cfg.watch_folder)
    while not _stop.is_set():
        try:
            if folder.is_dir():
                scan_folder(folder, cfg.watch_settle_seconds)
                workflow.sweep()                                          # output / finish follow the dashboard
            else:
                log.warning("WATCH_FOLDER %s does not exist or is not reachable", folder)
        except Exception:
            log.exception("watch folder error")
        _stop.wait(cfg.watcher_poll_seconds)


def start() -> None:
    cfg = get_settings()
    _stop.clear()
    with session_factory()() as db:                          # jobs interrupted by a restart go back to the queue
        for j in db.scalars(select(Job).where(Job.status == "RUNNING")):
            j.status = "QUEUED"
        db.commit()
    if not cfg.watch_folder:
        log.warning("WATCH_FOLDER is not set - no invoices will be picked up automatically")
    else:
        workflow.ensure_folders()
        log.info("PDF folders: %s", {k: str(v) for k, v in workflow.folders().items()})
    for fn, ok in ((_loop, True), (_watch, bool(cfg.watch_folder))):
        if ok:
            t = threading.Thread(target=fn, daemon=True, name=fn.__name__)
            t.start()
            _threads.append(t)


def stop() -> None:
    _stop.set()
    _wake.set()
    _threads.clear()
