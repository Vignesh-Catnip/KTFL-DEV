"""
KTFL Invoice Extractor — LangChain + Ollama + PostgreSQL
=========================================================
Tables used:
  - invoices        : main invoice data
  - invoice_lines   : line items
  - invoice_master  : master reference only (NOT a gate/blocker)
  - audit_logs      : every action logged

Duplicate rule:
  invoices table → skip if same invoice_number + po_number + vendor_code
  invoice_master → upsert (ON CONFLICT DO NOTHING on invoice_number)

Speed optimisation:
  - Pass 1 (main fields) + Pass 2 (GRN) run with fresh LLM per PDF
  - GRN: scans pages 1→last, stops at first hit
  - Scanned PDFs: image-only mode (no pdf text)
  - Text PDFs: OCR text sent alongside image for grounding
"""

import os
for k in ["HTTP_PROXY","HTTPS_PROXY","ALL_PROXY",
          "http_proxy","https_proxy","all_proxy"]:
    os.environ.pop(k, None)
os.environ["NO_PROXY"] = "localhost,127.0.0.1"
os.environ["no_proxy"] = "localhost,127.0.0.1"

import base64, json, re, sys, time
from datetime import datetime, timezone
from pathlib import Path

import fitz
import httpx
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

from sqlalchemy import create_engine, text, select
from sqlalchemy.orm import (sessionmaker, DeclarativeBase,
                             Mapped, mapped_column, relationship)
from sqlalchemy import String, Text, Float, DateTime, ForeignKey, JSON, Integer
from sqlalchemy.dialects.postgresql import insert as pg_insert

# ============================================================
# CONFIG
# ============================================================
PDF_FOLDER    = r"C:\Users\ckts00126\Desktop\KTFL-EXTRACTION\Input-pdf"
OLLAMA_URL    = "http://127.0.0.1:11434"
OLLAMA_MODEL  = "qwen3-vl:32b"
OUTPUT_FOLDER = r"C:\Users\ckts00126\Desktop\KTFL-EXTRACTION\Output-Excel"
DB_URL        = "postgresql+psycopg://appuser:appuser123@10.7.11.22:5432/miro_invoice"

MAIN_PAGES   = 2       # pages sent for main extraction (invoice data is on page 1-2)
MAX_PAGES    = 6       # max pages scanned for GRN stamp
IMAGE_ZOOM   = 1.2     # 1.2 = good quality + smaller payload
NUM_CTX         = 16384  # larger context for multi-page invoices
NUM_PREDICT     = 8192   # thinking tokens + JSON output — must be large enough
NUM_PREDICT_GRN = 512    # GRN response is tiny
TIMEOUT         = 900
# ============================================================


# ── DB Models ────────────────────────────────────────────────

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
    invoice = relationship("Invoice", back_populates="lines")

class InvoiceMaster(Base):
    __tablename__ = "invoice_master"
    id:             Mapped[int]      = mapped_column(primary_key=True)
    invoice_number: Mapped[str]      = mapped_column(String(100), unique=True)
    vendor:         Mapped[str|None] = mapped_column(String(255))
    vendor_code:    Mapped[str|None] = mapped_column(String(50))
    po_number:      Mapped[str|None] = mapped_column(String(100))
    first_seen_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    invoice_id:     Mapped[int|None] = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"))

class AuditLog(Base):
    __tablename__ = "audit_logs"
    id:         Mapped[int]      = mapped_column(primary_key=True)
    invoice_id: Mapped[int|None] = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"))
    action:     Mapped[str]      = mapped_column(String(100))
    detail:     Mapped[dict]     = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


# ── DB Setup ─────────────────────────────────────────────────

def setup_db():
    engine  = create_engine(DB_URL)
    Session = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    migrations = [
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS gstin VARCHAR(50)",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS hsn_sac VARCHAR(50)",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS vendor_code VARCHAR(50)",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS cgst FLOAT DEFAULT 0",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS sgst FLOAT DEFAULT 0",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS igst FLOAT DEFAULT 0",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS grn_number VARCHAR(100)",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS handwritten_references TEXT",
        "ALTER TABLE invoice_lines ADD COLUMN IF NOT EXISTS rate FLOAT DEFAULT 0",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try: conn.execute(text(sql))
            except: pass
        conn.commit()
    print("  DB ready ✅")
    return engine, Session


# ── Duplicate Check ───────────────────────────────────────────

def is_duplicate_in_invoices(session, invoice_number, po_number, vendor_code):
    if not invoice_number:
        return False, "No invoice number — skip duplicate check"
    filters = [Invoice.invoice_number == invoice_number]
    filters.append(Invoice.po_number == po_number if po_number else Invoice.po_number == None)
    filters.append(Invoice.vendor_code == vendor_code if vendor_code else Invoice.vendor_code == None)
    existing = session.scalar(select(Invoice).where(*filters))
    if existing:
        return True, f"DUPLICATE: inv='{invoice_number}' po='{po_number}' vc='{vendor_code}' (id={existing.id})"
    return False, "New combo — OK to push"


# ── DB Push ───────────────────────────────────────────────────

def push_to_db(session, header, lines, pdf_name):
    inv = Invoice(
        filename       = pdf_name,
        vendor         = header["vendor"],
        invoice_number = header["invoice_number"],
        invoice_date   = header["invoice_date"],
        po_number      = header["po_number"],
        gstin          = header["gstin"],
        hsn_sac        = header["hsn_sac"],
        vendor_code    = header["vendor_code"],
        tax_code       = "",
        business_place = "",
        currency       = header["currency"],
        subtotal       = header["subtotal"],
        cgst           = header["cgst"],
        sgst           = header["sgst"],
        igst           = header["igst"],
        tax            = header["total_tax"],
        total          = header["total"],
        confidence     = header["confidence"] / 100,
        grn_number     = header["grn_number"],
        handwritten_references = header["handwritten_references"],
        status         = "REVIEW",
    )
    session.add(inv)
    session.flush()

    for l in lines:
        session.add(InvoiceLine(
            invoice_id = inv.id,
            item       = l["item"],
            quantity   = l["quantity"],
            rate       = l["rate"],
            tax_code   = l["hsn_sac"],
            amount     = l["amount"],
        ))

    if header["invoice_number"]:
        stmt = pg_insert(InvoiceMaster).values(
            invoice_number  = header["invoice_number"],
            vendor          = header["vendor"],
            vendor_code     = header["vendor_code"],
            po_number       = header["po_number"],
            first_seen_date = now_utc(),
            invoice_id      = inv.id,
        ).on_conflict_do_nothing(index_elements=["invoice_number"])
        session.execute(stmt)

    session.add(AuditLog(
        invoice_id = inv.id,
        action     = "EXTRACTED_AND_INSERTED",
        detail     = {
            "source"    : "invoice_extractor_db.py",
            "model"     : OLLAMA_MODEL,
            "file"      : pdf_name,
            "confidence": header["confidence"],
            "total"     : header["total"],
            "grn"       : header["grn_number"],
        }
    ))
    session.commit()
    return inv.id


# ── Prompts ───────────────────────────────────────────────────

MAIN_PROMPT = r"""
You are an expert Indian invoice data extraction engine. You will receive one or more invoice page images (possibly scanned/photographed). Extract ALL fields accurately.

══════════════════════════════════════════════════
FIELD EXTRACTION RULES
══════════════════════════════════════════════════

VENDOR (supplier name):
  - The company/person who ISSUED this invoice (top of page, letterhead)
  - NOT the buyer (KTFL / Kalyani Technoforge)
  - Extract full legal name as printed

INVOICE NUMBER:
  - Look for: "Invoice No", "Bill No", "Tax Invoice No", "Inv No", "Invoice #"
  - Return exactly as printed (e.g. "DSK/2026-27/428")

INVOICE DATE:
  - Look for: "Invoice Date", "Date", "Bill Date", "Tax Invoice Date"
  - Return as found; format will be normalised separately

PO NUMBER:
  - Look for: "PO No", "Purchase Order No", "P.O. No", "Order No", "Our Order No", "Ref PO"
  - Strip prefix: "PO-5100002309" → "5100002309"

GSTIN:
  - 15-character alphanumeric code of the VENDOR (supplier)
  - Pattern: 2 digits + 10 alphanum + 1 digit + 1 alphanum + 1 alphanum
  - NOT the buyer's GSTIN

VENDOR CODE:
  - Look for: "Vendor Code", "Supplier Code", "Party Code", "Code No"
  - Usually a 6-digit number written next to vendor name or in header
  - If not found → ""

HSN/SAC:
  - Harmonised System of Nomenclature code for goods
  - 4–8 digit number in line items table under "HSN", "HSN/SAC", "SAC"
  - If multiple HSN codes → use the first/most common one

AMOUNTS (CRITICAL):
  - Remove ₹ symbol and commas: ₹6,19,763.14 → 619763.14
  - subtotal = taxable value / amount before tax
  - cgst = Central GST amount
  - sgst = State GST amount
  - igst = Integrated GST amount (inter-state only — usually 0 if cgst+sgst present)
  - total_tax = cgst + sgst + igst
  - total = grand total / invoice total (amount payable)
  - If tax shown as percentage only → calculate: subtotal × rate/100

CURRENCY:
  - Default "INR" unless explicitly stated otherwise

GRN NUMBER:
  - Set to "" always — extracted separately in a dedicated pass

HANDWRITTEN REFERENCES:
  - Any handwritten notes, stamps (other than GRN), annotations
  - E.g. "MM122E9355", "LR N 6021", "844", "21-8-26"
  - Comma-separated if multiple

LINE ITEMS:
  - Extract every product/service row from the invoice table
  - Fields: item (description), quantity, rate (unit price), hsn_sac, amount
  - Remove duplicate rows
  - Skip subtotal/tax/total summary rows

CONFIDENCE (0.0 to 1.0):
  - 0.9–1.0: clear printed invoice, all fields visible
  - 0.7–0.9: some fields unclear or missing
  - 0.5–0.7: heavily scanned/rotated, many fields guessed
  - Below 0.5: very poor quality

══════════════════════════════════════════════════
OUTPUT — RETURN ONLY THIS JSON, NO OTHER TEXT:
══════════════════════════════════════════════════
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
  "total_tax": 0.0,
  "total": 0.0,
  "grn_number": "",
  "handwritten_references": "",
  "confidence": 0.0,
  "lines": [
    {"item": "", "quantity": 0.0, "rate": 0.0, "hsn_sac": "", "amount": 0.0}
  ]
}
"""

GRN_PROMPT = r"""
You are examining ONE invoice page image. Your ONLY task: find the GRN number from a receiving/inward stamp.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHAT TO LOOK FOR
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Find a rubber stamp, handwritten box, or sticker — usually at the bottom or corner of the page — from the RECEIVING party (Kalyani Technoforge / KTFL).

Inside that stamp, find a line labelled EXACTLY one of:
  ✅ GRN NO     ✅ GRN No.    ✅ GRN No
  ✅ GRN        ✅ Inward No  ✅ INWARD NO
  ✅ MRN No     ✅ MRN

Extract the NUMBER written next to that specific label.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHAT TO IGNORE — NEVER EXTRACT FROM:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ❌ SCROLL NO      ← MOST COMMON MISTAKE — NOT the GRN
  ❌ CH NO / CHN NO
  ❌ Challan No / Challan Number
  ❌ E-way Bill No / EWB No
  ❌ Vehicle No / Lorry No
  ❌ Gate Pass No
  ❌ Delivery Note No / DN No
  ❌ Material Doc No
  ❌ LR No / Bilty No
  ❌ Invoice No / Bill No
  ❌ Any number from the printed invoice body

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EXAMPLE STAMP (read carefully):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  SCROLL NO  : 020040285    ← IGNORE (this is NOT GRN)
  DATE       : 21/08/26
  GRN NO     : 50 - 35689   ← EXTRACT THIS VALUE → "50 - 35689"
  GRN PREP BY: Ravi

Correct answer: {"grn_number": "50 - 35689"}
Wrong answer:   {"grn_number": "020040285"}   ← NEVER do this

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULES:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- If stamp has SCROLL NO but NO GRN NO label → {"grn_number": ""}
- If no receiving stamp found on this page → {"grn_number": ""}
- If GRN label found but number is illegible → {"grn_number": ""}
- Return ONLY the raw value next to GRN label — do NOT clean or reformat it

RETURN ONLY THIS JSON:
{"grn_number": "<value or empty string>"}
"""


# ── Helpers ───────────────────────────────────────────────────

def to_float(v) -> float:
    if v is None: return 0.0
    if isinstance(v, (int, float)): return float(v)
    s = re.sub(r"[^0-9.\-]", "", str(v).replace(",", "").replace("₹", ""))
    try: return float(s) if s else 0.0
    except: return 0.0

def clean(v) -> str:
    return str(v).strip() if v else ""

def clean_po(v) -> str:
    s = clean(v)
    s = re.sub(r"^(?:PO[-/.\s]*|P\.O\.[-/.\s]*|P/O[-/.\s]*)", "", s, flags=re.IGNORECASE).strip()
    return s

def clean_grn(v) -> str:
    """
    OCR correction for GRN numbers from stamp.
    Examples:
      "50 - 35689"  → "500035689"   (space-dash-space = "00")
      "S00356389"   → "500356389"
      "5OOO356389"  → "5000356389"
    """
    s = clean(v)
    if not s:
        return s
    # space - space or just - between digits → "00"
    s = re.sub(r"\s*-\s*", "00", s)
    # remove remaining spaces and dots
    s = re.sub(r"[\s\.]", "", s)
    # OCR character correction
    OCR_MAP = {
        'S': '5', 's': '5',
        'O': '0', 'o': '0',
        'I': '1', 'l': '1',
        'Z': '2', 'z': '2',
        'B': '8',
        'G': '6',
        'T': '7',
        'A': '4',
        'q': '9',
    }
    return "".join(OCR_MAP.get(ch, ch) for ch in s)

def extract_json(raw) -> dict:
    if isinstance(raw, list):
        raw = "".join(str(x.get("text", "") if isinstance(x, dict) else x) for x in raw)
    text = str(raw).strip()

    # Strip qwen3 thinking blocks (may span hundreds of lines)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    # Strip markdown code fences
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text).strip()

    # Direct parse
    try:
        return json.loads(text)
    except Exception:
        pass

    # Find outermost {...} block
    s, e = text.find("{"), text.rfind("}")
    if s >= 0 and e > s:
        try:
            return json.loads(text[s:e+1])
        except Exception:
            pass

    # Try to find any JSON-like block after stripping leading text
    m = re.search(r'\{[\s\S]*\}', text)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            pass

    raise ValueError(f"No valid JSON found. Raw (first 300 chars): {text[:300]}")

def format_date(raw: str) -> str:
    if not raw: return ""
    raw = str(raw).strip()
    MONTH_MAP = {
        "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
        "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
        "january":1,"february":2,"march":3,"april":4,"june":6,
        "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
    }
    formats = [
        "%d.%m.%Y","%d/%m/%Y","%Y-%m-%d","%d-%m-%Y",
        "%d-%m-%y","%d/%m/%y","%B %d, %Y","%b %d, %Y",
        "%d %B %Y","%d %b %Y","%Y/%m/%d",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.year < 100: dt = dt.replace(year=dt.year + 2000)
            return dt.strftime("%d.%m.%Y")
        except: pass
    m = re.match(r"(\d{1,2})[-/\s]([A-Za-z]+)[-/\s](\d{2,4})$", raw)
    if m:
        day, mon, yr = int(m.group(1)), m.group(2).lower()[:3], int(m.group(3))
        if yr < 100: yr += 2000
        mo = MONTH_MAP.get(mon, 0)
        if mo: return f"{day:02d}.{mo:02d}.{yr}"
    return raw

def fix_amounts(subtotal, cgst, sgst, igst, total_tax, total):
    if total > 0 and total_tax > total:
        candidate = round(total_tax / 100, 2)
        if abs(subtotal + candidate - total) < total * 0.02:
            total_tax = candidate
            cgst = round(cgst / 100, 2) if cgst else 0
            sgst = round(sgst / 100, 2) if sgst else 0
            igst = round(igst / 100, 2) if igst else 0
    if subtotal > 0 and total_tax >= 0:
        expected = subtotal + total_tax
        if expected > 0 and total > expected * 10:
            total = round(total / 100, 2)
    if total_tax == 0:
        total_tax = round(cgst + sgst + igst, 2)
    if subtotal == 0 and total > 0:
        subtotal = round(total - total_tax, 2)
    if total == 0:
        total = round(subtotal + total_tax, 2)
    return subtotal, cgst, sgst, igst, total_tax, total


# ── PDF → Images ──────────────────────────────────────────────

def pdf_to_images(path: Path, max_pages: int = MAIN_PAGES) -> list[str]:
    """
    Convert PDF pages to base64 PNG images.
    main extraction → MAIN_PAGES (default 2) — invoice header+lines fit on page 1-2
    GRN scan       → called with max_pages=MAX_PAGES to check all pages
    """
    doc  = fitz.open(str(path))
    zoom = fitz.Matrix(IMAGE_ZOOM, IMAGE_ZOOM)
    imgs = []
    for i in range(min(doc.page_count, max_pages)):
        pix = doc[i].get_pixmap(matrix=zoom, alpha=False)
        imgs.append(base64.b64encode(pix.tobytes("png")).decode())
    doc.close()
    return imgs

def get_pdf_text(path: Path) -> str:
    """Extract embedded text from PDF (returns '' for scanned PDFs)."""
    doc   = fitz.open(str(path))
    parts = []
    for i, page in enumerate(doc, 1):
        t = page.get_text("text").strip()
        if t and len(t) > 20:  # ignore near-empty pages
            parts.append(f"--- PAGE {i} ---\n{t[:2000]}")
    doc.close()
    return "\n".join(parts)

def is_scanned_pdf(pdf_text: str) -> bool:
    """True if PDF has no embedded text (image-only scan)."""
    return len(pdf_text.strip()) < 50


# ── LLM ──────────────────────────────────────────────────────

def ollama_call(prompt_text: str, images: list, num_predict: int = NUM_PREDICT) -> dict:
    """
    Uses /api/generate (not /api/chat) with streaming.

    Why /api/generate:
      - /api/chat with qwen3 thinking mode: <think> tokens consume num_predict budget,
        leaving no room for actual JSON output → done_reason=length → empty content
      - /api/generate separates thinking from response internally and streams
        only the final response tokens → JSON always complete
      - images passed as base64 list in "images" field (same format, works for VL models)
    """
    payload = {
        "model":  OLLAMA_MODEL,
        "prompt": prompt_text,
        "images": images,           # base64 list — same as /api/chat images
        "stream": True,
        "think":  False,            # disable extended thinking to save tokens
        "options": {
            "temperature":  0,
            "num_ctx":      NUM_CTX,
            "num_predict":  num_predict,
        },
    }

    full_text = ""
    with httpx.stream(
        "POST",
        f"{OLLAMA_URL}/api/generate",
        json=payload,
        timeout=TIMEOUT,
        trust_env=False,
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            line = line.strip()
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except Exception:
                continue
            token = chunk.get("response", "")   # /api/generate uses "response" not "content"
            if token:
                full_text += token
            if chunk.get("done"):
                done_reason = chunk.get("done_reason", "")
                if done_reason == "length":
                    print(f"\n  ⚠️  WARNING: response truncated (done_reason=length). "
                          f"Increase NUM_PREDICT (currently {num_predict})")
                break

    full_text = full_text.strip()
    if not full_text:
        raise ValueError("Ollama /api/generate returned empty response")

    return extract_json(full_text)


def invoke_main(images: list, pdf_text: str = "") -> dict:
    """Pass 1: Extract all invoice fields (except GRN)."""
    prompt = MAIN_PROMPT
    if pdf_text:
        prompt += f"\n\nEMBEDDED TEXT (use as grounding reference):\n{pdf_text[:3000]}"
    return ollama_call(prompt, images, NUM_PREDICT)


def invoke_grn(page_imgs: list) -> str:
    """
    Pass 2: GRN-only scan.
    - One fresh Ollama call per page — no shared context
    - Scans page 1 → last page, stops at first non-empty GRN
    """
    for page_num, img in enumerate(page_imgs, 1):
        print(f"     GRN scan page {page_num}/{len(page_imgs)}...", end=" ", flush=True)
        grn = ""
        try:
            result = ollama_call(GRN_PROMPT, [img], num_predict=NUM_PREDICT_GRN)
            grn = (result.get("grn_number") or "").strip()
        except Exception as e:
            print(f"(err: {e})")

        if grn:
            print(f"✅ GRN='{grn}'")
            return grn
        else:
            print("(no GRN)")

    return ""


# ── Normalise extracted data ──────────────────────────────────

def normalize(data: dict, filename: str):
    if not isinstance(data, dict): data = {}
    subtotal  = to_float(data.get("subtotal"))
    cgst      = to_float(data.get("cgst"))
    sgst      = to_float(data.get("sgst"))
    igst      = to_float(data.get("igst"))
    total_tax = to_float(data.get("total_tax"))
    total     = to_float(data.get("total"))
    subtotal, cgst, sgst, igst, total_tax, total = fix_amounts(
        subtotal, cgst, sgst, igst, total_tax, total)

    hsn_inv = clean(data.get("hsn_sac"))
    seen = set()
    lines = []
    for l in (data.get("lines") or []):
        if not isinstance(l, dict): continue
        item = clean(l.get("item") or l.get("description") or "")
        qty  = to_float(l.get("quantity") or l.get("qty"))
        rate = to_float(l.get("rate") or l.get("unit_price"))
        hsn  = clean(l.get("hsn_sac") or l.get("hsn") or "") or hsn_inv
        amt  = to_float(l.get("amount") or l.get("total"))
        if not item: continue
        key = (item.lower()[:40], round(amt))
        if key in seen: continue
        seen.add(key)
        if amt == 0 and qty == 0 and rate == 0 and len(item) < 6: continue
        lines.append({"item": item, "quantity": qty, "rate": rate, "hsn_sac": hsn, "amount": amt})
    for line in lines:
        if not line["hsn_sac"] and hsn_inv:
            line["hsn_sac"] = hsn_inv

    hw = data.get("handwritten_references") or ""
    if isinstance(hw, list): hw = ", ".join(str(x) for x in hw if x)

    return {
        "filename":         filename,
        "vendor":           clean(data.get("vendor")),
        "invoice_number":   clean(data.get("invoice_number")),
        "invoice_date":     format_date(clean(data.get("invoice_date"))),
        "po_number":        clean_po(data.get("po_number")),
        "gstin":            clean(data.get("gstin")),
        "hsn_sac":          hsn_inv,
        "vendor_code":      clean(data.get("vendor_code")),
        "currency":         clean(data.get("currency")) or "INR",
        "subtotal":         round(subtotal, 2),
        "cgst":             round(cgst, 2),
        "sgst":             round(sgst, 2),
        "igst":             round(igst, 2),
        "total_tax":        round(total_tax, 2),
        "total":            round(total, 2),
        "grn_number":       "",   # filled by Pass 2
        "handwritten_references": clean(hw),
        "confidence":       round(max(0, min(1, to_float(data.get("confidence")))) * 100, 1),
    }, lines


# ── Excel output ──────────────────────────────────────────────

def create_excel(header, lines, out_folder):
    Path(out_folder).mkdir(parents=True, exist_ok=True)
    fname = re.sub(r'[<>:"/\\|?*]', "_",
                   header["invoice_number"] or Path(header["filename"]).stem).strip(" ._") + ".xlsx"
    path  = Path(out_folder) / fname
    wb    = Workbook()
    alt   = PatternFill("solid", fgColor="EBF3FB")

    def shdr(ws, row, color):
        fill = PatternFill("solid", fgColor=color)
        font = Font(bold=True, color="FFFFFF", size=10)
        for c in ws[row]:
            c.fill = fill; c.font = font
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    ws1 = wb.active; ws1.title = "Invoice Data"
    ws1.append(["Field", "Value"]); shdr(ws1, 1, "1F4E79")
    fields = [
        ("File Name",             header["filename"]),
        ("Vendor",                header["vendor"]),
        ("Invoice Number",        header["invoice_number"]),
        ("Invoice Date",          header["invoice_date"]),
        ("PO Number",             header["po_number"]),
        ("Vendor GSTIN",          header["gstin"]),
        ("HSN / SAC",             header["hsn_sac"]),
        ("Vendor Code",           header["vendor_code"]),
        ("Currency",              header["currency"]),
        ("Subtotal",              header["subtotal"]),
        ("CGST",                  header["cgst"]),
        ("SGST",                  header["sgst"]),
        ("IGST",                  header["igst"]),
        ("Total Tax",             header["total_tax"]),
        ("Invoice Total",         header["total"]),
        ("GRN / Inward Number",   header["grn_number"]),
        ("Handwritten References",header["handwritten_references"]),
        ("AI Confidence %",       header["confidence"]),
    ]
    for i, (f, v) in enumerate(fields):
        ws1.append([f, v])
        if i % 2 == 1:
            for c in ws1[i + 2]: c.fill = alt
    ws1.column_dimensions["A"].width = 28
    ws1.column_dimensions["B"].width = 65
    ws1.freeze_panes = "A2"

    ws2 = wb.create_sheet("Line Items")
    ws2.append(["Sl.No", "Item / Description", "Quantity", "Rate", "HSN/SAC", "Amount"])
    shdr(ws2, 1, "145A32")
    for i, l in enumerate(lines, 1):
        ws2.append([i, l["item"], l["quantity"], l["rate"], l["hsn_sac"], l["amount"]])
        if i % 2 == 0:
            for c in ws2[i + 1]: c.fill = alt
    for i, w in enumerate([8, 60, 12, 15, 18, 18], 1):
        ws2.column_dimensions[get_column_letter(i)].width = w
    ws2.freeze_panes = "A2"
    wb.save(path)
    print(f"  📄 Excel: {fname}")


# ── Main ──────────────────────────────────────────────────────

def main():
    folder = Path(PDF_FOLDER)
    out    = Path(OUTPUT_FOLDER)

    if not folder.exists():
        print(f"ERROR: Folder not found: {folder}"); sys.exit(1)

    seen = set(); pdfs = []
    for p in sorted(folder.iterdir()):
        if p.is_file() and p.suffix.lower() == ".pdf":
            if p.name.lower() not in seen:
                seen.add(p.name.lower()); pdfs.append(p)

    if not pdfs: print("ERROR: No PDFs found"); sys.exit(1)

    out.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("KTFL Invoice Extractor — LangChain + Ollama + PostgreSQL")
    print(f"PDFs   : {len(pdfs)} | Model: {OLLAMA_MODEL}")
    print(f"DB     : {DB_URL.split('@')[-1]}")
    print("=" * 60)

    print("\nConnecting to DB...")
    try:
        engine, Session = setup_db()
    except Exception as e:
        print(f"ERROR: DB failed: {e}"); sys.exit(1)

    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=10, trust_env=False)
        models = [m["name"] for m in r.json().get("models", [])]
        if OLLAMA_MODEL not in models:
            print(f"ERROR: {OLLAMA_MODEL} not installed."); sys.exit(1)
        print(f"Ollama : OK\n")
    except Exception as e:
        print(f"ERROR: Ollama failed: {e}"); sys.exit(1)

    stats = {"pushed": 0, "duplicate": 0, "error": 0}

    for idx, pdf in enumerate(pdfs, 1):
        t0 = time.time()
        print(f"\n[{idx}/{len(pdfs)}] {pdf.name}")

        try:
            # ── PDF info ──
            pdf_text   = get_pdf_text(pdf)
            scanned    = is_scanned_pdf(pdf_text)
            total_pages = fitz.open(str(pdf)).page_count

            # Pass 1 images: page 1+2 only (invoice header + line items fit here)
            main_imgs  = pdf_to_images(pdf, max_pages=min(MAIN_PAGES, total_pages))
            # GRN scan images: ALL pages (stamp can be anywhere)
            grn_imgs   = pdf_to_images(pdf, max_pages=min(MAX_PAGES, total_pages))

            print(f"  Total pages: {total_pages} | Main extraction: {len(main_imgs)} pages | GRN scan: {len(grn_imgs)} pages")
            print(f"  Type: {'SCANNED (image-only)' if scanned else 'TEXT PDF'}")

            # ── Pass 1: main invoice fields (page 1-2 only) ──
            data  = invoke_main(main_imgs, "" if scanned else pdf_text)
            header, lines = normalize(data, pdf.name)

            # ── Pass 2: GRN — scan all pages, fresh call per page ──
            print(f"  GRN pass 2:")
            raw_grn   = invoke_grn(grn_imgs)
            grn_clean = clean_grn(raw_grn)
            header["grn_number"] = grn_clean
            print(f"    GRN raw='{raw_grn}' → clean='{grn_clean}'")

            print(f"  Invoice : {header['invoice_number'] or '?'}")
            print(f"  Vendor  : {header['vendor'] or '?'}")
            print(f"  Total   : {header['total']}  GRN: {header['grn_number'] or '-'}")

            with Session() as session:
                dup, reason = is_duplicate_in_invoices(
                    session, header["invoice_number"],
                    header["po_number"], header["vendor_code"],
                )
                print(f"  Check   : {reason}")

                if dup:
                    print(f"  ⚠️  SKIPPED — duplicate in invoices table")
                    session.add(AuditLog(
                        invoice_id = None,
                        action     = "DUPLICATE_SKIPPED",
                        detail     = {
                            "file": pdf.name,
                            "invoice_number": header["invoice_number"],
                            "po_number": header["po_number"],
                            "vendor_code": header["vendor_code"],
                            "reason": reason,
                        }
                    ))
                    session.commit()
                    stats["duplicate"] += 1
                    create_excel(header, lines, out)
                    continue

                inv_id  = push_to_db(session, header, lines, pdf.name)
                elapsed = round(time.time() - t0, 1)
                print(f"  ✅ DB pushed — id={inv_id}  ({elapsed}s)")
                stats["pushed"] += 1

            create_excel(header, lines, out)

        except Exception as e:
            elapsed = round(time.time() - t0, 1)
            print(f"  ❌ ERROR: {e}  ({elapsed}s)")
            try:
                with Session() as session:
                    session.add(AuditLog(
                        invoice_id = None,
                        action     = "EXTRACTION_ERROR",
                        detail     = {"file": pdf.name, "error": str(e)},
                    ))
                    session.commit()
            except: pass
            stats["error"] += 1

        if idx < len(pdfs): time.sleep(0.3)

    print("\n" + "=" * 60)
    print("DONE")
    print(f"  ✅ Pushed to DB  : {stats['pushed']}")
    print(f"  ⚠️  Duplicates    : {stats['duplicate']} (skipped)")
    print(f"  ❌ Errors        : {stats['error']}")
    print(f"  📁 Excel folder  : {out}")
    print("=" * 60)


if __name__ == "__main__":
    main()
