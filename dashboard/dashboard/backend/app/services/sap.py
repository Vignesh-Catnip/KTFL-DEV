"""
SAP data lake (SQL Server, BRONZE_DB) - READ ONLY lookups used to cross-check every invoice.

Tables: MSEG (goods receipt), EKKO (PO header), EKPO (PO items).

Rules (agreed with the AP team):
  * Vendor code, plant, PO date, GRN date / value, company code come from SAP when the invoice
    does not print them.
  * HSN/SAC is NEVER copied from SAP. The invoice's own HSN is kept as is; SAP's HSN (EKPO.STAWN) is
    only stored next to it (`sap_hsn`) so the two can be compared.
  * A value the invoice prints and SAP disagrees with is NOT overwritten - it is reported as a mismatch
    (exception: plant and vendor code, where SAP is authoritative).
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import URL, Engine

from ..config import Settings, get_settings
from .linematch import compare_lines
from .textutil import (clean, fmt_sap_date, is_valid_po, json_safe, norm_hsn, norm_key,
                       strip_zeros, to_float)

log = logging.getLogger("clearmatch.sap")

MSEG_COLS = """MBLNR, MJAHR, BWART, EBELN, EBELP, LIFNR, WERKS, MENGE, MEINS, ERFMG, ERFME,
               DMBTR, WAERS, MWSKZ, XBLNR_MKPF, BUDAT_MKPF, SGTXT, SMBLN"""


# ------------------------------------------------------------------------------ gateway

class SapGateway:
    def __init__(self, engine: Engine, client: str = "", grn_types: tuple[str, ...] = ("101", "105")):
        self.engine = engine
        self.client = client
        self.grn_types = grn_types

    def rows(self, sql: str, **params) -> list[dict]:
        stmt = text(sql)
        expanding = [k for k, v in params.items() if isinstance(v, (list, tuple))]
        if expanding:
            stmt = stmt.bindparams(*[bindparam(k, expanding=True) for k in expanding])
        if self.client and ":mandt" in sql:
            params = {**params, "mandt": self.client}
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(stmt, params)]

    def mandt(self, alias: str) -> str:
        return f" AND {alias}.MANDT = :mandt" if self.client else ""

    def ping(self) -> None:
        with self.engine.connect() as conn:
            conn.execute(text("SELECT 1"))


def build_sap_engine(cfg: Settings) -> Engine | None:
    if cfg.sap_db_url:
        return create_engine(cfg.sap_db_url)
    if not (cfg.sap_server and cfg.sap_user and cfg.sap_password):
        return None
    url = URL.create("mssql+pyodbc", username=cfg.sap_user, password=cfg.sap_password,
                     host=cfg.sap_server, database=cfg.sap_database,
                     query={"driver": cfg.sap_driver, "TrustServerCertificate": "yes"})
    return create_engine(url, pool_pre_ping=True, pool_size=5)


_gateway: SapGateway | None = None
_gateway_lock = threading.Lock()
_last_attempt = 0.0
_last_error = ""


def get_sap(force: bool = False) -> SapGateway | None:
    """The shared gateway, or None when SAP is disabled / unreachable (retried at most once a minute)."""
    global _gateway, _last_attempt, _last_error
    cfg = get_settings()
    if not cfg.sap_enabled:
        return None
    with _gateway_lock:
        if _gateway is not None:
            return _gateway
        if not force and time.time() - _last_attempt < 60:
            return None
        _last_attempt = time.time()
        try:
            eng = build_sap_engine(cfg)
            if eng is None:
                _last_error = "SAP credentials are not configured"
                return None
            gw = SapGateway(eng, cfg.sap_client, cfg.grn_types)
            gw.ping()
            _gateway, _last_error = gw, ""
        except Exception as exc:
            _last_error = f"{type(exc).__name__}: {exc}"
            log.warning("SAP connection failed: %s", _last_error)
            return None
    return _gateway


def set_sap(gw: SapGateway | None) -> None:
    """Tests (and the app on shutdown) replace the shared gateway."""
    global _gateway, _last_attempt
    _gateway = gw
    _last_attempt = 0.0


def sap_status() -> dict:
    cfg = get_settings()
    if not cfg.sap_enabled:
        return {"enabled": False, "connected": False, "detail": "SAP check disabled"}
    gw = get_sap()
    if gw is not None:
        try:
            gw.ping()
            return {"enabled": True, "connected": True,
                    "detail": f"{cfg.sap_server or 'SAP'}/{cfg.sap_database}"}
        except Exception as exc:
            set_sap(None)
            return {"enabled": True, "connected": False, "detail": f"{type(exc).__name__}: {exc}"}
    return {"enabled": True, "connected": False, "detail": _last_error or "not connected"}


# ------------------------------------------------------------------------------ enrichment

def _mismatch(h: dict, field: str, invoice_value, sap_value, label: str | None = None) -> None:
    h["sap_mismatches"].append({
        "code": "FIELD_MISMATCH", "field": field, "invoice_value": str(invoice_value),
        "sap_value": str(sap_value),
        "message": f"{label or field}: invoice '{invoice_value}' vs SAP '{sap_value}'"})


def sap_enrich(gw: SapGateway | None, h: dict, lines: list[dict], allow_grn_switch: bool = True) -> list[str]:
    """Cross-check the invoice against SAP. Mutates `h` and `lines`; returns audit notes.

    allow_grn_switch=False is used after a person typed the GRN by hand: SAP must confirm that exact
    GRN, it is never silently replaced by a different one.
    """
    cfg = get_settings()
    notes: list[str] = []
    h["sap_mismatches"], h["line_mismatches"] = [], []
    h["sap_checked"] = False
    if gw is None:
        return ["SAP not connected - invoice could not be cross-checked"]
    h["sap_checked"] = True
    src = h["field_sources"]
    base_amount_tol, round_tol = cfg.amount_tolerance, cfg.round_tolerance

    def fill(field, value, source, overwrite=False):
        """Fill an empty field from SAP. A different non-empty value is reported, not overwritten
        (unless overwrite=True for fields where SAP is authoritative)."""
        if value in (None, "", 0, 0.0):
            return
        cur = h.get(field)
        if cur in (None, "", 0, 0.0):
            h[field] = value
            src[field] = source
            notes.append(f"{field} <- {source}: {value}")
        elif str(cur).strip().lstrip("0") != str(value).strip().lstrip("0"):
            if overwrite:
                notes.append(f"{field} overwritten by {source}: '{cur}' -> '{value}'")
                h[field] = value
                src[field] = source
            else:
                _mismatch(h, field, cur, value)
                notes.append(f"MISMATCH {field}: invoice '{cur}' vs SAP '{value}'")

    inv_no, grn = h["invoice_number"], h["grn_number"]
    po_now = h["po_number"]
    types = list(gw.grn_types)

    def live(rows):
        """Drop reversed documents; prefer rows carrying item text (SGTXT) - same as the manual check
        XBLNR_MKPF = invoice AND EBELN = PO AND SGTXT is not null."""
        rows = [r for r in rows if not clean(r.get("SMBLN"))]
        txt = [r for r in rows if clean(r.get("SGTXT"))]
        return txt or rows

    def by_grn(rows):
        g: dict[str, list] = {}
        for r in rows:
            g.setdefault(clean(r["MBLNR"]), []).append(r)
        return g

    def score(rows, use_po=True):
        """How well a GR document fits THIS invoice: line amounts, quantities, item text, PO."""
        sc = 0.0
        amts = [to_float(r["DMBTR"]) for r in rows]
        qtys = [to_float(r["MENGE"]) for r in rows]
        texts = [re.sub(r"[^A-Z0-9]", "", clean(r.get("SGTXT")).upper()) for r in rows]
        for l in lines:
            item = re.sub(r"[^A-Z0-9]", "", l["item"].upper())
            if l["amount"] and any(abs(l["amount"] - a) <= round_tol for a in amts):
                sc += 1
            if l["quantity"] and any(abs(l["quantity"] - q) < cfg.qty_tolerance for q in qtys):
                sc += 0.5
            if item and any(len(t) >= 6 and (t in item or item in t) for t in texts):
                sc += 2
        sub = h["subtotal"]
        if sub and sum(amts) and abs(sum(amts) - sub) <= max(round_tol, sub * base_amount_tol):
            sc += 5
        if use_po and is_valid_po(po_now) and any(clean(r["EBELN"]) == po_now for r in rows):
            sc += 3
        return sc

    # 1) GR lines for the GRN read from the stamp (goods receipt movements only: 101/105)
    grn_rows: list[dict] = []
    if grn:
        grn_rows = live(gw.rows(f"SELECT {MSEG_COLS} FROM dbo.MSEG m WHERE m.MBLNR = :grn "
                                f"AND m.BWART IN :bwart{gw.mandt('m')}", grn=grn, bwart=types))
        if not grn_rows:
            notes.append(f"GRN {grn} not found in SAP (MSEG)" +
                         (" - looking it up by invoice no / PO / line items" if allow_grn_switch else ""))

    # 2) GR lines by the vendor's invoice reference (MSEG.XBLNR_MKPF) - spaces ignored
    inv_rows: list[dict] = []
    inv_all: list[dict] = []
    if inv_no and (allow_grn_switch or not grn_rows):
        raw = gw.rows(f"""SELECT {MSEG_COLS} FROM dbo.MSEG m
                          WHERE UPPER(REPLACE(LTRIM(RTRIM(m.XBLNR_MKPF)),' ','')) = :inv
                            AND m.BWART IN :bwart{gw.mandt('m')}""", inv=norm_key(inv_no), bwart=types)
        raw = [r for r in raw if not clean(r.get("SMBLN"))]

        # The same invoice no (e.g. "428/26-27") is used by many vendors: keep only rows whose PO belongs
        # to THE invoice's vendor (vendor from the PO header EKKO.LIFNR - MSEG.LIFNR is often blank).
        po_vendor: dict[str, str] = {}
        pos_raw = sorted({clean(r["EBELN"]) for r in raw if clean(r["EBELN"])} |
                         ({po_now} if is_valid_po(po_now) else set()))
        if pos_raw:
            for r in gw.rows(f"SELECT EBELN, LIFNR FROM dbo.EKKO k WHERE k.EBELN IN :pos{gw.mandt('k')}",
                             pos=pos_raw):
                po_vendor[clean(r["EBELN"])] = strip_zeros(r["LIFNR"])
        own_v = po_vendor.get(po_now, "") if is_valid_po(po_now) else ""
        if not own_v:
            own_v = next((strip_zeros(r["LIFNR"]) for r in raw
                          if clean(r["EBELN"]) == po_now and clean(r["LIFNR"])), "")
        if own_v:
            inv_all = [r for r in raw if po_vendor.get(clean(r["EBELN"]), "") == own_v]
            if len(inv_all) < len(raw):
                other = sorted({clean(r["EBELN"]) for r in raw} - {clean(r["EBELN"]) for r in inv_all})
                notes.append(f"Ignored {len(raw) - len(inv_all)} SAP rows with the same invoice no but another "
                             f"vendor's PO ({', '.join(other)}); vendor {own_v} kept")
        else:
            inv_all = raw
        inv_rows = live(inv_all)
    inv_groups = by_grn(inv_rows)

    def po_ok(rows):
        return (not is_valid_po(po_now)) or any(clean(r["EBELN"]) == po_now for r in rows)

    # 3) decide which GRN is the right one
    groups = dict(inv_groups)
    resolved = None
    if grn_rows and not po_ok(grn_rows):
        notes.append(f"GRN {grn} exists in SAP but for another PO - not this invoice's PO {po_now}")
    if grn_rows and po_ok(grn_rows) and (not groups or grn in groups or not allow_grn_switch):
        resolved = (grn, grn_rows)
    elif allow_grn_switch:
        if not groups:                                      # no invoice-ref match -> try by PO
            pos_try = sorted(set(h["po_numbers_printed"]) | ({po_now} if is_valid_po(po_now) else set()))
            if pos_try:
                po_rows = live(gw.rows(f"""SELECT {MSEG_COLS} FROM dbo.MSEG m
                                           WHERE m.EBELN IN :pos AND m.BWART IN :bwart{gw.mandt('m')}""",
                                       pos=pos_try, bwart=types))
                groups = by_grn(po_rows)
        best = max(groups.items(), key=lambda kv: (score(kv[1]), len(kv[1])), default=None)
        ok = bool(best) and (bool(inv_groups) or score(best[1], use_po=False) >= 1)
        if ok and grn_rows and score(grn_rows, use_po=False) >= score(best[1], use_po=False):
            ok = False                                      # the stamp GRN fits at least as well
        if ok:
            resolved = best
            if len(groups) > 1:
                notes.append(f"Several GRNs found {sorted(groups)} - picked {best[0]} "
                             f"(best match on line amounts / taxable value / PO)")
        elif grn_rows:
            resolved = (grn, grn_rows)

    gr_rows: list[dict] = []
    h["grn_in_sap"] = False
    if resolved:
        new_grn, gr_rows = resolved
        h["grn_in_sap"] = True
        if new_grn != grn:
            notes.append(f"GRN {grn or '(not read)'} (stamp) -> {new_grn} taken from SAP "
                         f"(invoice {inv_no} / PO {po_now or '?'})")
            h["grn_number"] = new_grn
            src["grn_number"] = "SAP_MSEG"
    else:
        notes.append(f"No GRN found in SAP for invoice {inv_no or '?'} / PO {po_now or '?'}")

    own_gr: list[dict] = []
    if gr_rows:
        own_gr = [r for r in gr_rows if clean(r["EBELN"]) == h["po_number"]] or gr_rows
        fill("grn_date", fmt_sap_date(own_gr[0]["BUDAT_MKPF"]), "SAP_MSEG")
        if clean(own_gr[0]["WERKS"]):                       # the GRN's plant is authoritative
            h["plant_code"] = clean(own_gr[0]["WERKS"])
            src["plant_code"] = "SAP_MSEG"
            h["plant_source"] = "SAP_MSEG"
        g = [r for r in own_gr if clean(r["MBLNR"]) == h["grn_number"]] or own_gr
        vals = [to_float(r["DMBTR"]) for r in g]
        if vals and all(v > 0 for v in vals):
            h["grn_amount"] = round(sum(vals), 2)
            src["grn_amount"] = "SAP_MSEG"
        else:       # job-work / free-of-charge GR lines carry 0.00 - the value is not comparable
            h["grn_amount"] = None
            notes.append("GR value not compared with invoice (GR lines carry zero value, e.g. job-work)")
        fill("sap_tax_code", clean(own_gr[0].get("MWSKZ")), "SAP_MSEG")

    # all POs this invoice touches in SAP -> multiple POs = red flag
    sap_pos = sorted({clean(r["EBELN"]) for r in (gr_rows + inv_rows) if clean(r["EBELN"])})
    if not is_valid_po(h["po_number"]) and sap_pos:
        h["po_number"] = sap_pos[0]
        src["po_number"] = "SAP_MSEG"
        notes.append(f"po_number <- SAP_MSEG: {sap_pos[0]}")
    all_pos = sorted(set(sap_pos) | set(h["po_numbers_printed"]) |
                     ({h["po_number"]} if is_valid_po(h["po_number"]) else set()))
    h["po_list"] = ", ".join(all_pos)
    h["multi_po_flag"], h["red_flag_reason"] = False, ""
    scope_rows = gr_rows                     # the goods-receipt rows this invoice is checked against
    if len(sap_pos) > 1:
        # One vendor invoice booked against several POs / GRNs: NOT a block. The invoice's PO, GRN and
        # line items are checked against ALL its goods-receipt rows; if they agree it is Matched and the
        # robot picks the right GR lines in MIRO.
        h["multi_po_flag"] = True
        h["red_flag_reason"] = (f"Invoice is booked against {len(sap_pos)} POs in SAP ({', '.join(sap_pos)}) - "
                                f"lines checked across all of them")
        notes.append("MULTI-PO: " + h["red_flag_reason"])
        seen = {(clean(r["MBLNR"]), clean(r["EBELP"]), clean(r["EBELN"])) for r in gr_rows}
        scope_rows = gr_rows + [r for r in inv_rows if (clean(r["MBLNR"]), clean(r["EBELP"]), clean(r["EBELN"])) not in seen]
        vals = [to_float(r["DMBTR"]) for r in scope_rows]
        if vals and all(v > 0 for v in vals):
            h["grn_amount"] = round(sum(vals), 2)
    elif len(all_pos) > 1:
        _mismatch(h, "po_number", ", ".join(all_pos), ", ".join(sap_pos) or "none",
                  "PO numbers printed on invoice vs SAP GR")

    snapshot = {"checked_at": datetime.now(timezone.utc).isoformat(), "grn": h["grn_number"],
                "po": None, "po_items": [], "gr_rows": [], "pos": all_pos}

    if not all_pos:
        notes.append("No PO found (invoice + SAP)")
        h["sap_snapshot"] = json_safe(snapshot)
        return notes

    # 4) PO header (EKKO)
    ekko = gw.rows(f"SELECT EBELN, LIFNR, WAERS, BEDAT, BUKRS FROM dbo.EKKO k "
                   f"WHERE k.EBELN IN :pos{gw.mandt('k')}", pos=all_pos)
    main = next((r for r in ekko if clean(r["EBELN"]) == h["po_number"]), None)
    h["po_in_sap"] = main is not None
    if main:
        fill("vendor_code", strip_zeros(main["LIFNR"]), "SAP_EKKO", overwrite=True)
        if h["currency"] in ("", "INR") and clean(main["WAERS"]):
            h["currency"] = clean(main["WAERS"])
            src["currency"] = "SAP_EKKO"
        fill("po_date", fmt_sap_date(main["BEDAT"]), "SAP_EKKO")
        if clean(main.get("BUKRS")):
            h["company_code"] = clean(main["BUKRS"])
        snapshot["po"] = main
    else:
        notes.append(f"PO {h['po_number']} not found in SAP (EKKO)")

    # 5) PO items (EKPO) - plant fall-back, tax code, and the SAP HSN (compare only!)
    ekpo = gw.rows(f"""SELECT EBELN, EBELP, TXZ01, MATNR, WERKS, STAWN, MENGE, MEINS,
                              NETPR, PEINH, NETWR, BRTWR, MWSKZ
                       FROM dbo.EKPO p
                       WHERE p.EBELN IN :pos AND COALESCE(p.LOEKZ,'') = ''{gw.mandt('p')}
                       ORDER BY p.EBELN, p.EBELP""", pos=all_pos)
    ekpo_map = {(clean(r["EBELN"]), clean(r["EBELP"])): r for r in ekpo}
    own = [r for r in ekpo if clean(r["EBELN"]) == h["po_number"]] or ekpo
    if own:
        if src.get("plant_code") != "SAP_MSEG" and clean(own[0]["WERKS"]):
            h["plant_code"] = clean(own[0]["WERKS"])
            src["plant_code"] = "SAP_EKPO"
            h["plant_source"] = "SAP_EKPO"
        elif clean(own[0]["WERKS"]) and clean(own[0]["WERKS"]) != h["plant_code"]:
            notes.append(f"Plant: GRN plant {h['plant_code']} vs PO item plant {clean(own[0]['WERKS'])} "
                         f"- GRN plant kept")
        fill("sap_tax_code", clean(own[0]["MWSKZ"]), "SAP_EKPO")
        hs = sorted({norm_hsn(r.get("STAWN")) for r in own if norm_hsn(r.get("STAWN"))})
        h["sap_hsn"] = ", ".join(hs)                         # compare only - never copied into hsn_sac
        if len({clean(r["WERKS"]) for r in own if clean(r["WERKS"])}) > 1:
            notes.append("PO spans multiple plants")
        if h["multi_po_flag"]:
            hs = sorted({norm_hsn(r.get("STAWN")) for r in ekpo if norm_hsn(r.get("STAWN"))})
            h["sap_hsn"] = ", ".join(hs)
    snapshot["po_items"] = ekpo if h["multi_po_flag"] else own
    snapshot["gr_rows"] = (scope_rows if h["multi_po_flag"]
                           else [r for r in own_gr if clean(r["MBLNR"]) == h["grn_number"]] or own_gr)

    # 6) invoice lines: take them from SAP only when the vision pass found none (HSN stays blank)
    if not lines:
        rows = [r for r in gr_rows if clean(r["MBLNR"]) == h["grn_number"]] or gr_rows
        for r in rows:
            p = ekpo_map.get((clean(r["EBELN"]), clean(r["EBELP"])), {})
            qty = to_float(r["MENGE"])
            lines.append({"item": clean(p.get("TXZ01")) or clean(r["SGTXT"]), "quantity": qty,
                          "uom": clean(r["MEINS"]),
                          "rate": round(to_float(r["DMBTR"]) / qty, 2) if qty else 0.0,
                          "hsn_sac": "", "amount": to_float(r["DMBTR"]),
                          "po_number": clean(r["EBELN"]), "po_item": clean(r["EBELP"]),
                          "plant_code": clean(r["WERKS"]), "source": "SAP_MSEG"})
        if not lines:
            for p in ekpo:
                peinh = to_float(p["PEINH"]) or 1
                lines.append({"item": clean(p["TXZ01"]), "quantity": to_float(p["MENGE"]),
                              "uom": clean(p["MEINS"]), "rate": round(to_float(p["NETPR"]) / peinh, 2),
                              "hsn_sac": "", "amount": to_float(p["NETWR"]),
                              "po_number": clean(p["EBELN"]), "po_item": clean(p["EBELP"]),
                              "plant_code": clean(p["WERKS"]), "source": "SAP_EKPO"})
        if lines:
            notes.append(f"{len(lines)} line items filled from SAP (invoice lines could not be read)")
    else:
        for l in lines:
            if not l.get("plant_code"):
                l["plant_code"] = h["plant_code"]

    # 7) line-by-line: invoice quantity / amount vs SAP goods receipt
    vision_lines = [l for l in lines if l.get("source", "VISION") == "VISION"]
    if gr_rows and vision_lines:
        if h["multi_po_flag"]:
            gp = scope_rows
        else:
            gp = [r for r in gr_rows if clean(r["EBELN"]) == h["po_number"]] or gr_rows
            gp = [r for r in gp if clean(r["MBLNR"]) == h["grn_number"]] or gp
        h["line_mismatches"], extra = compare_lines(vision_lines, gp, ekpo_map)
        if extra:
            notes.append(f"{len(extra)} SAP GR rows are not matched to any invoice line "
                         f"(e.g. {clean(extra[0].get('SGTXT'))[:30]} qty {to_float(extra[0]['MENGE']):g})")
        for m in h["line_mismatches"]:
            notes.append("MISMATCH " + m["message"])
        if not h["line_mismatches"]:
            notes.append(f"All {len(vision_lines)} invoice lines match SAP GR (qty and amount)")

    # 8) taxable value: only filled from the GRN when the invoice has none; differences are judged
    #    (with tolerance) in validate()
    if not h["subtotal"] and h["grn_amount"]:
        h["subtotal"] = h["grn_amount"]
        src["subtotal"] = "SAP_MSEG"

    h["sap_snapshot"] = json_safe(snapshot)
    return notes
