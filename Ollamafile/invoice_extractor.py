"""
KTFL MIRO Invoice Extractor  (FIXED: Ollama /api/chat + think=False)
Vision (Ollama) -> SAP SQL Server fallback -> Validation -> GST tax-code mapping -> PostgreSQL
=============================================================================================
Flow per PDF:
  1. Vision pass   : invoice header + line items                       (qwen3-vl)
  2. Vision pass   : GRN number from receiving stamp                   (10-digit validated)
  3. SAP fallback  : BRONZE_DB (EKKO / EKPO / MSEG)
  4. GST mapping   : GST % on invoice -> SAP tax code (GST_RATE master)
  5. Status        : RED_FLAG / MATCHED / REVIEW
  6. Validation    : arithmetic, GST split, SAP match, formats
  7. PostgreSQL    : invoices, invoice_lines, invoice_master, gst_tax_codes, audit_logs
  8. Excel per invoice

Install:
  pip install pymupdf httpx openpyxl sqlalchemy "psycopg[binary]" pyodbc

Run (passwords from environment variables, NOT hard-coded):
  set SAP_DB_PASSWORD=********
  set PG_PASSWORD=********
  python invoice_extractor_fixed.py
"""

import os
for k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
    os.environ.pop(k, None)
os.environ["NO_PROXY"] = "localhost,127.0.0.1"
os.environ["no_proxy"] = "localhost,127.0.0.1"

import base64, json, re, sys, time
from datetime import datetime, timezone
from pathlib import Path

import fitz
import httpx
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

from sqlalchemy import (create_engine, text, select, bindparam, String, Text, Float,
                        DateTime, ForeignKey, JSON, Integer, Boolean)
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import insert as pg_insert

# ============================================================
# CONFIG
# ============================================================
PDF_FOLDER    = r"C:\Users\ckts00126\Desktop\KTFL-EXTRACTION\Input-pdf"
OUTPUT_FOLDER = r"C:\Users\ckts00126\Desktop\KTFL-EXTRACTION\Output-Excel"
GST_RATE_XLSX = r"C:\Users\ckts00126\Desktop\KTFL-EXTRACTION\GST RATE.xlsx"

OLLAMA_URL   = "http://127.0.0.1:11434"
MODEL_VLM    = "qwen3-vl:32b"

PG_USER      = "appuser"
PG_PASSWORD  = "appuser123"
PG_HOST      = "10.7.11.22:5432"
PG_DB        = "miro_invoice"
PG_URL       = f"postgresql+psycopg://{PG_USER}:{PG_PASSWORD}@{PG_HOST}/{PG_DB}"

# SAP Data Lake (SQL Server) — read only.
SAP_SERVER    = "ktfldlsqldb.kalyanicorp.com"
SAP_DATABASE  = "BRONZE_DB"
SAP_USER      = "automation_reader"
SAP_PASSWORD  = "Qwerty_12345"
SAP_DRIVER    = "SQL Server"
SAP_CLIENT    = "150"
GRN_MOVEMENT_TYPES = ("101", "105")

KTFL_GSTIN_PREFIXES = ("27AABCK0618A",)
AMOUNT_TOLERANCE    = 0.02
ROUND_TOLERANCE     = 1.0

MAX_PAGES   = 6
IMAGE_ZOOM  = 1.4
MAIN_PAGES  = 3        # pages sent for the main (header + lines) pass; delivery notes / e-way bill are not needed
NUM_CTX     = 16384
NUM_PREDICT = 6144
TIMEOUT     = 900
# ============================================================


# ── PostgreSQL models ────────────────────────────────────────

def now_utc(): return datetime.now(timezone.utc)

class Base(DeclarativeBase): pass

class Invoice(Base):
    __tablename__ = "invoices"
    id:             Mapped[int]      = mapped_column(Integer, primary_key=True)
    filename:       Mapped[str]      = mapped_column(String(255))
    vendor:         Mapped[str|None] = mapped_column(String(255))
    invoice_number: Mapped[str|None] = mapped_column(String(100), index=True)
    invoice_date:   Mapped[str|None] = mapped_column(String(30))
    po_number:      Mapped[str|None] = mapped_column(String(100))
    gstin:          Mapped[str|None] = mapped_column(String(50))
    hsn_sac:        Mapped[str|None] = mapped_column(String(50))
    vendor_code:    Mapped[str|None] = mapped_column(String(50))
    tax_code:       Mapped[str|None] = mapped_column(String(30))
    business_place: Mapped[str|None] = mapped_column(String(100))
    currency:       Mapped[str]      = mapped_column(String(10), default="INR")
    subtotal:       Mapped[float]    = mapped_column(Float, default=0)
    cgst:           Mapped[float]    = mapped_column(Float, default=0)
    sgst:           Mapped[float]    = mapped_column(Float, default=0)
    igst:           Mapped[float]    = mapped_column(Float, default=0)
    tax:            Mapped[float]    = mapped_column(Float, default=0)
    total:          Mapped[float]    = mapped_column(Float, default=0)
    confidence:     Mapped[float]    = mapped_column(Float, default=0)
    grn_number:     Mapped[str|None] = mapped_column(String(100))
    handwritten_references: Mapped[str|None] = mapped_column(Text)
    plant_code:     Mapped[str|None] = mapped_column(String(20))
    plant_source:   Mapped[str|None] = mapped_column(String(20))
    po_date:        Mapped[str|None] = mapped_column(String(30))
    grn_date:       Mapped[str|None] = mapped_column(String(30))
    grn_amount:     Mapped[float|None] = mapped_column(Float)
    po_list:        Mapped[str|None] = mapped_column(Text)
    multi_po_flag:  Mapped[bool]     = mapped_column(Boolean, default=False)
    red_flag_reason: Mapped[str|None] = mapped_column(Text)
    gst_percent:      Mapped[float|None] = mapped_column(Float)
    gst_percent_component: Mapped[float|None] = mapped_column(Float)
    gst_nature:       Mapped[str|None]   = mapped_column(String(20))
    gst_tax_code:     Mapped[str|None]   = mapped_column(String(10))
    gst_tax_code_desc:Mapped[str|None]   = mapped_column(String(120))
    sap_tax_code:     Mapped[str|None]   = mapped_column(String(10))
    tax_code_match:   Mapped[bool|None]  = mapped_column(Boolean)
    validation_status: Mapped[str|None] = mapped_column(String(20), index=True)
    validation_errors: Mapped[dict]     = mapped_column(JSON, default=dict)
    dedupe_key:     Mapped[str|None] = mapped_column(String(220), index=True)
    field_sources:     Mapped[dict]     = mapped_column(JSON, default=dict)
    status:         Mapped[str]      = mapped_column(String(40), default="REVIEW", index=True)
    error_message:  Mapped[str|None] = mapped_column(Text)
    created_at:     Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at:     Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)
    lines = relationship("InvoiceLine", back_populates="invoice", cascade="all, delete-orphan")

class InvoiceLine(Base):
    __tablename__ = "invoice_lines"
    id:         Mapped[int]   = mapped_column(primary_key=True)
    invoice_id: Mapped[int]   = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"))
    item:       Mapped[str]   = mapped_column(String(255))
    quantity:   Mapped[float] = mapped_column(Float, default=0)
    rate:       Mapped[float] = mapped_column(Float, default=0)
    tax_code:   Mapped[str]   = mapped_column(String(30), default="")
    amount:     Mapped[float] = mapped_column(Float, default=0)
    hsn_sac:    Mapped[str|None] = mapped_column(String(50))
    po_number:  Mapped[str|None] = mapped_column(String(100))
    po_item:    Mapped[str|None] = mapped_column(String(10))
    plant_code: Mapped[str|None] = mapped_column(String(20))
    uom:        Mapped[str|None] = mapped_column(String(10))
    source:     Mapped[str]   = mapped_column(String(20), default="VISION")
    invoice = relationship("Invoice", back_populates="lines")

class GstTaxCode(Base):
    __tablename__ = "gst_tax_codes"
    id:          Mapped[int]      = mapped_column(primary_key=True)
    client:      Mapped[str]      = mapped_column(String(10))
    procedure:   Mapped[str|None] = mapped_column(String(20))
    tax_code:    Mapped[str]      = mapped_column(String(10))
    tax_type:    Mapped[str|None] = mapped_column(String(5))
    description: Mapped[str|None] = mapped_column(String(160))
    rate:        Mapped[float]    = mapped_column(Float, default=0)
    rate_component: Mapped[float] = mapped_column(Float, default=0)
    nature:      Mapped[str|None] = mapped_column(String(20))
    category:    Mapped[str|None] = mapped_column(String(20))
    __table_args__ = ()

class InvoiceMaster(Base):
    __tablename__ = "invoice_master"
    id:              Mapped[int]      = mapped_column(primary_key=True)
    invoice_number:  Mapped[str]      = mapped_column(String(100), unique=True)
    vendor:          Mapped[str|None] = mapped_column(String(255))
    vendor_code:     Mapped[str|None] = mapped_column(String(50))
    po_number:       Mapped[str|None] = mapped_column(String(100))
    first_seen_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    invoice_id:      Mapped[int|None] = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"))

class AuditLog(Base):
    __tablename__ = "audit_logs"
    id:         Mapped[int]      = mapped_column(primary_key=True)
    invoice_id: Mapped[int|None] = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"))
    action:     Mapped[str]      = mapped_column(String(100))
    detail:     Mapped[dict]     = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


MIGRATIONS = [
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS gstin VARCHAR(50)",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS hsn_sac VARCHAR(50)",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS vendor_code VARCHAR(50)",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS cgst FLOAT DEFAULT 0",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS sgst FLOAT DEFAULT 0",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS igst FLOAT DEFAULT 0",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS grn_number VARCHAR(100)",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS handwritten_references TEXT",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS plant_code VARCHAR(20)",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS plant_source VARCHAR(20)",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS po_date VARCHAR(30)",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS grn_date VARCHAR(30)",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS grn_amount FLOAT",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS po_list TEXT",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS multi_po_flag BOOLEAN DEFAULT FALSE",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS red_flag_reason TEXT",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS gst_percent FLOAT",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS gst_percent_component FLOAT",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS gst_nature VARCHAR(20)",
    "ALTER TABLE gst_tax_codes ADD COLUMN IF NOT EXISTS rate_component FLOAT DEFAULT 0",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS gst_tax_code VARCHAR(10)",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS gst_tax_code_desc VARCHAR(120)",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS sap_tax_code VARCHAR(10)",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS tax_code_match BOOLEAN",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS validation_status VARCHAR(20)",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS validation_errors JSON",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS dedupe_key VARCHAR(220)",
    """UPDATE invoices SET dedupe_key =
         UPPER(REGEXP_REPLACE(COALESCE(invoice_number,''), '\\s+', '', 'g')) || '::' ||
         UPPER(REGEXP_REPLACE(COALESCE(po_number,''), '\\s+', '', 'g'))
       WHERE dedupe_key IS NULL AND invoice_number IS NOT NULL AND invoice_number <> ''""",
    """UPDATE invoices SET dedupe_key = NULL
       WHERE id NOT IN (SELECT MIN(id) FROM invoices WHERE dedupe_key IS NOT NULL GROUP BY dedupe_key)
         AND dedupe_key IS NOT NULL""",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_invoices_dedupe_key ON invoices (dedupe_key) "
    "WHERE dedupe_key IS NOT NULL",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS field_sources JSON",
    "ALTER TABLE invoice_lines ADD COLUMN IF NOT EXISTS rate FLOAT DEFAULT 0",
    "ALTER TABLE invoice_lines ADD COLUMN IF NOT EXISTS hsn_sac VARCHAR(50)",
    "ALTER TABLE invoice_lines ADD COLUMN IF NOT EXISTS po_number VARCHAR(100)",
    "ALTER TABLE invoice_lines ADD COLUMN IF NOT EXISTS po_item VARCHAR(10)",
    "ALTER TABLE invoice_lines ADD COLUMN IF NOT EXISTS plant_code VARCHAR(20)",
    "ALTER TABLE invoice_lines ADD COLUMN IF NOT EXISTS uom VARCHAR(10)",
    "ALTER TABLE invoice_lines ADD COLUMN IF NOT EXISTS source VARCHAR(20) DEFAULT 'VISION'",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_gst_tax_codes ON gst_tax_codes (client, tax_code)",
]


def setup_pg():
    if not PG_PASSWORD:
        print("  ⚠️  PG_PASSWORD env variable not set  (set PG_PASSWORD=...)")
    engine = create_engine(PG_URL)
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        for sql in MIGRATIONS:
            try: conn.execute(text(sql))
            except Exception: pass
        conn.commit()
    print("  PostgreSQL ready ✅")
    return engine, sessionmaker(bind=engine)


def setup_sap():
    if not SAP_PASSWORD:
        print("  ⚠️  SAP_DB_PASSWORD env variable not set — SAP fallback disabled")
        return None
    url = URL.create("mssql+pyodbc", username=SAP_USER, password=SAP_PASSWORD,
                     host=SAP_SERVER, database=SAP_DATABASE,
                     query={"driver": SAP_DRIVER, "TrustServerCertificate": "yes"})
    eng = create_engine(url, pool_pre_ping=True)
    with eng.connect() as c:
        c.execute(text("SELECT 1"))
    print(f"  SAP SQL Server ready ✅ ({SAP_SERVER}/{SAP_DATABASE})")
    return eng


# ── GST master ───────────────────────────────────────────────

def parse_gst_description(desc: str):
    d = (desc or "").strip()
    u = d.upper()

    if u.startswith("TCS") or "TCS " in u:          category = "TCS"
    elif u.startswith("ND"):                        category = "ND"
    elif u.startswith("RCM"):                       category = "RCM"
    elif u.startswith("ISD") or "ISD " in u:        category = "ISD"
    elif "EXPORT" in u:                             category = "EXPORT"
    elif "IMPORT" in u:                             category = "IMPORT"
    elif u.startswith("OUTPUT"):                    category = "OUTPUT"
    elif "INPUT" in u:                              category = "INPUT"
    else:                                           category = "OTHER"

    if "CGST" in u or "SGST" in u:
        nature = "CGST_SGST"
    elif "IGST" in u:
        nature = "IGST"
    else:
        nature = "OTHER"

    nums = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*%", d)]
    if category == "TCS":
        rate = comp = (nums[-1] if nums else 0.0)
    elif nature == "CGST_SGST" and len(nums) >= 2:
        comp = round(nums[0], 2)
        rate = round(nums[0] + nums[1], 2)
    elif nature == "CGST_SGST" and len(nums) == 1:
        comp = round(nums[0], 2)
        rate = round(nums[0] * 2, 2) if nums[0] else 0.0
    else:
        rate = comp = round(nums[0], 2) if nums else 0.0
    return rate, comp, nature, category


def load_gst_master(Session, xlsx_path: str) -> int:
    p = Path(xlsx_path)
    if not p.exists():
        print(f"  ⚠️  GST master not found: {p} — tax-code mapping will be skipped")
        return 0
    wb = load_workbook(p, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    hdr = [str(c or "").strip().lower() for c in rows[0]]
    idx = {name: hdr.index(name) for name in
           ("client", "procedure", "tax code", "tax type", "description") if name in hdr}

    n = 0
    with Session() as s:
        for r in rows[1:]:
            if not r or not r[idx.get("tax code", 2)]:
                continue
            code = str(r[idx["tax code"]]).strip()
            desc = str(r[idx.get("description", 4)] or "").strip()
            rate, comp, nature, category = parse_gst_description(desc)
            proc = str(r[idx.get("procedure", 1)] or "").strip()
            ttype = str(r[idx.get("tax type", 3)] or "").strip()
            s.execute(pg_insert(GstTaxCode).values(
                client=str(r[idx.get("client", 0)] or SAP_CLIENT).strip(),
                procedure=proc, tax_code=code, tax_type=ttype,
                description=desc, rate=rate, rate_component=comp,
                nature=nature, category=category,
            ).on_conflict_do_update(
                index_elements=["client", "tax_code"],
                set_={"description": desc, "rate": rate, "rate_component": comp,
                      "nature": nature, "category": category,
                      "procedure": proc, "tax_type": ttype},
            ))
            n += 1
        s.commit()
    print(f"  GST master loaded ✅ ({n} tax codes)")
    return n


def pick_tax_code(Session, gst_total, gst_component, nature, is_import=False, is_rcm=False):
    if nature == "NIL" or gst_total is None:
        return None
    category = "IMPORT" if is_import else ("RCM" if is_rcm else "INPUT")
    tol = 0.01
    with Session() as s:
        q = select(GstTaxCode).where(
            GstTaxCode.tax_type == "V",
            GstTaxCode.category == category,
            GstTaxCode.nature == nature,
        ).order_by(GstTaxCode.tax_code)
        rows = list(s.scalars(q))
    cands = [r for r in rows if abs((r.rate or 0) - gst_total) <= tol]
    if not cands and gst_component is not None:
        cands = [r for r in rows if abs((r.rate_component or 0) - gst_component) <= tol]
    return cands[0] if cands else None


# ── Prompts ──────────────────────────────────────────────────

GRN_PROMPT = r"""
You are looking at ONE invoice image. Your ONLY job is to find the GRN number from the receiving/inward stamp.

STEP 1 — Find the receiving stamp (usually a "KALYANI TECHNOFORGE LTD." rubber stamp, bottom of page).
STEP 2 — Find the row labelled "GRN NO", "GRN No.", "GRN", "JRN NO", "Inward No", "MRN No".
         It is normally the row BELOW "SCROLL NO".
STEP 3 — Extract the handwritten number EXACTLY as written (keep dash / dots / spaces).
STEP 4 — A GRN always starts with "50" and has EXACTLY 10 digits.
         "50-343863" means 50 + 00 + 343863 = 5000343863. Count the digits carefully.

NEVER extract from: SCROLL NO (starts with 200...), CH NO, Challan No, E-way Bill No,
Vehicle No, Gate Pass No, Delivery Note No, Buyer's Order No.

Return ONLY: {"grn_number": "<value or empty string>"}
"""

GRN_RETRY_HINT = """
Your previous answer was "{bad}" which cleans to "{bad_clean}" ({n} digits).
A valid GRN has EXACTLY 10 digits and starts with "50". Re-read the handwriting stroke by stroke.
"""

PROMPT = r"""
You are an Indian GST purchase-invoice extraction engine. The BUYER is Kalyani Technoforge Ltd (KTFL).
Read the invoice IMAGES. Return ONLY valid JSON.

FIELD RULES
- vendor         : the SELLER company printed at the top. NEVER "Kalyani Technoforge".
- invoice_number : value under "Invoice No." (e.g. "428/26-27", "AB2627143", "BM/26-27/1328").
- invoice_date   : the "Dated" value next to the invoice number.
- po_number      : "Buyer's Order No." — KTFL POs are 10 digits starting 81 or 51 (e.g. 8100007531).
                   Strip any "PO-" prefix. If MORE THAN ONE different PO is printed anywhere on the
                   invoice, list every one of them comma-separated in "po_numbers_all".
- vendor_code    : "Vendor Code" printed on the invoice, if present.
- gstin          : the SELLER's GSTIN (15 chars). Never KTFL's own GSTIN (starts 27AABCK0618A).
- hsn_sac        : the HSN/SAC code of the goods (4–8 digits).
- plant / ship-to: if a KTFL plant code or unit is printed (e.g. "Plant: 1200"), put it in "plant_hint".
- Amounts        : ₹6,19,763.14 -> 619763.14  (remove ₹ and thousands separators, keep the decimal).
- subtotal       : total taxable value BEFORE GST.
- cgst/sgst/igst : the GST AMOUNTS in rupees, not the percentages.
- gst_rate_percent: the GST RATE printed on the invoice as a number (18 for 9%+9% or 18%). 0 if none.
- total          : the final payable invoice total.
- grn_number     : always "" (it is extracted separately).
- Ignore Delivery Challan / E-way bill / weighment slip pages when collecting line items.

LINE ITEMS: only the invoice's own goods rows that carry an amount. No duplicate rows.

Return exactly:
{
  "vendor": "", "invoice_number": "", "invoice_date": "", "po_number": "",
  "po_numbers_all": "", "gstin": "", "hsn_sac": "", "vendor_code": "", "plant_hint": "",
  "currency": "INR", "subtotal": 0.0, "cgst": 0.0, "sgst": 0.0, "igst": 0.0,
  "gst_rate_percent": 0.0, "total_tax": 0.0, "total": 0.0,
  "grn_number": "", "handwritten_references": "", "confidence": 0.0,
  "lines": [{"item": "", "quantity": 0.0, "uom": "", "rate": 0.0, "hsn_sac": "", "amount": 0.0}]
}
"""


# ── Helpers ──────────────────────────────────────────────────

def to_float(v) -> float:
    if v is None: return 0.0
    if isinstance(v, (int, float)): return float(v)
    s = re.sub(r"[^0-9.\-]", "", str(v).replace(",", "").replace("₹", ""))
    try: return float(s) if s else 0.0
    except ValueError: return 0.0

def clean(v) -> str:
    return str(v).strip() if v not in (None, "None", "null") else ""

def clean_po(v) -> str:
    s = clean(v)
    s = re.sub(r"^(?:PO[-/.\s]*|P\.O\.[-/.\s]*|P/O[-/.\s]*)", "", s, flags=re.IGNORECASE).strip()
    m = re.search(r"\d{10}", s)
    return m.group(0) if m else s

def is_valid_po(po: str) -> bool:
    return bool(re.fullmatch(r"\d{10}", po or ""))

def clean_grn(v) -> str:
    s = clean(v)
    if not s: return s
    ocr = {'S':'5','s':'5','O':'0','o':'0','I':'1','l':'1','Z':'2','z':'2',
           'B':'8','G':'6','T':'7','A':'4','q':'9'}
    s = "".join(ocr.get(c, c) for c in s)
    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", s)
    if m:
        a, b = m.group(1), m.group(2)
        return a + "0" * max(0, 10 - len(a) - len(b)) + b
    return re.sub(r"[^\d]", "", s)

def grn_is_valid(g: str) -> bool:
    return len(g) == 10 and g.startswith("50")

def strip_zeros(v) -> str:
    return str(v or "").strip().lstrip("0")

def fmt_sap_date(v) -> str:
    if v is None or str(v).strip() in ("", "00000000"): return ""
    if isinstance(v, datetime): return v.strftime("%d.%m.%Y")
    s = str(v).strip()
    for f, n in (("%Y%m%d", 8), ("%Y-%m-%d", 10), ("%Y-%m-%d %H:%M:%S", 19)):
        try: return datetime.strptime(s[:n], f).strftime("%d.%m.%Y")
        except ValueError: pass
    return s

def extract_json(raw) -> dict:
    if isinstance(raw, list):
        raw = "".join(str(x.get("text", "") if isinstance(x, dict) else x) for x in raw)
    t = re.sub(r"<think>.*?</think>", "", str(raw), flags=re.DOTALL | re.I).strip()
    t = re.sub(r"^```(?:json)?", "", t, flags=re.I); t = re.sub(r"```$", "", t).strip()
    try: return json.loads(t)
    except Exception: pass
    s, e = t.find("{"), t.rfind("}")
    if s >= 0 and e > s:
        try: return json.loads(t[s:e + 1])
        except Exception: pass
    return repair_json(t)

def repair_json(t: str) -> dict:
    """The model output was cut off or has a broken comma (e.g. 'Expecting , delimiter').
    Walk back to the last complete object and close the open brackets, so the header and
    every line item that WAS completed are still kept."""
    s = t.find("{")
    if s < 0:
        raise ValueError("No valid JSON in response")
    body = t[s:]
    cuts = [m.end() for m in re.finditer(r"[}\]]", body)]
    for end in reversed(cuts[-80:]):
        cand = body[:end].rstrip().rstrip(",")
        for closer in ("", "}", "]}", "}]}", "]}}"):
            try:
                out = json.loads(cand + closer)
                if isinstance(out, dict):
                    print("     ⚠️  JSON was cut/broken — repaired (some trailing line items may be missing)")
                    return out
            except Exception:
                continue
    raise ValueError("No valid JSON in response (repair failed)")

def pdf_images(path: Path):
    doc = fitz.open(str(path)); zoom = fitz.Matrix(IMAGE_ZOOM, IMAGE_ZOOM); out = []
    for i in range(min(doc.page_count, MAX_PAGES)):
        out.append(base64.b64encode(doc[i].get_pixmap(matrix=zoom, alpha=False).tobytes("png")).decode())
    doc.close(); return out

def pdf_text(path: Path) -> str:
    doc = fitz.open(str(path)); parts = []
    for i, p in enumerate(doc, 1):
        t = p.get_text("text")
        if t.strip(): parts.append(f"--- PAGE {i} ---\n{t[:2000]}")
    doc.close(); return "\n".join(parts)

def format_date(raw: str) -> str:
    if not raw: return ""
    raw = raw.strip()
    for f in ("%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d-%m-%y", "%d/%m/%y",
              "%d-%b-%y", "%d-%b-%Y", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y"):
        try:
            dt = datetime.strptime(raw, f)
            if dt.year < 100: dt = dt.replace(year=dt.year + 2000)
            return dt.strftime("%d.%m.%Y")
        except ValueError: pass
    return raw

def fix_amounts(sub, cgst, sgst, igst, tax, total):
    if tax == 0: tax = round(cgst + sgst + igst, 2)
    if sub == 0 and total > 0: sub = round(total - tax, 2)
    if total == 0: total = round(sub + tax, 2)
    return sub, cgst, sgst, igst, tax, total

GST_STANDARD_RATES  = (0, 0.1, 0.25, 1, 1.5, 3, 5, 6, 7.5, 12, 18, 28)
GST_COMPONENT_RATES = (0, 0.05, 0.125, 0.5, 0.75, 1.5, 2.5, 3, 3.75, 6, 9, 14)

def snap_gst_rate(rate, component=False):
    if rate is None: return None
    table = GST_COMPONENT_RATES if component else GST_STANDARD_RATES
    best = min(table, key=lambda r: abs(r - rate))
    return float(best) if abs(best - rate) <= 0.4 else round(rate, 2)


# ── LLM (FIXED) ──────────────────────────────────────────────
# /api/chat + think=False  (same payload style that worked in ectract.py)
# qwen3-vl is a "thinking" model: without think=False the tokens get spent on
# thinking and the answer comes back empty.

def create_llm():
    return None   # kept so call sites don't change

def _post_chat(prompt: str, images: list, num_predict: int, use_format: bool) -> dict:
    payload = {
        "model": MODEL_VLM,
        "messages": [{"role": "user", "content": prompt, "images": images}],
        "stream": False,
        "think": False,
        "options": {"temperature": 0, "num_ctx": NUM_CTX, "num_predict": num_predict},
    }
    if use_format:
        payload["format"] = "json"
    r = httpx.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=TIMEOUT, trust_env=False)
    r.raise_for_status()
    return r.json()

def _raw_ollama(prompt: str, images: list, num_predict=NUM_PREDICT) -> dict:
    data = _post_chat(prompt, images, num_predict, use_format=True)
    msg = data.get("message", {}) or {}
    raw = (msg.get("content") or msg.get("thinking") or "").strip()
    if not raw:
        # retry once without forced JSON format
        data = _post_chat(prompt, images, num_predict, use_format=False)
        msg = data.get("message", {}) or {}
        raw = (msg.get("content") or msg.get("thinking") or "").strip()
    if not raw:
        print(f"    ⚠️  Ollama empty. keys={list(data.keys())} done_reason={data.get('done_reason')}")
        raise ValueError("Ollama returned empty response")
    return extract_json(raw)

def ask(llm, prompt: str, images: list, num_predict=NUM_PREDICT) -> dict:
    return _raw_ollama(prompt, images, num_predict)

def invoke_main(llm, images, text_layer) -> dict:
    prompt = PROMPT + ("\nOCR TEXT (may be noisy):\n" + text_layer[:2000] if text_layer else "")
    pages = images[:MAIN_PAGES]
    try:
        return ask(llm, prompt, pages)
    except Exception as e:
        print(f"     main pass failed ({e}) — retrying with first 2 pages, short output...")
        short = prompt + "\nIMPORTANT: keep it compact. At most 25 line items, item text max 40 characters."
        return ask(llm, short, images[:2])

def invoke_grn(llm, images, max_retry=2) -> str:
    best = ""
    for pno, img in enumerate(images, 1):
        prompt = GRN_PROMPT
        for _ in range(max_retry + 1):
            try: grn = clean(ask(llm, prompt, [img], 256).get("grn_number"))
            except Exception as e:
                print(f"     GRN page {pno} error: {e}"); grn = ""
            if not grn: break
            c = clean_grn(grn)
            if grn_is_valid(c):
                print(f"     GRN page {pno}: '{grn}' → {c} ✅"); return grn
            best = best or grn
            prompt = GRN_PROMPT + GRN_RETRY_HINT.format(bad=grn, bad_clean=c, n=len(c))
    return best


def normalize(data: dict, filename: str):
    data = data if isinstance(data, dict) else {}
    sub, cgst, sgst, igst, tax, total = fix_amounts(
        to_float(data.get("subtotal")), to_float(data.get("cgst")), to_float(data.get("sgst")),
        to_float(data.get("igst")), to_float(data.get("total_tax")), to_float(data.get("total")))

    hsn_inv = re.sub(r"[^\d]", "", clean(data.get("hsn_sac")))
    seen, lines = set(), []
    for l in data.get("lines") or []:
        if not isinstance(l, dict): continue
        item = clean(l.get("item") or l.get("description"))
        amt = to_float(l.get("amount"))
        if not item: continue
        key = (item.lower()[:40], round(amt))
        if key in seen: continue
        seen.add(key)
        lines.append({"item": item, "quantity": to_float(l.get("quantity")), "uom": clean(l.get("uom")),
                      "rate": to_float(l.get("rate")),
                      "hsn_sac": re.sub(r"[^\d]", "", clean(l.get("hsn_sac"))) or hsn_inv,
                      "amount": amt, "po_number": "", "po_item": "", "plant_code": "",
                      "source": "VISION"})

    hw = data.get("handwritten_references") or ""
    if isinstance(hw, list): hw = ", ".join(map(str, hw))

    gstin = clean(data.get("gstin")).upper().replace(" ", "")
    if any(gstin.startswith(p) for p in KTFL_GSTIN_PREFIXES):
        gstin = ""

    po_all = [clean_po(p) for p in re.split(r"[,;/\s]+", clean(data.get("po_numbers_all"))) if p]

    header = {
        "filename": filename, "vendor": clean(data.get("vendor")),
        "invoice_number": clean(data.get("invoice_number")),
        "invoice_date": format_date(clean(data.get("invoice_date"))),
        "po_number": clean_po(data.get("po_number")),
        "po_numbers_printed": sorted({p for p in po_all if is_valid_po(p)}),
        "gstin": gstin, "hsn_sac": hsn_inv,
        "vendor_code": clean(data.get("vendor_code")),
        "plant_code": re.sub(r"[^\w]", "", clean(data.get("plant_hint")))[:20],
        "plant_source": "", "currency": clean(data.get("currency")) or "INR",
        "subtotal": round(sub, 2), "cgst": round(cgst, 2), "sgst": round(sgst, 2),
        "igst": round(igst, 2), "total_tax": round(tax, 2), "total": round(total, 2),
        "gst_rate_printed": to_float(data.get("gst_rate_percent")),
        "grn_number": "", "handwritten_references": clean(hw),
        "confidence": round(max(0, min(1, to_float(data.get("confidence")))) * 100, 1),
        "tax_code": "", "po_date": "", "grn_date": "", "grn_amount": None,
        "po_list": "", "multi_po_flag": False, "red_flag_reason": "",
        "gst_percent": None, "gst_nature": "", "gst_tax_code": "", "gst_tax_code_desc": "",
        "sap_tax_code": "", "tax_code_match": None,
        "gst_percent_component": None, "sap_mismatches": [], "sap_checked": False,
        "grn_in_sap": False,
        "validation_status": "", "validation_errors": {},
    }
    if header["plant_code"]:
        header["plant_source"] = "VISION"
    header["field_sources"] = {
        k: "VISION" for k, v in header.items()
        if k not in ("filename", "field_sources", "validation_errors")
        and v not in ("", None, 0, 0.0, [], False, {})}
    return header, lines


# ── SAP fallback ─────────────────────────────────────────────

def _q(conn, sql: str, **params):
    stmt = text(sql)
    for k, v in params.items():
        if isinstance(v, (list, tuple)):
            stmt = stmt.bindparams(bindparam(k, expanding=True))
    return [dict(r._mapping) for r in conn.execute(stmt, params)]

def _mandt(alias: str) -> str:
    return f" AND {alias}.MANDT = :mandt" if SAP_CLIENT else ""

MSEG_COLS = """MBLNR, MJAHR, BWART, EBELN, EBELP, LIFNR, WERKS, MENGE, MEINS, ERFMG, ERFME,
               DMBTR, WAERS, MWSKZ, XBLNR_MKPF, BUDAT_MKPF, SGTXT, SMBLN"""

def sap_enrich(sap_engine, header: dict, lines: list) -> list:
    notes = []
    if sap_engine is None:
        return ["SAP disabled — no cross-check done"]
    header["sap_checked"] = True
    src = header["field_sources"]
    base = {"mandt": SAP_CLIENT} if SAP_CLIENT else {}

    def fill(field, value, source, overwrite=False):
        if value in (None, "", 0, 0.0): return
        cur = header.get(field)
        if cur in (None, "", 0, 0.0):
            header[field] = value; src[field] = source
            notes.append(f"{field} ← {source}: {value}")
        elif str(cur).strip().lstrip("0") != str(value).strip().lstrip("0"):
            if overwrite:
                notes.append(f"{field} OVERWRITTEN by {source}: '{cur}' → '{value}'")
                header[field] = value; src[field] = source
            else:
                msg = f"{field}: invoice='{cur}' vs SAP='{value}'"
                header["sap_mismatches"].append(msg)
                notes.append("MISMATCH " + msg)

    with sap_engine.connect() as conn:
        inv_no = header["invoice_number"]
        grn    = header["grn_number"]
        vcode  = strip_zeros(header["vendor_code"])

        # ---- helpers for GRN resolution ---------------------------------------------------
        def live(rows):
            """drop reversed documents; prefer rows that carry item text (SGTXT) — same as the
            manual check:  XBLNR_MKPF = invoice AND EBELN = PO AND SGTXT is not null"""
            rows = [r for r in rows if not clean(r.get("SMBLN"))]
            txt = [r for r in rows if clean(r.get("SGTXT"))]
            return txt or rows

        def by_grn(rows):
            g = {}
            for r in rows:
                g.setdefault(clean(r["MBLNR"]), []).append(r)
            return g

        def score(rows, use_po=True):
            """How well a GR document matches THIS invoice: line amounts, taxable value, PO."""
            sc = 0.0
            amts = [to_float(r["DMBTR"]) for r in rows]
            for l in lines:
                if l["amount"] and any(abs(l["amount"] - a) <= ROUND_TOLERANCE for a in amts):
                    sc += 1
            sub = header["subtotal"]
            if sub and abs(sum(amts) - sub) <= max(ROUND_TOLERANCE, sub * AMOUNT_TOLERANCE):
                sc += 5
            if use_po and is_valid_po(header["po_number"]) and \
               any(clean(r["EBELN"]) == header["po_number"] for r in rows):
                sc += 3
            return sc

        # 1) GR lines for the GRN the vision pass read from the stamp
        grn_rows = []
        if grn:
            grn_rows = live(_q(conn, f"SELECT {MSEG_COLS} FROM dbo.MSEG m "
                                     f"WHERE m.MBLNR = :grn {_mandt('m')}", grn=grn, **base))
            if not grn_rows:
                notes.append(f"GRN {grn} (read from stamp) NOT found in MSEG — "
                             f"looking it up by invoice no / PO / line items")

        # 2) GR lines by vendor invoice reference (MSEG.XBLNR_MKPF) — spaces ignored
        inv_rows = []
        if inv_no:
            inv_rows = _q(conn, f"""SELECT {MSEG_COLS} FROM dbo.MSEG m
                                    WHERE UPPER(REPLACE(LTRIM(RTRIM(m.XBLNR_MKPF)),' ','')) = :inv
                                      AND m.BWART IN :bwart {_mandt('m')}""",
                          inv=norm_key(inv_no), bwart=list(GRN_MOVEMENT_TYPES), **base)
            if vcode:   # printed vendor code is only a hint — never let it wipe out the result
                same_vendor = [r for r in inv_rows if strip_zeros(r["LIFNR"]) == vcode]
                if same_vendor: inv_rows = same_vendor
            inv_rows = live(inv_rows)
        inv_groups = by_grn(inv_rows)

        # 3) decide which GRN is the right one
        groups = dict(inv_groups)
        resolved = None
        if grn_rows and (not groups or grn in groups):
            resolved = (grn, grn_rows)                      # stamp GRN confirmed by SAP
        else:
            if not groups:                                  # no invoice-ref match → try by PO
                pos_try = sorted(set(header["po_numbers_printed"]) |
                                 ({header["po_number"]} if is_valid_po(header["po_number"]) else set()))
                if pos_try:
                    po_rows = live(_q(conn, f"""SELECT {MSEG_COLS} FROM dbo.MSEG m
                                                WHERE m.EBELN IN :pos AND m.BWART IN :bwart
                                                {_mandt('m')}""",
                                      pos=pos_try, bwart=list(GRN_MOVEMENT_TYPES), **base))
                    groups = by_grn(po_rows)
            best = max(groups.items(), key=lambda kv: (score(kv[1]), len(kv[1])), default=None)
            ok = bool(best) and (bool(inv_groups) or score(best[1], use_po=False) >= 1)
            if ok and grn_rows and score(grn_rows, use_po=False) >= score(best[1], use_po=False):
                ok = False                                  # stamp GRN fits the invoice at least as well
            if ok:
                resolved = best
                if len(groups) > 1:
                    notes.append(f"Several GRNs found {sorted(groups)} — picked {best[0]} "
                                 f"(best match on line amounts / taxable value / PO)")
            elif grn_rows:
                resolved = (grn, grn_rows)

        gr_rows = []
        if resolved:
            new_grn, gr_rows = resolved
            header["grn_in_sap"] = True
            if new_grn != grn:
                notes.append(f"GRN {grn or '(not read)'} (stamp) → {new_grn} taken from SAP "
                             f"(invoice {inv_no} / PO {header['po_number'] or '?'})")
                header["grn_number"] = new_grn
                src["grn_number"] = "SAP_MSEG"
        else:
            notes.append(f"No GRN found in SAP for invoice {inv_no or '?'} / "
                         f"PO {header['po_number'] or '?'}")

        if gr_rows:
            fill("grn_date", fmt_sap_date(gr_rows[0]["BUDAT_MKPF"]), "SAP_MSEG")
            fill("plant_code", clean(gr_rows[0]["WERKS"]), "SAP_MSEG", overwrite=True)
            header["plant_source"] = src.get("plant_code", header["plant_source"])
            g = [r for r in gr_rows if clean(r["MBLNR"]) == header["grn_number"]] or gr_rows
            header["grn_amount"] = round(sum(to_float(r["DMBTR"]) for r in g), 2)
            src["grn_amount"] = "SAP_MSEG"
            fill("sap_tax_code", clean(gr_rows[0].get("MWSKZ")), "SAP_MSEG")

        # all POs this invoice touches in SAP (any GRN) → multi-PO red flag
        sap_pos = sorted({clean(r["EBELN"]) for r in (gr_rows + inv_rows) if clean(r["EBELN"])})
        if not is_valid_po(header["po_number"]) and sap_pos:
            header["po_number"] = ""
            fill("po_number", sap_pos[0], "SAP_MSEG")

        all_pos = sorted(set(sap_pos) | set(header["po_numbers_printed"]) |
                         ({header["po_number"]} if is_valid_po(header["po_number"]) else set()))
        header["po_list"] = ", ".join(all_pos)
        if len(all_pos) > 1:
            header["multi_po_flag"] = True
            header["red_flag_reason"] = f"Invoice linked to {len(all_pos)} POs: {header['po_list']}"
            notes.append("🔴 " + header["red_flag_reason"])

        if not all_pos:
            notes.append("No PO found (vision + SAP)")
            return notes

        ekko = _q(conn, f"SELECT EBELN, LIFNR, WAERS, BEDAT, BUKRS FROM dbo.EKKO k "
                        f"WHERE k.EBELN IN :pos {_mandt('k')}", pos=all_pos, **base)
        main = next((r for r in ekko if clean(r["EBELN"]) == header["po_number"]),
                    ekko[0] if ekko else None)
        if main:
            fill("vendor_code", strip_zeros(main["LIFNR"]), "SAP_EKKO")
            if header["currency"] in ("", "INR") and clean(main["WAERS"]):
                header["currency"] = clean(main["WAERS"]); src["currency"] = "SAP_EKKO"
            fill("po_date", fmt_sap_date(main["BEDAT"]), "SAP_EKKO")
        else:
            notes.append(f"PO {header['po_number']} NOT found in EKKO")

        ekpo = _q(conn, f"""SELECT EBELN, EBELP, TXZ01, MATNR, WERKS, STAWN, MENGE, MEINS,
                                   NETPR, PEINH, NETWR, BRTWR, MWSKZ
                            FROM dbo.EKPO p
                            WHERE p.EBELN IN :pos AND ISNULL(p.LOEKZ,'') = '' {_mandt('p')}
                            ORDER BY p.EBELN, p.EBELP""", pos=all_pos, **base)
        ekpo_map = {(clean(r["EBELN"]), clean(r["EBELP"])): r for r in ekpo}
        if ekpo:
            fill("plant_code", clean(ekpo[0]["WERKS"]), "SAP_EKPO", overwrite=True)
            header["plant_source"] = src.get("plant_code", header["plant_source"])
            fill("sap_tax_code", clean(ekpo[0]["MWSKZ"]), "SAP_EKPO")
            sap_hsn = re.sub(r"[^\d]", "", clean(ekpo[0].get("STAWN")))
            if sap_hsn:
                fill("hsn_sac", sap_hsn, "SAP_EKPO")
            plants = {clean(r["WERKS"]) for r in ekpo if clean(r["WERKS"])}
            if len(plants) > 1:
                notes.append(f"PO spans multiple plants: {sorted(plants)}")

        if not lines:
            rows = [r for r in gr_rows if clean(r["MBLNR"]) == header["grn_number"]] or gr_rows
            for r in rows:
                p = ekpo_map.get((clean(r["EBELN"]), clean(r["EBELP"])), {})
                qty = to_float(r["MENGE"])
                lines.append({"item": clean(p.get("TXZ01")) or clean(r["SGTXT"]),
                              "quantity": qty, "uom": clean(r["MEINS"]),
                              "rate": round(to_float(r["DMBTR"]) / qty, 2) if qty else 0.0,
                              "hsn_sac": re.sub(r"[^\d]", "", clean(p.get("STAWN"))),
                              "amount": to_float(r["DMBTR"]),
                              "po_number": clean(r["EBELN"]), "po_item": clean(r["EBELP"]),
                              "plant_code": clean(r["WERKS"]), "source": "SAP_MSEG"})
            if not lines:
                for p in ekpo:
                    peinh = to_float(p["PEINH"]) or 1
                    lines.append({"item": clean(p["TXZ01"]), "quantity": to_float(p["MENGE"]),
                                  "uom": clean(p["MEINS"]),
                                  "rate": round(to_float(p["NETPR"]) / peinh, 2),
                                  "hsn_sac": re.sub(r"[^\d]", "", clean(p.get("STAWN"))),
                                  "amount": to_float(p["NETWR"]),
                                  "po_number": clean(p["EBELN"]), "po_item": clean(p["EBELP"]),
                                  "plant_code": clean(p["WERKS"]), "source": "SAP_EKPO"})
            if lines: notes.append(f"{len(lines)} line items filled from SAP")
        else:
            for l in lines:
                if not l["plant_code"]: l["plant_code"] = header["plant_code"]
                if not l["hsn_sac"] and ekpo:
                    l["hsn_sac"] = re.sub(r"[^\d]", "", clean(ekpo[0].get("STAWN")))

        if header["grn_amount"] and header["subtotal"]:
            if abs(header["grn_amount"] - header["subtotal"]) > header["subtotal"] * AMOUNT_TOLERANCE:
                notes.append(f"Amount gap: invoice subtotal {header['subtotal']} "
                             f"vs GRN value {header['grn_amount']}")
        fill("subtotal", header["grn_amount"], "SAP_MSEG")

    return notes


# ── Validation + GST tax-code mapping ────────────────────────

def compute_gst(header: dict) -> None:
    sub, cgst, sgst, igst = header["subtotal"], header["cgst"], header["sgst"], header["igst"]
    tax_total = round(cgst + sgst + igst, 2) or header["total_tax"]

    if igst > 0 and (cgst + sgst) == 0:   nature = "IGST"
    elif (cgst + sgst) > 0:               nature = "CGST_SGST"
    elif tax_total == 0:                  nature = "NIL"
    else:                                 nature = "IGST" if igst else "CGST_SGST"

    pct = comp = None
    if sub > 0 and tax_total > 0:
        pct = snap_gst_rate(round(tax_total / sub * 100, 2))
        if nature == "CGST_SGST":
            comp = snap_gst_rate(round(max(cgst, sgst) / sub * 100, 2), component=True) \
                   if (cgst or sgst) else round(pct / 2, 2)
        else:
            comp = pct
    elif header["gst_rate_printed"] > 0:
        r = snap_gst_rate(header["gst_rate_printed"])
        if nature == "CGST_SGST" and r and r <= 14:
            comp, pct = r, snap_gst_rate(r * 2)
        else:
            pct = comp = r
    elif nature == "NIL":
        pct = comp = 0.0

    header["gst_percent"] = pct
    header["gst_percent_component"] = comp
    header["gst_nature"] = nature


def apply_tax_code(Session, header: dict, notes: list) -> None:
    compute_gst(header)
    pct, comp, nature = header["gst_percent"], header["gst_percent_component"], header["gst_nature"]
    if pct is None:
        notes.append("GST% could not be computed — tax code not mapped")
        return
    shown = f"{comp}%+{comp}% (total {pct}%)" if nature == "CGST_SGST" else f"{pct}%"
    row = pick_tax_code(Session, pct, comp, nature)
    if row:
        header["gst_tax_code"] = row.tax_code
        header["gst_tax_code_desc"] = row.description
        header["field_sources"]["gst_tax_code"] = "GST_MASTER"
        header["field_sources"]["gst_percent"] = "CALCULATED"
        if not header["tax_code"]:
            header["tax_code"] = row.tax_code
        notes.append(f"GST {shown} {nature} → tax code {row.tax_code} ({row.description})")
    else:
        notes.append(f"No input tax code in GST master for {shown} {nature}")

    if header["sap_tax_code"] and header["gst_tax_code"]:
        header["tax_code_match"] = header["sap_tax_code"].upper() == header["gst_tax_code"].upper()
        if not header["tax_code_match"]:
            notes.append(f"Tax-code mismatch: invoice→{header['gst_tax_code']} "
                         f"vs PO/GR→{header['sap_tax_code']}")


GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z]\d[A-Z][A-Z0-9]$")

MANDATORY_FIELDS = [("invoice_number", "Invoice number"), ("invoice_date", "Invoice date"),
                    ("vendor", "Vendor"), ("vendor_code", "Vendor code"),
                    ("po_number", "PO number"), ("grn_number", "GRN number"),
                    ("plant_code", "Plant"), ("hsn_sac", "HSN/SAC"),
                    ("gst_tax_code", "GST tax code")]

def validate(header: dict, lines: list, sap_on: bool) -> None:
    missing, mismatches, doubts = [], [], []

    for key, label in MANDATORY_FIELDS:
        if not header.get(key):
            missing.append(f"{label} missing")
    if header["po_number"] and not is_valid_po(header["po_number"]):
        mismatches.append(f"PO not a valid 10-digit number: '{header['po_number']}'")
    if header["grn_number"] and not grn_is_valid(header["grn_number"]):
        mismatches.append(f"GRN not a valid 10-digit number: '{header['grn_number']}'")
    if header["invoice_date"] and not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", header["invoice_date"]):
        doubts.append(f"Invoice date not parsed: {header['invoice_date']}")
    if header["hsn_sac"] and not re.fullmatch(r"\d{4}|\d{6}|\d{8}", header["hsn_sac"]):
        doubts.append(f"HSN not 4/6/8 digits: {header['hsn_sac']}")
    if header["gstin"] and not GSTIN_RE.match(header["gstin"]):
        doubts.append(f"Vendor GSTIN format looks wrong: {header['gstin']}")

    sub, cgst, sgst, igst = header["subtotal"], header["cgst"], header["sgst"], header["igst"]
    tax, total = header["total_tax"], header["total"]
    if total <= 0: missing.append("Invoice total is zero")
    if sub <= 0:   missing.append("Taxable value is zero")
    if (cgst + sgst + igst) > 0 and abs((cgst + sgst + igst) - tax) > ROUND_TOLERANCE:
        mismatches.append(f"CGST+SGST+IGST {round(cgst+sgst+igst,2)} != total tax {tax}")
    if abs((sub + tax) - total) > ROUND_TOLERANCE:
        mismatches.append(f"Taxable {sub} + tax {tax} != total {total}")
    if cgst > 0 and abs(cgst - sgst) > 0.5:
        mismatches.append(f"CGST {cgst} != SGST {sgst}")
    if cgst > 0 and igst > 0:
        mismatches.append("Both CGST/SGST and IGST charged on one invoice")
    if header["gst_percent"] is not None and header["gst_percent"] not in GST_STANDARD_RATES:
        doubts.append(f"Non-standard GST rate {header['gst_percent']}%")

    if not lines:
        doubts.append("No line items extracted")
    else:
        line_sum = round(sum(l["amount"] for l in lines), 2)
        if sub > 0 and abs(line_sum - sub) > max(ROUND_TOLERANCE, sub * AMOUNT_TOLERANCE):
            doubts.append(f"Line items total {line_sum} != taxable value {sub}")

    if sap_on:
        mismatches += header.get("sap_mismatches", [])
        if not header["po_date"]:
            mismatches.append(f"PO {header['po_number'] or '?'} not found in SAP (EKKO)")
        if not header["grn_in_sap"]:
            mismatches.append(f"GRN {header['grn_number'] or '?'} not found in SAP (MSEG) "
                              f"by stamp, invoice no, PO or line items")
        if header["grn_amount"] and sub and \
           abs(header["grn_amount"] - sub) > sub * AMOUNT_TOLERANCE:
            mismatches.append(f"GRN value {header['grn_amount']} != invoice taxable value {sub}")
        if header["tax_code_match"] is False:
            mismatches.append(f"Tax code {header['gst_tax_code']} differs from PO/GR "
                              f"{header['sap_tax_code']}")
    else:
        doubts.append("SAP not checked — status cannot be confirmed")

    if header["confidence"] and header["confidence"] < 60:
        doubts.append(f"Low AI confidence {header['confidence']}%")

    if header["multi_po_flag"]:
        status = "RED_FLAG"
    elif missing or mismatches or doubts:
        status = "REVIEW"
    else:
        status = "MATCHED"

    header["validation_status"] = status
    header["validation_errors"] = {
        "status": status,
        "red_flag": [header["red_flag_reason"]] if header["multi_po_flag"] else [],
        "missing": missing, "mismatch": mismatches, "doubt": doubts,
    }


# ── PG push ──────────────────────────────────────────────────

def norm_key(s: str) -> str:
    return re.sub(r"\s+", "", (s or "")).upper()

def make_dedupe_key(h: dict):
    inv = norm_key(h["invoice_number"])
    if not inv:
        return None
    po = norm_key(h["po_number"]) or norm_key(h["vendor"])
    return f"{inv}::{po}"

def is_duplicate(session, h):
    key = make_dedupe_key(h)
    if not key:
        return False, "No invoice number — duplicate check skipped"
    ex = session.scalar(select(Invoice).where(Invoice.dedupe_key == key))
    return (True, f"DUPLICATE of id={ex.id} (same invoice+PO already in invoices table)") \
           if ex else (False, "New — OK to push")

def push_to_db(session, h: dict, lines: list, notes: list) -> int:
    status = h["validation_status"]
    problems = (h["validation_errors"].get("missing", []) +
                h["validation_errors"].get("mismatch", []) +
                h["validation_errors"].get("red_flag", []))
    inv = Invoice(
        filename=h["filename"], vendor=h["vendor"], invoice_number=h["invoice_number"],
        invoice_date=h["invoice_date"], po_number=h["po_number"] or None, gstin=h["gstin"],
        hsn_sac=h["hsn_sac"], vendor_code=h["vendor_code"] or None, tax_code=h["tax_code"],
        business_place=h["plant_code"], currency=h["currency"], subtotal=h["subtotal"],
        cgst=h["cgst"], sgst=h["sgst"], igst=h["igst"], tax=h["total_tax"], total=h["total"],
        confidence=h["confidence"] / 100, grn_number=h["grn_number"],
        handwritten_references=h["handwritten_references"],
        plant_code=h["plant_code"] or None, plant_source=h["plant_source"] or None,
        po_date=h["po_date"], grn_date=h["grn_date"], grn_amount=h["grn_amount"],
        po_list=h["po_list"], multi_po_flag=h["multi_po_flag"],
        red_flag_reason=h["red_flag_reason"] or None,
        gst_percent=h["gst_percent"], gst_percent_component=h["gst_percent_component"],
        gst_nature=h["gst_nature"] or None,
        gst_tax_code=h["gst_tax_code"] or None, gst_tax_code_desc=h["gst_tax_code_desc"] or None,
        sap_tax_code=h["sap_tax_code"] or None, tax_code_match=h["tax_code_match"],
        validation_status=h["validation_status"], validation_errors=h["validation_errors"],
        field_sources=h["field_sources"], status=status,
        error_message="; ".join(problems) or None,
        dedupe_key=make_dedupe_key(h),
    )
    session.add(inv); session.flush()
    for l in lines:
        session.add(InvoiceLine(
            invoice_id=inv.id, item=l["item"][:255], quantity=l["quantity"], rate=l["rate"],
            tax_code=h["gst_tax_code"] or "", amount=l["amount"], hsn_sac=l["hsn_sac"] or None,
            po_number=l["po_number"] or None, po_item=l["po_item"] or None,
            plant_code=l["plant_code"] or h["plant_code"] or None,
            uom=l["uom"] or None, source=l["source"]))
    if h["invoice_number"]:
        session.execute(pg_insert(InvoiceMaster).values(
            invoice_number=h["invoice_number"], vendor=h["vendor"], vendor_code=h["vendor_code"],
            po_number=h["po_number"], first_seen_date=now_utc(), invoice_id=inv.id,
        ).on_conflict_do_nothing(index_elements=["invoice_number"]))
    action = {"RED_FLAG": "RED_FLAG_MULTI_PO", "REVIEW": "NEEDS_REVIEW",
              "MATCHED": "MATCHED_READY_FOR_MIRO"}[status]
    session.add(AuditLog(invoice_id=inv.id, action=action,
                         detail={"file": h["filename"], "model": MODEL_VLM, "sap_notes": notes,
                                 "field_sources": h["field_sources"], "po_list": h["po_list"],
                                 "validation": h["validation_errors"],
                                 "gst": {"percent": h["gst_percent"], "nature": h["gst_nature"],
                                         "code": h["gst_tax_code"], "po_code": h["sap_tax_code"]}}))
    session.commit()
    return inv.id


# ── Excel ────────────────────────────────────────────────────

RED   = PatternFill("solid", fgColor="FF9999")
AMBER = PatternFill("solid", fgColor="FFE0A3")
GREEN = PatternFill("solid", fgColor="C6EFCE")
ALT   = PatternFill("solid", fgColor="EBF3FB")
SAPF  = PatternFill("solid", fgColor="FFF2CC")
STATUS_FILL = {"MATCHED": GREEN, "REVIEW": AMBER, "RED_FLAG": RED}

def create_excel(h, lines, notes, out_folder):
    Path(out_folder).mkdir(parents=True, exist_ok=True)
    name = re.sub(r'[<>:"/\\|?*]', "_", h["invoice_number"] or Path(h["filename"]).stem).strip(" ._")
    path = Path(out_folder) / f"{name}.xlsx"
    wb = Workbook()

    def hdr(ws, color):
        for c in ws[1]:
            c.fill = PatternFill("solid", fgColor=color)
            c.font = Font(bold=True, color="FFFFFF")
            c.alignment = Alignment(horizontal="center", wrap_text=True)

    ws = wb.active; ws.title = "Invoice Data"
    ws.append(["Field", "Value", "Source"]); hdr(ws, "1F4E79")
    rows = [("File Name","filename"),("STATUS","validation_status"),
            ("Vendor","vendor"),("Invoice Number","invoice_number"),("Invoice Date","invoice_date"),
            ("PO Number","po_number"),("All POs","po_list"),("Multi-PO Red Flag","multi_po_flag"),
            ("Red Flag Reason","red_flag_reason"),("PO Date","po_date"),
            ("Vendor GSTIN","gstin"),("Vendor Code","vendor_code"),
            ("Plant","plant_code"),("Plant Source","plant_source"),
            ("HSN / SAC","hsn_sac"),("Currency","currency"),
            ("Subtotal","subtotal"),("CGST","cgst"),("SGST","sgst"),("IGST","igst"),
            ("Total Tax","total_tax"),("Invoice Total","total"),
            ("GST % (total)","gst_percent"),("GST % (each half)","gst_percent_component"),
            ("GST Type","gst_nature"),
            ("GST Tax Code","gst_tax_code"),("Tax Code Description","gst_tax_code_desc"),
            ("PO/GR Tax Code (SAP)","sap_tax_code"),("Tax Code Match","tax_code_match"),
            ("GRN Number","grn_number"),("GRN Date","grn_date"),("GRN Value (SAP)","grn_amount"),
            ("Handwritten Refs","handwritten_references"),("AI Confidence %","confidence")]
    for i, (label, key) in enumerate(rows):
        val = h.get(key)
        val = "YES 🔴" if (key == "multi_po_flag" and val is True) else \
              ("YES" if val is True else ("NO" if val is False else val))
        source = h["field_sources"].get(key, "")
        ws.append([label, val, source])
        r = ws[i + 2]
        bad = (key in ("multi_po_flag", "red_flag_reason", "po_list") and h["multi_po_flag"]) or \
              (key == "tax_code_match" and h["tax_code_match"] is False)
        if key == "validation_status":
            fill_ = STATUS_FILL.get(h["validation_status"], AMBER)
            for c in r: c.fill = fill_
            r[1].font = Font(bold=True)
        elif bad:
            for c in r: c.fill = RED
        elif str(source).startswith(("SAP", "GST_MASTER", "CALCULATED")):
            for c in r: c.fill = SAPF
        elif i % 2:
            for c in r: c.fill = ALT
    ws.column_dimensions["A"].width = 24; ws.column_dimensions["B"].width = 60
    ws.column_dimensions["C"].width = 14; ws.freeze_panes = "A2"

    ws2 = wb.create_sheet("Line Items")
    ws2.append(["Sl","Item","Qty","UoM","Rate","HSN/SAC","Amount","PO","PO Item","Plant","Source"])
    hdr(ws2, "145A32")
    for i, l in enumerate(lines, 1):
        ws2.append([i, l["item"], l["quantity"], l["uom"], l["rate"], l["hsn_sac"], l["amount"],
                    l["po_number"], l["po_item"], l["plant_code"], l["source"]])
        if l["source"] != "VISION":
            for c in ws2[i + 1]: c.fill = SAPF
    for i, w in enumerate([6, 50, 10, 8, 12, 12, 14, 14, 9, 9, 12], 1):
        ws2.column_dimensions[get_column_letter(i)].width = w

    ws3 = wb.create_sheet("Status Check")
    ws3.append(["Type", "Message"]); hdr(ws3, "7F1D1D")
    ve = h["validation_errors"]
    for kind, items, fill_ in (("RED FLAG", ve.get("red_flag", []), RED),
                               ("MISSING",  ve.get("missing", []),  AMBER),
                               ("MISMATCH", ve.get("mismatch", []), RED),
                               ("DOUBT",    ve.get("doubt", []),    AMBER)):
        for m in items:
            ws3.append([kind, m])
            for c in ws3[ws3.max_row]: c.fill = fill_
    if ws3.max_row == 1:
        ws3.append(["MATCHED", "All fields filled and matched with SAP — ready for MIRO"])
        for c in ws3[2]: c.fill = GREEN
    ws3.column_dimensions["A"].width = 12; ws3.column_dimensions["B"].width = 100

    ws4 = wb.create_sheet("SAP Check")
    ws4.append(["Notes"]); hdr(ws4, "7F6000")
    for n in notes or ["-"]:
        ws4.append([n])
        if n.startswith(("🔴", "MISMATCH", "Amount gap", "Tax-code mismatch")):
            ws4[ws4.max_row][0].fill = RED
    ws4.column_dimensions["A"].width = 110
    wb.save(path)
    print(f"  📄 Excel: {path.name}")


# ── Main ─────────────────────────────────────────────────────

def main():
    folder, out = Path(PDF_FOLDER), Path(OUTPUT_FOLDER)
    if not folder.exists(): print(f"ERROR: folder not found {folder}"); sys.exit(1)
    pdfs = sorted({p.name.lower(): p for p in folder.glob("*") if p.suffix.lower() == ".pdf"}.values())
    if not pdfs: print("ERROR: No PDFs found"); sys.exit(1)

    print("=" * 68)
    print("KTFL MIRO Extractor — Vision + SAP + Validation + GST mapping → PostgreSQL")
    print(f"PDFs: {len(pdfs)} | Model: {MODEL_VLM}")
    print("=" * 68)

    _, Session = setup_pg()
    load_gst_master(Session, GST_RATE_XLSX)
    try:
        sap = setup_sap()
    except Exception as e:
        print(f"  ⚠️  SAP connect failed ({e}) — continuing vision-only"); sap = None

    r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=10, trust_env=False)
    if MODEL_VLM not in [m["name"] for m in r.json().get("models", [])]:
        print(f"ERROR: {MODEL_VLM} not installed"); sys.exit(1)
    print("  Ollama OK ✅")

    llm = create_llm()
    stats = {"MATCHED": 0, "REVIEW": 0, "RED_FLAG": 0, "duplicate": 0, "error": 0}

    for idx, pdf in enumerate(pdfs, 1):
        t0 = time.time()
        print(f"\n[{idx}/{len(pdfs)}] {pdf.name}")
        try:
            images = pdf_images(pdf)
            print("  Main extraction...")
            header, lines = normalize(invoke_main(llm, images, pdf_text(pdf)), pdf.name)

            print("  GRN pass...")
            header["grn_number"] = clean_grn(invoke_grn(llm, images))
            if header["grn_number"]:
                header["field_sources"]["grn_number"] = "VISION"

            print("  SAP cross-check / fallback...")
            try:
                notes = sap_enrich(sap, header, lines)
            except Exception as e:
                notes = [f"SAP lookup error: {e}"]

            apply_tax_code(Session, header, notes)
            validate(header, lines, sap_on=sap is not None)
            for n in notes: print(f"    • {n}")

            st = header["validation_status"]
            mark = {"MATCHED": "✅ MATCHED", "REVIEW": "⚠️  REVIEW", "RED_FLAG": "🔴 RED FLAG"}[st]
            gst_show = (f"{header['gst_percent_component']}%+{header['gst_percent_component']}%"
                        if header["gst_nature"] == "CGST_SGST" else f"{header['gst_percent']}%")
            print(f"  Invoice {header['invoice_number'] or '?'} | PO {header['po_number'] or '?'} "
                  f"| GRN {header['grn_number'] or '-'} | Plant {header['plant_code'] or '-'} "
                  f"| HSN {header['hsn_sac'] or '-'} | GST {gst_show} → "
                  f"{header['gst_tax_code'] or '-'} | Total {header['total']}")
            print(f"  STATUS: {mark}")
            ve = header["validation_errors"]
            for m in ve["red_flag"]: print(f"    🔴 {m}")
            for m in ve["missing"]:  print(f"    ▫️  MISSING  {m}")
            for m in ve["mismatch"]: print(f"    ❌ MISMATCH {m}")
            for m in ve["doubt"]:    print(f"    ⚠️  DOUBT    {m}")

            with Session() as s:
                dup, reason = is_duplicate(s, header)
                if dup:
                    print(f"  ⚠️  SKIPPED — {reason}")
                    s.add(AuditLog(action="DUPLICATE_SKIPPED",
                                   detail={"file": pdf.name, "invoice": header["invoice_number"],
                                           "reason": reason}))
                    s.commit(); stats["duplicate"] += 1
                else:
                    try:
                        inv_id = push_to_db(s, header, lines, notes)
                        stats[st] += 1
                        print(f"  → PG id={inv_id} status={st} ({round(time.time()-t0,1)}s)")
                    except Exception as e:
                        s.rollback()
                        if "ux_invoices_dedupe_key" in str(e) or "dedupe_key" in str(e):
                            print("  ⚠️  SKIPPED — duplicate caught at DB level (unique index)")
                            s.add(AuditLog(action="DUPLICATE_SKIPPED",
                                           detail={"file": pdf.name, "invoice": header["invoice_number"],
                                                   "reason": "unique index ux_invoices_dedupe_key"}))
                            s.commit(); stats["duplicate"] += 1
                        else:
                            raise
            create_excel(header, lines, notes, out)

        except Exception as e:
            print(f"  ❌ ERROR: {e}")
            stats["error"] += 1
            try:
                with Session() as s:
                    s.add(AuditLog(action="EXTRACTION_ERROR",
                                   detail={"file": pdf.name, "error": str(e)}))
                    s.commit()
            except Exception: pass

    print("\n" + "=" * 68)
    print(f"  ✅ MATCHED : {stats['MATCHED']}   (all fields filled & matched with SAP)")
    print(f"  ⚠️  REVIEW  : {stats['REVIEW']}   (something missing / mismatch / doubt)")
    print(f"  🔴 RED FLAG: {stats['RED_FLAG']}   (invoice has multiple POs)")
    print(f"  ⏭️  Duplicate: {stats['duplicate']}   💥 Errors: {stats['error']}")
    print("=" * 68)


if __name__ == "__main__":
    main()
