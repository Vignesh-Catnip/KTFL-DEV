"""Test data: extractor-shaped PostgreSQL tables + a SAP simulator scenario."""
import json
import os
from pathlib import Path

import psycopg

HERE = Path(__file__).resolve().parent

# subset of the tables created by invoice_extractor.py (same names / types)
SCHEMA = """
DROP TABLE IF EXISTS audit_logs, invoice_lines, invoices CASCADE;
CREATE TABLE invoices (
  id SERIAL PRIMARY KEY, filename VARCHAR(255), vendor VARCHAR(255), invoice_number VARCHAR(100),
  invoice_date VARCHAR(30), po_number VARCHAR(100), gstin VARCHAR(50), vendor_code VARCHAR(50),
  tax_code VARCHAR(30), currency VARCHAR(10) DEFAULT 'INR', subtotal FLOAT DEFAULT 0,
  cgst FLOAT DEFAULT 0, sgst FLOAT DEFAULT 0, igst FLOAT DEFAULT 0, tax FLOAT DEFAULT 0,
  total FLOAT DEFAULT 0, grn_number VARCHAR(100), plant_code VARCHAR(20),
  gst_tax_code VARCHAR(10), sap_tax_code VARCHAR(10), validation_status VARCHAR(20),
  status VARCHAR(40) DEFAULT 'REVIEW', created_at TIMESTAMPTZ DEFAULT NOW());
CREATE TABLE invoice_lines (
  id SERIAL PRIMARY KEY, invoice_id INT REFERENCES invoices(id) ON DELETE CASCADE,
  item VARCHAR(255), quantity FLOAT DEFAULT 0, rate FLOAT DEFAULT 0, tax_code VARCHAR(30) DEFAULT '',
  amount FLOAT DEFAULT 0, uom VARCHAR(10), source VARCHAR(20) DEFAULT 'VISION');
CREATE TABLE audit_logs (
  id SERIAL PRIMARY KEY, invoice_id INT REFERENCES invoices(id) ON DELETE SET NULL,
  action VARCHAR(100), detail JSON, created_at TIMESTAMPTZ DEFAULT NOW());
"""

PO1, PO2 = "8100007531", "8100007600"
GRN_A, GRN_B, GRN_C = "5000343863", "5000343900", "5000344001"

# MIRO item list for PO1 - GRN_A rows are NOT contiguous and run over several screen pages
PO1_ROWS = [
    {"item": "10", "ref": GRN_A, "amount": 10000.00, "qty": 100, "text": "FLANGE 6IN", "tax": "G2", "refpos": "1"},
    {"item": "20", "ref": GRN_A, "amount": 20000.00, "qty": 200, "text": "FLANGE 8IN", "tax": "G2", "refpos": "2"},
    {"item": "30", "ref": GRN_B, "amount": 5000.00, "qty": 50, "text": "RING 2IN", "tax": "G2", "refpos": "1"},
    {"item": "40", "ref": GRN_A, "amount": 15000.50, "qty": 150, "text": "SHAFT 40", "tax": "G2", "refpos": "3"},
    {"item": "50", "ref": GRN_C, "amount": 7000.00, "qty": 70, "text": "PIN 12", "tax": "G2", "refpos": "1"},
    {"item": "60", "ref": GRN_B, "amount": 3000.00, "qty": 30, "text": "RING 3IN", "tax": "G2", "refpos": "2"},
    {"item": "70", "ref": GRN_A, "amount": 12345.67, "qty": 120, "text": "HUB 10", "tax": "G2", "refpos": "4"},
    {"item": "80", "ref": GRN_C, "amount": 900.00, "qty": 9, "text": "BOLT M12", "tax": "G2", "refpos": "2"},
    {"item": "90", "ref": GRN_B, "amount": 1100.00, "qty": 11, "text": "NUT M12", "tax": "G2", "refpos": "3"},
    {"item": "100", "ref": GRN_A, "amount": 4000.00, "qty": 40, "text": "COLLAR 5", "tax": "G2", "refpos": "5"},
    {"item": "110", "ref": GRN_C, "amount": 2500.00, "qty": 25, "text": "WASHER", "tax": "G2", "refpos": "3"},
    {"item": "120", "ref": GRN_A, "amount": 654.33, "qty": 6, "text": "SPACER", "tax": "G2", "refpos": "6"},
    {"item": "130", "ref": GRN_B, "amount": 800.00, "qty": 8, "text": "CLIP", "tax": "G2", "refpos": "4"},
    {"item": "140", "ref": GRN_C, "amount": 1200.00, "qty": 12, "text": "KEY 8", "tax": "G2", "refpos": "4"},
]
GRN_A_NET = round(sum(r["amount"] for r in PO1_ROWS if r["ref"] == GRN_A), 2)      # 62000.50
GRN_A_ROWS = [i for i, r in enumerate(PO1_ROWS) if r["ref"] == GRN_A]               # 0,1,3,6,9,11

PO2_ROWS = [{"item": "10", "ref": "5000350000", "amount": 50000.00, "qty": 10, "text": "FORGING", "tax": "I2", "refpos": "1"}]

SCENARIO = {
    "connection": "KTFL PRD", "client": "150", "user": "ROBOT01", "password": "secret",
    "logged_on": False, "visible_rows": 5,
    "tax_rates": {"G2": 0.18, "I2": 0.18, "G0": 0.0},
    "duplicates": ["DUP/26-27"],
    "pos": {PO1: PO1_ROWS, PO2: PO2_ROWS},
}


def gross(net, rate=0.18):
    return round(net + round(net * rate, 2), 2)


INVOICES = [
    # inv_no,       date,         po,   grn,          subtotal,   total,                tax, status
    ("428/26-27",  "04.08.2026", PO1, GRN_A,        GRN_A_NET, gross(GRN_A_NET),   "G2", "MATCHED"),  # happy path
    ("NOGRN/1",    "05.08.2026", PO1, "5000999999", 1000.0,    1180.0,             "G2", "MATCHED"),  # GRN not in MIRO
    ("DUP/26-27",  "05.08.2026", PO1, GRN_B,        9900.0,    gross(9900.0),      "G2", "MATCHED"),  # duplicate warning
    ("GAP/1",      "06.08.2026", PO1, GRN_C,        99999.0,   gross(99999.0),     "G2", "MATCHED"),  # amount gap
    ("REV/1",      "06.08.2026", PO1, GRN_A,        GRN_A_NET, gross(GRN_A_NET),   "G2", "REVIEW"),   # not MATCHED -> ignored
    ("BADPO/1",    "07.08.2026", "8100000000", GRN_A, 100.0,   118.0,              "G2", "MATCHED"),  # PO unknown in SAP
    ("IGST/9",     "2026-08-08", PO2, "5000350000", 50000.0,   59000.0,            "I2", "MATCHED"),  # 2nd PO, ISO date
]


def conninfo():
    return (f"host={os.environ.get('TEST_PG_HOST', '127.0.0.1')} "
            f"port={os.environ.get('TEST_PG_PORT', '55432')} dbname=miro_invoice "
            f"user=appuser password={os.environ.get('TEST_PG_PASSWORD', 'testpw')}")


def reset_db():
    with psycopg.connect(conninfo()) as c, c.cursor() as cur:
        cur.execute(SCHEMA)
        for inv, dt, po, grn, sub, tot, tax, st in INVOICES:
            cur.execute("""INSERT INTO invoices (filename, vendor, invoice_number, invoice_date,
                             po_number, grn_number, subtotal, tax, total, gst_tax_code, currency,
                             validation_status, status)
                           VALUES (%s,'ACME FORGE',%s,%s,%s,%s,%s,%s,%s,%s,'INR',%s,%s) RETURNING id""",
                        (f"{inv}.pdf", inv, dt, po, grn, sub, round(tot - sub, 2), tot, tax, st, st))
            iid = cur.fetchone()[0]
            if inv == "428/26-27":
                for r in PO1_ROWS:
                    if r["ref"] == GRN_A:
                        cur.execute("INSERT INTO invoice_lines (invoice_id,item,quantity,amount) "
                                    "VALUES (%s,%s,%s,%s)", (iid, r["text"], r["qty"], r["amount"]))
        c.commit()


def write_scenario(path: Path) -> Path:
    path.write_text(json.dumps(SCENARIO, indent=1), encoding="utf-8")
    return path


def set_env(scenario_path: Path, out_dir: Path, mode="POST"):
    os.environ.update({
        "PG_HOST": "127.0.0.1", "PG_PORT": os.environ.get("TEST_PG_PORT", "55432"),
        "PG_DB": "miro_invoice", "PG_USER": "appuser",
        "PG_PASSWORD": os.environ.get("TEST_PG_PASSWORD", "testpw"),
        "MIRO_SAP_BACKEND": "fake", "MIRO_FAKE_SCENARIO": str(scenario_path),
        "SAP_CONNECTION": "KTFL PRD", "SAP_USER": "ROBOT01", "SAP_PASSWORD": "secret",
        "SAP_CLIENT": "150", "MIRO_MODE": mode, "MIRO_OUTPUT_DIR": str(out_dir),
        "MIRO_POLL_INTERVAL": "0.01",
    })
