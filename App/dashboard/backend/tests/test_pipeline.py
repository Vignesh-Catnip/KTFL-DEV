from app.models import Invoice
from app.services.pipeline import process_pdf, recheck_invoice
from app.services import sap as sapmod
from tests.conftest import FIX, FakeVlm, dsk_vision, make_sap, seed_dsk

PDF = FIX / "inv_dsk_428.pdf"


def run(db, data=None, gw=None, grn="50-343863"):
    return process_pdf(db, PDF, "inv_dsk_428.pdf", vlm=FakeVlm(data or dsk_vision(), grn), gw=gw or make_sap())


def issues(db, rid):
    return {i["code"]: i for i in db.get(Invoice, rid).issues}


def test_clean_invoice_is_matched(db):
    r = run(db)
    inv = db.get(Invoice, r.invoice_id)
    assert r.outcome == "CREATED" and inv.queue == "MATCHED" and inv.status == "MATCHED"
    assert inv.grn_number == "5000343863" and inv.plant_code == "2002" and not inv.vendor_code and inv.business_place == "2002"
    # plant code is taken from SQL (GRN plant); vendor code / currency / taxable value are NOT copied from SQL
    assert inv.subtotal == 64240.0 and inv.currency == "INR"
    assert inv.gst_tax_code == "I3" and inv.hsn_match is True and inv.sap_hsn == "82073000"
    assert len(inv.lines) == 11 and all(l.line_status == "OK" for l in inv.lines)


def test_duplicate_blocked(db):
    run(db)
    r2 = run(db)
    assert r2.outcome == "DUPLICATE" and db.query(Invoice).count() == 1


def test_hsn_never_taken_from_sap(db):
    d = dsk_vision(hsn_sac="", lines=[{**l, "hsn_sac": ""} for l in dsk_vision()["lines"]])
    r = run(db, d)
    inv = db.get(Invoice, r.invoice_id)
    assert not inv.hsn_sac and inv.sap_hsn == "82073000"
    assert inv.queue == "RECONCILE" and "HSN_MISSING_INVOICE" in issues(db, inv.id)


def test_hsn_missing_in_sap_reconcile(db):
    r = run(db, gw=make_sap(hsn=""))
    assert db.get(Invoice, r.invoice_id).queue == "RECONCILE"
    assert "HSN_MISSING_SAP" in issues(db, r.invoice_id)


def test_hsn_mismatch_reconcile(db):
    r = run(db, gw=make_sap(hsn="73181500"))
    assert "HSN_MISMATCH" in issues(db, r.invoice_id)


def test_qty_mismatch_goes_to_reconcile_with_line(db):
    r = run(db, gw=make_sap(qty_override={3: 18}))
    inv = db.get(Invoice, r.invoice_id)
    assert inv.queue == "RECONCILE" and "QTY_MISMATCH" in issues(db, inv.id)
    assert [l.line_status for l in inv.lines].count("OK") == 10


def test_amount_mismatch(db):
    r = run(db, gw=make_sap(amount_override={2: 1500}))
    assert "AMOUNT_MISMATCH" in issues(db, r.invoice_id)


def test_line_missing_in_grn(db):
    r = run(db, gw=make_sap(drop_rows=(11,)))
    inv = db.get(Invoice, r.invoice_id)
    assert "LINE_NOT_IN_GR" in issues(db, inv.id) and inv.queue == "RECONCILE"


def test_grn_not_in_sap_blocked(db):
    r = run(db, gw=make_sap(seed=None))
    inv = db.get(Invoice, r.invoice_id)
    assert inv.queue == "BLOCKED" and "PO_NOT_IN_SAP" in issues(db, inv.id)


def test_few_fields_missing_goes_to_reconcile(db):
    r = run(db, dsk_vision(invoice_number=""))
    inv = db.get(Invoice, r.invoice_id)
    assert inv.queue == "RECONCILE" and inv.completeness == 0.9
    assert "MISSING_INVOICE_NUMBER" in issues(db, inv.id)


def test_three_missing_is_70_percent_reconcile_four_is_blocked(db):
    r = run(db, dsk_vision(invoice_number="", invoice_date="", vendor=""))
    inv = db.get(Invoice, r.invoice_id)
    assert inv.completeness == 0.7 and inv.queue == "RECONCILE"
    r = run(db, dsk_vision(invoice_number="", invoice_date="", vendor="", po_number="", po_numbers_all=""), grn="")
    inv = db.get(Invoice, r.invoice_id)
    assert inv.completeness < 0.7 and inv.queue == "BLOCKED"


def test_missing_data_cannot_be_waived(db):
    r = run(db, dsk_vision(invoice_number=""))
    inv = db.get(Invoice, r.invoice_id)
    inv.approved_variance = True
    recheck_invoice(db, inv, actor="t", gw=make_sap())
    db.refresh(inv)
    assert inv.queue == "RECONCILE"


def test_missing_grn_resolved_from_sap(db):
    r = run(db, grn="")
    inv = db.get(Invoice, r.invoice_id)
    assert inv.grn_number == "5000343863" and inv.queue == "MATCHED"


def test_sap_down_goes_to_reconcile(db):
    r = process_pdf(db, PDF, vlm=FakeVlm(), gw=None)
    inv = db.get(Invoice, r.invoice_id)
    assert inv.queue in ("RECONCILE", "BLOCKED") and "SAP_NOT_CHECKED" in issues(db, inv.id)


def _multi_seed(second_grn=None, qty_override=None):
    def seed(conn, **kw):
        seed_dsk(conn, qty_override=qty_override)
        conn.execute(sapmod.text("INSERT INTO dbo.EKKO VALUES ('150','8100009999','0000070042','INR','20260710','2000')"))
        conn.execute(sapmod.text("INSERT INTO dbo.EKPO VALUES ('150','8100009999','00010','x','m','2002','82073000',30,'NOS',250,1,7500,7500,'I3','')"))
        conn.execute(sapmod.text("UPDATE dbo.MSEG SET EBELN='8100009999', EBELP='00010' WHERE ZEILE='0011'"
                                 + (f", MBLNR='{second_grn}'" if False else "")))
        if second_grn:
            conn.execute(sapmod.text(f"UPDATE dbo.MSEG SET MBLNR='{second_grn}' WHERE ZEILE='0011'"))
    return seed


def test_multi_po_all_correct_is_matched(db):
    """One invoice on two POs (same GRN): PO, GRN, lines agree -> MATCHED, not blocked."""
    r = run(db, gw=make_sap(seed=_multi_seed()))
    inv = db.get(Invoice, r.invoice_id)
    assert inv.multi_po_flag and inv.queue == "MATCHED" and inv.status == "MATCHED"
    assert all(l.line_status == "OK" for l in inv.lines) and inv.grn_amount == 64240.0
    assert inv.po_list == "8100007531, 8100009999"


def test_multi_po_second_grn_is_matched(db):
    r = run(db, gw=make_sap(seed=_multi_seed(second_grn="5000343999")))
    inv = db.get(Invoice, r.invoice_id)
    assert inv.multi_po_flag and inv.queue == "MATCHED" and inv.grn_number == "5000343863"
    assert [l.gr_row for l in inv.lines][-1].startswith("5000343999")


def test_multi_po_with_wrong_line_is_reconcile(db):
    r = run(db, gw=make_sap(seed=_multi_seed(qty_override={3: 18})))
    inv = db.get(Invoice, r.invoice_id)
    assert inv.multi_po_flag and inv.queue == "RECONCILE" and "QTY_MISMATCH" in issues(db, inv.id)


def test_recheck_after_fix(db):
    r = run(db, gw=make_sap(qty_override={3: 18}))
    inv = db.get(Invoice, r.invoice_id)
    assert inv.queue == "RECONCILE"
    recheck_invoice(db, inv, actor="t", gw=make_sap())
    db.refresh(inv)
    assert inv.queue == "MATCHED"


def test_approved_variance_waives(db):
    r = run(db, gw=make_sap(qty_override={3: 18}))
    inv = db.get(Invoice, r.invoice_id)
    inv.approved_variance = True
    recheck_invoice(db, inv, actor="t", gw=make_sap(qty_override={3: 18}))
    db.refresh(inv)
    assert inv.queue == "MATCHED"


def test_watch_folder_picks_settled_pdfs_only(db, tmp_path, fake_vlm, sap):
    import os, shutil, time
    from app.models import Job
    from app.services import worker
    folder = tmp_path / "in"
    folder.mkdir()
    shutil.copy(PDF, folder / "new.pdf")
    assert worker.scan_folder(folder, settle=60) == 0                # file was just written -> wait
    os.utime(folder / "new.pdf", (time.time() - 120, time.time() - 120))
    assert worker.scan_folder(folder, settle=60) == 1
    assert (folder / "processed" / "new.pdf").exists() and not (folder / "new.pdf").exists()
    job = db.query(Job).one()
    assert job.source == "WATCH" and job.status == "QUEUED"
    worker.run_job(job.id)
    db.expire_all()
    assert db.get(Job, job.id).status == "DONE" and db.query(Invoice).count() == 1


MSEG_HSN = {"MSEG": ["J_1IG_HSN_SAC"], "EKPO": ["STAWN"], "MARC": []}


def test_hsn_found_in_grn_row_when_po_has_none(db):
    """HSN kept on the MSEG row (goods receipt) is enough - invoice agrees -> still MATCHED."""
    r = run(db, gw=make_sap(hsn="", mseg_hsn="82073000", hsn_cols=MSEG_HSN))
    inv = db.get(Invoice, r.invoice_id)
    assert inv.queue == "MATCHED" and inv.hsn_match is True and inv.sap_hsn == "82073000"
    assert inv.sap_snapshot["hsn"] == {"MSEG.J_1IG_HSN_SAC": ["82073000"]}


def test_ncm_with_dots_and_grn_hsn_differs(db):
    r = run(db, gw=make_sap(hsn="8207.30.00", mseg_hsn="7318.15.00", hsn_cols=MSEG_HSN))
    inv = db.get(Invoice, r.invoice_id)
    assert inv.hsn_match is True                         # agrees with the PO value; GRN value is shown too
    assert set(inv.sap_hsn.split(", ")) == {"82073000", "73181500"}


def test_hsn_nowhere_in_sap_is_reconcile(db):
    r = run(db, gw=make_sap(hsn="", mseg_hsn="", hsn_cols=MSEG_HSN))
    assert "HSN_MISSING_SAP" in issues(db, r.invoice_id)
    assert db.get(Invoice, r.invoice_id).queue == "RECONCILE"
