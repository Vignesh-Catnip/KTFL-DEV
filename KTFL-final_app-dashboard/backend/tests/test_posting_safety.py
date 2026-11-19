"""Duplicate protection and robot posting safety (Phase 7)."""
from sqlalchemy import text

from app import database
from app.models import AuditLog, Invoice
from app.services import robot
from app.services.pipeline import process_pdf
from tests.conftest import FIX, FakeVlm, dsk_vision, make_sap

PDF = FIX / "inv_dsk_428.pdf"


def matched_invoice(db):
    r = process_pdf(db, PDF, "inv_dsk_428.pdf", vlm=FakeVlm(dsk_vision(), "50-343863"), gw=make_sap())
    inv = db.get(Invoice, r.invoice_id)
    assert inv.queue == "MATCHED"
    return inv


def test_dedupe_index_is_reported_present(db, engine):
    ok, why = database.dedupe_protection_ok(engine)
    assert ok, why


def test_missing_dedupe_index_disables_posting(db, engine, monkeypatch):
    matched_invoice(db)
    assert robot._pending_count(db, "POST") == 1
    with engine.begin() as c:
        c.execute(text(f"DROP INDEX {database.DEDUPE_INDEX}"))
    ok, why = database.dedupe_protection_ok(engine)
    assert not ok and "missing" in why
    monkeypatch.setattr(database, "get_engine", lambda: engine)
    assert robot.pending_count(db, "POST") == 0                          # nothing is "waiting" without duplicate protection
    monkeypatch.setattr(robot, "configured", lambda: (True, ""))
    r = robot.trigger("t", "MANUAL")
    assert r["started"] is False and "missing" in r["reason"]


def test_killed_robot_leaves_unknown_never_retryable(db):
    inv = matched_invoice(db)
    inv.miro_status = "IN_PROGRESS"
    db.commit()
    assert robot.quarantine_in_progress(db) == 1
    db.refresh(inv)
    assert inv.miro_status == "UNKNOWN" and "MIR4" in inv.miro_message
    assert robot._pending_count(db, "POST") == 0                         # UNKNOWN is not picked up again
    assert db.query(AuditLog).filter(AuditLog.action == "MIRO_UNKNOWN").count() == 1


def test_unknown_and_in_progress_cannot_be_pushed_or_requeued(db):
    inv = matched_invoice(db)
    for st in ("UNKNOWN", "IN_PROGRESS", "POSTED", "PARKED"):
        inv.miro_status = st
        db.commit()
        assert robot._pending_count(db, "POST") == 0
