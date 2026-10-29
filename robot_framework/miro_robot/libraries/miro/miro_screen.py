"""
SAP logon + MIRO screen operations, based on the field IDs in the SAP GUI recording:

  login        wnd[0]/usr/txtRSYST-BNAME, pwdRSYST-BCODE
  company code wnd[1]/usr/ctxtBKPF-BUKRS
  header       ...SAPLFDCB:0010/ctxtINVFO-BLDAT (invoice date), txtINVFO-XBLNR (reference)
  PO           ...SAPLMR1M:6211/ctxtRM08M-EBELN
  item table   ...SAPLMR1M:6310/tblSAPLMR1MTC_MR1M  (+ btnDESELECT_ALL, getAbsoluteRow(n).selected)
  collapse     wnd[0]/usr/btnRM08M-HEADER_COLLAPSE

All fields are located by technical NAME (FindByName), so the code keeps working when the
subscreen number changes (6005 expanded / 6006 collapsed header).
"""
from __future__ import annotations

import re
import time

from .config import Config
from .matching import (MiroDataError, MiroRow, extract_document_number, format_sap_amount,
                       format_sap_date, is_duplicate_message, parse_sap_number)
from .sapgui import SapError, SapSession, get_scripting_application

TABLE = "SAPLMR1MTC_MR1M"


# ======================================================================= LOGON

def _logged_on(sess) -> bool:
    try:
        return bool(sess.Info.User) and sess.Info.Program != "SAPMSYST"
    except Exception:
        return False


def connect_sap(cfg: Config) -> SapSession:
    app = get_scripting_application(cfg)

    # 1) reuse an already logged-on session of the right client
    try:
        for ci in range(app.Children.Count):
            conn = app.Children.ElementAt(ci)
            for si in range(conn.Children.Count):
                sess = conn.Children.ElementAt(si)
                if _logged_on(sess) and (not cfg.sap_client or str(sess.Info.Client) == cfg.sap_client):
                    if not cfg.sap_user or str(sess.Info.User).upper() == cfg.sap_user.upper():
                        s = SapSession(sess, cfg)
                        s.find("wnd[0]").Maximize()
                        return s
    except Exception:
        pass

    # 2) open the connection from SAP Logon pad and log on
    if not cfg.sap_connection:
        raise SapError("No logged-on SAP session found and SAP_CONNECTION is not set "
                       "(name of the system in SAP Logon, e.g. 'KTFL PRD')")
    conn = app.OpenConnection(cfg.sap_connection, True)
    sess = conn.Children.ElementAt(0)
    s = SapSession(sess, cfg)
    s.find("wnd[0]").Maximize()
    logon(s, cfg)
    return s


def logon(s: SapSession, cfg: Config) -> None:
    if _logged_on(s.s):
        return
    if not (cfg.sap_user and cfg.sap_password):
        raise SapError("SAP_USER / SAP_PASSWORD environment variables are not set")
    if s.exists("wnd[0]/usr/txtRSYST-MANDT") and cfg.sap_client:
        s.find("wnd[0]/usr/txtRSYST-MANDT").Text = cfg.sap_client
    s.find("wnd[0]/usr/txtRSYST-BNAME").Text = cfg.sap_user
    s.find("wnd[0]/usr/pwdRSYST-BCODE").Text = cfg.sap_password
    if s.exists("wnd[0]/usr/txtRSYST-LANGU") and cfg.sap_language:
        s.find("wnd[0]/usr/txtRSYST-LANGU").Text = cfg.sap_language
    s.enter()

    for _ in range(6):                                   # popups after logon
        mt, txt = s.status()
        if mt in ("E", "A") and not _logged_on(s.s):
            raise SapError(f"SAP logon failed: {txt}")
        if not s.popup_open():
            break
        if s.exists("wnd[1]/usr/pwdRSYST-NCODE"):
            raise SapError("SAP asks for a NEW password for the robot user - change it manually")
        opt = f"wnd[1]/usr/radMULTI_LOGON_OPT{cfg.sap_multi_logon_option}"
        if s.exists(opt):                                # "user already logged on" popup
            s.find(opt).Select()
        if s.exists("wnd[1]/tbar[0]/btn[0]"):
            s.find("wnd[1]/tbar[0]/btn[0]").Press()
            s.wait_ready()
        else:
            s.enter(1)

    if not _logged_on(s.s):
        _, txt = s.status()
        raise SapError(f"SAP logon not completed ({txt or 'still on logon screen'})")


# ======================================================================= DB fields

def tax_code(inv: dict) -> str:
    """invoices.tax_code (e.g. I3); older rows: gst_tax_code / sap_tax_code."""
    for k in ("tax_code", "gst_tax_code", "sap_tax_code"):
        v = str(inv.get(k) or "").strip().upper()
        if v:
            return v
    return ""


def _norm(t: str) -> str:
    return " ".join(str(t or "").lower().replace("'", "'").split())


def pick_doc_type(entries: list[tuple[str, str]], key: str = "", desc: str = "") -> str:
    """The drop-down entry for an invoice: exact description, then exact key, then a unique description that
    contains / is contained in the wanted text. Returns the SAP key ('' = not found / not unique)."""
    d, k = _norm(desc), str(key or "").strip().upper()
    if d:
        hit = [ek for ek, ev in entries if _norm(ev) == d]
        if len(hit) == 1:
            return hit[0]
    if k:
        hit = [ek for ek, _ in entries if ek.strip().upper() == k]
        if len(hit) == 1:
            return hit[0]
    if d:
        hit = [ek for ek, ev in entries if d in _norm(ev) or (_norm(ev) and _norm(ev) in d)]
        if len(hit) == 1:
            return hit[0]
    return ""


def business_place(inv: dict) -> str:
    """invoices.business_place (e.g. 2002); fallback plant_code."""
    for k in ("business_place", "plant_code"):
        v = str(inv.get(k) or "").strip()
        if v:
            return v
    return ""


# ======================================================================= MIRO

class MiroScreen:
    def __init__(self, s: SapSession, cfg: Config):
        self.s = s
        self.cfg = cfg
        self.log: list[str] = []
        self.collapsed = False

    def note(self, msg: str) -> None:
        self.log.append(msg)

    # ------------------------------------------------------------ messages
    def handle_messages(self, stage: str, max_confirms: int = 4) -> None:
        """After an Enter: raise on errors, stop on duplicate-invoice warning,
        confirm other warnings / info popups with Enter."""
        for _ in range(max_confirms):
            if self.s.popup_open():
                ptxt = self.s.popup_text()
                if self.s.exists("wnd[1]/usr/ctxtBKPF-BUKRS"):
                    return                                     # company-code popup handled elsewhere
                self.note(f"[{stage}] popup: {ptxt}")
                if self.cfg.stop_on_duplicate_warning and is_duplicate_message(ptxt):
                    raise MiroDataError(f"SAP duplicate-invoice warning: {ptxt}")
                self.s.enter(1)
                continue
            mt, txt = self.s.status()
            if mt in ("E", "A"):
                raise SapError(f"[{stage}] SAP error: {txt}")
            if mt == "W":
                self.note(f"[{stage}] warning: {txt}")
                if self.cfg.stop_on_duplicate_warning and is_duplicate_message(txt):
                    raise MiroDataError(f"SAP duplicate-invoice warning: {txt}")
                self.s.enter()                                  # confirm warning
                continue
            if txt:
                self.note(f"[{stage}] {mt or 'msg'}: {txt}")
            return

    # ------------------------------------------------------------ start
    def open(self) -> None:
        self.leave()                                          # clean start, discards any half-entered doc
        self.collapsed = False
        self.s.okcode("/nMIRO")
        if self.s.exists("wnd[1]/usr/ctxtBKPF-BUKRS"):
            self.s.find("wnd[1]/usr/ctxtBKPF-BUKRS").Text = self.cfg.company_code
            self.s.enter(1)
        if self.s.transaction().upper() != "MIRO":
            _, txt = self.s.status()
            raise SapError(f"Could not open MIRO ({txt or self.s.screen_info()})")

        title = str(self.s.find("wnd[0]").Text or "")
        if self.cfg.company_code and self.cfg.company_code not in title:
            self.note(f"MIRO opened for '{title}' - switching to company code {self.cfg.company_code}")
            self.s.select_menu("Edit", "Switch Company Code")
            self.s.find("wnd[1]/usr/ctxtBKPF-BUKRS").Text = self.cfg.company_code
            self.s.enter(1)
            title = str(self.s.find("wnd[0]").Text or "")
            if self.cfg.company_code not in title:
                raise SapError(f"MIRO is not in company code {self.cfg.company_code}: '{title}'")

    # ------------------------------------------------------------ header (Basic data tab)
    def select_tab(self, tab: str) -> None:
        """HEADER_TOTAL = Basic data, HEADER_FI = Details."""
        self.expand_header()
        el = self.s.find_name(tab, types=("GuiTab",), required=False)
        if el is None:
            raise SapError(f"MIRO header tab {tab} not found ({self.s.screen_info()})")
        self.s._show(el, f"Open tab {'Details' if tab == 'HEADER_FI' else 'Basic data'}")
        el.Select()
        self.s.wait_ready()

    def expand_header(self) -> None:
        if self.collapsed:
            self.s.press_name("RM08M-HEADER_COLLAPSE")
            self.collapsed = False

    def fill_header(self, inv: dict) -> None:
        """Basic data part 1 (before the PO): date, reference, currency -> Enter."""
        c = self.cfg
        date = format_sap_date(inv.get("invoice_date"), c.sap_date_format)
        ref = str(inv.get("invoice_number") or "").strip()
        if not ref:
            raise MiroDataError("Invoice number is empty")
        if len(ref) > 16:
            raise MiroDataError(f"Invoice number '{ref}' longer than 16 characters (SAP Reference)")
        # check DB data now, so a bad invoice stops before anything is typed
        if c.enter_plant and not business_place(inv):
            raise MiroDataError("business_place / plant_code is empty in the database - "
                                "needed for Business Place / Section")
        if c.enter_tax_code and not tax_code(inv):
            raise MiroDataError("tax_code is empty in the database (invoices.tax_code)")

        self.s.set_field("INVFO-BLDAT", date)
        self.s.set_field("INVFO-XBLNR", ref)
        if c.enter_currency and inv.get("currency"):
            self.s.set_field("INVFO-WAERS", str(inv["currency"]).strip())
        self.s.enter()
        self.handle_messages("header")
        self.note(f"Header: date {date}, reference {ref}")

    def fill_tax_and_plant(self, inv: dict) -> None:
        """Basic data part 2 - AFTER the PO is entered (SAP overwrites the header tax code with
        the PO default, e.g. I0, when the PO is loaded). Same order as the SAP recording:
        Calculate tax, Tax code, Business Place, Section, Text -> Enter -> tax code verified."""
        c = self.cfg
        plant = business_place(inv)
        text = ""
        if c.enter_item_text:
            for line in inv.get("lines") or []:
                text = str(line.get("item") or "").strip()
                if text:
                    break
            text = text[:50]                                  # SGTXT is 50 characters

        self.select_tab("HEADER_TOTAL")
        if c.calculate_tax:
            self.s.set_field("INVFO-XMWST", True)
        tax = tax_code(inv) if c.enter_tax_code else ""
        if tax:
            self.set_tax_code(tax)
        if c.enter_plant:
            self.s.set_field("INVFO-BUPLA", plant)
            self.s.set_field("INVFO-SECCO", plant)
        if text:
            self.s.set_field("INVFO-SGTXT", text)
        self.s.enter()
        self.handle_messages("tax / business place")
        if tax:
            self.ensure_tax_code(tax)
        self.note(f"Tax code {tax or '-'}, business place {plant or '-'}, text '{text}'")
        if c.collapse_header and not self.collapsed:
            if self.s.press_name("RM08M-HEADER_COLLAPSE", required=False):
                self.collapsed = True

    def ensure_tax_code(self, code: str) -> None:
        """After an Enter SAP may put the PO default tax code back - set it again once."""
        self.select_tab("HEADER_TOTAL")
        combo = self.s.find_name("INVFO-MWSKZ", types=("GuiComboBox", "GuiCTextField"))
        got = str(getattr(combo, "Key", "") or "").strip().upper()
        if got == code:
            return
        self.note(f"Tax code changed by SAP to '{got}' - setting {code} again")
        self.set_tax_code(code)
        self.s.enter()
        self.handle_messages("tax code")
        combo = self.s.find_name("INVFO-MWSKZ", types=("GuiComboBox", "GuiCTextField"))
        got = str(getattr(combo, "Key", "") or "").strip().upper()
        if got != code:
            raise SapError(f"SAP keeps tax code '{got}' instead of '{code}'")

    def set_tax_code(self, code: str) -> None:
        """Basic data: Tax code combo (cmbINVFO-MWSKZ).key = 'I3' - as in the SAP recording."""
        combo = self.s.find_name("INVFO-MWSKZ", types=("GuiComboBox", "GuiCTextField"))
        entries = []
        try:
            for i in range(combo.Entries.Count):
                e = combo.Entries.ElementAt(i)
                entries.append(str(e.Key).strip())
        except Exception:
            pass
        if entries and code not in entries:
            raise MiroDataError(f"Tax code '{code}' from the database is not in the MIRO tax code list "
                                f"{entries}")
        try:
            self.s.set_field("INVFO-MWSKZ", code)
        except Exception as e:
            raise SapError(f"Could not set tax code '{code}': {e}")
        self.s.wait_ready()
        combo = self.s.find_name("INVFO-MWSKZ", types=("GuiComboBox", "GuiCTextField"))
        got = str(getattr(combo, "Key", "") or getattr(combo, "Text", "") or "").strip().upper()
        if got != code:
            raise SapError(f"Tax code not taken by SAP: wanted '{code}', field shows '{got}'. "
                           f"List: {entries}")
        self.s._show(combo, f"Tax code OK: {code}")

    # ------------------------------------------------------------ PO
    def enter_po(self, po: str) -> None:
        po = str(po or "").strip()
        if not po:
            raise MiroDataError("PO number is empty")
        ref_type = self.s.find_name("RM08M-REFERENZBELEGTYP", types=("GuiComboBox",), required=False)
        if ref_type is not None and str(ref_type.Key) != "1":
            ref_type.Key = "1"                                 # 1 = Purchase Order / Scheduling Agreement
            self.s.wait_ready()
        self.s.set_field("RM08M-EBELN", po)
        self.s.enter()
        self.handle_messages("purchase order")

    # ------------------------------------------------------------ amount / balance
    def enter_amount(self, inv: dict) -> float:
        """Basic data: Amount = invoice total, Enter, return the MIRO balance."""
        total = float(inv.get("total") or 0)
        if total <= 0:
            raise MiroDataError("Invoice total is 0 / empty in the database")
        self.select_tab("HEADER_TOTAL")
        self.s.set_field("INVFO-WRBTR", format_sap_amount(total, self.cfg.sap_decimal_sep))
        self.s.enter()
        self.handle_messages("amount")
        if self.cfg.enter_tax_code and tax_code(inv):
            self.ensure_tax_code(tax_code(inv))
        bal = self.balance()
        if bal is None:
            raise SapError(f"Balance field {self.cfg.balance_field} not found")
        self.note(f"Amount {total:,.2f} entered, balance {bal:,.2f}")
        return bal

    def settle_balance(self, inv: dict, balance: float) -> float:
        """Balance 0 -> nothing to do. Otherwise Details tab: Doc. Type by plant,
        Unpl. Del. Csts = balance, Enter, balance must now be 0. Returns the unplanned amount."""
        c = self.cfg
        if abs(balance) <= c.balance_tolerance:
            self.s._show(None, f"Balance {balance:,.2f} = OK, invoice total matches")
            return 0.0
        if balance < 0:
            raise MiroDataError(f"Balance is {balance:,.2f}: selected GRN lines + tax are MORE than the "
                                f"invoice total - cannot use unplanned delivery costs")
        if balance > c.max_unplanned_cost:
            raise MiroDataError(f"Balance {balance:,.2f} is more than MIRO_MAX_UNPLANNED_COST "
                                f"{c.max_unplanned_cost:,.2f} - not booked as unplanned delivery costs")

        self.s._show(None, f"Balance {balance:,.2f} not zero -> Details tab, Unpl. Del. Csts")
        self.select_tab("HEADER_FI")
        self.set_doc_type(inv)
        self.s.set_field("INVFO-BEZNK", format_sap_amount(balance, c.sap_decimal_sep))
        self.s.enter()
        self.handle_messages("unplanned delivery costs")
        after = self.balance()
        if after is None or abs(after) > c.balance_tolerance:
            raise MiroDataError(f"Balance still {after:,.2f} after Unpl. Del. Csts {balance:,.2f} "
                                f"(tax on delivery costs?) - check manually")
        self.note(f"Unpl. Del. Csts {balance:,.2f} entered, balance now {after:,.2f}")
        return balance

    def set_doc_type(self, inv: dict) -> None:
        """Details tab: Doc. Type. The SAP drop-down shows DESCRIPTIONS ("Invoice - R'gaon R1"), so the entry is
        found by the description stored on the invoice (invoices.doc_type_desc, from the dashboard's document-type
        master for the plant) - or by the key invoices.doc_type (X1 ...). Only when the invoice carries neither
        the old rules apply: MIRO_DOC_TYPE_BY_PLANT, else an entry whose text contains the plant code."""
        plant = business_place(inv)
        combo = self.s.find_name("INVFO-BLART", types=("GuiComboBox",), required=False)
        if combo is None:
            raise SapError("Doc. Type (INVFO-BLART) not found on Details tab")
        entries = []
        try:
            for i in range(combo.Entries.Count):
                e = combo.Entries.ElementAt(i)
                entries.append((str(e.Key).strip(), str(e.Value).strip()))
        except Exception:
            pass
        want_key = str(inv.get("doc_type") or "").strip()
        want_desc = str(inv.get("doc_type_desc") or "").strip()
        if want_key or want_desc:
            key = pick_doc_type(entries, want_key, want_desc)
            if not key:
                raise SapError(f"Doc. Type '{want_desc or want_key}' (key {want_key or '?'}, plant {plant}) is not in "
                               f"the SAP drop-down - check the dashboard document-type master. "
                               f"Available: {[v for _, v in entries][:40]}")
            self.s.set_field("INVFO-BLART", key)
            self.note(f"Doc. Type {key} '{want_desc or want_key}' for plant {plant} (from the invoice)")
            return
        key = self.cfg.doc_type_for_plant(plant)
        if not key and plant:
            hits = [k for k, v in entries if plant in v or plant == k]
            key = hits[0] if len(hits) == 1 else ""
        if not key:
            self.note(f"Doc. Type: invoice has no doc_type and no mapping for plant {plant} - kept '{combo.Value}'. "
                      f"Available: {entries}")
            self.s._show(combo, f"Doc. Type kept '{combo.Value}' (no mapping for plant {plant})")
            return
        if entries and key not in [k for k, _ in entries]:
            raise SapError(f"Doc. Type '{key}' (plant {plant}) is not in the list: {entries}")
        self.s.set_field("INVFO-BLART", key)
        self.note(f"Doc. Type {key} for plant {plant}")

    def fill_details(self, inv: dict) -> None:
        """Details tab, always: Doc. Type of the invoice's plant (before the balance is settled / saved).
        Then back to Basic data."""
        self.select_tab("HEADER_FI")
        self.set_doc_type(inv)
        self.select_tab("HEADER_TOTAL")

    # ------------------------------------------------------------ simulate
    def simulate(self) -> str:
        """Press Simulate, read the result, close the simulation window. Raises on SAP errors."""
        btn = self.s.find(self.cfg.simulate_button, required=False)
        if btn is None:
            raise SapError(f"Simulate button {self.cfg.simulate_button} not found")
        self.s._show(btn, "Press Simulate")
        btn.Press()
        self.s.wait_ready()
        info = ""
        for _ in range(4):
            if not self.s.popup_open():
                break
            ptxt = self.s.popup_text()
            info = info or ptxt
            self.note(f"[simulate] window: {ptxt}")
            if self.cfg.step_delay > 0:
                import time as _t
                _t.sleep(self.cfg.step_delay * 2)              # let the person look at it
            self.s.vkey(12, wnd=1)                             # close simulation (Cancel/Back)
        if self.s.popup_open():
            raise SapError("Simulation window did not close")
        mt, txt = self.s.status()
        if mt in ("E", "A"):
            raise SapError(f"[simulate] SAP error: {txt}")
        if txt:
            self.note(f"[simulate] {mt}: {txt}")
        return info or txt

    # ------------------------------------------------------------ item table
    def _table(self):
        return self.s.find_name(TABLE, types=("GuiTableControl",))

    def _columns(self, table) -> dict:
        """field name / title -> column index, read from the cells of the first row."""
        cols = {}
        for ci in range(table.Columns.Count):
            try:
                name = str(table.GetCell(0, ci).Name or "")
            except Exception:
                name = ""
            try:
                title = str(table.Columns.ElementAt(ci).Title or "")
            except Exception:
                title = ""
            if name:
                cols.setdefault(name.upper(), ci)
            if title:
                cols.setdefault("TITLE:" + title.strip().upper(), ci)
        return cols

    def _col(self, cols: dict, field: str, *titles: str, required: bool = True):
        if field.upper() in cols:
            return cols[field.upper()]
        for t in titles:
            for k, v in cols.items():
                if k.startswith("TITLE:") and t.upper() in k:
                    return v
        if required:
            raise SapError(f"Column {field} ({'/'.join(titles)}) not visible in the MIRO item table. "
                           f"Choose an item-list layout that shows it (layout 'All information').")
        return None

    def _scroll_to(self, position: int):
        table = self._table()
        sb = table.VerticalScrollbar
        pos = max(0, min(position, int(sb.Maximum)))
        if int(sb.Position) != pos:
            sb.Position = pos
            self.s.wait_ready()
            table = self._table()                              # control is rebuilt after scrolling
        return table, pos

    def _cell(self, table, row: int, col) -> str:
        if col is None:
            return ""
        try:
            return str(table.GetCell(row, col).Text or "").strip()
        except Exception:
            try:                                               # column scrolled away horizontally
                table.HorizontalScrollbar.Position = col
                self.s.wait_ready()
                return str(self._table().GetCell(row, col).Text or "").strip()
            except Exception:
                return ""

    def read_rows(self) -> list[MiroRow]:
        table, _ = self._scroll_to(0)
        self.s._show(table, "Reading MIRO item table (all rows, Reference Document column)")
        cols = self._columns(table)
        c = self.cfg
        i_ref = self._col(cols, c.col_ref_doc, "REFERENCE DOC", "REF. DOC", "REF.DOC")
        i_po = self._col(cols, c.col_po, "PURCHASING DOC", "PURCHASE ORDER", required=False)
        i_item = self._col(cols, c.col_po_item, "ITEM", required=False)
        i_amt = self._col(cols, c.col_amount, "AMOUNT")
        i_qty = self._col(cols, c.col_quantity, "QUANTITY", required=False)
        i_txt = self._col(cols, c.col_item_text, "SHORT TEXT", required=False)
        i_hsn = self._col(cols, c.col_hsn, "HSN", required=False)

        total = int(table.RowCount)
        visible = max(1, int(table.VisibleRowCount))
        rows: dict[int, MiroRow] = {}
        start = 0
        while start < total:
            table, pos = self._scroll_to(start)
            for r in range(visible):
                idx = pos + r
                if idx >= total or idx in rows:
                    continue
                row = MiroRow(
                    index=idx,
                    po=self._cell(table, r, i_po),
                    po_item=self._cell(table, r, i_item),
                    ref_doc=self._cell(table, r, i_ref),
                    amount=parse_sap_number(self._cell(table, r, i_amt), c.sap_decimal_sep),
                    quantity=parse_sap_number(self._cell(table, r, i_qty), c.sap_decimal_sep),
                    text=self._cell(table, r, i_txt),
                    hsn=self._cell(table, r, i_hsn))
                if row.po or row.ref_doc or row.amount:
                    rows[idx] = row
            start = pos + visible
        self._scroll_to(0)
        out = [rows[k] for k in sorted(rows)]
        self.note(f"MIRO item table: {len(out)} rows, GRNs: "
                  f"{sorted({r.ref_doc for r in out if r.ref_doc})}")
        return out

    def select_rows(self, indexes: list[int]) -> None:
        if not self.s.press_name("DESELECT_ALL", required=False):
            raise SapError("'Deselect all' button not found in MIRO item list")
        for idx in sorted(indexes):
            table, _ = self._scroll_to(idx)
            self.s._show(None, f"Tick item row {idx + 1}")
            table.GetAbsoluteRow(idx).Selected = True
        self._scroll_to(0)
        self.s.enter()
        self.handle_messages("line selection")
        # verify what SAP really has selected
        got = self.selected_indexes()
        if sorted(got) != sorted(indexes):
            raise SapError(f"Row selection mismatch: wanted {sorted(i + 1 for i in indexes)}, "
                           f"SAP has {sorted(i + 1 for i in got)}")

    def refresh_items(self, plan_rows: list) -> list[MiroRow]:
        """Press Refresh in the item list: SAP drops every NOT selected row. Then read the table
        again and make sure exactly the GRN rows are left (same count, GRN, amount) and selected.
        Returns the rows with their NEW row numbers."""
        if not self.s.press_name("REFRESH", required=False):
            raise SapError("Refresh button (btnREFRESH) not found in MIRO item list")
        self.handle_messages("refresh items")
        rows = self.read_rows()
        want_grn = {r.ref_doc.strip().lstrip("0") for r in plan_rows}
        got_grn = {r.ref_doc.strip().lstrip("0") for r in rows}
        want_amt = round(sum(r.amount for r in plan_rows), 2)
        got_amt = round(sum(r.amount for r in rows), 2)
        if len(rows) != len(plan_rows) or got_grn != want_grn or abs(got_amt - want_amt) > 0.01:
            raise SapError(f"After Refresh the item list is not as expected: {len(rows)} rows, "
                           f"GRN {sorted(got_grn)}, {got_amt:,.2f} (expected {len(plan_rows)} rows, "
                           f"GRN {sorted(want_grn)}, {want_amt:,.2f})")
        sel = self.selected_indexes()
        if sel != [r.index for r in rows]:
            self.note(f"After Refresh selected rows {[i + 1 for i in sel]} - selecting all again")
            self.select_rows([r.index for r in rows])
        self.note(f"Refresh: {len(rows)} GRN row(s) left in the item list")
        return rows

    def update_hsn(self, changes: dict) -> None:
        """Type the DB HSN into the HSN/SAC cell of the given rows, Enter, then read back."""
        if not changes:
            return
        table, _ = self._scroll_to(0)
        col = self._col(self._columns(table), self.cfg.col_hsn, "HSN")
        for idx in sorted(changes):
            table, pos = self._scroll_to(idx)
            r = idx - pos
            try:
                cell = table.GetCell(r, col)
            except Exception:
                table.HorizontalScrollbar.Position = col            # column far to the right
                self.s.wait_ready()
                table = self._table()
                cell = table.GetCell(r, col)
            self.s._show(cell, f"Row {idx + 1}: HSN '{str(cell.Text).strip()}' -> {changes[idx]}")
            try:
                cell.Text = changes[idx]
            except Exception as e:
                raise SapError(f"HSN cell of row {idx + 1} cannot be changed (read-only?): {e}")
        try:
            table.HorizontalScrollbar.Position = 0
        except Exception:
            pass
        self._scroll_to(0)
        self.s.enter()
        self.handle_messages("HSN change")
        got = {r.index: r.hsn for r in self.read_rows()}
        wrong = {i: got.get(i) for i, v in changes.items()
                 if re.sub(r"\D", "", str(got.get(i) or "")) != v}
        if wrong:
            raise SapError(f"HSN not taken by SAP for rows {[i + 1 for i in wrong]}: {wrong}")
        self.note(f"HSN changed: { {i + 1: v for i, v in changes.items()} }")

    def selected_indexes(self) -> list[int]:
        table, _ = self._scroll_to(0)
        total, visible = int(table.RowCount), max(1, int(table.VisibleRowCount))
        out, start = set(), 0
        while start < total:
            table, pos = self._scroll_to(start)
            for r in range(visible):
                idx = pos + r
                if idx < total:
                    try:
                        if table.GetAbsoluteRow(idx).Selected:
                            out.add(idx)
                    except Exception:
                        pass
            start = pos + visible
        self._scroll_to(0)
        return sorted(out)

    # ------------------------------------------------------------ balance / save
    def balance(self) -> float | None:
        el = self.s.find_name(self.cfg.balance_field, types=("GuiTextField", "GuiCTextField"),
                              required=False)
        if el is None:
            return None
        return parse_sap_number(str(el.Text or ""), self.cfg.sap_decimal_sep)

    def post(self) -> tuple[str, str]:
        """Ctrl+S. Returns (document_number, status_text). Raises SapError if SAP refused."""
        self.s.vkey(11)
        popup_doc = ""
        for _ in range(4):
            if not self.s.popup_open():
                break
            ptxt = self.s.popup_text()
            self.note(f"[post] popup: {ptxt}")
            if is_duplicate_message(ptxt) and self.cfg.stop_on_duplicate_warning:
                self.s.vkey(12, wnd=1)                          # cancel the popup
                raise MiroDataError(f"SAP duplicate-invoice warning at posting: {ptxt}")
            # the document number can be shown in this popup - read it before Enter closes it
            popup_doc = extract_document_number(ptxt, strict=True) or popup_doc
            self.s.enter(1)
        mt, txt = self.s.status()
        doc = extract_document_number(txt) or popup_doc
        if mt in ("S", "I", "") and doc and not (mt in ("E", "A")):
            if popup_doc:
                self.note(f"[post] document number {doc}" + (" (from popup)" if not extract_document_number(txt) else ""))
            return doc, txt or f"Document {doc} posted"
        if mt in ("E", "A"):
            raise SapError(f"[post] SAP refused posting: {txt}")
        return "", txt

    def park(self) -> tuple[str, str]:
        self.s.select_menu(*[p.strip() for p in self.cfg.park_menu.split(">")])
        popup_doc = ""
        for _ in range(3):
            if not self.s.popup_open():
                break
            ptxt = self.s.popup_text()
            self.note(f"[park] popup: {ptxt}")
            popup_doc = extract_document_number(ptxt, strict=True) or popup_doc
            self.s.enter(1)
        mt, txt = self.s.status()
        doc = extract_document_number(txt) or popup_doc
        if mt in ("S", "I", "") and doc:
            return doc, txt or f"Document {doc} parked"
        if mt in ("E", "A"):
            raise SapError(f"[park] SAP refused parking: {txt}")
        return "", txt

    def leave(self) -> None:
        """Exit MIRO without saving (answers 'Yes, data will be lost')."""
        try:
            self.s.okcode("/n")
            for _ in range(4):
                if not self.s.popup_open():
                    break
                for btn in ("wnd[1]/usr/btnSPOP-OPTION1", "wnd[1]/tbar[0]/btn[0]"):
                    if self.s.exists(btn):
                        self.s.find(btn).Press()
                        self.s.wait_ready()
                        break
                else:
                    self.s.vkey(12, wnd=1)
                time.sleep(self.cfg.poll_interval)
        except Exception:
            pass
