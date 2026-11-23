"""Invoice line <-> SAP goods-receipt row matching (quantity / amount), with typed mismatches."""
from __future__ import annotations

from ..config import get_settings
from .textutil import clean, norm_text, to_float


def _text_hit(item: str, sgtxt: str) -> bool:
    a, b = norm_text(item), norm_text(sgtxt)
    return len(a) >= 6 and len(b) >= 6 and (a in b or b in a)


_TOK = __import__("re").compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return [t for t in _TOK.findall((text or "").lower()) if len(t) >= 2]


def _text_score(item: str, sgtxt: str) -> float:
    """Share of the shorter text's words that also appear in the other text. A word counts when it is equal, or one is
    the start of the other ('20MNCr5H' ~ '20MNCR5'); plain numbers must be equal ('70' = '70')."""
    a, b = _tokens(item), _tokens(sgtxt)
    if not a or not b:
        return 0.0
    hits = 0
    for t in set(b):
        for u in set(a):
            if t == u or (not t.isdigit() and not u.isdigit() and min(len(t), len(u)) >= 4 and (t.startswith(u) or u.startswith(t))):
                hits += 1
                break
    return hits / min(len(set(a)), len(set(b))) if hits >= 2 else 0.0


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
        l["gr_po"], l["gr_item"], l["sap_item_hsn"] = "", "", ""
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
                     gr_row=row_id(r), line_status="OK", gr_po=clean(r.get("EBELN")), gr_item=clean(r.get("EBELP")))
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

        if cand is None:                                     # similar words in the item text ('20MNCr5H 70 DIA' ~ '20MNCR5 - 70 DIA')
            best = 0.0
            for i, r in free:
                sc = _text_score(l.get("item", ""), r.get("SGTXT", ""))
                if sc >= 0.5 and sc > best:
                    best, cand = sc, (i, r)
        if cand is None and q and a:                         # same rate per unit: only the quantity (and so the amount) differs
            rate = a / q
            for i, r in free:
                rq, rv = to_float(r["MENGE"]), to_float(r["DMBTR"])
                if rq and rv > 0 and abs(rv / rq - rate) <= max(0.005 * rate, 0.01):
                    cand = (i, r)
                    break
        if cand is None and q:                               # quantity within 5 % (closest wins)
            near = [(abs(to_float(r["MENGE"]) - q) / q, i, r) for i, r in free if to_float(r["MENGE"]) > 0]
            near = [t for t in near if t[0] <= 0.05]
            if near:
                near.sort(key=lambda t: t[0])
                cand = (near[0][1], near[0][2])
        if cand is None and len(free) == 1:                  # the last free GR row and the last open invoice line belong together
            open_lines = [x for x in inv_lines[idx:] if to_float(x.get("quantity")) or to_float(x.get("amount"))]
            r0 = free[0][1]
            rq0, rv0 = to_float(r0["MENGE"]), to_float(r0["DMBTR"])
            near = (q and rq0 and abs(rq0 - q) / q <= 0.25) or (a and rv0 and abs(rv0 - a) / a <= 0.25)
            if len(open_lines) == 1 and near:                # ...and are not wildly different
                cand = free[0]

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
        l.update(gr_quantity=rq, gr_amount=rv, gr_row=row_id(r),
                 gr_po=clean(r.get("EBELN")), gr_item=clean(r.get("EBELP")))
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
