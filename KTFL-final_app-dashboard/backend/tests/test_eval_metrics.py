"""Tests of the METRIC CODE with small hand-made examples. They say nothing about the real model's accuracy."""
from eval.metrics import aggregate, compare_value, match_lines, score_invoice, score_validation
from eval.run_eval import main


def test_compare_value_categories():
    assert compare_value("invoice_number", "428/26-27", "428/26-27") == "exact"
    assert compare_value("invoice_number", "428 / 26-27", "428/26-27") == "partial"
    assert compare_value("invoice_number", None, "428/26-27") == "missing"
    assert compare_value("invoice_number", "429/26-27", "428/26-27") == "false_value"
    assert compare_value("invoice_number", None, None) == "correct_null"
    assert compare_value("invoice_number", "428/26-27", None) == "invented"
    assert compare_value("total", "1,42,115.00", 142115) == "exact"                 # same number
    assert compare_value("total", 142115.5, "142115.00") == "false_value"           # money is never 'partially' right


def test_lines_are_paired_one_to_one_and_repeated_rows_are_not_merged():
    exp = [{"item": "PIN A", "quantity": 5, "amount": 100}, {"item": "PIN A", "quantity": 5, "amount": 100}]
    pred = [{"item": "PIN A", "quantity": 5, "amount": 100}]
    pairs, missing, extra = match_lines(pred, exp)
    assert len(pairs) == 1 and len(missing) == 1 and not extra            # the second identical row was dropped: counted


def test_score_and_aggregate():
    exp = {"invoice_number": "A1", "total": "100.00", "gstin": None,
           "lines": [{"item": "X", "quantity": 1, "amount": 100, "hsn_sac": None}]}
    pred = {"invoice_number": "A1", "total": 99, "gstin": "27ABCDE1234F1Z5",
            "lines": [{"item": "X", "quantity": 1, "amount": 100, "hsn_sac": "7228"}]}
    s = score_invoice(pred, exp)
    assert s["header"] == {"invoice_number": "exact", "total": "false_value", "gstin": "invented"}
    assert s["line_fields"]["hsn_sac"] == ["invented"] and s["lines"]["exact"] == 0
    ag = aggregate([s])
    assert ag["header_fields"]["total"]["false_value_rate"] == 1.0 and ag["overall"]["invented_value_count"] == 2


def test_validation_accuracy_counts_false_matched_separately():
    r = score_validation([{"expected": "REVIEW", "actual_queue": "MATCHED"},
                          {"expected": "MATCHED", "actual_queue": "MATCHED"},
                          {"expected": "REVIEW", "actual_queue": "RECONCILE"},
                          {"expected": "MATCHED", "actual_queue": "RECONCILE"}])
    assert r["false_matched"] == 1 and r["missed_match"] == 1 and r["false_matched_rate"] == 0.25


def test_runner_refuses_to_evaluate_without_verified_labels(tmp_path, capsys):
    (tmp_path / "x.json").write_text('{"_verified_by": "PENDING", "expected": {}}')
    assert main(["--labels", str(tmp_path), "--predictions", str(tmp_path / "p.json"), "--out", str(tmp_path / "r")]) == 2
