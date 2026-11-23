import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import sap as sapmod, vlm as vlmmod
from tests.conftest import FIX, FakeVlm, dsk_vision, make_sap


@pytest.fixture
def client(engine, fake_vlm, sap):
    with TestClient(create_app()) as c:
        r = c.post("/api/auth/login", json={"username": "admin", "password": "admin-pass-1"})
        c.headers["Authorization"] = "Bearer " + r.json()["token"]
        yield c


def upload(c, name="inv_dsk_428.pdf"):
    with open(FIX / "inv_dsk_428.pdf", "rb") as f:
        r = c.post("/api/jobs/upload", files={"files": (name, f, "application/pdf")})
    assert r.status_code == 202, r.text
    from app.services import worker
    worker.run_job(r.json()["jobs"][0]["id"])
    return r.json()["jobs"][0]["id"]


def test_auth_required(engine):
    with TestClient(create_app()) as c:
        assert c.get("/api/invoices").status_code == 401
        assert c.post("/api/auth/login", json={"username": "admin", "password": "bad"}).status_code == 401


def test_upload_to_matched_and_compare(client):
    upload(client)
    items = client.get("/api/invoices?queue=MATCHED").json()["items"]
    assert len(items) == 1
    d = client.get(f"/api/invoices/{items[0]['id']}").json()
    assert d["compare"]["lines"] and len(d["compare"]["lines"]) == 11
    hsn = next(r for r in d["compare"]["rows"] if r["field"] == "hsn_sac")
    assert hsn["status"] == "OK" and hsn["invoice"] == "82073000" and hsn["po"] == "82073000"
    assert client.get(f"/api/invoices/{d['id']}/page/1.png").headers["content-type"] == "image/png"
    assert client.get("/api/stats").json()["queues"]["MATCHED"] == 1


def test_duplicate_upload(client):
    upload(client)
    jid = upload(client)
    jobs = client.get("/api/jobs").json()
    assert jobs[0]["status"] == "DUPLICATE"
    assert client.get("/api/invoices").json()["total"] == 1


def test_rejects_non_pdf(client):
    r = client.post("/api/jobs/upload", files={"files": ("x.pdf", b"hello", "application/pdf")})
    assert r.status_code == 422


def test_reconcile_flow_error_resolve_push(client):
    sapmod.set_sap(make_sap(qty_override={3: 18}))
    upload(client)
    inv = client.get("/api/invoices").json()["items"][0]
    assert inv["queue"] == "RECONCILE" and inv["exception_type"] == "Quantity mismatch"
    ex = client.get("/api/exceptions").json()
    assert ex["total"] == 1 and ex["items"][0]["bad_lines"][0]["gr_quantity"] == 18
    bl = ex["items"][0]["bad_lines"][0]                         # Error Flow tells WHAT is wrong on the line and what to do
    assert bl["status"] == "QTY" and "Quantity" in bl["problem"] and bl["fix"] and bl["diff_quantity"] == bl["quantity"] - 18
    assert all(x["fix"] for x in ex["items"][0]["issues"])
    assert client.post(f"/api/invoices/{inv['id']}/push").status_code == 409
    r = client.post(f"/api/invoices/{inv['id']}/resolve",
                    json={"resolution": "Accept variance", "note": "short supply ok", "approve_variance": True})
    assert r.json()["queue"] == "MATCHED"
    assert client.post(f"/api/invoices/{inv['id']}/push").status_code == 200


def test_blocked_cannot_be_resolved_then_edit_fixes(client):
    vlmmod.set_vlm(FakeVlm(dsk_vision(po_number="", po_numbers_all=""), grn=""))
    sapmod.set_sap(make_sap(seed=None))
    upload(client)
    inv = client.get("/api/invoices").json()["items"][0]
    assert inv["queue"] == "BLOCKED"
    assert client.post(f"/api/invoices/{inv['id']}/resolve", json={"resolution": "x1"}).status_code == 409
    sapmod.set_sap(make_sap())
    r = client.patch(f"/api/invoices/{inv['id']}", json={"po_number": "5100007531", "grn_number": "5000343863"})
    assert r.status_code == 200 and r.json()["queue"] == "MATCHED"


def test_posting_log_and_requeue(client, db):
    upload(client)
    iid = client.get("/api/invoices").json()["items"][0]["id"]
    from app.database import session_factory
    from app.models import Invoice
    with session_factory()() as s:       # the robot writes these columns
        i = s.get(Invoice, iid)
        i.miro_status, i.miro_message = "FAILED", "Balance not zero"
        s.commit()
    p = client.get("/api/posting").json()
    assert p["counts"]["FAILED"] == 1 and p["items"][0]["miro_message"] == "Balance not zero"
    assert "Balance not zero" in client.get("/api/posting/export.csv").text
    assert client.post(f"/api/posting/{iid}/requeue").status_code == 200
    assert client.get("/api/posting?status=WAITING").json()["items"][0]["id"] == iid


def test_viewer_cannot_edit(client):
    client.post("/api/auth/users", json={"username": "v1", "password": "viewer-pass-1", "role": "viewer"})
    upload(client)
    iid = client.get("/api/invoices").json()["items"][0]["id"]
    tok = client.post("/api/auth/login", json={"username": "v1", "password": "viewer-pass-1"}).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert client.patch(f"/api/invoices/{iid}", json={"vendor": "X"}, headers=h).status_code == 403
    assert client.get("/api/invoices", headers=h).status_code == 200


def test_audit_trail(client):
    upload(client)
    acts = [a["action"] for a in client.get("/api/audit").json()]
    assert "EXTRACTED_MATCHED" in acts and "LOGIN" in acts


def test_upload_switched_off(client):
    from app.config import get_settings
    cfg = get_settings()
    cfg.allow_upload = False
    try:
        with open(FIX / "inv_dsk_428.pdf", "rb") as f:
            r = client.post("/api/jobs/upload", files={"files": ("a.pdf", f, "application/pdf")})
        assert r.status_code == 403
        c = client.get("/api/config").json()
        assert c["allow_upload"] is False and c["min_completeness"] == 0.7
    finally:
        cfg.allow_upload = True


def test_robot_endpoints_and_push_starts_robot(client, monkeypatch):
    from app.services import robot
    calls = []
    monkeypatch.setattr(robot, "trigger", lambda by="system", reason="MANUAL":
                        calls.append((by, reason)) or {"started": True, "queued": False, "run_id": 7, "reason": ""})
    upload(client)
    inv = client.get("/api/invoices?queue=MATCHED").json()["items"][0]
    st = client.get("/api/robot/status").json()
    assert st["configured"] is False and st["enabled"] is False          # off by default, reported not crashing
    assert client.post("/api/robot/run").status_code in (200, 409)
    r = client.post(f"/api/invoices/{inv['id']}/push")
    assert r.status_code == 200 and r.json()["robot"]["run_id"] == 7 and "Robot started" in r.json()["message"]
    assert calls and calls[-1][0] == "admin"
    assert client.get("/api/robot/runs").json() == []
    assert client.get("/api/robot/runs/999/log").status_code == 404
    assert client.get("/api/health").json()["robot"]["enabled"] is False
    # a reconcile / blocked invoice can not be pushed
    csv = client.get("/api/posting/export.csv")
    assert csv.status_code == 200 and "SAP status" in csv.text


def test_doc_type_master_api_and_invoice_doc_type(client):
    rows = client.get("/api/doc-types").json()
    assert len(rows) == 18 and {r["doc_type"] for r in rows if r["plant_code"] == "2022"} == {"X4", "XD"}
    upload(client)
    inv = client.get("/api/invoices?queue=MATCHED").json()["items"][0]
    assert inv["doc_type"] == "X1" and inv["doc_type_desc"] == "Invoice - R'gaon R1"      # plant 2002; the text SAP shows
    d = client.get(f"/api/invoices/{inv['id']}").json()
    assert d["doc_type_options"][0]["doc_type"] == "X1"
    r = client.post("/api/doc-types", json={"doc_type": "xz", "plant_code": "2999", "description": "Test"})
    assert r.status_code == 200 and r.json()["doc_type"] == "XZ"
    assert client.post("/api/doc-types", json={"doc_type": "XZ", "plant_code": "2999"}).status_code == 409
    assert client.delete(f"/api/doc-types/{r.json()['id']}").status_code == 200


def test_edit_doc_type_is_validated_against_plant(client):
    upload(client)
    inv = client.get("/api/invoices?queue=MATCHED").json()["items"][0]
    r = client.patch(f"/api/invoices/{inv['id']}", json={"doc_type": "xd"})            # XD belongs to plant 2022, not 2002
    assert r.status_code == 200
    d = client.get(f"/api/invoices/{inv['id']}").json()
    assert d["doc_type"] == "X1" and d["doc_type_desc"] == "Invoice - R'gaon R1"      # plant 2002 has only X1


def test_gst_tax_code_edit_by_rate_or_code_sticks(client):
    """Typing '9%' (or 18, or 'i3') in the GST tax code box gives the master's code (I3) and it survives the re-check."""
    upload(client)
    inv = client.get("/api/invoices").json()["items"][0]
    iid = inv["id"]
    for typed in ("9%", "18", "i3"):
        r = client.patch(f"/api/invoices/{iid}", json={"gst_tax_code": typed})
        assert r.status_code == 200, (typed, r.text)
        d = client.get(f"/api/invoices/{iid}").json()
        assert d["gst_tax_code"] == "I3", (typed, d["gst_tax_code"])
    r = client.patch(f"/api/invoices/{iid}", json={"gst_tax_code": "banana"})
    assert r.status_code == 422
    assert client.get(f"/api/invoices/{iid}").json()["gst_tax_code"] == "I3"


def test_gst_tax_code_box_text_is_forgiven(client):
    """Real case: the box held ' (0%)' and the person typed I3 in front -> 'I3 (0%)' was refused. Now the code in front wins."""
    upload(client)
    iid = client.get("/api/invoices").json()["items"][0]["id"]
    for typed in ("I3 (0%)", "I3 (18%)", "(18%)", "I3 - Input CGST+SGST @ 9%+9%", " 18 % "):
        r = client.patch(f"/api/invoices/{iid}", json={"gst_tax_code": typed})
        assert r.status_code == 200, (typed, r.text)
        assert client.get(f"/api/invoices/{iid}").json()["gst_tax_code"] == "I3", typed


def test_gst_nature_from_printed_rate_and_states(client):
    """Real invoice (Polycab): totals page missing, GST% 18 printed in the item table, seller + buyer both GSTIN 27."""
    from app.database import session_factory
    from app.services.gst import apply_tax_code, compute_gst
    from app.services.normalize import new_header
    def hdr(seller, buyer):
        h = new_header("x.pdf")
        h.update(gstin=seller, buyer_gstin=buyer, gst_rate_printed=18.0, sap_tax_code="I3")
        return h
    with session_factory()() as db:
        h = hdr("27AABCI4883P1ZV", "27AABCK0618A1Z7"); notes = []
        apply_tax_code(db, h, notes)
        assert (h["gst_nature"], h["gst_percent"], h["gst_percent_component"], h["gst_tax_code"]) == ("CGST_SGST", 18.0, 9.0, "I3")
        assert h["tax_code_match"] is True
        h = hdr("29AAJCS4396P1Z9", "27AABCK0618A1Z7"); apply_tax_code(db, h, [])
        assert (h["gst_nature"], h["gst_tax_code"], h["tax_code_match"]) == ("IGST", "I8", False)
        h = hdr("", ""); apply_tax_code(db, h, [])                       # states unknown: no guess
        assert h["gst_tax_code"] == "" and h["gst_percent"] is None
        h = new_header("x.pdf"); h.update(gst_percent=18.0, gst_percent_component=9.0, gst_nature="CGST_SGST")   # re-check, no amounts
        compute_gst(h)
        assert (h["gst_percent"], h["gst_nature"]) == (18.0, "CGST_SGST")
