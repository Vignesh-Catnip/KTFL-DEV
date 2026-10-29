"""Runs only when PG_TEST_URL is set (a throw-away PostgreSQL database):
   PG_TEST_URL=postgresql+psycopg://appuser:pw@localhost/miro_invoice pytest tests/test_postgres.py"""
import os

import pytest
from sqlalchemy import inspect, text

PG = os.environ.get("PG_TEST_URL")
pytestmark = pytest.mark.skipif(not PG, reason="PG_TEST_URL not set")

from app import database                                    # noqa: E402
from app.models import Invoice                              # noqa: E402
from app.services.gst import load_gst_master                # noqa: E402
from app.services.pipeline import process_pdf               # noqa: E402
from tests.conftest import FIX, FakeVlm, make_sap           # noqa: E402

LEGACY = """
DROP TABLE IF EXISTS audit_logs, invoice_master, invoice_lines, invoices, gst_tax_codes, app_users, extraction_jobs CASCADE;
CREATE TABLE invoices (id SERIAL PRIMARY KEY, filename VARCHAR(255), vendor VARCHAR(255), invoice_number VARCHAR(100),
  invoice_date VARCHAR(30), po_number VARCHAR(100), gstin VARCHAR(50), hsn_sac VARCHAR(50), vendor_code VARCHAR(50),
  tax_code VARCHAR(30), business_place VARCHAR(100), currency VARCHAR(10) DEFAULT 'INR', subtotal DOUBLE PRECISION DEFAULT 0,
  cgst DOUBLE PRECISION DEFAULT 0, sgst DOUBLE PRECISION DEFAULT 0, igst DOUBLE PRECISION DEFAULT 0, tax DOUBLE PRECISION DEFAULT 0,
  total DOUBLE PRECISION DEFAULT 0, confidence DOUBLE PRECISION DEFAULT 0, grn_number VARCHAR(100), handwritten_references TEXT,
  status VARCHAR(40) DEFAULT 'REVIEW', error_message TEXT, created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now());
INSERT INTO invoices (filename, invoice_number, status) VALUES ('old.pdf','OLD-1','MATCHED');
"""


def test_upgrade_legacy_schema_and_process():
    eng = database.init_engine(PG)
    with eng.begin() as c:
        for stmt in [s for s in LEGACY.split(";\n") if s.strip()]:
            c.execute(text(stmt))
    added = database.ensure_schema(eng)
    assert "invoices.queue" in added and "invoices.miro_status" in added
    assert database.ensure_schema(eng) == []                      # idempotent
    s = database.session_factory()()
    load_gst_master(s, FIX / "GST_RATE.xlsx", "150")
    from app.services.doctype import load_master
    load_master(s)
    s.commit()
    old = s.query(Invoice).filter_by(invoice_number="OLD-1").one()          # legacy row untouched
    assert old.status == "MATCHED"
    r = process_pdf(s, FIX / "inv_dsk_428.pdf", vlm=FakeVlm(), gw=make_sap())
    assert r.queue == "MATCHED"
    assert process_pdf(s, FIX / "inv_dsk_428.pdf", vlm=FakeVlm(), gw=make_sap()).outcome == "DUPLICATE"
    cols = {c["name"] for c in inspect(eng).get_columns("invoices")}
    assert {"dedupe_key", "sap_snapshot", "miro_doc_number"} <= cols
    idx = {i["name"] for i in inspect(eng).get_indexes("invoices")}
    assert "ux_invoices_dedupe_key" in idx
