from __future__ import annotations

from collections import deque
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import RobotRun, User
from ..security import current_user, require
from ..services import robot
from ..services.persist import audit
from ..services.sap import get_sap

router = APIRouter(prefix="/api", tags=["robot"])


@router.get("/robot/status")
def robot_status(_: User = Depends(current_user)):
    return robot.status()


@router.post("/robot/run")
def robot_run(db: Session = Depends(get_db), user: User = Depends(require("approver"))):
    r = robot.trigger(user.username, "MANUAL")
    if not r["started"] and not r["queued"]:
        raise HTTPException(409, r["reason"])
    audit(db, "ROBOT_START_REQUESTED", None, r, user.username)
    return r


@router.post("/robot/stop")
def robot_stop(db: Session = Depends(get_db), user: User = Depends(require("admin"))):
    ok = robot.stop_current()
    audit(db, "ROBOT_STOP_REQUESTED", None, {"stopped": ok}, user.username)
    return {"stopped": ok}


@router.get("/robot/runs")
def robot_runs(limit: int = 20, db: Session = Depends(get_db), _: User = Depends(current_user)):
    return [robot.run_dict(r) for r in db.scalars(select(RobotRun).order_by(RobotRun.id.desc()).limit(min(limit, 100)))]


def _run(db: Session, run_id: int) -> RobotRun:
    r = db.get(RobotRun, run_id)
    if r is None:
        raise HTTPException(404, "Run not found")
    return r


@router.get("/robot/runs/{run_id}/log", response_class=PlainTextResponse)
def robot_log(run_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    r = _run(db, run_id)
    if not r.log_path or not Path(r.log_path).is_file():
        return "(no log yet)"
    with open(r.log_path, "r", encoding="utf-8", errors="replace") as fh:
        return "".join(deque(fh, maxlen=400))              # last 400 lines


@router.get("/robot/runs/{run_id}/report")
def robot_report(run_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    r = _run(db, run_id)
    if not r.report_path or not Path(r.report_path).is_file():
        raise HTTPException(404, "Robot report not available for this run")
    return FileResponse(r.report_path, media_type="text/html")


@router.get("/sap/hsn-columns")
def hsn_columns(_: User = Depends(require("approver"))):
    """Which SQL columns the HSN / NCM check reads (auto-detected in MSEG / EKPO / MARC)."""
    gw = get_sap()
    if gw is None:
        raise HTTPException(503, "SQL Server (SAP data) is not connected")
    return {"columns": gw.hsn_columns()}
