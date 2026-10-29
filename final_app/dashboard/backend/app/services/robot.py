"""
Starts the MIRO robot (Robot Framework, SAP GUI scripting) from the dashboard and reports what it did.

 * MATCHED invoices are picked up automatically (debounced) - robot_auto_run
 * "Push to SAP MIRO" and "Run robot now" start it immediately
 * only ONE robot process at a time (one SAP GUI session); a start request while it runs is remembered
   and the robot is started again right after, so nothing waits for the next schedule
 * the robot reads its own .env (PG + SAP credentials). The dashboard never reads or shows them; it only
   looks for MIRO_MODE to display the mode.
"""
from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, or_, select

from ..config import get_settings
from ..database import session_factory
from ..models import AuditLog, Invoice, RobotRun
from .persist import audit

log = logging.getLogger("clearmatch.robot")

_lock = threading.RLock()
_running_id: int | None = None
_rerun = False
_auto_due: float | None = None
_proc: subprocess.Popen | None = None
_stop = threading.Event()
_thread: threading.Thread | None = None


def now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------------------ configuration
def robot_paths():
    """(robot folder, task file). Forgiving: ROBOT_DIR may also be given as the task file or the tasks folder."""
    cfg = get_settings()
    if not cfg.robot_dir.strip():
        return None, None
    root = Path(cfg.robot_dir.strip().strip('"').strip("'")).expanduser()
    task_rel = cfg.robot_task.strip() or "tasks/miro_posting.robot"
    if root.suffix.lower() == ".robot":                      # ROBOT_DIR=...\miro_robot\tasks\miro_posting.robot
        task = root
        root = root.parent.parent if root.parent.name.lower() == "tasks" else root.parent
        return root, task
    if root.name.lower() == "tasks":                        # ROBOT_DIR=...\miro_robot\tasks
        root = root.parent
    return root, root / task_rel


def configured() -> tuple[bool, str]:
    cfg = get_settings()
    if not cfg.robot_enabled:
        return False, "Robot is switched off (ROBOT_ENABLED=false)"
    root, task = robot_paths()
    if root is None:
        return False, "ROBOT_DIR is not set"
    if not root.is_dir():
        return False, f"ROBOT_DIR folder not found: {root}"
    if not task.is_file():
        return False, f"Robot task file not found: {task}"
    return True, ""


def robot_mode() -> str:
    """CHECK / PARK / POST. Only the MIRO_MODE line of the robot's .env is read - nothing else."""
    cfg = get_settings()
    if cfg.robot_mode.strip():
        return cfg.robot_mode.strip().upper()
    root, _ = robot_paths()
    if root and (root / ".env").is_file():
        try:
            for line in (root / ".env").read_text(encoding="utf-8", errors="ignore").splitlines():
                m = re.match(r"\s*MIRO_MODE\s*=\s*([A-Za-z]+)", line)
                if m:
                    return m.group(1).upper()
        except OSError:
            pass
    return os.environ.get("MIRO_MODE", "CHECK").upper()


def pending_count(db, mode: str | None = None) -> int:
    """Matched invoices the robot still has to handle in the current mode.
    CHECK mode (dry run) never writes invoices.miro_status - its result is only in audit_logs (MIRO_CHECK_*).
    An invoice that already has such a result (and was not changed since) is NOT waiting, so a restart of the
    dashboard or a new run does not start the robot again for invoices that were already checked."""
    mode = mode or robot_mode()
    cond = or_(Invoice.miro_status.is_(None), Invoice.miro_status == "")
    if mode != "CHECK":
        cond = or_(cond, Invoice.miro_status == "CHECKED",
                   (Invoice.miro_status == "FAILED") & (func.coalesce(Invoice.miro_attempts, 0) < 3))
    q = select(Invoice).where(Invoice.status == "MATCHED", Invoice.queue == "MATCHED", cond)
    n = 0
    checked: dict[int, datetime] = {}
    if mode == "CHECK":
        for inv_id, at in db.execute(select(AuditLog.invoice_id, func.max(AuditLog.created_at)).where(
                AuditLog.action.like("MIRO_CHECK_%")).group_by(AuditLog.invoice_id)).all():
            if inv_id is not None:
                checked[inv_id] = at
    for inv in db.scalars(q):
        at = checked.get(inv.id)
        if at is not None:
            a = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
            u = inv.updated_at
            u = (u if u.tzinfo else u.replace(tzinfo=timezone.utc)) if u else None
            if u is None or u <= a + timedelta(seconds=3):
                continue                                          # already dry-run checked, unchanged since
        n += 1
    return n


# ------------------------------------------------------------------------------ status / start
def status() -> dict:
    ok, why = configured()
    with session_factory()() as db:
        last = db.scalar(select(RobotRun).order_by(RobotRun.id.desc()).limit(1))
        mode = robot_mode()
        return {"enabled": get_settings().robot_enabled, "configured": ok, "problem": why, "mode": mode,
                "running": _running_id is not None, "running_id": _running_id,
                "auto_run": get_settings().robot_auto_run, "waiting": pending_count(db, mode) if ok else 0,
                "next_auto_in": max(0, int(_auto_due - time.time())) if _auto_due else None,
                "last": run_dict(last) if last else None}


def run_dict(r: RobotRun) -> dict:
    return {"id": r.id, "status": r.status, "trigger": r.trigger, "by": r.triggered_by, "mode": r.mode,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "exit_code": r.exit_code, "summary": r.summary or {}, "error": r.error,
            "has_report": bool(r.report_path)}


def trigger(by: str | None = "system", reason: str = "MANUAL") -> dict:
    """Start the robot now (or remember to run again when the current run ends)."""
    global _running_id, _rerun, _auto_due, _thread
    ok, why = configured()
    if not ok:
        return {"started": False, "queued": False, "reason": why}
    with _lock:
        _auto_due = None
        if _running_id is not None:
            _rerun = True
            return {"started": False, "queued": True, "run_id": _running_id,
                    "reason": "Robot is already running - it will run again right after this run"}
        with session_factory()() as db:
            run = RobotRun(trigger=reason, triggered_by=by, mode=robot_mode(), status="RUNNING")
            db.add(run)
            db.commit()
            _running_id = run.id
        t = threading.Thread(target=_execute, args=(run.id, by), daemon=True, name=f"robot-run-{run.id}")
        t.start()
        return {"started": True, "queued": False, "run_id": run.id, "reason": ""}


def schedule_auto() -> None:
    """A matched invoice appeared: start the robot after robot_auto_delay_seconds of quiet."""
    global _auto_due
    cfg = get_settings()
    if cfg.robot_enabled and cfg.robot_auto_run and configured()[0]:
        _auto_due = time.time() + cfg.robot_auto_delay_seconds


def _kill(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=30)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _command(out_dir: Path) -> list[str]:
    cfg = get_settings()
    root, task = robot_paths()
    py = cfg.robot_python.strip() or sys.executable
    return [py, "-m", "robot", "--outputdir", str(out_dir), "--consolewidth", "120", str(task)]


def _summarise(db, started: datetime) -> dict:
    """What changed while the robot ran: new miro_status values and MIRO_* audit rows."""
    since = started - timedelta(seconds=5)
    counts: dict[str, int] = {}
    for st, n in db.execute(select(Invoice.miro_status, func.count()).where(Invoice.miro_updated_at >= since)
                            .group_by(Invoice.miro_status)).all():
        counts[st or "NONE"] = n
    for act, n in db.execute(select(AuditLog.action, func.count()).where(
            AuditLog.created_at >= since, AuditLog.action.like("MIRO_CHECK_%")).group_by(AuditLog.action)).all():
        counts[act.replace("MIRO_CHECK_", "CHECK_")] = n
    return counts


def _execute(run_id: int, by: str | None) -> None:
    global _running_id, _rerun, _proc
    cfg = get_settings()
    root, _ = robot_paths()
    log_dir = Path(cfg.robot_log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"run_{run_id}.log"
    out_dir = root / "output" / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    status_, error, code = "DONE", None, None
    started = now()
    try:
        env = dict(os.environ)
        if cfg.robot_mode.strip():
            env["MIRO_MODE"] = cfg.robot_mode.strip().upper()
        kw: dict = {}
        if os.name == "nt":
            kw["creationflags"] = 0x08000000                    # CREATE_NO_WINDOW
        else:
            kw["start_new_session"] = True
        with open(log_file, "wb") as fh:
            _proc = subprocess.Popen(_command(out_dir), cwd=str(root), stdout=fh, stderr=subprocess.STDOUT, env=env, **kw)
            try:
                code = _proc.wait(timeout=cfg.robot_timeout_minutes * 60)
            except subprocess.TimeoutExpired:
                _kill(_proc)
                status_, error = "TIMEOUT", f"Robot stopped after {cfg.robot_timeout_minutes} minutes"
        if status_ == "DONE" and code != 0:
            status_, error = "FAILED", f"Robot finished with {code} failed task(s) - see the log"
    except Exception as exc:
        log.exception("robot run failed to start")
        status_, error = "FAILED", f"{type(exc).__name__}: {exc}"
    finally:
        _proc = None
    with session_factory()() as db:
        run = db.get(RobotRun, run_id)
        run.status, run.exit_code, run.error, run.finished_at = status_, code, error, now()
        run.log_path = str(log_file)
        report = out_dir / "log.html"
        run.report_path = str(report) if report.is_file() else None
        run.summary = _summarise(db, started)
        audit(db, "ROBOT_RUN", None, {"run_id": run_id, "status": status_, "summary": run.summary,
                                      "mode": run.mode}, by, commit=False)
        db.commit()
    with _lock:
        again, _rerun, _running_id = _rerun, False, None
    if again:
        trigger(by, "AUTO")


def stop_current() -> bool:
    p = _proc
    if p and p.poll() is None:
        _kill(p)
        return True
    return False


# ------------------------------------------------------------------------------ background timer
def _loop() -> None:
    global _auto_due
    while not _stop.is_set():
        due = _auto_due
        if due and time.time() >= due and _running_id is None:
            _auto_due = None
            try:
                r = trigger("AUTO", "AUTO")
                log.info("auto-start robot: %s", r)
            except Exception:
                log.exception("auto start failed")
        _stop.wait(3)


def start() -> None:
    """Called at application start-up."""
    global _thread, _running_id
    with session_factory()() as db:                              # a restart killed the robot's watcher, not the robot
        for r in db.scalars(select(RobotRun).where(RobotRun.status == "RUNNING")):
            r.status, r.error, r.finished_at = "INTERRUPTED", "Dashboard restarted while the robot was running", now()
        db.commit()
        waiting = pending_count(db) if configured()[0] else 0
    _running_id = None
    _stop.clear()
    cfg = get_settings()
    if cfg.robot_enabled:
        ok, why = configured()
        if not ok:
            log.warning("Robot not usable: %s", why)
        else:
            log.info("Robot ready (mode %s, folder %s)", robot_mode(), cfg.robot_dir)
            if waiting and cfg.robot_auto_run:
                schedule_auto()
        _thread = threading.Thread(target=_loop, daemon=True, name="robot-auto")
        _thread.start()


def stop() -> None:
    _stop.set()
