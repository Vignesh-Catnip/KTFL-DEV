import os
import time
from dotenv import load_dotenv
from robot.api import logger

load_dotenv()

class SAPMIROLibrary:
    ROBOT_LIBRARY_SCOPE = "SUITE"

    def __init__(self):
        self.session = None

    def _sap_application(self):
        import win32com.client
        gui = win32com.client.GetObject("SAPGUI")
        return gui.GetScriptingEngine

    def login_or_attach_to_sap(self):
        if os.name != "nt":
            raise RuntimeError("SAP GUI Scripting requires Windows.")
        try:
            app = self._sap_application()
            # Prefer an existing active SAP session.
            for ci in range(app.Children.Count):
                conn = app.Children(ci)
                if conn.Children.Count:
                    self.session = conn.Children(0)
                    if self._is_logged_in():
                        logger.info("Attached to an already logged-in SAP session.")
                        return True

            connection_name = os.getenv("SAP_CONNECTION_NAME", "").strip()
            if not connection_name or connection_name == "CHANGE_ME":
                raise RuntimeError(
                    "Set SAP_CONNECTION_NAME to the exact SAP Logon connection name in .env, "
                    "or log in manually first."
                )
            connection = app.OpenConnection(connection_name, True)
            time.sleep(2)
            if connection.Children.Count == 0:
                raise RuntimeError("SAP connection opened but no session was created.")
            self.session = connection.Children(0)

            # Standard SAP GUI login controls. Custom SSO/MFA screens may differ.
            try:
                user_id = "wnd[0]/usr/txtRSYST-BNAME"
                pass_id = "wnd[0]/usr/pwdRSYST-BCODE"
                client_id = "wnd[0]/usr/txtRSYST-MANDT"
                lang_id = "wnd[0]/usr/txtRSYST-LANGU"
                username = os.getenv("SAP_USERNAME", "")
                password = os.getenv("SAP_PASSWORD", "")
                if username and password and self._control_exists(user_id) and self._control_exists(pass_id):
                    if os.getenv("SAP_CLIENT", "").strip() and self._control_exists(client_id):
                        self.session.findById(client_id).text = os.getenv("SAP_CLIENT").strip()
                    if os.getenv("SAP_LANGUAGE", "").strip() and self._control_exists(lang_id):
                        self.session.findById(lang_id).text = os.getenv("SAP_LANGUAGE").strip()
                    self.session.findById(user_id).text = username
                    self.session.findById(pass_id).text = password
                    self.session.findById("wnd[0]").sendVKey(0)
                    time.sleep(3)
            except Exception as exc:
                logger.warn("Automatic login fields were not available; login may require manual action: " + str(exc))

            if not self._is_logged_in():
                raise RuntimeError(
                    "SAP login not confirmed. Complete SSO/MFA or custom login manually, then rerun."
                )
            return True
        except Exception as exc:
            raise RuntimeError("SAP login/attach failed: " + str(exc)) from exc

    def _control_exists(self, control_id):
        try:
            self.session.findById(control_id)
            return True
        except Exception:
            return False

    def _is_logged_in(self):
        if self.session is None:
            return False
        try:
            # SAP login screen normally contains RSYST-BNAME. Absence is a useful,
            # but not infallible, indication that login screen is no longer active.
            return not self._control_exists("wnd[0]/usr/txtRSYST-BNAME")
        except Exception:
            return False

    def open_miro_transaction(self):
        if self.session is None:
            self.login_or_attach_to_sap()
        self.session.findById("wnd[0]/tbar[0]/okcd").text = "/n" + os.getenv("SAP_TRANSACTION", "MIRO").strip()
        self.session.findById("wnd[0]").sendVKey(0)
        time.sleep(2)
        company_code = os.getenv("SAP_COMPANY_CODE", "2000").strip()
        # Company-code dialog may or may not appear depending on SAP config.
        for control_id in ("wnd[1]/usr/ctxtBKPF-BUKRS", "wnd[1]/usr/ctxtINVFO-BUKRS"):
            try:
                self.session.findById(control_id).text = company_code
                self.session.findById("wnd[1]").sendVKey(0)
                time.sleep(1)
                break
            except Exception:
                pass
        try:
            title = str(self.session.findById("wnd[0]").text)
        except Exception:
            title = ""
        logger.info(f"MIRO requested for company code {company_code}; window title: {title}")
        return title

    def fill_miro_header_and_po(self, invoice):
        if self.session is None:
            self.login_or_attach_to_sap()
        ids = {
            "invoice_date": os.getenv("SAP_INVOICE_DATE_ID", "").strip(),
            "invoice_number": os.getenv("SAP_REFERENCE_ID", "").strip(),
            "currency": os.getenv("SAP_CURRENCY_ID", "").strip(),
            "po_number": os.getenv("SAP_PO_NUMBER_ID", "").strip(),
        }
        missing = [field for field, control_id in ids.items() if not control_id]
        if missing:
            logger.warn("No header/PO fields filled. Configure verified SAP IDs for: " + ", ".join(missing))
            return {"filled": False, "selected_row": False, "reason": "missing_verified_field_ids", "missing": missing}
        for field, control_id in ids.items():
            try:
                value = invoice[field]
                if field == "invoice_date" and hasattr(value, "strftime"):
                    value = value.strftime("%d.%m.%Y")
                self.session.findById(control_id).text = str(value)
            except Exception as exc:
                raise RuntimeError(f"Failed to fill {field} using SAP ID {control_id}: {exc}") from exc
        self.session.findById("wnd[0]").sendVKey(0)
        time.sleep(2)
        selected = self.select_matching_po_grn_row(invoice)
        return {"filled": True, "selected_row": selected, "reason": "header_filled_row_selection_attempted"}

    def select_matching_po_grn_row(self, invoice):
        grid_id = os.getenv("SAP_PO_GRID_ID", "").strip()
        po_key = os.getenv("SAP_PO_COLUMN_KEY", "").strip()
        grn_key = os.getenv("SAP_GRN_COLUMN_KEY", "").strip()
        if not grid_id or not po_key:
            logger.warn("PO/GRN table selection skipped: SAP_PO_GRID_ID and SAP_PO_COLUMN_KEY are not configured.")
            return False
        try:
            grid = self.session.findById(grid_id)
            po_expected = str(invoice.get("po_number", "")).strip().lstrip("0")
            grn_expected = str(invoice.get("grn_number", "") or "").strip().lstrip("0")
            matches = []
            for row_idx in range(int(grid.RowCount)):
                po_actual = str(grid.GetCellValue(row_idx, po_key)).strip().lstrip("0")
                if po_actual != po_expected:
                    continue
                if grn_key and grn_expected:
                    grn_actual = str(grid.GetCellValue(row_idx, grn_key)).strip().lstrip("0")
                    if grn_actual != grn_expected:
                        continue
                matches.append(row_idx)
            if len(matches) != 1:
                logger.warn(f"Expected exactly one PO/GRN row; found {len(matches)}. No row selected.")
                return False
            row_idx = matches[0]
            try:
                grid.setCurrentCell(row_idx, po_key)
                grid.selectedRows = str(row_idx)
                grid.clickCurrentCell()
            except Exception as exc:
                logger.warn(f"Matching row {row_idx} found, but selection method needs SAP-specific adjustment: {exc}")
                return False
            logger.info(f"Selected unique matching PO/GRN table row index {row_idx}.")
            return True
        except Exception as exc:
            logger.warn("Could not inspect/select SAP PO grid. Verify grid ID and column keys: " + str(exc))
            return False

    def dump_sap_control_tree(self, output_path="reports/sap_control_tree.txt"):
        if self.session is None:
            self.login_or_attach_to_sap()
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        lines = []
        def walk(component, depth=0):
            try:
                children = component.Children
                count = children.Count
            except Exception:
                count = 0
            for idx in range(count):
                try:
                    child = children(idx)
                    cid = str(child.Id)
                    typ = str(getattr(child, "Type", ""))
                    name = str(getattr(child, "Name", ""))
                    value = ""
                    for attr in ("Text", "Tooltip", "DefaultTooltip"):
                        try:
                            candidate = getattr(child, attr)
                            if candidate:
                                value = str(candidate).replace("\\n", " ")[:80]
                                break
                        except Exception:
                            pass
                    lines.append("  " * depth + f"{typ} | {cid} | {name} | {value}")
                    walk(child, depth + 1)
                except Exception as exc:
                    lines.append("  " * depth + f"<read error: {exc}>")
        walk(self.session)
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write("\\n".join(lines))
        logger.info(f"SAP control tree saved to {output_path}")
        return output_path
