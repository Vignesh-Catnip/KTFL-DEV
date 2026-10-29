"""Invoice line <-> SAP goods-receipt row matching (quantity / amount), with typed mismatches."""
from __future__ import annotations

from ..config import get_settings
from .textutil import clean, norm_text, to_float


def _text_hit(item: str, sgtxt: str) -> bool:
    a, b = norm_text(item), norm_text(sgtxt)
    return len(a) >= 6 and len(b) >= 6 and (a in b or b in a)


def compare_lines(inv_lines: list[dict], gr_rows: list[dict], po_items: dict | None = None):
    """
    Pair every invoice line with ONE goods-receipt row and report what differs.

    returns (mismatches, unmatched_gr_rows). Each mismatch is
        {code: QTY_MISMATCH | AMOUNT_MISMATCH | LINE_NOT_IN_GR, line_index, item, invoice_value,
         sap_value, variance, message}
    and every invoice line is annotated with gr_quantity / gr_amount / gr_row / line_status.

    Zero-value GR rows (job-work) carry no amount - only the quantity is compared for those.
    """
    cfg = get_settings()
    qtol, atol = cfg.qty_tolerance, cfg.round_tolerance
    used: set[int] = set()
    mismatches: list[dict] = []

    def row_id(r):
        return f"{clean(r.get('MBLNR'))}/{clean(r.get('EBELP'))}"

    for idx, l in enumerate(inv_lines):
        q, a = to_float(l.get("quantity")), to_float(l.get("amount"))
        l["gr_quantity"], l["gr_amount"], l["gr_row"], l["line_status"] = None, None, None, None
        if not q and not a:
            continue

        # 1) exact: same quantity and (same amount, or zero-value GR)
        hit = None
        for i, r in enumerate(gr_rows):
            if i in used:
                continue
            rq, rv = to_float(r["MENGE"]), to_float(r["DMBTR"])
            if q and abs(rq - q) > qtol:
                continue
            if a and rv > 0 and abs(rv - a) > atol:
                continue
            hit = i
            break
        if hit is not None:
            used.add(hit)
            r = gr_rows[hit]
            l.update(gr_quantity=to_float(r["MENGE"]), gr_amount=to_float(r["DMBTR"]),
                     gr_row=row_id(r), line_status="OK")
            continue

        # 2) nearest candidate, to say WHAT differs
        free = [(i, r) for i, r in enumerate(gr_rows) if i not in used]
        cand = None
        for i, r in free:                                    # same item text
            if _text_hit(l.get("item", ""), r.get("SGTXT", "")):
                cand = (i, r)
                break
        if cand is None:
            for i, r in free:                                # same amount (quantity differs)
                if a and to_float(r["DMBTR"]) > 0 and abs(to_float(r["DMBTR"]) - a) <= atol:
                    cand = (i, r)
                    break
        if cand is None:
            for i, r in free:                                # same quantity (amount differs)
                if q and abs(to_float(r["MENGE"]) - q) <= qtol:
                    cand = (i, r)
                    break

        name = (l.get("item") or "")[:35]
        if cand is None:
            l["line_status"] = "MISSING"
            mismatches.append({
                "code": "LINE_NOT_IN_GR", "line_index": idx, "item": name,
                "invoice_value": f"qty {q:g} / {a:,.2f}", "sap_value": "no matching GR row",
                "variance": a,
                "message": f"Line '{name}' (qty {q:g}, {a:,.2f}) not found in SAP goods receipt"})
            continue

        i, r = cand
        used.add(i)
        rq, rv = to_float(r["MENGE"]), to_float(r["DMBTR"])
        l.update(gr_quantity=rq, gr_amount=rv, gr_row=row_id(r))
        if q and abs(rq - q) > qtol:
            l["line_status"] = "QTY"
            rate = to_float(l.get("rate")) or (a / q if q else 0.0)
            mismatches.append({
                "code": "QTY_MISMATCH", "line_index": idx, "item": name,
                "invoice_value": f"{q:g}", "sap_value": f"{rq:g}",
                "variance": round((q - rq) * rate, 2),
                "message": f"Line '{name}': invoice quantity {q:g} vs goods receipt {rq:g}"})
        else:
            l["line_status"] = "AMOUNT"
            mismatches.append({
                "code": "AMOUNT_MISMATCH", "line_index": idx, "item": name,
                "invoice_value": f"{a:,.2f}", "sap_value": f"{rv:,.2f}",
                "variance": round(a - rv, 2),
                "message": f"Line '{name}': invoice amount {a:,.2f} vs goods receipt {rv:,.2f}"})

    extra = [r for i, r in enumerate(gr_rows) if i not in used and to_float(r["MENGE"]) > 0]
    return mismatches, extra
