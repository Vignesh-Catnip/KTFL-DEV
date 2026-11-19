"""Accuracy metrics: AI extraction output vs MANUALLY VERIFIED expected values. No ground truth is ever created or
edited here - this module only compares. Extraction accuracy and SAP-validation accuracy are reported separately."""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation

HEADER_FIELDS = ("invoice_number", "invoice_date", "vendor", "gstin", "po_number", "grn_number", "currency",
                 "subtotal", "cgst", "sgst", "igst", "total", "hsn_sac")
LINE_FIELDS = ("item", "hsn_sac", "quantity", "uom", "rate", "amount")
MONEY = {"subtotal", "cgst", "sgst", "igst", "total", "quantity", "rate", "amount"}

EXACT, PARTIAL, MISSING, FALSE, CORRECT_NULL, INVENTED = (
    "exact", "partial", "missing", "false_value", "correct_null", "invented")


def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _dec(v):
    try:
        return Decimal(str(v).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _squash(v) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", str(v)).casefold()


def compare_value(field: str, predicted, expected) -> str:
    """exact: identical as printed (money: identical number). partial: same after removing spacing / case /
    punctuation (or a description that contains / is contained in the expected one). missing: expected has a value,
    the model gave null. false_value: both have values and they differ. invented: expected is null, the model gave a value."""
    pb, eb = _blank(predicted), _blank(expected)
    if pb and eb:
        return CORRECT_NULL
    if eb:
        return INVENTED
    if pb:
        return MISSING
    if field in MONEY:
        p, e = _dec(predicted), _dec(expected)
        return EXACT if (p is not None and e is not None and p == e) else FALSE
    if str(predicted).strip() == str(expected).strip():
        return EXACT
    sp, se = _squash(predicted), _squash(expected)
    if sp == se:
        return PARTIAL
    if field == "item" and sp and se and (sp in se or se in sp):
        return PARTIAL
    return FALSE


def match_lines(pred: list[dict], exp: list[dict]):
    """One-to-one pairing of expected and predicted lines by (amount, quantity, item text), best first.
    Returns (pairs [(pred_idx, exp_idx)], unmatched_expected, unmatched_predicted)."""
    cand = []
    for ei, e in enumerate(exp):
        for pi, p in enumerate(pred):
            score = 0.0
            ea, pa = _dec(e.get("amount")), _dec(p.get("amount"))
            eq, pq = _dec(e.get("quantity")), _dec(p.get("quantity"))
            score += 3 if ea is not None and ea == pa else 0
            score += 1.5 if eq is not None and eq == pq else 0
            a, b = _squash(e.get("item") or ""), _squash(p.get("item") or "")
            score += 2 if a and b and (a == b or a in b or b in a) else 0
            if score >= 3:
                cand.append((score, -abs(ei - pi), pi, ei))
    cand.sort(reverse=True)
    used_p, used_e, pairs = set(), set(), []
    for _, _, pi, ei in cand:
        if pi not in used_p and ei not in used_e:
            used_p.add(pi); used_e.add(ei); pairs.append((pi, ei))
    return (pairs, [i for i in range(len(exp)) if i not in used_e], [i for i in range(len(pred)) if i not in used_p])


def score_invoice(pred: dict, exp: dict) -> dict:
    """Per-invoice outcome of every header field and every line field."""
    header = {f: compare_value(f, pred.get(f), exp.get(f)) for f in HEADER_FIELDS if f in exp}
    pl, el = pred.get("lines") or [], exp.get("lines") or []
    pairs, miss_e, extra_p = match_lines(pl, el)
    line_fields: dict[str, list[str]] = defaultdict(list)
    exact_lines = 0
    for pi, ei in pairs:
        outs = {f: compare_value(f, pl[pi].get(f), el[ei].get(f)) for f in LINE_FIELDS if f in el[ei]}
        for f, o in outs.items():
            line_fields[f].append(o)
        exact_lines += all(o in (EXACT, CORRECT_NULL) for o in outs.values())
    for ei in miss_e:                                   # an expected line the model never produced: all its fields missing
        for f in LINE_FIELDS:
            if f in el[ei] and not _blank(el[ei].get(f)):
                line_fields[f].append(MISSING)
    return {"header": header, "line_fields": dict(line_fields),
            "lines": {"expected": len(el), "predicted": len(pl), "matched": len(pairs), "missing": len(miss_e),
                      "extra": len(extra_p), "exact": exact_lines, "all_found": not miss_e and not extra_p,
                      "count_equal": len(pl) == len(el)}}


def _rates(counter: Counter) -> dict:
    n = sum(counter.values())
    answered = n - counter[CORRECT_NULL]                 # fields where a value exists in the expected data
    exp_nonnull = counter[EXACT] + counter[PARTIAL] + counter[MISSING] + counter[FALSE]
    return {
        "n": n,
        "exact_match_rate": round(counter[EXACT] / exp_nonnull, 4) if exp_nonnull else None,
        "exact_or_partial_rate": round((counter[EXACT] + counter[PARTIAL]) / exp_nonnull, 4) if exp_nonnull else None,
        "missing_value_rate": round(counter[MISSING] / exp_nonnull, 4) if exp_nonnull else None,
        "false_value_rate": round(counter[FALSE] / exp_nonnull, 4) if exp_nonnull else None,
        "invented_value_count": counter[INVENTED],
        "counts": dict(counter),
    }


def aggregate(scored: list[dict]) -> dict:
    """Field-level, line-level and overall rates for a list of score_invoice() results."""
    by_field: dict[str, Counter] = defaultdict(Counter)
    line_by_field: dict[str, Counter] = defaultdict(Counter)
    lines = Counter()
    for s in scored:
        for f, o in s["header"].items():
            by_field[f][o] += 1
        for f, outs in s["line_fields"].items():
            line_by_field[f].update(outs)
        for k in ("expected", "predicted", "matched", "missing", "extra", "exact"):
            lines[k] += s["lines"][k]
        lines["invoices_all_lines_found"] += s["lines"]["all_found"]
        lines["invoices_line_count_equal"] += s["lines"]["count_equal"]
    overall = Counter()
    for c in list(by_field.values()) + list(line_by_field.values()):
        overall.update(c)
    return {"invoices": len(scored),
            "header_fields": {f: _rates(c) for f, c in sorted(by_field.items())},
            "line_fields": {f: _rates(c) for f, c in sorted(line_by_field.items())},
            "line_items": {**dict(lines),
                           "line_recall": round(lines["matched"] / lines["expected"], 4) if lines["expected"] else None,
                           "line_precision": round(lines["matched"] / lines["predicted"], 4) if lines["predicted"] else None,
                           "exact_line_rate": round(lines["exact"] / lines["expected"], 4) if lines["expected"] else None},
            "overall": _rates(overall)}


def score_validation(rows: list[dict]) -> dict:
    """SAP VALIDATION accuracy, kept apart from extraction accuracy. Each row: {"expected": "MATCHED"|"REVIEW",
    "actual_queue": "MATCHED"|"RECONCILE"|"BLOCKED"}.  false_matched = the dangerous case (posting a wrong invoice)."""
    c = Counter()
    for r in rows:
        exp_ok = r["expected"] == "MATCHED"
        act_ok = r["actual_queue"] == "MATCHED"
        c["false_matched" if (act_ok and not exp_ok) else "missed_match" if (exp_ok and not act_ok) else
          "correct_matched" if act_ok else "correct_review"] += 1
    n = sum(c.values())
    return {"n": n, **dict(c), "false_matched_rate": round(c["false_matched"] / n, 4) if n else None,
            "agreement": round((c["correct_matched"] + c["correct_review"]) / n, 4) if n else None}
