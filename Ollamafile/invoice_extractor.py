"""
KTFL Invoice Extractor — FINAL
================================
Model  : qwen3-vl:32b (single call per PDF)
Target : ~60-90s per scanned PDF, ~20-40s for text PDF
Strategy:
  - ONE Ollama call extracts invoice data + GRN stamp together
  - Text PDF → text prompt only (no vision) → fastest path
  - Scanned PDF → page 1 image only (not 2 pages)
  - No LangChain overhead
  - think:false + streaming
"""

import os, base64, json, re, sys, time, threading
from datetime import datetime, timezone
from pathlib import Path

for _k in ["HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","http_proxy","https_proxy","all_proxy"]:
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "localhost,127.0.0.1"

import fitz
import httpx
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter
from sqlalchemy import create_engine, text, select, String, Text, Float, DateTime, ForeignKey, JSON, Integer
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import insert as pg_insert

# ─────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────
PDF_FOLDER    = r"C:\Users\ckts00126\Desktop\KTFL-EXTRACTION\Input-pdf"
OUTPUT_FOLDER = r"C:\Users\ckts00126\Desktop\KTFL-EXTRACTION\Output-Excel"
OLLAMA_URL    = "http://127.0.0.1:11434"
OLLAMA_MODEL  = "qwen3-vl:32b"
DB_URL        = "postgresql+psycopg://appuser:appuser123@10.7.11.22:5432/miro_invoice"

IMAGE_ZOOM   = 1.2
NUM_CTX      = 16384
NUM_PREDICT  = 8192
TIMEOUT      = 600
# ─────────────────────────────────────────


# ══════════════════════════════════════════
#  DATABASE MODELS
# ══════════════════════════════════════════

def utcnow(): return datetime.now(timezone.utc)

class Base(DeclarativeBase): pass

class Invoice(Base):
    __tablename__ = "invoices"
    id             : Mapped[int]       = mapped_column(Integer, primary_key=True)
    filename       : Mapped[str]       = mapped_column(String(255))
    vendor         : Mapped[str|None]  = mapped_column(String(255))
    invoice_number : Mapped[str|None]  = mapped_column(String(100), index=True)
    invoice_date   : Mapped[str|None]  = mapped_column(String(30))
    po_number      : Mapped[str|None]  = mapped_column(String(100))
    gstin          : Mapped[str|None]  = mapped_column(String(50))
    hsn_sac        : Mapped[str|None]  = mapped_column(String(50))
    vendor_code    : Mapped[str|None]  = mapped_column(String(50))
    tax_code       : Mapped[str|None]  = mapped_column(String(30))
    business_place : Mapped[str|None]  = mapped_column(String(100))
    currency       : Mapped[str]       = mapped_column(String(10), default="INR")
    subtotal       : Mapped[float]     = mapped_column(Float, default=0)
    cgst           : Mapped[float]     = mapped_column(Float, default=0)
    sgst           : Mapped[float]     = mapped_column(Float, default=0)
    igst           : Mapped[float]     = mapped_column(Float, default=0)
    tax            : Mapped[float]     = mapped_column(Float, default=0)
    total          : Mapped[float]     = mapped_column(Float, default=0)
    confidence     : Mapped[float]     = mapped_column(Float, default=0)
    grn_number     : Mapped[str|None]  = mapped_column(String(100))
    scroll_number  : Mapped[str|None]  = mapped_column(String(100))
    handwritten_references: Mapped[str|None] = mapped_column(Text)
    status         : Mapped[str]       = mapped_column(String(40), default="REVIEW", index=True)
    error_message  : Mapped[str|None]  = mapped_column(Text)
    created_at     : Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at     : Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    lines = relationship("InvoiceLine", back_populates="invoice", cascade="all, delete-orphan")

class InvoiceLine(Base):
    __tablename__ = "invoice_lines"
    id         : Mapped[int]   = mapped_column(primary_key=True)
    invoice_id : Mapped[int]   = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"))
    item       : Mapped[str]   = mapped_column(String(500))
    quantity   : Mapped[float] = mapped_column(Float, default=0)
    rate       : Mapped[float] = mapped_column(Float, default=0)
    hsn_sac    : Mapped[str]   = mapped_column(String(30), default="")
    amount     : Mapped[float] = mapped_column(Float, default=0)
    invoice = relationship("Invoice", back_populates="lines")

class InvoiceMaster(Base):
    __tablename__ = "invoice_master"
    id             : Mapped[int]      = mapped_column(primary_key=True)
    invoice_number : Mapped[str]      = mapped_column(String(100), unique=True)
    vendor         : Mapped[str|None] = mapped_column(String(255))
    vendor_code    : Mapped[str|None] = mapped_column(String(50))
    po_number      : Mapped[str|None] = mapped_column(String(100))
    first_seen_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    invoice_id     : Mapped[int|None] = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"))

class AuditLog(Base):
    __tablename__ = "audit_logs"
    id         : Mapped[int]      = mapped_column(primary_key=True)
    invoice_id : Mapped[int|None] = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"))
    action     : Mapped[str]      = mapped_column(String(100))
    detail     : Mapped[dict]     = mapped_column(JSON, default=dict)
    created_at : Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ══════════════════════════════════════════
#  DB SETUP
# ══════════════════════════════════════════

def setup_db():
    engine  = create_engine(DB_URL, pool_pre_ping=True)
    Session = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    for sql in [
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS gstin VARCHAR(50)",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS hsn_sac VARCHAR(50)",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS vendor_code VARCHAR(50)",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS cgst FLOAT DEFAULT 0",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS sgst FLOAT DEFAULT 0",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS igst FLOAT DEFAULT 0",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS grn_number VARCHAR(100)",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS scroll_number VARCHAR(100)",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS handwritten_references TEXT",
        "ALTER TABLE invoice_lines ADD COLUMN IF NOT EXISTS rate FLOAT DEFAULT 0",
        "ALTER TABLE invoice_lines ADD COLUMN IF NOT EXISTS hsn_sac VARCHAR(30) DEFAULT ''",
    ]:
        try:
            with engine.connect() as c:
                c.execute(text(sql)); c.commit()
        except: pass
    return engine, Session


# ══════════════════════════════════════════
#  SINGLE COMBINED PROMPT
#  (invoice fields + GRN stamp in ONE call)
# ══════════════════════════════════════════

COMBINED_PROMPT = """You are an OCR engine for Indian GST invoices. Read ALL text in the image carefully.

=== PART A: INVOICE FIELDS ===

VENDOR (seller at top of invoice, NOT Kalyani Technoforge / KTFL):
  Look at the TOP-LEFT letterhead. Examples: AB Industries, DSK Engineers, Bharat Metals

KEY FIELDS:
  invoice_number → value next to "Invoice No" / "Invoice No."
  invoice_date   → value next to "Dated" / "Date"
  po_number      → value next to "Buyer's Order No" / "PO No" (digits only, remove PO- prefix)
  gstin          → vendor 15-char GSTIN (e.g. 27FOCPK0952N1ZL)
  hsn_sac        → HSN code from line items table (6-8 digits)
  vendor_code    → number next to "Vendor Code" in buyer address (e.g. 70042)
  subtotal       → taxable value before tax
  cgst           → CGST amount
  sgst           → SGST amount
  igst           → IGST amount
  total          → Grand Total / final payable amount
  lines          → each goods row in the table (NOT tax rows, NOT total row)

=== PART B: RECEIVING STAMP (bottom-left corner) ===

The stamp from Kalyani Technoforge has this structure:
  ┌─────────────┬──────┬───────┬──────┐
  │ SCROLL NO   │ DATE │ TIME  │ SIGN │
  │ P002049285  │ ...  │ ...   │ ...  │
  ├─────────────┼──────┴───────┴──────┤
  │ JRN NO      │ DATE │ GRN PREPARED │
  │ 50-35 6087  │ ...  │ ...          │
  └─────────────┴──────┴──────────────┘

  scroll_number → number in "SCROLL NO" row (starts with P00...)
  grn_number    → number in "JRN NO" or "GRN NO" row (starts with 50...)
                  If JRN NO row is blank → grn_number = ""
                  NEVER put SCROLL NO value into grn_number

IMPORTANT:
  - SCROLL NO ≠ GRN NO (they are different fields in different rows)
  - grn_number comes ONLY from the JRN NO / GRN NO row

Return ONLY this JSON (no explanation, no markdown):
{
  "vendor": "",
  "invoice_number": "",
  "invoice_date": "",
  "po_number": "",
  "gstin": "",
  "hsn_sac": "",
  "vendor_code": "",
  "currency": "INR",
  "subtotal": 0.0,
  "cgst": 0.0,
  "sgst": 0.0,
  "igst": 0.0,
  "total": 0.0,
  "grn_number": "",
  "scroll_number": "",
  "confidence": 0.0,
  "lines": [
    {"item": "", "quantity": 0.0, "rate": 0.0, "hsn_sac": "", "amount": 0.0}
  ]
}"""

COMBINED_PROMPT_TEXT = """You are extracting data from an Indian GST invoice.

=== EMBEDDED TEXT FROM PDF ===
{TEXT}
=== END TEXT ===

Extract ALL fields. Return ONLY this JSON:

RULES:
  vendor         → company name at top-left (NOT Kalyani Technoforge / KTFL)
  invoice_number → after "Invoice No"
  invoice_date   → after "Dated" or "Date"
  po_number      → after "Buyer's Order No" or "PO No" (digits only)
  gstin          → vendor 15-char GSTIN
  hsn_sac        → HSN/SAC code from items
  vendor_code    → after "Vendor Code" (e.g. 70042)
  subtotal       → taxable value before tax
  cgst/sgst/igst → tax amounts
  total          → Grand Total
  scroll_number  → after "SCROLL NO" in stamp (if in text)
  grn_number     → after "JRN NO" or "GRN NO" in stamp (NOT scroll number)
  confidence     → 0.9

{
  "vendor": "",
  "invoice_number": "",
  "invoice_date": "",
  "po_number": "",
  "gstin": "",
  "hsn_sac": "",
  "vendor_code": "",
  "currency": "INR",
  "subtotal": 0.0,
  "cgst": 0.0,
  "sgst": 0.0,
  "igst": 0.0,
  "total": 0.0,
  "grn_number": "",
  "scroll_number": "",
  "confidence": 0.9,
  "lines": [
    {"item": "", "quantity": 0.0, "rate": 0.0, "hsn_sac": "", "amount": 0.0}
  ]
}"""


# ══════════════════════════════════════════
#  PROGRESS TICKER
# ══════════════════════════════════════════

class Ticker:
    def __init__(self, label):
        self.label = label
        self._stop = False

    def _run(self):
        t0 = time.time()
        while not self._stop:
            print(f"\r    ⏳ {self.label} [{int(time.time()-t0)}s]...", end="", flush=True)
            time.sleep(1)

    def start(self):
        self._stop = False
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop = True
        time.sleep(1.1)
        print(f"\r    ✅ {self.label} done                    ")


# ══════════════════════════════════════════
#  OLLAMA CALL — streaming /api/generate
# ══════════════════════════════════════════

def ollama_call(prompt: str, images: list | None, num_predict: int = NUM_PREDICT) -> str:
    payload = {
        "model":   OLLAMA_MODEL,
        "prompt":  prompt,
        "stream":  True,
        "think":   False,
        "options": {
            "temperature": 0,
            "num_ctx":     NUM_CTX,
            "num_predict": num_predict,
        },
    }
    if images:
        payload["images"] = images

    tokens = []
    truncated = False

    with httpx.stream("POST", f"{OLLAMA_URL}/api/generate",
                      json=payload, timeout=TIMEOUT, trust_env=False) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line.strip(): continue
            try: chunk = json.loads(line)
            except: continue
            t = chunk.get("response", "")
            if t: tokens.append(t)
            if chunk.get("done"):
                if chunk.get("done_reason") == "length":
                    truncated = True
                break

    raw = "".join(tokens).strip()

    # Auto-repair truncated JSON
    if truncated and raw:
        opens = raw.count("{") - raw.count("}")
        if opens > 0:
            raw += "}" * opens

    return raw


def parse_json(raw: str) -> dict:
    # Strip thinking blocks
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL | re.IGNORECASE).strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw).strip()
    try: return json.loads(raw)
    except: pass
    s, e = raw.find("{"), raw.rfind("}")
    if s >= 0 and e > s:
        try: return json.loads(raw[s:e+1])
        except: pass
    raise ValueError(f"No JSON found. Response (first 400):\n{raw[:400]}")


# ══════════════════════════════════════════
#  PDF UTILITIES
# ══════════════════════════════════════════

def page_to_b64(path: Path, idx: int) -> str:
    doc = fitz.open(str(path))
    mat = fitz.Matrix(IMAGE_ZOOM, IMAGE_ZOOM)
    pix = doc[idx].get_pixmap(matrix=mat, alpha=False)
    doc.close()
    return base64.b64encode(pix.tobytes("png")).decode()

def get_page_texts(path: Path) -> list[str]:
    doc = fitz.open(str(path))
    out = [pg.get_text("text").strip() for pg in doc]
    doc.close()
    return out

def page_count(path: Path) -> int:
    doc = fitz.open(str(path))
    n = doc.page_count
    doc.close()
    return n


# ══════════════════════════════════════════
#  EXTRACTION — ONE CALL
# ══════════════════════════════════════════

def extract(path: Path) -> dict:
    """
    Single Ollama call: extracts invoice fields + GRN stamp together.
    Text PDF → text prompt (fastest).
    Scanned PDF → page 1 image only.
    """
    texts = get_page_texts(path)
    full_text = "\n\n".join(t for t in texts[:2] if len(t) > 50)
    is_scanned = len(full_text.strip()) < 50

    tick = Ticker("Extracting (invoice + GRN)")
    tick.start()
    try:
        if not is_scanned:
            # TEXT PATH — fastest, no image
            prompt = COMBINED_PROMPT_TEXT.replace("{TEXT}", full_text[:3000])
            raw = ollama_call(prompt, images=None)
        else:
            # VISION PATH — page 1 only (stamp is on page 1)
            img = page_to_b64(path, 0)
            raw = ollama_call(COMBINED_PROMPT, images=[img])
    finally:
        tick.stop()

    if not raw:
        raise ValueError(
            "Empty response from Ollama.\n"
            "Fix: run  ollama run qwen3-vl:32b  once to warm up, then retry."
        )

    return parse_json(raw), is_scanned


# ══════════════════════════════════════════
#  DATA CLEANING
# ══════════════════════════════════════════

def to_f(v) -> float:
    if v is None: return 0.0
    if isinstance(v, (int, float)): return float(v)
    s = re.sub(r"[^\d.\-]", "", str(v).replace(",", ""))
    try: return float(s)
    except: return 0.0

def clean(v) -> str:
    return str(v).strip() if v else ""

def clean_po(v) -> str:
    s = clean(v)
    return re.sub(r"^(PO|P\.O\.?|P/O)[\-/\s]*", "", s, flags=re.IGNORECASE).strip()

def clean_grn(v) -> str:
    s = clean(v)
    if not s: return s
    # "50-35 6087" → remove spaces/dashes between digit groups
    # dash between digits → "00" only if it's a separator (e.g. 50-35 → 5035)
    s = re.sub(r"(\d)\s*-\s*(\d)", r"\g<1>\2", s)   # remove dash: 50-35 → 5035
    s = re.sub(r"\s", "", s)
    OCR = {'S':'5','s':'5','O':'0','o':'0','I':'1','l':'1',
           'Z':'2','z':'2','B':'8','G':'6','T':'7','A':'4','q':'9'}
    return "".join(OCR.get(c, c) for c in s)

def fmt_date(raw: str) -> str:
    if not raw: return ""
    raw = raw.strip()
    MN = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
          "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}
    for fmt in ["%d.%m.%Y","%d/%m/%Y","%Y-%m-%d","%d-%m-%Y",
                "%d-%m-%y","%d/%m/%y","%d %b %Y","%d %B %Y",
                "%b %d, %Y","%d-%b-%y","%d/%b/%Y"]:
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.year < 100: dt = dt.replace(year=dt.year+2000)
            return dt.strftime("%d.%m.%Y")
        except: pass
    m = re.match(r"(\d{1,2})[-/\s]([A-Za-z]+)[-/\s](\d{2,4})", raw)
    if m:
        d, mn, y = int(m.group(1)), m.group(2).lower()[:3], int(m.group(3))
        if y < 100: y += 2000
        mo = MN.get(mn, 0)
        if mo: return f"{d:02d}.{mo:02d}.{y}"
    return raw

def normalize(data: dict, fname: str) -> tuple[dict, list]:
    sub  = to_f(data.get("subtotal"))
    cgst = to_f(data.get("cgst"))
    sgst = to_f(data.get("sgst"))
    igst = to_f(data.get("igst"))
    ttax = round(cgst + sgst + igst, 2)
    tot  = to_f(data.get("total"))
    if tot == 0: tot = round(sub + ttax, 2)
    if sub == 0 and tot > 0: sub = round(tot - ttax, 2)

    hsn_top = clean(data.get("hsn_sac"))
    seen, lines = set(), []
    for row in (data.get("lines") or []):
        if not isinstance(row, dict): continue
        item = clean(row.get("item") or row.get("description") or "")
        if not item or len(item) < 2: continue
        qty  = to_f(row.get("quantity") or row.get("qty"))
        rate = to_f(row.get("rate") or row.get("unit_price"))
        hsn  = clean(row.get("hsn_sac") or row.get("hsn") or "") or hsn_top
        amt  = to_f(row.get("amount") or row.get("total"))
        key  = (item.lower()[:40], round(amt, 0))
        if key in seen: continue
        seen.add(key)
        lines.append({"item": item, "quantity": qty, "rate": rate, "hsn_sac": hsn, "amount": amt})

    header = {
        "filename":      fname,
        "vendor":        clean(data.get("vendor")),
        "invoice_number":clean(data.get("invoice_number")),
        "invoice_date":  fmt_date(clean(data.get("invoice_date"))),
        "po_number":     clean_po(data.get("po_number")),
        "gstin":         clean(data.get("gstin")),
        "hsn_sac":       hsn_top,
        "vendor_code":   clean(data.get("vendor_code")),
        "currency":      clean(data.get("currency")) or "INR",
        "subtotal":      round(sub, 2),
        "cgst":          round(cgst, 2),
        "sgst":          round(sgst, 2),
        "igst":          round(igst, 2),
        "total_tax":     round(ttax, 2),
        "total":         round(tot, 2),
        "grn_number":    clean_grn(data.get("grn_number") or ""),
        "scroll_number": clean(data.get("scroll_number") or ""),
        "handwritten_references": "",
        "confidence":    round(max(0.0, min(1.0, to_f(data.get("confidence")))) * 100, 1),
    }
    return header, lines


# ══════════════════════════════════════════
#  DB OPERATIONS
# ══════════════════════════════════════════

def is_duplicate(session, inv_no: str) -> tuple[bool, str]:
    if not inv_no: return False, "No invoice number"
    row = session.scalar(select(InvoiceMaster).where(InvoiceMaster.invoice_number == inv_no))
    if row: return True, f"Already in DB (first seen {row.first_seen_date.date()})"
    return False, "New"

def save_to_db(session, h: dict, lines: list, fname: str) -> int:
    inv = Invoice(
        filename=fname, vendor=h["vendor"],
        invoice_number=h["invoice_number"], invoice_date=h["invoice_date"],
        po_number=h["po_number"], gstin=h["gstin"], hsn_sac=h["hsn_sac"],
        vendor_code=h["vendor_code"], tax_code="", business_place="",
        currency=h["currency"], subtotal=h["subtotal"],
        cgst=h["cgst"], sgst=h["sgst"], igst=h["igst"],
        tax=h["total_tax"], total=h["total"],
        confidence=h["confidence"] / 100,
        grn_number=h["grn_number"],
        scroll_number=h["scroll_number"],
        handwritten_references=h["handwritten_references"],
        status="REVIEW",
    )
    session.add(inv); session.flush()

    for l in lines:
        session.add(InvoiceLine(
            invoice_id=inv.id, item=l["item"],
            quantity=l["quantity"], rate=l["rate"],
            hsn_sac=l["hsn_sac"], amount=l["amount"],
        ))

    if h["invoice_number"]:
        session.execute(
            pg_insert(InvoiceMaster).values(
                invoice_number=h["invoice_number"], vendor=h["vendor"],
                vendor_code=h["vendor_code"], po_number=h["po_number"],
                first_seen_date=utcnow(), invoice_id=inv.id,
            ).on_conflict_do_nothing(index_elements=["invoice_number"])
        )

    session.add(AuditLog(
        invoice_id=inv.id, action="EXTRACTED",
        detail={"file": fname, "model": OLLAMA_MODEL,
                "total": h["total"], "grn": h["grn_number"],
                "scroll": h["scroll_number"], "confidence": h["confidence"]},
    ))
    session.commit()
    return inv.id


# ══════════════════════════════════════════
#  EXCEL
# ══════════════════════════════════════════

def save_excel(h: dict, lines: list, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[<>:"/\\|?*]', "_",
                  h["invoice_number"] or Path(h["filename"]).stem).strip(" ._")
    wb  = Workbook()
    alt = PatternFill("solid", fgColor="EBF3FB")

    def hdr(ws, row_n, color):
        fill = PatternFill("solid", fgColor=color)
        for c in ws[row_n]:
            c.fill = fill
            c.font = Font(bold=True, color="FFFFFF", size=10)
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    ws1 = wb.active; ws1.title = "Invoice Data"
    ws1.append(["Field", "Value"]); hdr(ws1, 1, "1F4E79")
    fields = [
        ("File Name",         h["filename"]),
        ("Vendor",            h["vendor"]),
        ("Invoice Number",    h["invoice_number"]),
        ("Invoice Date",      h["invoice_date"]),
        ("PO Number",         h["po_number"]),
        ("Vendor GSTIN",      h["gstin"]),
        ("HSN / SAC",         h["hsn_sac"]),
        ("Vendor Code",       h["vendor_code"]),
        ("Currency",          h["currency"]),
        ("Subtotal",          h["subtotal"]),
        ("CGST",              h["cgst"]),
        ("SGST",              h["sgst"]),
        ("IGST",              h["igst"]),
        ("Total Tax",         h["total_tax"]),
        ("Invoice Total",     h["total"]),
        ("GRN Number",        h["grn_number"]),
        ("Scroll Number",     h["scroll_number"]),
        ("AI Confidence %",   h["confidence"]),
    ]
    for i, (f, v) in enumerate(fields):
        ws1.append([f, v])
        if i % 2 == 1:
            for c in ws1[i+2]: c.fill = alt
    ws1.column_dimensions["A"].width = 22
    ws1.column_dimensions["B"].width = 60
    ws1.freeze_panes = "A2"

    ws2 = wb.create_sheet("Line Items")
    ws2.append(["Sl.No", "Description", "Qty", "Rate", "HSN/SAC", "Amount"])
    hdr(ws2, 1, "145A32")
    for i, l in enumerate(lines, 1):
        ws2.append([i, l["item"], l["quantity"], l["rate"], l["hsn_sac"], l["amount"]])
        if i % 2 == 0:
            for c in ws2[i+1]: c.fill = alt
    for i, w in enumerate([7, 60, 10, 14, 14, 18], 1):
        ws2.column_dimensions[get_column_letter(i)].width = w
    ws2.freeze_panes = "A2"

    out = out_dir / f"{safe}.xlsx"
    wb.save(out)
    return out


# ══════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════

def main():
    in_dir  = Path(PDF_FOLDER)
    out_dir = Path(OUTPUT_FOLDER)

    if not in_dir.exists():
        print(f"\nERROR: Input folder not found: {in_dir}"); sys.exit(1)

    seen, pdfs = set(), []
    for p in sorted(in_dir.iterdir()):
        if p.is_file() and p.suffix.lower() == ".pdf":
            if p.name.lower() not in seen:
                seen.add(p.name.lower()); pdfs.append(p)

    if not pdfs:
        print("ERROR: No PDF files found"); sys.exit(1)

    out_dir.mkdir(parents=True, exist_ok=True)

    print("\n" + "═"*62)
    print("  KTFL Invoice Extractor — Final")
    print(f"  Model  : {OLLAMA_MODEL}")
    print(f"  PDFs   : {len(pdfs)}")
    print(f"  Calls  : 1 per PDF (invoice + GRN together)")
    print("═"*62)

    # Ollama check
    print("\n  Checking Ollama...")
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=10, trust_env=False)
        r.raise_for_status()
        models = [m["name"] for m in r.json().get("models", [])]
        match = next((m for m in models if "qwen3-vl" in m.lower()), None)
        if not match:
            print(f"  ERROR: qwen3-vl not found. Available: {models}"); sys.exit(1)
        print(f"  ✅ Ollama OK — {match}")
    except Exception as e:
        print(f"  ERROR: Cannot reach Ollama: {e}"); sys.exit(1)

    # DB check
    print("\n  Connecting to DB...")
    try:
        engine, Session = setup_db()
        with engine.connect() as c: c.execute(text("SELECT 1"))
        print(f"  ✅ DB OK")
    except Exception as e:
        print(f"  ERROR: DB failed: {e}"); sys.exit(1)

    print()
    stats = {"ok": 0, "dup": 0, "err": 0}
    t_all = time.time()

    for idx, pdf in enumerate(pdfs, 1):
        t0 = time.time()
        pc = page_count(pdf)
        print(f"{'─'*62}")
        print(f"  [{idx}/{len(pdfs)}] {pdf.name}  ({pc} pages)")

        try:
            # ONE call — gets everything
            data, is_scanned = extract(pdf)
            header, lines = normalize(data, pdf.name)

            print(f"  {'Scanned' if is_scanned else 'Text'} PDF")
            print(f"  Vendor   : {header['vendor'] or '⚠ not found'}")
            print(f"  Invoice  : {header['invoice_number'] or '⚠ not found'}")
            print(f"  Date     : {header['invoice_date'] or '⚠ not found'}")
            print(f"  PO       : {header['po_number'] or '⚠ not found'}")
            print(f"  Total    : ₹{header['total']:,.2f}  (CGST:{header['cgst']} SGST:{header['sgst']} IGST:{header['igst']})")
            print(f"  GRN      : {header['grn_number'] or '(not found)'}")
            print(f"  Scroll   : {header['scroll_number'] or '(not found)'}")
            print(f"  Items    : {len(lines)}")
            print(f"  Confidence: {header['confidence']}%")

            with Session() as session:
                dup, reason = is_duplicate(session, header["invoice_number"])
                if dup:
                    print(f"\n  ⚠️  DUPLICATE — {reason} — skipped DB insert")
                    session.add(AuditLog(invoice_id=None, action="DUPLICATE_SKIPPED",
                        detail={"file": pdf.name, "invoice_number": header["invoice_number"]}))
                    session.commit()
                    stats["dup"] += 1
                else:
                    inv_id = save_to_db(session, header, lines, pdf.name)
                    print(f"\n  ✅ Saved to DB — id={inv_id}")
                    stats["ok"] += 1

            xls = save_excel(header, lines, out_dir)
            print(f"  📄 Excel : {xls.name}")

        except Exception as e:
            print(f"\n  ❌ ERROR: {e}")
            import traceback; traceback.print_exc()
            try:
                with Session() as s:
                    s.add(AuditLog(invoice_id=None, action="ERROR",
                        detail={"file": pdf.name, "error": str(e)}))
                    s.commit()
            except: pass
            stats["err"] += 1

        print(f"  ⏱  {round(time.time()-t0, 1)}s\n")

    total = round(time.time() - t_all, 0)
    print("═"*62)
    print(f"  COMPLETED — {int(total//60)}m {int(total%60)}s total")
    print(f"  ✅ Extracted  : {stats['ok']}")
    print(f"  ⚠️  Duplicates : {stats['dup']} (skipped)")
    print(f"  ❌ Errors     : {stats['err']}")
    print(f"  📁 Excel      : {out_dir}")
    print("═"*62)


if __name__ == "__main__":
    main()
