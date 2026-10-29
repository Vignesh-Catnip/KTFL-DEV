"""Shared fixtures: app DB (SQLite in memory), simulated SAP (SQLite with an attached `dbo` schema),
GST master from the real GST_RATE.xlsx and a fake vision model."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAP_ENABLED", "true")
os.environ.setdefault("WORKER_ENABLED", "false")
os.environ.setdefault("ALLOW_UPLOAD", "true")
os.environ.setdefault("ADMIN_PASSWORD", "admin-pass-1")
os.environ.setdefault("JWT_SECRET", "test-secret-test-secret-test-secret")

FIX = Path(__file__).parent / "fixtures"

from app import database                                       # noqa: E402
from app.config import get_settings, reset_settings_cache       # noqa: E402
from app.services import sap as sapmod, vlm as vlmmod           # noqa: E402
from app.services.gst import load_gst_master                    # noqa: E402

# ---------------------------------------------------------------- the invoice used everywhere
DSK_LINES = [  # (item, qty, rate, amount)
    ("DPTOPEJPSIZ01763:SEMIFINISH TOP EJECTION PIN SIZ-01763", 30, 209, 6270),
    ("DPBLBEJPSIZ01648:SEMIFINISH BLOCKER BOT EJP SIZ-01648", 5, 310, 1550),
    ("DPBOTEJPSIZ02060:SEMIFINISH BOT EJECTION PIN SIZ-02060", 20, 310, 6200),
    ("DPBOTEJPSIZ02061:SEMIFINISH BOT EJECTION PIN SIZ-02061", 30, 310, 9300),
    ("DPTEJPINSIZ02061:SEMIFINISH TOP EJECTION PIN SIZ-02061", 30, 280, 8400),
    ("DPBOTEJPSIZ01297:SEMIFINISH BOT EJECTION PIN SIZ-01297", 30, 250, 7500),
    ("DPTEJPINSIZ02059:SEMIFINISH TOP EJECTION PIN SIZ-02059", 30, 209, 6270),
    ("DPBLBEJPSIZ01649:SEMIFINISH BLOCKER BOT EJP SIZ-01649", 5, 250, 1250),
    ("DPBOTEJPSIZ01157:SEMIFINISH BOT EJECTION PIN SIZ-01157", 20, 250, 5000),
    ("DPBOTEJP16SIZ0349:SEMIFINISH BOT EJE PIN 1600T SIZ-01349", 20, 250, 5000),
    ("DPBOTEJPSIZ02059:SEMIFINISH BOT EJECTION PIN SIZ-02059", 30, 250, 7500),
]


def dsk_vision(**over) -> dict:
    """What the vision model returns for the DSK Engineers invoice 428/26-27 (PO 8100007531)."""
    d = {
        "vendor": "DSK ENGINEERS", "invoice_number": "428/26-27", "invoice_date": "04-Aug-26",
        "po_number": "8100007531", "po_numbers_all": "8100007531", "gstin": "27ABRPI9475K1ZQ",
        "hsn_sac": "82073000", "currency": "INR", "subtotal": 64240.00, "cgst": 5781.60,
        "sgst": 5781.60, "igst": 0, "total_tax": 11563.20, "total": 75803.20,
        "gst_rate_percent": 18, "confidence": 0.93,
        "lines": [{"item": i, "quantity": q, "uom": "NOS", "rate": r, "hsn_sac": "82073000", "amount": a}
                  for i, q, r, a in DSK_LINES],
    }
    d.update(over)
    return d


class FakeVlm:
    def __init__(self, data=None, grn="50-343863"):
        self.data, self.grn, self.calls = data if data is not None else dsk_vision(), grn, 0

    def extract_invoice(self, images, text_layer):
        self.calls += 1
        return self.data

    def read_grn(self, images, max_retry=2):
        return self.grn

    def health(self):
        return True, "fake"


# ---------------------------------------------------------------- simulated SAP (BRONZE_DB.dbo)
SAP_DDL = [
    """CREATE TABLE dbo.MSEG (MANDT TEXT, MBLNR TEXT, MJAHR TEXT, ZEILE TEXT, BWART TEXT, EBELN TEXT, EBELP TEXT,
       LIFNR TEXT, WERKS TEXT, MENGE REAL, MEINS TEXT, ERFMG REAL, ERFME TEXT, DMBTR REAL, WAERS TEXT,
       MWSKZ TEXT, XBLNR_MKPF TEXT, BUDAT_MKPF TEXT, SGTXT TEXT, SMBLN TEXT, J_1IG_HSN_SAC TEXT)""",
    "CREATE TABLE dbo.EKKO (MANDT TEXT, EBELN TEXT, LIFNR TEXT, WAERS TEXT, BEDAT TEXT, BUKRS TEXT)",
    """CREATE TABLE dbo.EKPO (MANDT TEXT, EBELN TEXT, EBELP TEXT, TXZ01 TEXT, MATNR TEXT, WERKS TEXT, STAWN TEXT,
       MENGE REAL, MEINS TEXT, NETPR REAL, PEINH REAL, NETWR REAL, BRTWR REAL, MWSKZ TEXT, LOEKZ TEXT)""",
]


def seed_dsk(conn, *, hsn="82073000", mseg_hsn="", qty_override=None, amount_override=None, drop_rows=(), tax="I3",
             grn="5000343863", po="8100007531", inv_ref="428/26-27"):
    """GRN 5000343863 for PO 8100007531, plant 2002, one MSEG row per invoice line."""
    conn.execute(text("INSERT INTO dbo.EKKO VALUES ('150', :po, '0000070042', 'INR', '20260710', '2000')"),
                 {"po": po})
    for n, (item, q, r, a) in enumerate(DSK_LINES, 1):
        ebelp = f"{n * 10:05d}"
        conn.execute(text("INSERT INTO dbo.EKPO VALUES ('150', :po, :p, :t, :m, '2002', :h, :q, 'NOS', :r, 1, :a, :a, :tx, '')"),
                     {"po": po, "p": ebelp, "t": item.split(":")[0], "m": item.split(":")[0], "h": hsn, "q": q,
                      "r": r, "a": a, "tx": tax})
        if n in drop_rows:
            continue
        mq = (qty_override or {}).get(n, q)
        ma = (amount_override or {}).get(n, a)
        conn.execute(text("INSERT INTO dbo.MSEG VALUES ('150', :g, '2026', :z, '101', :po, :p, '0000070042', '2002', "
                          ":q, 'NOS', :q, 'NOS', :a, 'INR', :tx, :ref, '20260805', :txt, '', :mh)"),
                     {"g": grn, "z": f"{n:04d}", "po": po, "p": ebelp, "q": mq, "a": ma, "tx": tax,
                      "ref": inv_ref, "txt": item.split(":")[0], "mh": mseg_hsn})


def make_sap(seed=seed_dsk, hsn_cols=None, **kw) -> sapmod.SapGateway:
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

    @event.listens_for(eng, "connect")
    def _attach(dbapi, _):
        dbapi.execute("ATTACH DATABASE ':memory:' AS dbo")

    with eng.begin() as c:
        for ddl in SAP_DDL:
            c.execute(text(ddl))
        if seed:
            seed(c, **kw)
    return sapmod.SapGateway(eng, "150", ("101", "105"),
                             hsn_cols=hsn_cols or {"MSEG": [], "EKPO": ["STAWN"], "MARC": []})


@pytest.fixture
def sap_factory():
    return make_sap


@pytest.fixture
def sap():
    gw = make_sap()
    sapmod.set_sap(gw)
    yield gw
    sapmod.set_sap(None)


@pytest.fixture
def engine(tmp_path):
    reset_settings_cache()
    os.environ["UPLOAD_DIR"] = str(tmp_path / "uploads")
    os.environ["GST_RATE_XLSX"] = str(FIX / "GST_RATE.xlsx")
    reset_settings_cache()
    eng = database.init_engine("sqlite://")
    database.ensure_schema(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    s = database.session_factory()()
    load_gst_master(s, FIX / "GST_RATE.xlsx", get_settings().sap_client)
    s.commit()
    yield s
    s.close()


@pytest.fixture
def fake_vlm():
    v = FakeVlm()
    vlmmod.set_vlm(v)
    yield v
    vlmmod.set_vlm(None)
