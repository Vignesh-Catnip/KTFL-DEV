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
from .pipeline import process_pdf

log = logging.getLogger("clearmatch.worker")
_stop = threading.Event()
_threads: list[threading.Thread] = []
_wake = threading.Event()


def now():
    return datetime.now(timezone.utc)


def enqueue(db, src: Path, filename: str, source: str = "UPLOAD", user: str | None = None) -> Job:
    cfg = get_settings()
    dest_dir = Path(cfg.upload_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{int(time.time() * 1000)}_{Path(filename).name}"
    if Path(src) != dest:
        shutil.copyfile(src, dest)
    job = Job(filename=Path(filename).name, stored_path=str(dest), source=source, created_by=user)
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

        def progress(stage):
            job.stage = stage
            db.commit()
        try:
            res = process_pdf(db, job.stored_path, job.filename, actor=job.created_by, source=job.source,
                              progress=progress)
            job.status = "DUPLICATE" if res.outcome == "DUPLICATE" else "DONE"
            job.invoice_id, job.queue, job.stage = res.invoice_id, res.queue, "Finished"
        except Exception as exc:
            db.rollback()
            log.exception("job %s failed", job_id)
            job = db.get(Job, job_id)
            job.status, job.error, job.stage = "FAILED", f"{type(exc).__name__}: {exc}"[:1000], "Failed"
            audit(db, "EXTRACTION_FAILED", None, {"file": job.filename, "error": job.error}, job.created_by)
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
    """Queue every settled PDF of `folder` and move it to folder/processed. Returns how many were queued."""
    n = 0
    SessionLocal = session_factory()
    done = folder / "processed"
    for pdf in sorted(folder.glob("*.pdf")) + sorted(folder.glob("*.PDF")):
        if not _settled(pdf, settle):
            continue
        try:
            with SessionLocal() as db:
                enqueue(db, pdf, pdf.name, "WATCH", "WATCHER")      # copies into UPLOAD_DIR first
            done.mkdir(exist_ok=True)
            target = done / pdf.name
            if target.exists():
                target = done / f"{int(time.time())}_{pdf.name}"
            shutil.move(str(pdf), target)
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
    for fn, ok in ((_loop, True), (_watch, bool(cfg.watch_folder))):
        if ok:
            t = threading.Thread(target=fn, daemon=True, name=fn.__name__)
            t.start()
            _threads.append(t)


def stop() -> None:
    _stop.set()
    _wake.set()
    _threads.clear()
