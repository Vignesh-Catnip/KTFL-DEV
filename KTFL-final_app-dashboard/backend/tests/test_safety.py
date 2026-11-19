"""Regression tests for the approved implementation rules (strict null policy, every page read, SAP never fills or
creates invoice values, no silent JSON repair, strict tolerances, restricted waivers, SQL table restriction)."""
from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from app.models import ExtractionRun, Invoice
from app.services import sap as sapmod
from app.services.pipeline import process_pdf, recheck_invoice
from app.services.textutil import IncompleteJson, extract_json_strict, parse_amount
from app.services.vlm import OllamaVlm, adapt_invoice, merge_group_results
from tests.conftest import FIX, FakeVlm, dsk_vision, make_sap

PDF = FIX / "inv_dsk_428.pdf"


def run(db, data=None, gw=None, grn="50-343863", vlm=None):
    return process_pdf(db, PDF, "inv_dsk_428.pdf", vlm=vlm or FakeVlm(data or dsk_vision(), grn), gw=gw or make_sap())


def codes(db, rid):
    return {i["code"]: i for i in db.get(Invoice, rid).issues}


# ------------------------------------------------------------------ E. numbers: null, not zero
@pytest.mark.parametrize("raw,expected", [
    ("1,42,115.00", Decimal("142115.00")), ("0.00", Decimal("0.00")), ("(500.00)", Decimal("-500.00")),
    ("-12.50", Decimal("-12.50")), ("Rs. 1,000.50", Decimal("1000.50")), (1234.5, Decimal("1234.5")),
])
def test_parse_amount_valid(raw, expected):
    val, why = parse_amount(raw)
    assert val == expected and why is None


@pytest.mark.parametrize("raw", [None, "", "NA", "abc", "1.5.0", "1.234,50", "12,50", "1,23,45.00"])
def test_parse_amount_failure_is_null_with_reason_never_zero(raw):
    val, why = parse_amount(raw)
    assert val is None and why


# ------------------------------------------------------------------ J. JSON is never repaired silently
def test_truncated_json_is_not_repaired():
    cut = '{"vendor": "X", "lines": [{"item": "A", "amount": "1"}, {"item": "B", "amo'
    with pytest.raises(IncompleteJson):
        extract_json_strict(cut)
    with pytest.raises(IncompleteJson):
        extract_json_strict("")
    with pytest.raises(IncompleteJson):
        extract_json_strict("[1, 2]")


def _vlm(monkeypatch, replies, seen=None):
    """OllamaVlm whose HTTP call returns the given replies (dict = ollama body) one after another."""
    v = OllamaVlm()
    it = iter(replies)

    def fake_post(prompt, images, num_predict, use_format):
        if seen is not None:
            seen.append({"prompt": prompt, "images": len(images)})
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(v, "_post_chat", fake_post)
    return v


def body(obj, **kw):
    return {"message": {"content": obj if isinstance(obj, str) else json.dumps(obj)}, "done_reason": "stop",
            "prompt_eval_count": 900, "eval_count": 120, "total_duration": 5_000_000_000, **kw}


def test_done_reason_length_is_incomplete_not_parsed(monkeypatch):
    v = _vlm(monkeypatch, [body({"vendor": "X"}, done_reason="length")])
    obj, meta = v._ask_raw("p", ["img"])
    assert obj is None and meta["status"] == "INCOMPLETE"


def test_timeout_empty_and_invalid_json_are_reported(monkeypatch):
    v = _vlm(monkeypatch, [httpx.ReadTimeout("slow")])
    assert v._ask_raw("p", ["i"])[1]["status"] == "TIMEOUT"
    v = _vlm(monkeypatch, [{"message": {"content": ""}}, {"message": {"content": ""}}])
    assert v._ask_raw("p", ["i"])[1]["status"] == "EMPTY"
    v = _vlm(monkeypatch, [body('{"a": [1, 2')])
    assert v._ask_raw("p", ["i"])[1]["status"] == "INVALID_JSON"


def test_context_full_is_incomplete(monkeypatch):
    v = _vlm(monkeypatch, [body({"vendor": "X"}, prompt_eval_count=OllamaVlm().cfg.num_ctx)])
    assert v._ask_raw("p", ["i"])[1]["status"] == "INCOMPLETE"


def test_ollama_metrics_are_captured(monkeypatch):
    v = _vlm(monkeypatch, [body({"vendor": "X"})])
    obj, meta = v._ask_raw("p", ["i"])
    assert obj == {"vendor": "X"} and meta["metrics"]["prompt_eval_count"] == 900 and meta["metrics"]["eval_count"] == 120
    assert meta["metrics"]["total_duration_ms"] == 5000


# ------------------------------------------------------------------ A. every page is read, in labelled groups
def _page(n):
    return {"vendor": "V", "invoice_number": "INV-1", "lines": [{"item": f"row on page {n}", "amount": "10.00",
                                                                  "quantity": "1", "page": n}],
            "subtotal": None, "total": None, "pages": [{"page": n, "kind": "invoice", "legible": True}]}


def test_every_page_is_sent_in_labelled_groups(monkeypatch):
    seen = []
    last = {**_page(5), "subtotal": "50.00", "cgst": "4.50", "sgst": "4.50", "igst": None, "total": "59.00"}
    v = _vlm(monkeypatch, [body(_page(1)), body(_page(3)), body(last)], seen)
    data = v.extract_invoice(["i1", "i2", "i3", "i4", "i5"], "")
    assert [s["images"] for s in seen] == [2, 2, 1]                       # 5 pages, none skipped
    assert "PAGES 1 to 2 of 5" in seen[0]["prompt"] and "PAGE 5 of 5" in seen[2]["prompt"]
    pi = data["_page_info"]
    assert pi["pages_total"] == 5 and pi["pages_failed"] == [] and pi["prompt_hash"] and len(data["_runs"]) == 3
    assert len(data["lines"]) == 3 and data["total"] == 59.0              # lines from all groups, totals from the last page


def test_failed_group_is_retried_page_by_page_then_reported(monkeypatch):
    bad = body('{"cut', done_reason="length")
    v = _vlm(monkeypatch, [bad, body(_page(1)), bad, bad])                # group fails; page 1 ok; page 2 fails twice
    data = v.extract_invoice(["i1", "i2"], "")
    assert data["_page_info"]["pages_failed"] == [2] and data["_page_info"]["pages_read"] == [1]
    assert {r["status"] for r in data["_runs"]} >= {"OK", "INCOMPLETE"}


def test_failed_page_sends_invoice_to_review_never_matched(db):
    d = dsk_vision()
    d["_page_info"] = {"pages_total": 3, "pages_failed": [3], "pages_read": [1, 2]}
    r = run(db, d)
    inv = db.get(Invoice, r.invoice_id)
    assert inv.queue != "MATCHED" and "PAGE_NOT_PROCESSED" in codes(db, inv.id)


def test_conflicting_header_values_across_pages_are_reported():
    out, conflicts, _ = merge_group_results([
        (1, 2, {"invoice_number": "A-1", "lines": []}), (3, 3, {"invoice_number": "B-9", "total": "5", "lines": []})])
    assert out["invoice_number"] == "A-1" and conflicts and conflicts[0]["field"] == "invoice_number"


# ------------------------------------------------------------------ C. null stays null end to end
def test_adapt_keeps_unreadable_values_null_and_lists_them():
    d = adapt_invoice({"vendor": "V", "subtotal": "1,000.00", "cgst": None, "sgst": None, "igst": "180.00",
                       "total": "abc", "lines": [{"item": "x", "quantity": None, "amount": "1,000.00"}]})
    assert d["subtotal"] == 1000.0 and d["total"] is None and "total" in d["unread"]
    assert d["total_tax"] == 180.0 and d["cgst"] is None
    assert d["parse_errors"]["total"].startswith("not a number")
    assert d["lines"][0]["quantity"] is None and "quantity" in d["lines"][0]["unread"]


def test_unreadable_total_is_unread_not_zero_and_blocks_matching(db):
    data = adapt_invoice({**{k: v for k, v in dsk_vision().items() if k not in ("po_number", "po_numbers_all")},
                          "po_number_raw": "5100007531", "total": None, "subtotal": "64240.00", "cgst": "5781.60",
                          "sgst": "5781.60", "igst": "0.00",
                          "lines": [{**l, "quantity": str(l["quantity"]), "rate": str(l["rate"]),
                                     "amount": str(l["amount"])} for l in dsk_vision()["lines"]]})
    r = run(db, data)
    inv = db.get(Invoice, r.invoice_id)
    assert inv.queue != "MATCHED" and "MISSING_TOTAL" in codes(db, inv.id)
    assert inv.field_sources["total"] == "UNREAD"                       # the NOT NULL column holds 0, the marker says null
    assert "TOTAL_MISMATCH" not in codes(db, inv.id)                    # no arithmetic against an unread value


def test_unread_marker_survives_recheck(db):
    d = dsk_vision(); d["unread"] = ["total"]; d["total"] = None
    r = run(db, d)
    inv = db.get(Invoice, r.invoice_id)
    recheck_invoice(db, inv, actor="t", gw=make_sap())
    db.refresh(inv)
    assert "MISSING_TOTAL" in codes(db, inv.id) and inv.queue != "MATCHED"


# ------------------------------------------------------------------ D. lines are never merged or invented
def test_repeated_identical_rows_are_kept_and_flagged(db):
    d = dsk_vision()
    d["lines"] = d["lines"] + [dict(d["lines"][0])]
    d["subtotal"] = d["subtotal"] + d["lines"][0]["amount"]
    r = run(db, d)
    inv = db.get(Invoice, r.invoice_id)
    assert len(inv.lines) == 12 and "LINE_POSSIBLE_DUPLICATE" in codes(db, inv.id)
    assert inv.queue != "MATCHED"


def test_header_hsn_is_not_copied_into_lines(db):
    d = dsk_vision()
    for l in d["lines"]:
        l["hsn_sac"] = ""
    r = run(db, d)
    assert all(not l.hsn_sac for l in db.get(Invoice, r.invoice_id).lines)


# ------------------------------------------------------------------ B. SAP never creates lines or fills values
def test_zero_extracted_lines_never_become_sap_lines_and_never_match(db):
    d = dsk_vision(); d["lines"] = []
    r = run(db, d)
    inv = db.get(Invoice, r.invoice_id)
    assert len(inv.lines) == 0 and inv.queue != "MATCHED" and "NO_LINES" in codes(db, inv.id)


def test_sap_never_fills_po_or_grn(db):
    d = dsk_vision(po_number="", po_numbers_all="")                      # PO and GRN unreadable, invoice number readable
    r = run(db, d, grn="")
    inv = db.get(Invoice, r.invoice_id)
    assert not inv.po_number and not inv.grn_number and inv.queue != "MATCHED"
    sug = inv.sap_snapshot["suggestions"]
    assert sug.get("po_number") == "5100007531" and sug.get("grn_number") == "5000343863"   # kept apart, never applied


def test_sap_never_fills_invoice_number(db):
    r = run(db, dsk_vision(invoice_number=""))
    inv = db.get(Invoice, r.invoice_id)
    assert not inv.invoice_number and inv.queue != "MATCHED"
    assert inv.sap_snapshot["suggestions"].get("invoice_number") == "428/26-27"


def test_wrong_stamp_grn_is_not_silently_replaced(db):
    r = run(db, grn="50-343999")                                         # a GRN that is not in SAP
    inv = db.get(Invoice, r.invoice_id)
    assert inv.grn_number == "5000343999" and inv.queue != "MATCHED"
    assert "GRN_NOT_IN_SAP" in codes(db, inv.id)


# ------------------------------------------------------------------ V. validation rules
def test_total_mismatch_cannot_be_waived_by_one_approval(db):
    d = dsk_vision(total=75000.00)
    r = run(db, d)
    inv = db.get(Invoice, r.invoice_id)
    assert "TOTAL_MISMATCH" in codes(db, inv.id)
    inv.approved_variance = True
    recheck_invoice(db, inv, actor="t", gw=make_sap())
    db.refresh(inv)
    assert inv.queue == "RECONCILE" and "TOTAL_MISMATCH" in codes(db, inv.id)


def test_currency_mismatch_goes_to_review(db):
    r = run(db, dsk_vision(currency="USD"))
    inv = db.get(Invoice, r.invoice_id)
    assert inv.queue == "RECONCILE" and "CURRENCY_MISMATCH" in codes(db, inv.id)


def test_vendor_code_mismatch_goes_to_review(db):
    r = run(db, dsk_vision(vendor_code="99999"))
    inv = db.get(Invoice, r.invoice_id)
    assert inv.queue == "RECONCILE" and "VENDOR_MISMATCH" in codes(db, inv.id)


def test_strict_tolerance_catches_small_value_difference(db):
    r = run(db, gw=make_sap(amount_override={2: 1500}))                  # one line 3% off
    assert "AMOUNT_MISMATCH" in codes(db, r.invoice_id)


def test_nonstandard_gst_rate_is_not_snapped(db):
    d = dsk_vision(cgst=5700.0, sgst=5700.0, total_tax=11400.0, total=75640.0)   # 17.7 %
    r = run(db, d)
    inv = db.get(Invoice, r.invoice_id)
    assert inv.gst_percent == pytest.approx(17.75, abs=0.01) and inv.queue != "MATCHED"
    assert "NON_STANDARD_GST" in codes(db, inv.id)


# ------------------------------------------------------------------ extraction_runs
def test_raw_model_answers_are_stored_in_extraction_runs(db):
    d = dsk_vision()
    d["_runs"] = [{"call_kind": "INVOICE", "page_from": 1, "page_to": 1, "pages_total": 1, "model": "m",
                   "prompt_version": "v2", "prompt_hash": "abc", "raw_response": '{"vendor": "DSK"}',
                   "parsed_json": {"vendor": "DSK"}, "status": "OK", "eval_count": 7, "not_a_column": "ignored"}]
    r = run(db, d)
    row = db.query(ExtractionRun).one()
    assert row.invoice_id == r.invoice_id and row.raw_response == '{"vendor": "DSK"}' and row.eval_count == 7


# ------------------------------------------------------------------ 7. SQL Server table restriction
def test_only_ekko_ekpo_mseg_are_queried(db):
    gw = make_sap()
    seen = []
    orig = gw.rows

    def spy(sql, **kw):
        seen.append(sql)
        return orig(sql, **kw)
    gw.rows = spy
    run(db, gw=gw)
    import re
    tables = {t.upper() for sql in seen for t in re.findall(r"\bdbo\.(\w+)", sql, flags=re.I)}
    assert tables <= {"EKKO", "EKPO", "MSEG"} and "INFORMATION_SCHEMA" not in " ".join(seen).upper()


def test_hsn_auto_mode_is_gone(monkeypatch):
    from app.config import get_settings
    gw = make_sap(hsn_cols=None)
    gw._hsn_cols = None
    monkeypatch.setattr(get_settings(), "sap_hsn_columns", "auto", raising=False)
    calls = []
    orig = gw.rows
    gw.rows = lambda sql, **kw: (calls.append(sql), orig(sql, **kw))[1]
    cols = gw.hsn_columns()
    assert not any("INFORMATION_SCHEMA" in c.upper() for c in calls) and cols["EKPO"] == ["J_1BNBM"]
    gw._hsn_cols = None
    monkeypatch.setattr(get_settings(), "sap_hsn_columns", "MARC.STEUC,EKPO.STAWN", raising=False)
    assert gw.hsn_columns()["EKPO"] == ["STAWN"] and "MARC" not in gw.hsn_columns()
