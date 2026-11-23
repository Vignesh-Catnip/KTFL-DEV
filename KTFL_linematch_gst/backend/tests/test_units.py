from app.services.textutil import clean_grn, grn_is_valid, is_valid_po, norm_key
from app.services.validate import hsn_agree
from app.models import GstTaxCode
from app.services.gst import pick_tax_code


def test_grn_clean():
    assert clean_grn("50-343863") == "5000343863" and grn_is_valid("5000343863")


def test_po():
    assert is_valid_po("5100007531") and not is_valid_po("123")


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


def test_line_match_quantity_differs_is_qty_mismatch_not_missing():
    """Real cases: the invoice quantity differs from the goods receipt - the line must still be PAIRED and typed QTY."""
    from app.services.linematch import compare_lines
    # invoice 1514: '20MNCr5H 70 DIA AS ROLLED' 34.77 TO / 2,433,900 vs GR '20MNCR5 - 70 DIA' 34.744 TO / 2,432,080
    lines = [{"item": "20MNCr5H 70 DIA AS ROLLED", "quantity": 34.77, "amount": 2433900.0, "rate": 70000.0}]
    gr = [{"MBLNR": "5000396179", "EBELN": "5100002448", "EBELP": "00090", "MENGE": 34.744, "DMBTR": 2432080.0, "SGTXT": "20MNCR5 - 70 DIA"}]
    mm, extra = compare_lines(lines, gr)
    assert [m["code"] for m in mm] == ["QTY_MISMATCH"] and lines[0]["line_status"] == "QTY" and lines[0]["gr_quantity"] == 34.744
    assert abs(mm[0]["variance"] - round((34.77 - 34.744) * 70000, 2)) < 0.01 and not extra
    # invoice 1500: names differ completely, same rate 255/kg, quantity 252.4 vs 244.6
    lines = [{"item": "222/5mm H-13 Round Bar", "quantity": 153.2, "amount": 39066.0},
             {"item": "202/5mm H13 Round Bar 72284000", "quantity": 252.4, "amount": 64362.0}]
    gr = [{"MBLNR": "5000365809", "EBELN": "5100002351", "EBELP": "00010", "MENGE": 153.2, "DMBTR": 39066.0, "SGTXT": "H13-DIA 225 X 495"},
          {"MBLNR": "5000365809", "EBELN": "5100002351", "EBELP": "00020", "MENGE": 244.6, "DMBTR": 62373.0, "SGTXT": "H11-DIA 205 X 480"}]
    mm, extra = compare_lines(lines, gr)
    assert [m["code"] for m in mm] == ["QTY_MISMATCH"] and lines[0]["line_status"] == "OK" and lines[1]["line_status"] == "QTY"
    assert lines[1]["gr_quantity"] == 244.6 and lines[1]["gr_item"] == "00020"
    # a line that really is not in the GR stays MISSING
    mm, _ = compare_lines([{"item": "Something else", "quantity": 5.0, "amount": 9999.0}],
                          [{"MBLNR": "1", "EBELN": "2", "EBELP": "10", "MENGE": 100.0, "DMBTR": 100.0, "SGTXT": "Bolts"}])
    assert mm[0]["code"] == "LINE_NOT_IN_GR"
