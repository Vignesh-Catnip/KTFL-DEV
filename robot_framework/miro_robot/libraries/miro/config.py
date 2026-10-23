"""
Central configuration for the KTFL MIRO robot.

Every value can be overridden with an environment variable (or a .env file in the
project root). Passwords are NEVER stored in code - set them as environment variables
or in the Windows credential store / Orchestrator vault and export them before the run.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader. Real environment variables always win.

    KEY=VALUE                 plain value
    KEY=VALUE   # comment     comment after whitespace + '#' is removed
    KEY="pa ss#word"          quote a value that must keep spaces or ' #'
    """
    if not path.exists():
        return
    # utf-8-sig: Notepad may save the file with a BOM
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip()
        if val[:1] in ('"', "'") and val.find(val[0], 1) > 0:
            val = val[1:val.find(val[0], 1)]          # quoted: keep everything inside the quotes
        else:
            val = re.split(r"\s+#", val, maxsplit=1)[0].strip()
        os.environ.setdefault(key, val)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    v = _env(name, "")
    return default if v == "" else v.lower() in ("1", "true", "yes", "y", "on")


def _env_float(name: str, default: float) -> float:
    v = _env(name, "")
    try:
        return default if v == "" else float(v)
    except ValueError:
        raise ValueError(f"{name} must be a number, got '{v}' (check the .env file)") from None


def _env_int(name: str, default: int) -> int:
    v = _env(name, "")
    try:
        return default if v == "" else int(v)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, got '{v}' (check the .env file)") from None


VALID_MODES = ("CHECK", "PARK", "POST")


@dataclass
class Config:
    # ---- PostgreSQL (the extractor's database) -------------------------------------------
    pg_host: str = "10.7.11.22"
    pg_port: int = 5432
    pg_db: str = "miro_invoice"
    pg_user: str = "appuser"
    pg_password: str = ""

    # which invoices are picked up
    source_status: str = "MATCHED"        # invoices.status value that means "ready for MIRO"
    max_attempts: int = 3                 # FAILED invoices are retried up to this many times
    batch_limit: int = 200                # max invoices per run

    # ---- SAP GUI -------------------------------------------------------------------------
    sap_backend: str = "gui"              # "gui" = real SAP GUI scripting, "fake" = simulator (tests)
    sap_logon_exe: str = r"C:\Program Files\SAP\FrontEnd\SAPGUI\saplogon.exe"
    sap_connection: str = ""              # SAP Logon pad entry description, e.g. "KTFL PRD"
    sap_client: str = "150"
    sap_user: str = ""
    sap_password: str = ""
    sap_language: str = "EN"
    sap_multi_logon_option: str = "2"     # 1 = end other sessions, 2 = continue with this logon
    company_code: str = "2000"

    # number / date format of the SAP user (SU3 -> Defaults)
    #   "1,234,567.89" -> "." decimal   |   "1.234.567,89" -> "," decimal
    sap_decimal_sep: str = "."
    sap_date_format: str = "%d.%m.%Y"

    # ---- MIRO behaviour ------------------------------------------------------------------
    # CHECK = fill everything, verify balance, leave WITHOUT saving (safe default for UAT)
    # PARK  = park the document (Invoice Document -> Park)
    # POST  = post the document (Ctrl+S)
    mode: str = "CHECK"
    collapse_header: bool = True
    enter_amount: bool = True             # type the invoice gross total in the header
    enter_tax_code: bool = True           # Basic data Tax code = invoices.gst_tax_code (e.g. I3)
    calculate_tax: bool = True            # tick "Calculate tax" on Basic data
    enter_plant: bool = True              # Business Place + Section = invoices.plant_code
    enter_item_text: bool = True          # Text = first invoice line item
    max_unplanned_cost: float = 1000.00   # max balance moved to "Unpl. Del. Csts" automatically
    doc_type_by_plant: str = ""           # "2002=RE,2001=RJ" - Doc. Type per plant (Details tab)
    doc_type_default: str = ""            # Doc. Type key when the plant has no mapping ("" = leave SAP default)
    simulate: bool = True                 # press Simulate before park / post / exit
    simulate_button: str = "wnd[0]/tbar[1]/btn[43]"
    enter_currency: bool = True
    amount_tolerance: float = 1.00        # selected GR value vs invoice taxable value (INR)
    balance_tolerance: float = 0.00       # MIRO balance must be within this to park/post
    stop_on_duplicate_warning: bool = True

    # MIRO item table columns (field names). Title words are used as fallback.
    col_ref_doc: str = "DRSEG-LFBNR"      # "Reference Document" = GRN number
    col_po: str = "DRSEG-EBELN"
    col_po_item: str = "DRSEG-EBELP"
    col_amount: str = "DRSEG-WRBTR"
    col_quantity: str = "DRSEG-MENGE"
    col_item_text: str = "DRSEG-TXZ01"
    col_hsn: str = "DRSEG-HSN_SAC"                  # HSN/SAC column in the item table
    check_hsn: bool = True                          # compare + correct HSN of the selected rows
    balance_field: str = "RM08M-DIFFERENZ"          # MIRO "Balance" field
    park_menu: str = "Invoice document>Park"        # menu path used in PARK mode

    # ---- timing / output -----------------------------------------------------------------
    wait_timeout: float = 30.0
    poll_interval: float = 0.3
    step_delay: float = 0.0               # seconds to pause on every step (watch mode), e.g. 1.5
    output_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "output")
    fake_scenario: str = ""               # JSON file for the SAP simulator (tests only)

    def validate(self) -> None:
        self.mode = self.mode.upper()
        if self.mode not in VALID_MODES:
            raise ValueError(f"MIRO_MODE must be one of {VALID_MODES}, got '{self.mode}'")
        if self.sap_decimal_sep not in (".", ","):
            raise ValueError("SAP_DECIMAL_SEP must be '.' or ','")
        if not self.pg_password:
            raise ValueError("PG_PASSWORD is not set (environment variable / .env)")
        if self.max_unplanned_cost < 0:
            raise ValueError("MIRO_MAX_UNPLANNED_COST cannot be negative")
        if self.sap_backend not in ("gui", "fake"):
            raise ValueError("MIRO_SAP_BACKEND must be 'gui' or 'fake'")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "screenshots").mkdir(parents=True, exist_ok=True)

    def doc_type_for_plant(self, plant: str) -> str:
        """MIRO_DOC_TYPE_BY_PLANT=2002=RE,2001=RJ  ->  '2002' -> 'RE'."""
        plant = str(plant or "").strip()
        for pair in self.doc_type_by_plant.replace(";", ",").split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                if k.strip() == plant:
                    return v.strip()
        return self.doc_type_default

    @property
    def pg_conninfo(self) -> str:
        return (f"host={self.pg_host} port={self.pg_port} dbname={self.pg_db} "
                f"user={self.pg_user} password={self.pg_password} connect_timeout=15 "
                f"application_name=ktfl_miro_robot")


def load_config() -> Config:
    _load_dotenv(PROJECT_ROOT / ".env")
    c = Config(
        pg_host=_env("PG_HOST", Config.pg_host),
        pg_port=_env_int("PG_PORT", Config.pg_port),
        pg_db=_env("PG_DB", Config.pg_db),
        pg_user=_env("PG_USER", Config.pg_user),
        pg_password=_env("PG_PASSWORD", ""),
        source_status=_env("MIRO_SOURCE_STATUS", Config.source_status),
        max_attempts=_env_int("MIRO_MAX_ATTEMPTS", Config.max_attempts),
        batch_limit=_env_int("MIRO_BATCH_LIMIT", Config.batch_limit),
        sap_backend=_env("MIRO_SAP_BACKEND", Config.sap_backend).lower(),
        sap_logon_exe=_env("SAP_LOGON_EXE", Config.sap_logon_exe),
        sap_connection=_env("SAP_CONNECTION", ""),
        sap_client=_env("SAP_CLIENT", Config.sap_client),
        sap_user=_env("SAP_USER", ""),
        sap_password=_env("SAP_PASSWORD", ""),
        sap_language=_env("SAP_LANGUAGE", Config.sap_language),
        sap_multi_logon_option=_env("SAP_MULTI_LOGON_OPTION", Config.sap_multi_logon_option),
        company_code=_env("MIRO_COMPANY_CODE", Config.company_code),
        sap_decimal_sep=_env("SAP_DECIMAL_SEP", Config.sap_decimal_sep),
        sap_date_format=_env("SAP_DATE_FORMAT", Config.sap_date_format),
        mode=_env("MIRO_MODE", Config.mode),
        collapse_header=_env_bool("MIRO_COLLAPSE_HEADER", Config.collapse_header),
        enter_amount=_env_bool("MIRO_ENTER_AMOUNT", Config.enter_amount),
        enter_tax_code=_env_bool("MIRO_ENTER_TAX_CODE", Config.enter_tax_code),
        calculate_tax=_env_bool("MIRO_CALCULATE_TAX", Config.calculate_tax),
        enter_plant=_env_bool("MIRO_ENTER_PLANT", Config.enter_plant),
        enter_item_text=_env_bool("MIRO_ENTER_ITEM_TEXT", Config.enter_item_text),
        max_unplanned_cost=_env_float("MIRO_MAX_UNPLANNED_COST", Config.max_unplanned_cost),
        doc_type_by_plant=_env("MIRO_DOC_TYPE_BY_PLANT", ""),
        doc_type_default=_env("MIRO_DOC_TYPE_DEFAULT", ""),
        simulate=_env_bool("MIRO_SIMULATE", Config.simulate),
        simulate_button=_env("MIRO_SIMULATE_BUTTON", Config.simulate_button),
        enter_currency=_env_bool("MIRO_ENTER_CURRENCY", Config.enter_currency),
        amount_tolerance=_env_float("MIRO_AMOUNT_TOLERANCE", Config.amount_tolerance),
        balance_tolerance=_env_float("MIRO_BALANCE_TOLERANCE", Config.balance_tolerance),
        stop_on_duplicate_warning=_env_bool("MIRO_STOP_ON_DUPLICATE", Config.stop_on_duplicate_warning),
        col_ref_doc=_env("MIRO_COL_REF_DOC", Config.col_ref_doc),
        col_po=_env("MIRO_COL_PO", Config.col_po),
        col_po_item=_env("MIRO_COL_PO_ITEM", Config.col_po_item),
        col_amount=_env("MIRO_COL_AMOUNT", Config.col_amount),
        col_quantity=_env("MIRO_COL_QUANTITY", Config.col_quantity),
        col_item_text=_env("MIRO_COL_ITEM_TEXT", Config.col_item_text),
        col_hsn=_env("MIRO_COL_HSN", Config.col_hsn),
        check_hsn=_env_bool("MIRO_CHECK_HSN", Config.check_hsn),
        balance_field=_env("MIRO_BALANCE_FIELD", Config.balance_field),
        park_menu=_env("MIRO_PARK_MENU", Config.park_menu),
        wait_timeout=_env_float("MIRO_WAIT_TIMEOUT", Config.wait_timeout),
        poll_interval=_env_float("MIRO_POLL_INTERVAL", Config.poll_interval),
        step_delay=_env_float("MIRO_STEP_DELAY", Config.step_delay),
        output_dir=Path(_env("MIRO_OUTPUT_DIR", str(PROJECT_ROOT / "output"))),
        fake_scenario=_env("MIRO_FAKE_SCENARIO", ""),
    )
    c.validate()
    return c
