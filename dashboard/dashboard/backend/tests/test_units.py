from app.services.textutil import clean_grn, grn_is_valid, is_valid_po, norm_key
from app.services.validate import hsn_agree
from app.models import GstTaxCode
from app.services.gst import pick_tax_code


def test_grn_clean():
    assert clean_grn("50-343863") == "5000343863" and grn_is_valid("5000343863")


def test_po():
    assert is_valid_po("8100007531") and not is_valid_po("123")


def test_dedupe_key():
    assert norm_key(" 428/26-27 ") == norm_key("428/26-27")


def test_hsn_agree():
    assert hsn_agree("8207", "82073000") and not hsn_agree("8207", "7318")


def test_gst_master(db):
    assert db.query(GstTaxCode).count() > 10
    r = pick_tax_code(db, 18.0, 9.0, "CGST_SGST")
    assert r and r.tax_code == "I3"
    r = pick_tax_code(db, 18.0, 18.0, "IGST")
    assert r and r.tax_code == "I8"
