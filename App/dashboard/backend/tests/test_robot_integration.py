"""Dashboard <-> MIRO robot, end to end. Runs the REAL robot (Robot Framework) with its built-in SAP simulator
against a real PostgreSQL that holds the dashboard's tables.

  PG_TEST_URL=postgresql+psycopg://appuser:pw@localhost/miro_invoice  ROBOT_TEST_DIR=/path/to/miro_robot  pytest tests/test_robot_integration.py
"""
import os
import shutil
import sys
import time

import pytest

PG = os.environ.get("PG_TEST_URL")
ROBOT = os.environ.get("ROBOT_TEST_DIR")
pytestmark = pytest.mark.skipif(not (PG and ROBOT), reason="PG_TEST_URL / ROBOT_TEST_DIR not set")

from sqlalchemy import text                                     # noqa: E402
from sqlalchemy.engine import make_url                           # noqa: E402

from app import database                                         # noqa: E402
from app.config import reset_settings_cache                      # noqa: E402
from app.models import AuditLog, Invoice, InvoiceLine, RobotRun  # noqa: E402
from app.services import robot                                   # noqa: E402


def _fx():
    sys.path.insert(0, os.path.join(ROBOT, "tests"))
    import fixtures                                              # robot's own scenario
    return fixtures


@pytest.fixture()
def env(tmp_path, monkeypatch):
    fx = _fx()
    u = make_url(PG)
    eng = database.init_engine(PG)
    with eng.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS robot_runs, audit_logs, invoice_lines, invoice_master, invoices, "
                       "gst_tax_codes, app_users, extraction_jobs CASCADE"))
    database.Base.metadata.create_all(eng)
    root = tmp_path / "miro_robot"                                # copy WITHOUT the real .env / old output
    shutil.copytree(ROBOT, root, ignore=shutil.ignore_patterns(".env", "output", "__pycache__", ".git"))
    sc = fx.write_scenario(tmp_path / "scenario.json")
    for k, v in {"PG_HOST": u.host, "PG_PORT": str(u.port or 5432), "PG_DB": u.database, "PG_USER": u.username,
                 "PG_PASSWORD": u.password, "MIRO_SAP_BACKEND": "fake", "MIRO_FAKE_SCENARIO": str(sc),
                 "SAP_CONNECTION": "KTFL PRD", "SAP_USER": "ROBOT01", "SAP_PASSWORD": "secret", "SAP_CLIENT": "150",
                 "MIRO_OUTPUT_DIR": str(tmp_path / "out"), "MIRO_POLL_INTERVAL": "0.01",
                 "ROBOT_ENABLED": "true", "ROBOT_DIR": str(root), "ROBOT_PYTHON": sys.executable,
                 "ROBOT_LOG_DIR": str(tmp_path / "runs"), "ROBOT_AUTO_RUN": "true", "ROBOT_AUTO_DELAY_SECONDS": "1",
                 "ROBOT_TIMEOUT_MINUTES": "5", "MIRO_MODE": "POST", "ROBOT_MODE": ""}.items():
        monkeypatch.setenv(k, v)
    reset_settings_cache()
    # dashboard invoices mirror the robot's scenario
    with database.session_factory()() as db:
        for inv, dt, po, grn, sub, tot, tax, st in fx.INVOICES:
            row = Invoice(filename=f"{inv}.pdf", vendor="ACME FORGE", invoice_number=inv, invoice_date=dt, po_number=po,
                          grn_number=grn, subtotal=sub, tax=round(tot - sub, 2), total=tot, gst_tax_code=tax,
                          currency="INR", plant_code="2002", business_place="2002", status=st, queue=("MATCHED" if st == "MATCHED" else "RECONCILE"))
            db.add(row)
            db.flush()
            if inv == "428/26-27":
                for r in fx.PO1_ROWS:
                    if r["ref"] == fx.GRN_A:
                        db.add(InvoiceLine(invoice_id=row.id, item=r["text"], quantity=r["qty"], amount=r["amount"]))
        db.commit()
    yield
    robot._stop.set()


def _wait(timeout=120):
    t0 = time.time()
    while robot._running_id is not None and time.time() - t0 < timeout:
        time.sleep(0.5)
    assert robot._running_id is None, "robot did not finish"


def _by_no(db, no):
    return db.query(Invoice).filter_by(invoice_number=no).one()


def test_robot_run_reads_dashboard_and_writes_back(env):
    """Real robot process <-> dashboard PostgreSQL. (The SAP step itself is the robot's built-in simulator, which in the
    delivered robot zip is older than the robot - so the final SAP outcome may be FAILED here; what is verified is
    the dashboard side: start, single-run lock, which invoices are picked, write-back, run record, audit.)"""
    ok, why = robot.configured()
    assert ok, why
    with database.session_factory()() as db:
        assert robot.pending_count(db, "POST") == 6              # 6 MATCHED waiting, the REVIEW one is not counted
    r = robot.trigger("tester", "MANUAL")
    assert r["started"] is True
    again = robot.trigger("tester", "MANUAL")                    # second click while running -> queued, never parallel
    assert again["queued"] is True and again["started"] is False
    _wait()
    with database.session_factory()() as db:
        run = db.query(RobotRun).order_by(RobotRun.id).first()
        assert run.status in ("DONE", "FAILED") and run.finished_at and run.log_path and os.path.isfile(run.log_path)
        assert run.mode == "POST" and run.triggered_by == "tester"
        assert _by_no(db, "REV/1").miro_status in (None, "")     # not MATCHED -> robot never touches it
        picked = [i for i in db.query(Invoice).filter(Invoice.queue == "MATCHED").all()]
        assert picked and all(i.miro_status for i in picked), [(i.invoice_number, i.miro_status) for i in picked]
        assert all(i.miro_message for i in picked if i.miro_status in ("FAILED", "REVIEW"))   # reason is stored
        assert db.query(AuditLog).filter(AuditLog.action == "ROBOT_RUN").count() >= 1
        assert run.summary                                       # what changed during the run
    # the queued second run executed right after the first
    time.sleep(1)
    _wait()
    with database.session_factory()() as db:
        assert db.query(RobotRun).count() == 2


def test_check_mode_is_visible_as_dry_run(env, monkeypatch):
    monkeypatch.setenv("ROBOT_MODE", "CHECK")
    reset_settings_cache()
    robot.trigger("tester", "MANUAL")
    _wait()
    with database.session_factory()() as db:
        assert db.query(Invoice).filter(Invoice.miro_status == "POSTED").count() == 0     # nothing posted
        assert db.query(AuditLog).filter(AuditLog.action.like("MIRO_CHECK_%")).count() >= 1
        from app.routers.common import apply_effective, invoice_row
        invs = db.query(Invoice).filter(Invoice.status == "MATCHED").all()
        rows = [invoice_row(i) for i in invs]
        apply_effective(db, invs, rows)
        assert any(r.get("dry_run") and r["miro_status"] for r in rows)       # dry-run result is shown to the user


def test_not_configured_is_reported_not_crashing(monkeypatch):
    monkeypatch.setenv("ROBOT_ENABLED", "true")
    monkeypatch.setenv("ROBOT_DIR", "/nonexistent/robot")
    reset_settings_cache()
    assert robot.trigger("x")["started"] is False
    assert "not found" in robot.configured()[1]
