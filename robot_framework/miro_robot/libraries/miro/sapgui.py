"""
Thin, defensive wrapper around SAP GUI Scripting (the same API your .vbs recording uses).

Why SAP GUI Scripting and not keyboard/Tab automation (IBM RPA flow):
  * fields are addressed by their technical name (INVFO-BLDAT, RM08M-EBELN ...), not by Tab counts
  * the MIRO item table can be read cell by cell, so lines are chosen by GRN, not by position
  * status-bar messages (errors / warnings / document number) can be read reliably

Prerequisites on the robot PC / SAP server:
  * SAP GUI option  "Enable scripting"  ON, and "Notify when a script attaches" OFF
  * server parameter  sapgui/user_scripting = TRUE  (Basis) for the robot user
  * pywin32 installed (pip install pywin32)
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from .config import Config


class SapError(Exception):
    """SAP showed an error message or did not reach the expected screen."""
    ROBOT_SUPPRESS_NAME = True


FIELD_TYPES = ("GuiCTextField", "GuiTextField", "GuiComboBox", "GuiPasswordField", "GuiCheckBox",
               "GuiRadioButton", "GuiButton", "GuiTableControl", "GuiLabel")


def get_scripting_application(cfg: Config):
    """Return the SAP GUI scripting engine (starts SAP Logon if needed)."""
    if cfg.sap_backend == "fake":
        from .fake_sap import FakeSapApplication
        return FakeSapApplication.from_scenario(cfg.fake_scenario)

    import pythoncom  # noqa: F401  (pywin32)
    import win32com.client

    def _engine():
        return win32com.client.GetObject("SAPGUI").GetScriptingEngine

    try:
        return _engine()
    except Exception:
        if not Path(cfg.sap_logon_exe).exists():
            raise SapError(f"SAP Logon not running and not found at {cfg.sap_logon_exe}")
        subprocess.Popen([cfg.sap_logon_exe])
        deadline = time.time() + 60
        while time.time() < deadline:
            time.sleep(2)
            try:
                return _engine()
            except Exception:
                continue
        raise SapError("SAP Logon started but scripting engine not available - is scripting enabled?")


class SapSession:
    def __init__(self, session, cfg: Config):
        self.s = session
        self.cfg = cfg

    # ------------------------------------------------------------ element access
    def find(self, element_id: str, required: bool = True):
        try:
            return self.s.FindById(element_id)
        except Exception:
            if required:
                raise SapError(f"SAP element not found: {element_id}")
            return None

    def exists(self, element_id: str) -> bool:
        return self.find(element_id, required=False) is not None

    def find_name(self, name: str, types=FIELD_TYPES, container: str = "wnd[0]/usr", required: bool = True):
        """Find a control by technical name anywhere inside a container, independent of the
        subscreen number (MIRO switches 6005 <-> 6006 when the header is collapsed)."""
        cont = self.find(container, required=required)
        if cont is not None:
            for t in types:
                try:
                    el = cont.FindByName(name, t)
                    if el is not None:
                        return el
                except Exception:
                    continue
        if required:
            raise SapError(f"Field '{name}' not found on screen {self.screen_info()}")
        return None

    def set_field(self, name: str, value: str, container: str = "wnd[0]/usr") -> None:
        el = self.find_name(name, container=container)
        t = getattr(el, "Type", "")
        if t == "GuiComboBox":
            el.Key = str(value)
        elif t == "GuiCheckBox":
            el.Selected = bool(value)
        else:
            el.Text = str(value)
            try:
                el.SetFocus()
                el.CaretPosition = len(str(value))
            except Exception:
                pass

    def get_text(self, name: str, container: str = "wnd[0]/usr", required: bool = True) -> str:
        el = self.find_name(name, container=container, required=required)
        return "" if el is None else str(el.Text or "")

    def press_name(self, name: str, container: str = "wnd[0]/usr", required: bool = True) -> bool:
        el = self.find_name(name, types=("GuiButton",), container=container, required=required)
        if el is None:
            return False
        el.Press()
        self.wait_ready()
        return True

    # ------------------------------------------------------------ keys / transactions
    def enter(self, wnd: int = 0) -> None:
        self.find(f"wnd[{wnd}]").SendVKey(0)
        self.wait_ready()

    def vkey(self, key: int, wnd: int = 0) -> None:
        self.find(f"wnd[{wnd}]").SendVKey(key)
        self.wait_ready()

    def okcode(self, code: str) -> None:
        self.find("wnd[0]/tbar[0]/okcd").Text = code
        self.enter()

    def wait_ready(self) -> None:
        deadline = time.time() + self.cfg.wait_timeout
        while time.time() < deadline:
            try:
                if not self.s.Busy:
                    return
            except Exception:
                return
            time.sleep(self.cfg.poll_interval)
        raise SapError(f"SAP still busy after {self.cfg.wait_timeout}s")

    # ------------------------------------------------------------ status bar / popups
    def status(self) -> tuple[str, str]:
        sb = self.find("wnd[0]/sbar", required=False)
        if sb is None:
            return "", ""
        return str(sb.MessageType or ""), str(sb.Text or "")

    def popup_open(self) -> bool:
        return self.exists("wnd[1]")

    def popup_text(self) -> str:
        """All visible text of the popup (title + labels) for logging."""
        w = self.find("wnd[1]", required=False)
        if w is None:
            return ""
        parts = [str(getattr(w, "Text", "") or "")]
        usr = self.find("wnd[1]/usr", required=False)
        if usr is not None:
            try:
                for i in range(usr.Children.Count):
                    t = str(getattr(usr.Children.ElementAt(i), "Text", "") or "").strip()
                    if t:
                        parts.append(t)
            except Exception:
                pass
        return " | ".join(p for p in parts if p)

    def screen_info(self) -> str:
        try:
            i = self.s.Info
            return f"{i.Transaction}/{i.Program}/{i.ScreenNumber}"
        except Exception:
            return "?"

    def transaction(self) -> str:
        try:
            return str(self.s.Info.Transaction or "")
        except Exception:
            return ""

    def screenshot(self, path: Path) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.find("wnd[0]").HardCopy(str(path), "PNG")
            return str(path)
        except Exception:
            return ""

    def select_menu(self, *labels: str) -> None:
        """Walk the menu bar by visible text, e.g. select_menu('Invoice document', 'Park')."""
        node = self.find("wnd[0]/mbar")
        for label in labels:
            want = label.lower().replace("&", "")
            hit = None
            for i in range(node.Children.Count):
                child = node.Children.ElementAt(i)
                if str(child.Text).lower().replace("&", "").strip() == want:
                    hit = child
                    break
            if hit is None:
                raise SapError(f"Menu entry '{label}' not found")
            node = hit
        node.Select()
        self.wait_ready()
