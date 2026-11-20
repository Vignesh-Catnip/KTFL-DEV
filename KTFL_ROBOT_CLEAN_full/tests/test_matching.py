import pytest

from miro.matching import (MiroDataError, MiroRow, extract_document_number, format_sap_amount,
                           format_sap_date, is_duplicate_message, parse_sap_number, plan_selection)


@pytest.mark.parametrize("txt,sep,val", [
    ("1,23,456.78", ".", 123456.78), ("1.234.567,89", ",", 1234567.89), ("12.000-", ",", -12000.0),
    ("500.00-", ".", -500.0), ("", ".", 0.0), ("  619,763.14 ", ".", 619763.14), ("0,00", ",", 0.0),
])
def test_parse_sap_number(txt, sep, val):
    assert parse_sap_number(txt, sep) == val


def test_format_amount():
    assert format_sap_amount(619763.14) == "619763.14"
    assert format_sap_amount(619763.1, ",") == "619763,10"


@pytest.mark.parametrize("raw", ["04.08.2026", "04/08/2026", "2026-08-04", "04-08-26", "04-Aug-2026"])
def test_format_date(raw):
    assert format_sap_date(raw) == "04.08.2026"


def test_bad_date():
    with pytest.raises(MiroDataError):
        format_sap_date("32.13.2026")


def rows():
    return [MiroRow(0, "8100007531", "10", "5000343863", 100.0, 1),
            MiroRow(1, "8100007531", "20", "5000343900", 50.0, 1),
            MiroRow(2, "8100007531", "30", "005000343863", 200.0, 2),   # leading zeros
            MiroRow(3, "8100009999", "10", "5000343863", 999.0, 1),     # same GRN, other PO
            MiroRow(4)]                                                 # empty padding row


def test_plan_picks_only_grn_rows_of_own_po():
    inv = {"grn_number": "5000343863", "po_number": "8100007531", "subtotal": 300.0}
    p = plan_selection(rows(), inv, 1.0)
    assert p.indexes == [0, 2]
    assert p.selected_amount == 300.0
    assert p.other_grns == ["5000343900"]
    assert any("other" in w.lower() or "skipped" in w for w in p.warnings)


def test_plan_grn_missing():
    with pytest.raises(MiroDataError, match="not found"):
        plan_selection(rows(), {"grn_number": "5000000001", "po_number": "8100007531"})


def test_plan_empty_grn():
    with pytest.raises(MiroDataError, match="empty"):
        plan_selection(rows(), {"grn_number": "", "po_number": "8100007531"})


def test_plan_amount_gap():
    inv = {"grn_number": "5000343863", "po_number": "8100007531", "subtotal": 310.0}
    with pytest.raises(MiroDataError, match="taxable"):
        plan_selection(rows(), inv, 1.0)


def test_plan_no_rows():
    with pytest.raises(MiroDataError, match="no items"):
        plan_selection([MiroRow(0)], {"grn_number": "5000343863", "po_number": "1"})


def test_quantity_warning_only():
    inv = {"grn_number": "5000343863", "po_number": "8100007531", "subtotal": 300.0,
           "lines": [{"quantity": 5, "amount": 300}]}
    p = plan_selection(rows(), inv)
    assert any("Quantity" in w for w in p.warnings)


def test_messages():
    assert extract_document_number("Document no. 5105600123 created") == "5105600123"
    assert extract_document_number("Saved") == ""
    assert is_duplicate_message("Check if invoice already entered under document number 51056")
    assert not is_duplicate_message("Balance not zero")
