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
    r = client.patch(f"/api/invoices/{inv['id']}", json={"po_number": "8100007531", "grn_number": "5000343863"})
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
