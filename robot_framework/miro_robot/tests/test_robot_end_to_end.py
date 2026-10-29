"""
End-to-end: real Robot Framework run + real PostgreSQL + SAP GUI simulator.
Needs a test PostgreSQL (TEST_PG_HOST / TEST_PG_PORT / TEST_PG_PASSWORD).
"""
from pathlib import Path

import psycopg
import pytest
import robot
from psycopg.rows import dict_row

from fixtures import (GRN_A, GRN_A_ROWS, PO1, conninfo, reset_db, set_env, write_scenario)
from miro.fake_sap import FakeSapApplication

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "tasks" / "miro_posting.robot"


def db_rows():
    with psycopg.connect(conninfo(), row_factory=dict_row) as c:
        return {r["invoice_number"]: r for r in c.execute(
            "SELECT invoice_number, status, miro_status, miro_doc_number, miro_message, "
            "miro_attempts, miro_selected_rows FROM invoices ORDER BY id")}


def run_robot(tmp_path, mode, tag):
    out = tmp_path / tag
    rc = robot.run(str(SUITE), outputdir=str(out), loglevel="DEBUG", consolewidth=100)
    return rc, out


@pytest.fixture
def env(tmp_path):
    reset_db()
    FakeSapApplication._instances.clear()
    scen = write_scenario(tmp_path / "scenario.json")
    return scen


def test_post_mode_full_run(tmp_path, env):
    set_env(env, tmp_path / "out", mode="POST")
    rc, out = run_robot(tmp_path, "POST", "run1")
    assert rc == 0, (out / "log.html")
    rows = db_rows()
    app = FakeSapApplication._instances[str(env)]

    # happy path: posted, exactly the GRN rows selected
    ok = rows["428/26-27"]
    assert ok["miro_status"] == "POSTED", ok["miro_message"]
    assert ok["miro_doc_number"] and ok["miro_doc_number"].startswith("51056")
    assert ok["miro_selected_rows"] == ",".join(str(i + 1) for i in GRN_A_ROWS)
    posted = [s for s in app.saved if s["ref"] == "428/26-27"][0]
    assert posted["po"] == PO1 and {r[0] for r in posted["rows"]} == {GRN_A}
    assert len(posted["rows"]) == len(GRN_A_ROWS)

    assert rows["IGST/9"]["miro_status"] == "POSTED"

    # data problems -> REVIEW, never posted
    assert rows["NOGRN/1"]["miro_status"] == "REVIEW" and "not found in MIRO" in rows["NOGRN/1"]["miro_message"]
    assert rows["DUP/26-27"]["miro_status"] == "REVIEW" and "duplicate" in rows["DUP/26-27"]["miro_message"].lower()
    assert rows["GAP/1"]["miro_status"] == "REVIEW" and "taxable" in rows["GAP/1"]["miro_message"]
    # SAP error -> FAILED (retryable)
    assert rows["BADPO/1"]["miro_status"] == "FAILED" and "does not exist" in rows["BADPO/1"]["miro_message"]
    # not MATCHED -> untouched
    assert rows["REV/1"]["miro_status"] is None

    assert sorted(s["ref"] for s in app.saved) == ["428/26-27", "IGST/9"]
    assert app.trace[0] == "LOGIN"

    # second run: nothing is posted twice; FAILED one retried (and fails again)
    rc, _ = run_robot(tmp_path, "POST", "run2")
    rows2 = db_rows()
    assert sorted(s["ref"] for s in app.saved) == ["428/26-27", "IGST/9"]
    assert rows2["BADPO/1"]["miro_attempts"] == 2
    assert rows2["428/26-27"]["miro_attempts"] == 1

    # audit trail written
    with psycopg.connect(conninfo()) as c:
        actions = [r[0] for r in c.execute("SELECT action FROM audit_logs ORDER BY id")]
    assert actions.count("MIRO_POSTED") == 2 and "MIRO_REVIEW" in actions


def test_check_mode_saves_nothing_then_post(tmp_path, env):
    set_env(env, tmp_path / "out", mode="CHECK")
    rc, _ = run_robot(tmp_path, "CHECK", "check")
    assert rc == 0
    rows = db_rows()
    app = FakeSapApplication._instances[str(env)]
    assert rows["428/26-27"]["miro_status"] == "CHECKED"
    assert app.saved == []

    # switch to POST: CHECKED invoices are taken again and posted
    set_env(env, tmp_path / "out", mode="POST")
    rc, _ = run_robot(tmp_path, "POST", "post")
    rows = db_rows()
    assert rows["428/26-27"]["miro_status"] == "POSTED"
    assert sorted(s["ref"] for s in app.saved) == ["428/26-27", "IGST/9"]


def test_park_mode(tmp_path, env):
    set_env(env, tmp_path / "out", mode="PARK")
    rc, _ = run_robot(tmp_path, "PARK", "park")
    rows = db_rows()
    app = FakeSapApplication._instances[str(env)]
    assert rows["428/26-27"]["miro_status"] == "PARKED"
    assert all(s["how"] == "PARK" for s in app.saved)


def test_stale_in_progress_becomes_unknown(tmp_path, env):
    with psycopg.connect(conninfo()) as c:
        c.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS miro_status VARCHAR(20)")
        c.execute("UPDATE invoices SET miro_status='IN_PROGRESS' WHERE invoice_number='IGST/9'")
    set_env(env, tmp_path / "out", mode="POST")
    run_robot(tmp_path, "POST", "stale")
    rows = db_rows()
    app = FakeSapApplication._instances[str(env)]
    assert rows["IGST/9"]["miro_status"] == "UNKNOWN"
    assert "IGST/9" not in [s["ref"] for s in app.saved]


def test_no_document_number_is_unknown_and_not_retried(tmp_path, env):
    import json
    sc = json.loads(env.read_text()); sc["post_no_doc"] = True; env.write_text(json.dumps(sc))
    set_env(env, tmp_path / "out", mode="POST")
    run_robot(tmp_path, "POST", "nodoc")
    rows = db_rows()
    assert rows["428/26-27"]["miro_status"] == "UNKNOWN"
    rc, _ = run_robot(tmp_path, "POST", "nodoc2")           # never re-posted automatically
    assert db_rows()["428/26-27"]["miro_attempts"] == 1


def test_wrong_sap_password_touches_nothing(tmp_path, env):
    set_env(env, tmp_path / "out", mode="POST")
    import os
    os.environ["SAP_PASSWORD"] = "wrong"
    rc, _ = run_robot(tmp_path, "POST", "badpw")
    assert rc != 0
    assert all(r["miro_status"] is None for r in db_rows().values())


def test_empty_queue_passes(tmp_path, env):
    with psycopg.connect(conninfo()) as c:
        c.execute("UPDATE invoices SET status='REVIEW'")
    set_env(env, tmp_path / "out", mode="POST")
    rc, _ = run_robot(tmp_path, "POST", "empty")
    assert rc == 0
