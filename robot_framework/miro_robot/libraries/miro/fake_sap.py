"""
SAP GUI Scripting SIMULATOR - for testing the robot without SAP (MIRO_SAP_BACKEND=fake).

Implements only the part of the scripting API the robot uses, but behaves like the real
thing where it matters:
  * login screen -> multiple-logon popup -> Easy Access
  * /nMIRO -> company-code popup (first time only)
  * entering a PO loads its open GR lines into the item table, ALL rows pre-selected
  * the table shows only VisibleRowCount rows; GetAbsoluteRow() works only for visible rows,
    so the robot must scroll exactly like on a real screen
  * balance is recalculated only on Enter (round trip), duplicate-invoice warning, posting /
    parking returns a document number, leaving with data asks "data will be lost?"
"""
from __future__ import annotations

import json
from pathlib import Path

PNG_1PX = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                        "0000000d4944415478da63f8ffff3f0005fe02fea7d6a4350000000049454e44ae426082")


class _Coll:
    def __init__(self, items):
        self._items = list(items)

    @property
    def Count(self):
        return len(self._items)

    def ElementAt(self, i):
        return self._items[i]

    def __call__(self, i):
        return self._items[i]


class _El:
    def __init__(self, sess, name, type_, text="", **kw):
        self.sess, self.Name, self.Type, self._text = sess, name, type_, text
        self.Selected = kw.get("selected", False)
        self.Key = kw.get("key", "")
        self.CaretPosition = 0
        self.on_press = kw.get("on_press")

    @property
    def Text(self):
        return self._text

    @Text.setter
    def Text(self, v):
        if self.Type in ("GuiLabel",):
            raise AttributeError("read-only")
        self._text = str(v)

    def SetFocus(self):
        pass

    def Press(self):
        if self.on_press:
            self.on_press()

    def Select(self):
        if self.Type == "GuiRadioButton":
            self.Selected = True
            self.sess._radio = self.Name
        elif self.on_press:
            self.on_press()


class _Scroll:
    def __init__(self, table):
        self.t = table
        self._pos = 0

    @property
    def Maximum(self):
        return max(0, len(self.t.rows) - self.t.VisibleRowCount)

    @property
    def Position(self):
        return self._pos

    @Position.setter
    def Position(self, v):
        self._pos = max(0, min(int(v), self.Maximum))


class _Row:
    def __init__(self, data):
        self.data = data

    @property
    def Selected(self):
        return self.data["_sel"]

    @Selected.setter
    def Selected(self, v):
        self.data["_sel"] = bool(v)


class _Column:
    def __init__(self, title):
        self.Title = title


class _Table:
    Type = "GuiTableControl"
    Name = "SAPLMR1MTC_MR1M"
    # (field name, title, key in row dict) - Reference Document deliberately far right, like SAP
    COLS = [("DRSEG-RBLGP", "Item", "pos"), ("DRSEG-WRBTR", "Amount", "amount"),
            ("DRSEG-MENGE", "Quantity", "qty"), ("DRSEG-EBELN", "Purchase Order", "po"),
            ("DRSEG-EBELP", "Item", "item"), ("DRSEG-TXZ01", "Short Text", "text"),
            ("DRSEG-MWSKZ", "Tax Code", "tax"), ("DRSEG-LFBNR", "Reference Document", "ref"),
            ("DRSEG-LFPOS", "Ref. Doc. Item", "refpos")]

    def __init__(self, sess, visible=5):
        self.sess = sess
        self.rows: list[dict] = []
        self.VisibleRowCount = visible
        self.VerticalScrollbar = _Scroll(self)
        self.Columns = _Coll([_Column(t) for _, t, _ in self.COLS])

    @property
    def RowCount(self):
        return max(len(self.rows), self.VisibleRowCount)       # SAP pads with empty rows

    def GetCell(self, r, c):
        if not 0 <= r < self.VisibleRowCount:
            raise IndexError("row not visible")
        idx = self.VerticalScrollbar.Position + r
        fname, _, key = self.COLS[c]
        val = ""
        if idx < len(self.rows):
            v = self.rows[idx].get(key, "")
            if key == "amount":
                v = f"{float(v):,.2f}"
            elif key == "qty":
                v = f"{float(v):,.3f}"
            elif key == "pos":
                v = str(idx + 1)
            val = str(v)
        return _El(self.sess, fname, "GuiTextField", val)

    def GetAbsoluteRow(self, idx):
        pos = self.VerticalScrollbar.Position
        if not pos <= idx < pos + self.VisibleRowCount:
            raise Exception(f"GetAbsoluteRow({idx}): row not on screen (scroll position {pos})")
        if idx >= len(self.rows):
            return _Row({"_sel": False})
        return _Row(self.rows[idx])


class _Menu:
    def __init__(self, text, children=(), action=None):
        self.Text, self.Children, self.action = text, _Coll(children), action

    def Select(self):
        if self.action:
            self.action()


class _Info:
    def __init__(self, sess):
        self.s = sess

    User = property(lambda self: self.s.user if self.s.screen != "LOGIN" else "")
    Program = property(lambda self: {"LOGIN": "SAPMSYST", "EASY": "SAPLSMTR_NAVIGATION",
                                     "MIRO": "SAPLMR1M"}[self.s.screen])
    Transaction = property(lambda self: {"LOGIN": "S000", "EASY": "SESSION_MANAGER",
                                         "MIRO": "MIRO"}[self.s.screen])
    Client = property(lambda self: self.s.app.sc.get("client", "150"))
    ScreenNumber = property(lambda self: "6005" if not self.s.collapsed else "6006")


class _Window:
    def __init__(self, sess, n):
        self.s, self.n = sess, n

    @property
    def Text(self):
        if self.n == 1:
            return self.s.popup["title"] if self.s.popup else ""
        if self.s.screen == "MIRO":
            return f"Enter Incoming Invoice: Company Code {self.s.bukrs}"
        return "SAP Easy Access" if self.s.screen == "EASY" else "SAP"

    def SendVKey(self, k):
        self.s._vkey(self.n, int(k))

    def Maximize(self):
        pass

    def HardCopy(self, path, fmt="PNG"):
        Path(path).write_bytes(PNG_1PX)
        return path


class _Container:
    def __init__(self, getter):
        self.getter = getter

    def FindByName(self, name, type_):
        el = self.getter().get(name)
        if el is None or el.Type != type_:
            raise Exception(f"{name}/{type_} not found")
        return el

    @property
    def Children(self):
        return _Coll(self.getter().values())


class FakeSession:
    def __init__(self, app):
        self.app = app
        sc = app.sc
        self.user = sc.get("user", "ROBOT")
        self.screen = "EASY" if sc.get("logged_on") else "LOGIN"
        self.Busy = False
        self.Info = _Info(self)
        self.sbar = _El(self, "sbar", "GuiStatusbar")
        self.sbar.MessageType = ""
        self.okcd = _El(self, "okcd", "GuiOkCodeField")
        self.popup = None
        self.bukrs = ""
        self.miro_seen = False
        self.table = _Table(self, sc.get("visible_rows", 5))
        self.login = {k: _El(self, k, t) for k, t in (("RSYST-MANDT", "GuiTextField"),
                                                       ("RSYST-BNAME", "GuiTextField"),
                                                       ("RSYST-BCODE", "GuiPasswordField"),
                                                       ("RSYST-LANGU", "GuiTextField"))}
        self._new_miro()

    # ---------------------------------------------------------- MIRO state
    def _new_miro(self):
        self._dup_warned = False
        self.collapsed = False
        self.balance_value = 0.0
        self.table.rows = []
        self.table.VerticalScrollbar.Position = 0
        self.f = {
            "INVFO-BLDAT": _El(self, "INVFO-BLDAT", "GuiCTextField"),
            "INVFO-XBLNR": _El(self, "INVFO-XBLNR", "GuiTextField"),
            "INVFO-WRBTR": _El(self, "INVFO-WRBTR", "GuiTextField"),
            "INVFO-WAERS": _El(self, "INVFO-WAERS", "GuiCTextField", "INR"),
            "INVFO-MWSKZ": _El(self, "INVFO-MWSKZ", "GuiComboBox"),
            "INVFO-XMWST": _El(self, "INVFO-XMWST", "GuiCheckBox"),
            "RM08M-REFERENZBELEGTYP": _El(self, "RM08M-REFERENZBELEGTYP", "GuiComboBox", key="1"),
            "RM08M-EBELN": _El(self, "RM08M-EBELN", "GuiCTextField"),
            "RM08M-DIFFERENZ": _El(self, "RM08M-DIFFERENZ", "GuiTextField", "0.00"),
            "RM08M-HEADER_COLLAPSE": _El(self, "RM08M-HEADER_COLLAPSE", "GuiButton",
                                         on_press=self._collapse),
            "DESELECT_ALL": _El(self, "DESELECT_ALL", "GuiButton", on_press=self._deselect_all),
            "SAPLMR1MTC_MR1M": self.table,
        }
        self.loaded_po = ""

    def _collapse(self):
        self.collapsed = not self.collapsed

    def _deselect_all(self):
        for r in self.table.rows:
            r["_sel"] = False

    def _status(self, t, msg):
        self.sbar.MessageType, self.sbar._text = t, msg

    def _dirty(self):
        return self.screen == "MIRO" and (self.f["INVFO-XBLNR"].Text or self.loaded_po)

    # ---------------------------------------------------------- scripting API
    def FindById(self, eid):
        eid = eid.replace("/app/con[0]/ses[0]/", "")
        if eid == "wnd[0]":
            return _Window(self, 0)
        if eid == "wnd[0]/sbar":
            return self.sbar
        if eid == "wnd[0]/tbar[0]/okcd":
            return self.okcd
        if eid == "wnd[0]/usr":
            return _Container(lambda: self.f if self.screen == "MIRO" else
                              (self.login if self.screen == "LOGIN" else {}))
        if eid == "wnd[0]/mbar":
            return self._menubar()
        if self.screen == "LOGIN" and eid.startswith("wnd[0]/usr/"):
            key = eid.split("/")[-1][3:]
            if key in self.login:
                return self.login[key]
        if eid.startswith("wnd[1]"):
            if not self.popup:
                raise Exception("wnd[1] not open")
            if eid == "wnd[1]":
                return _Window(self, 1)
            if eid == "wnd[1]/usr":
                return _Container(lambda: self.popup["els"])
            if eid == "wnd[1]/tbar[0]/btn[0]":
                return _El(self, "btn0", "GuiButton", on_press=lambda: self._vkey(1, 0))
            tail = eid.split("/")[-1]
            for k, el in self.popup["els"].items():
                if tail.endswith(k):
                    return el
        raise Exception(f"The control could not be found by id: {eid}")

    def _menubar(self):
        if self.screen != "MIRO":
            return _Menu("bar", [])
        park = _Menu("Park", action=lambda: self._save("PARK"))
        switch = _Menu("Switch Company Code", action=self._switch_bukrs)
        return _Menu("bar", [_Menu("Invoice Document", [park]), _Menu("Edit", [switch])])

    def _switch_bukrs(self):
        self._open_popup("Enter Company Code", {
            "BKPF-BUKRS": _El(self, "BKPF-BUKRS", "GuiCTextField", self.bukrs)}, kind="BUKRS")

    def _open_popup(self, title, els, kind):
        self.popup = {"title": title, "els": els, "kind": kind}

    # ---------------------------------------------------------- keys
    def _vkey(self, wnd, key):
        if wnd == 1:
            return self._popup_key(key)
        if self.popup:
            raise Exception("modal popup is open")
        if key == 0:
            code = self.okcd.Text.strip().upper()
            self.okcd._text = ""
            if code:
                return self._okcode(code)
            if self.screen == "LOGIN":
                return self._do_login()
            if self.screen == "MIRO":
                return self._miro_enter()
        elif key == 11 and self.screen == "MIRO":
            return self._save("POST")
        elif key in (3, 12, 15):
            return self._okcode("/N")

    def _popup_key(self, key):
        p = self.popup
        kind = p["kind"]
        if key == 12:
            self.popup = None
            return
        if kind == "MULTI":
            self.popup = None
            if getattr(self, "_radio", "MULTI_LOGON_OPT3") == "MULTI_LOGON_OPT3":
                self.screen = "LOGIN"
            else:
                self.screen = "EASY"
                self._status("S", "")
        elif kind == "BUKRS":
            self.bukrs = p["els"]["BKPF-BUKRS"].Text.strip()
            self.popup = None
            self._status("", "")
        elif kind == "LOSE":
            self.popup = None
            self.screen = "EASY"
            self._new_miro()
        else:
            self.popup = None

    def _okcode(self, code):
        if code in ("/N", "/NEX"):
            if self._dirty():
                yes = _El(self, "SPOP-OPTION1", "GuiButton", "Yes",
                          on_press=lambda: self._popup_key(0))
                self._open_popup("Exit invoice entry", {"SPOP-OPTION1": yes,
                                 "TXT": _El(self, "TXT", "GuiTextField", "Data will be lost")}, "LOSE")
                return
            self.screen = "EASY"
            self._new_miro()
        elif code in ("/NMIRO", "MIRO"):
            if self._dirty():
                return self._okcode("/N")
            self.screen = "MIRO"
            self._new_miro()
            self.app.trace.append("MIRO")
            if not self.miro_seen:
                self.miro_seen = True
                self._switch_bukrs()
        self._status("", "")

    def _do_login(self):
        sc = self.app.sc
        if self.login["RSYST-BNAME"].Text != sc.get("user") or \
           self.login["RSYST-BCODE"].Text != sc.get("password"):
            return self._status("E", "Name or password is incorrect (repeat logon)")
        self.user = sc["user"]
        opts = {f"MULTI_LOGON_OPT{i}": _El(self, f"MULTI_LOGON_OPT{i}", "GuiRadioButton")
                for i in (1, 2, 3)}
        opts["MULTI_LOGON_OPT3"].Selected = True       # SAP default = "terminate this logon"
        self.app.trace.append("LOGIN")
        self._open_popup("License Information for Multiple Logons", opts, "MULTI")

    # ---------------------------------------------------------- MIRO logic
    def _miro_enter(self):
        sc = self.app.sc
        self._status("", "")
        ref = self.f["INVFO-XBLNR"].Text.strip()
        if ref and ref in sc.get("duplicates", []) and not getattr(self, "_dup_warned", False):
            self._dup_warned = True
            return self._status("W", "Check if invoice already entered under document "
                                     "number 5105600001 2026")
        po = self.f["RM08M-EBELN"].Text.strip()
        if po and po != self.loaded_po:
            if po not in sc.get("pos", {}):
                return self._status("E", f"Document {po} does not exist")
            self.table.rows = [dict(r, po=po, _sel=True) for r in sc["pos"][po]]
            self.table.VerticalScrollbar.Position = 0
            self.loaded_po = po
            self.app.trace.append(f"PO {po}: {len(self.table.rows)} rows")
        self._recalc()

    def _recalc(self):
        rates = self.app.sc.get("tax_rates", {})
        code = (self.f["INVFO-MWSKZ"].Key or "").strip()
        rate = rates.get(code, 0.0) if self.f["INVFO-XMWST"].Selected else 0.0
        gross = float((self.f["INVFO-WRBTR"].Text or "0").replace(",", "") or 0)
        net = sum(float(r["amount"]) for r in self.table.rows if r["_sel"])
        self.balance_value = round(gross - round(net * (1 + rate), 2), 2)
        self.f["RM08M-DIFFERENZ"]._text = f"{abs(self.balance_value):,.2f}" + \
            ("-" if self.balance_value < 0 else "")

    def _save(self, how):
        self._recalc()
        if abs(self.balance_value) > 0.0:
            return self._status("E", "Balance not zero: "
                                     f"{self.balance_value:,.2f} debits: ... credits: ...")
        self.app.doc_counter += 1
        doc = str(self.app.doc_counter)
        sel = [(r["ref"], r["item"], r["amount"]) for r in self.table.rows if r["_sel"]]
        self.app.saved.append({"how": how, "doc": doc, "ref": self.f["INVFO-XBLNR"].Text,
                               "po": self.loaded_po, "rows": sel})
        self._new_miro()
        if self.app.sc.get("post_no_doc"):                   # simulate a lost/odd confirmation
            return self._status("S", "Processing complete")
        if how == "POST":
            self._status("S", f"Document no. {doc} created")
        else:
            self._status("S", f"Document {doc} was parked")


class FakeConnection:
    def __init__(self, app):
        self.Children = _Coll([FakeSession(app)])


class FakeSapApplication:
    def __init__(self, scenario: dict):
        self.sc = scenario
        self.trace: list[str] = []
        self.saved: list[dict] = []
        self.doc_counter = 5105600100
        self._conns = [FakeConnection(self)] if scenario.get("logged_on") else []

    _instances: dict = {}

    @classmethod
    def from_scenario(cls, path: str):
        if not path:
            raise RuntimeError("MIRO_FAKE_SCENARIO not set")
        if path not in cls._instances:
            cls._instances[path] = cls(json.loads(Path(path).read_text(encoding="utf-8")))
        return cls._instances[path]

    @property
    def Children(self):
        return _Coll(self._conns)

    def OpenConnection(self, desc, sync=True):
        if desc != self.sc.get("connection", "KTFL PRD"):
            raise Exception(f"SAP Logon entry '{desc}' not found")
        conn = FakeConnection(self)
        self._conns.append(conn)
        return conn
