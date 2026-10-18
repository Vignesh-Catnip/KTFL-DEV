"""
KTFL Invoice Extractor v3
=========================
SPEED OPTIMIZATIONS vs v2:
  1. Main extraction: sends ONLY page 1 (not 2 pages) — halves vision tokens
  2. GRN extraction: checks page 1 FIRST (stamp is usually page 1), stops immediately on find
  3. Reduced NUM_PREDICT to 4096 (enough for JSON) — think:false skips reasoning
  4. For text PDFs (Bharat Metals): uses embedded text → skips vision for main extraction
  5. Real-time progress ticker shows elapsed seconds during model call

EXPECTED TIME:
  Scanned PDF (AB/DSK): ~60-120s (was hanging indefinitely)
  Text PDF (Bharat):    ~20-40s  (text extraction, no vision for main)
"""

import os, base64, json, re, sys, time, threading
from datetime import datetime, timezone
from pathlib import Path

# Remove proxy env vars — Ollama is local
for _k in ["HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","http_proxy","https_proxy","all_proxy"]:
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "localhost,127.0.0.1"

import fitz          # pip install pymupdf
import httpx         # pip install httpx
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

IMAGE_ZOOM    = 1.5    # higher zoom = clearer stamp text
NUM_CTX       = 8192   # reduced context = faster
NUM_PREDICT   = 4096   # sufficient for JSON output with think:false
TIMEOUT       = 600    # 10 min max per call
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
    handwritten_refs: Mapped[str|None] = mapped_column(Text)
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
    migrations = [
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS gstin VARCHAR(50)",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS hsn_sac VARCHAR(50)",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS vendor_code VARCHAR(50)",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS cgst FLOAT DEFAULT 0",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS sgst FLOAT DEFAULT 0",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS igst FLOAT DEFAULT 0",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS grn_number VARCHAR(100)",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS scroll_number VARCHAR(100)",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS handwritten_refs TEXT",
        "ALTER TABLE invoice_lines ADD COLUMN IF NOT EXISTS rate FLOAT DEFAULT 0",
        "ALTER TABLE invoice_lines ADD COLUMN IF NOT EXISTS hsn_sac VARCHAR(30) DEFAULT ''",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try: conn.execute(text(sql))
            except: pass
        conn.commit()
    return engine, Session


# ══════════════════════════════════════════
#  PROMPTS  (tuned from real invoice images)
# ══════════════════════════════════════════

MAIN_PROMPT = """You are an OCR engine for Indian GST invoices. Study the invoice image carefully.

VENDOR IDENTIFICATION:
- The vendor (seller) name is in the TOP-LEFT box / letterhead
- Kalyani Technoforge / KTFL / KTFL Ranjangaon Plant-R1 is the BUYER — NEVER put them as vendor
- Look for: AB Industries, DSK Engineers, Bharat Metals, etc. at the top

KEY FIELDS TO FIND:
- invoice_number: next to "Invoice No" or "Invoice No." in top table
- invoice_date: next to "Dated" or "Date" in top table
- po_number: next to "Buyer's Order No" or "PO No" — extract digits only (remove PO- prefix)
- gstin: vendor's GSTIN (15 chars, starts with state code like 27...)
- hsn_sac: HSN code from line items table (6-8 digits, e.g. 82073000, 72284000)
- vendor_code: number next to "Vendor Code" in buyer address section (e.g. 70042)
- subtotal: taxable value / amount before tax
- cgst: CGST amount (if present)
- sgst: SGST amount (if present)
- igst: IGST amount (if present, usually 0 when CGST+SGST present)
- total: Grand Total / final invoice amount
- lines: each row in the goods table (NOT tax rows, NOT total row)

STAMP SECTION (bottom of page — Kalyani Technoforge stamp):
- scroll_number: number next to "SCROLL NO" in the receiving stamp
- grn_number: number next to "GRN NO" or "JRN NO" (NOT SCROLL NO) — often blank, put ""

Output ONLY this JSON, nothing else:
{"vendor":"","invoice_number":"","invoice_date":"","po_number":"","gstin":"","hsn_sac":"","vendor_code":"","currency":"INR","subtotal":0.0,"cgst":0.0,"sgst":0.0,"igst":0.0,"total":0.0,"scroll_number":"","grn_number":"","confidence":0.0,"lines":[{"item":"","quantity":0.0,"rate":0.0,"hsn_sac":"","amount":0.0}]}"""

MAIN_PROMPT_TEXT = """You are an Indian GST invoice data extractor.

Here is the embedded text from the invoice PDF:
{TEXT}

Extract ALL fields and return ONLY this JSON:
- vendor: company name at top (NOT Kalyani Technoforge / KTFL)
- invoice_number: value after "Invoice No"
- invoice_date: value after "Dated" or "Date"
- po_number: value after "Buyer's Order No" or "PO No" (digits only)
- gstin: vendor's 15-char GSTIN
- hsn_sac: HSN code from items table
- vendor_code: number after "Vendor Code" in buyer section
- subtotal: taxable value before tax
- cgst, sgst, igst: tax amounts
- total: Grand Total
- scroll_number: number after "SCROLL NO" in stamp (if visible in text)
- grn_number: number after "GRN NO" (usually "")
- confidence: 0.9 (text PDF, high confidence)
- lines: array of line items (not tax/total rows)

Return ONLY JSON:
{"vendor":"","invoice_number":"","invoice_date":"","po_number":"","gstin":"","hsn_sac":"","vendor_code":"","currency":"INR","subtotal":0.0,"cgst":0.0,"sgst":0.0,"igst":0.0,"total":0.0,"scroll_number":"","grn_number":"","confidence":0.9,"lines":[{"item":"","quantity":0.0,"rate":0.0,"hsn_sac":"","amount":0.0}]}"""

GRN_PROMPT = """Look at this invoice page image. Find the RECEIVING STAMP at the bottom-left corner added by Kalyani Technoforge.

The stamp has a table with these rows:
  SCROLL NO  | DATE  | TIME  | SIGN
  (number)   | ...   | ...   | ...
  JRN NO     | DATE  | GRN PREPARED BY
  (number)   | ...   | ...

YOUR TASK: Find "JRN NO" or "GRN NO" row. Extract the number in that row.

CRITICAL RULES:
- SCROLL NO is NOT the GRN — ignore it completely
- JRN NO = GRN NO (same thing, different label)
- The GRN number starts with "50" followed by more digits (e.g. 50-35 6087, 50-343863, 5000356389)
- If only SCROLL NO is visible and NO JRN/GRN row → return ""
- If JRN NO row is empty → return ""

Examples from real stamps:
  SCROLL NO: P002047601 → IGNORE
  JRN NO: 50-343863 → return "50-343863"

  SCROLL NO: P002049285 → IGNORE
  JRN NO: 50-35 6087 → return "50-35 6087"

Return ONLY this JSON:
{"grn_number":"","scroll_number":""}"""


# ══════════════════════════════════════════
#  PROGRESS TICKER
# ══════════════════════════════════════════

class Ticker:
    """Shows elapsed time while model is running"""
    def __init__(self, label: str):
        self.label = label
        self._stop = False
        self._t = None

    def _run(self):
        t0 = time.time()
        while not self._stop:
            elapsed = int(time.time() - t0)
            print(f"\r    {self.label} [{elapsed}s]...", end="", flush=True)
            time.sleep(1)

    def start(self):
        self._stop = False
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def stop(self, suffix=""):
        self._stop = True
        if self._t: self._t.join(timeout=2)
        print(f"\r    {self.label} done{suffix}      ")


# ══════════════════════════════════════════
#  OLLAMA CALL
# ══════════════════════════════════════════

def ollama_call(prompt: str, images: list[str] | None, num_predict: int = NUM_PREDICT) -> str:
    """
    Stream from Ollama /api/generate.
    images=None for text-only calls.
    Returns raw text (caller parses JSON).
    """
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

    collected = []
    with httpx.stream("POST", f"{OLLAMA_URL}/api/generate",
                      json=payload, timeout=TIMEOUT, trust_env=False) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line.strip(): continue
            try: chunk = json.loads(line)
            except: continue
            token = chunk.get("response", "")
            if token:
                collected.append(token)
            if chunk.get("done"):
                if chunk.get("done_reason") == "length":
                    print(f"\n    ⚠️  Token limit hit (NUM_PREDICT={num_predict}) — increase it")
                break
    return "".join(collected).strip()


def parse_json(raw: str) -> dict:
    """Strip thinking blocks + markdown, parse JSON."""
    # remove <think>...</think>
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL | re.IGNORECASE).strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw).strip()
    try: return json.loads(raw)
    except: pass
    s, e = raw.find("{"), raw.rfind("}")
    if s >= 0 and e > s:
        try: return json.loads(raw[s:e+1])
        except: pass
    raise ValueError(f"No JSON found. Raw (first 500 chars):\n{raw[:500]}")


# ══════════════════════════════════════════
#  PDF UTILITIES
# ══════════════════════════════════════════

def page_to_b64(path: Path, page_idx: int) -> str:
    doc = fitz.open(str(path))
    mat = fitz.Matrix(IMAGE_ZOOM, IMAGE_ZOOM)
    pix = doc[page_idx].get_pixmap(matrix=mat, alpha=False)
    doc.close()
    return base64.b64encode(pix.tobytes("png")).decode()

def pages_to_b64(path: Path, indices: list[int]) -> list[str]:
    doc = fitz.open(str(path))
    mat = fitz.Matrix(IMAGE_ZOOM, IMAGE_ZOOM)
    imgs = []
    for i in indices:
        if i < doc.page_count:
            pix = doc[i].get_pixmap(matrix=mat, alpha=False)
            imgs.append(base64.b64encode(pix.tobytes("png")).decode())
    doc.close()
    return imgs

def get_text_pages(path: Path) -> list[str]:
    """Return text per page. Empty string for scanned pages."""
    doc = fitz.open(str(path))
    pages = []
    for pg in doc:
        t = pg.get_text("text").strip()
        pages.append(t if len(t) > 50 else "")
    doc.close()
    return pages

def page_count(path: Path) -> int:
    doc = fitz.open(str(path))
    n = doc.page_count
    doc.close()
    return n


# ══════════════════════════════════════════
#  EXTRACTION — MAIN INVOICE
# ══════════════════════════════════════════

def extract_main(path: Path, text_pages: list[str]) -> dict:
    """
    Extract invoice header + line items.
    Strategy:
      - If page 1 has embedded text → use text prompt (fast, no vision)
      - Else → use page 1 image (vision call)
    """
    full_text = "\n\n".join(t for t in text_pages[:2] if t)
    is_scanned = len(full_text.strip()) < 50

    tick = Ticker("Extracting invoice data")
    tick.start()
    try:
        if not is_scanned and full_text:
            # TEXT PATH: no image needed — much faster
            prompt = MAIN_PROMPT_TEXT.replace("{TEXT}", full_text[:3000])
            raw = ollama_call(prompt, images=None, num_predict=NUM_PREDICT)
        else:
            # VISION PATH: send page 1 only (was sending 2 pages before)
            img = page_to_b64(path, 0)
            raw = ollama_call(MAIN_PROMPT, images=[img], num_predict=NUM_PREDICT)
    finally:
        tick.stop()

    if not raw:
        raise ValueError("Ollama returned empty response for main extraction")
    return parse_json(raw), is_scanned


# ══════════════════════════════════════════
#  EXTRACTION — GRN STAMP
# ══════════════════════════════════════════

def extract_grn(path: Path, total_pages: int) -> tuple[str, str]:
    """
    Scan each page for GRN stamp.
    Returns (grn_number, scroll_number).
    Stops at first page where GRN is found.
    Priority: page 1 (stamps are almost always on page 1).
    """
    # scan order: 1, 2, 3, ... (stamps are usually page 1)
    scan_order = list(range(min(total_pages, 6)))

    for i in scan_order:
        tick = Ticker(f"GRN scan page {i+1}/{min(total_pages,6)}")
        tick.start()
        try:
            img = page_to_b64(path, i)
            raw = ollama_call(GRN_PROMPT, images=[img], num_predict=512)
        finally:
            tick.stop()

        if not raw:
            print(f"    Page {i+1}: (empty response)")
            continue
        try:
            data = parse_json(raw)
            grn    = (data.get("grn_number") or "").strip()
            scroll = (data.get("scroll_number") or "").strip()
            if grn:
                print(f"    ✅ Page {i+1}: GRN='{grn}' SCROLL='{scroll}'")
                return grn, scroll
            elif scroll:
                print(f"    Page {i+1}: Only SCROLL='{scroll}' (GRN not found yet)")
                # keep scanning but remember scroll
                _scroll_found = scroll
            else:
                print(f"    Page {i+1}: (no stamp found)")
        except Exception as e:
            print(f"    Page {i+1}: parse error — {e}")

    # Return scroll if found, GRN empty
    return "", getattr(extract_grn, '_scroll_found', '')


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
    """Normalize GRN: fix OCR errors"""
    s = clean(v)
    if not s: return s
    # dash between digits = "00" (e.g. "50-35 6087" → "5000356087")
    # BUT only when dash separates number groups
    s = re.sub(r"(\d)\s*-\s*(\d)", r"\g<1>00\2", s)
    s = re.sub(r"[\s\.]", "", s)
    # OCR character corrections
    OCR = {'S':'5','s':'5','O':'0','o':'0','I':'1','l':'1',
           'Z':'2','z':'2','B':'8','G':'6','T':'7','A':'4','q':'9'}
    result = "".join(OCR.get(c, c) for c in s)
    return result

def fmt_date(raw: str) -> str:
    if not raw: return ""
    raw = raw.strip()
    MONTHS = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
              "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}
    for fmt in ["%d.%m.%Y","%d/%m/%Y","%Y-%m-%d","%d-%m-%Y",
                "%d-%m-%y","%d/%m/%y","%d %b %Y","%d %B %Y","%b %d, %Y",
                "%d-%b-%y","%d/%b/%Y"]:
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.year < 100: dt = dt.replace(year=dt.year+2000)
            return dt.strftime("%d.%m.%Y")
        except: pass
    m = re.match(r"(\d{1,2})[-/\s]([A-Za-z]+)[-/\s](\d{2,4})", raw)
    if m:
        d, mn, y = int(m.group(1)), m.group(2).lower()[:3], int(m.group(3))
        if y < 100: y += 2000
        mo = MONTHS.get(mn, 0)
        if mo: return f"{d:02d}.{mo:02d}.{y}"
    return raw

def normalize(data: dict, filename: str, grn_raw: str, scroll_raw: str) -> tuple[dict, list]:
    sub  = to_f(data.get("subtotal"))
    cgst = to_f(data.get("cgst"))
    sgst = to_f(data.get("sgst"))
    igst = to_f(data.get("igst"))
    ttax = round(cgst + sgst + igst, 2)
    tot  = to_f(data.get("total"))
    if tot == 0: tot = round(sub + ttax, 2)
    if sub == 0 and tot > 0: sub = round(tot - ttax, 2)

    hsn_top = clean(data.get("hsn_sac"))

    # merge GRN: model might find it in main extraction too
    grn_from_main = clean(data.get("grn_number"))
    scroll_from_main = clean(data.get("scroll_number"))
    final_grn    = clean_grn(grn_raw or grn_from_main)
    final_scroll = clean(scroll_raw or scroll_from_main)

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
        "filename":      filename,
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
        "grn_number":    final_grn,
        "scroll_number": final_scroll,
        "confidence":    round(max(0.0, min(1.0, to_f(data.get("confidence")))) * 100, 1),
    }
    return header, lines


# ══════════════════════════════════════════
#  DB OPERATIONS
# ══════════════════════════════════════════

def is_duplicate(session, inv_no: str, po_no: str, vendor: str) -> tuple[bool, str]:
    if not inv_no: return False, "No invoice number — skip dup check"
    existing = session.scalar(
        select(Invoice).where(Invoice.invoice_number == inv_no)
    )
    if existing:
        return True, f"Invoice {inv_no} already in DB (id={existing.id})"
    return False, "New invoice"

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
        scroll_number=h.get("scroll_number", ""),
        status="REVIEW",
    )
    session.add(inv); session.flush()

    for l in lines:
        session.add(InvoiceLine(
            invoice_id=inv.id, item=l["item"],
            quantity=l["quantity"], rate=l["rate"],
            hsn_sac=l["hsn_sac"], amount=l["amount"],
        ))

    # Upsert master record
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
                "scroll": h.get("scroll_number",""),
                "confidence": h["confidence"]},
    ))
    session.commit()
    return inv.id


# ══════════════════════════════════════════
#  EXCEL OUTPUT
# ══════════════════════════════════════════

def save_excel(h: dict, lines: list, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[<>:"/\\|?*]', "_",
                  h["invoice_number"] or Path(h["filename"]).stem).strip(" ._")
    wb  = Workbook()
    alt = PatternFill("solid", fgColor="EBF3FB")

    def hdr_style(ws, row_n, hex_color):
        fill = PatternFill("solid", fgColor=hex_color)
        for c in ws[row_n]:
            c.fill = fill
            c.font = Font(bold=True, color="FFFFFF", size=10)
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    # Sheet 1 — header fields
    ws1 = wb.active; ws1.title = "Invoice Header"
    ws1.append(["Field", "Value"])
    hdr_style(ws1, 1, "1F4E79")
    rows = [
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
        ("Scroll Number",     h.get("scroll_number","")),
        ("AI Confidence %",   h["confidence"]),
    ]
    for i, (f, v) in enumerate(rows):
        ws1.append([f, v])
        if i % 2 == 1:
            for c in ws1[i+2]: c.fill = alt
    ws1.column_dimensions["A"].width = 22
    ws1.column_dimensions["B"].width = 60
    ws1.freeze_panes = "A2"

    # Sheet 2 — line items
    ws2 = wb.create_sheet("Line Items")
    ws2.append(["Sl.No", "Description", "Qty", "Rate", "HSN/SAC", "Amount"])
    hdr_style(ws2, 1, "145A32")
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
        print(f"\nERROR: Input folder not found:\n  {in_dir}\nPlease update PDF_FOLDER in the script.")
        sys.exit(1)

    # Collect PDFs (unique names only)
    seen, pdfs = set(), []
    for p in sorted(in_dir.iterdir()):
        if p.is_file() and p.suffix.lower() == ".pdf":
            if p.name.lower() not in seen:
                seen.add(p.name.lower()); pdfs.append(p)

    if not pdfs:
        print(f"\nERROR: No PDF files found in:\n  {in_dir}")
        sys.exit(1)

    out_dir.mkdir(parents=True, exist_ok=True)

    print("\n" + "═"*65)
    print("  KTFL Invoice Extractor v3")
    print(f"  Model  : {OLLAMA_MODEL}")
    print(f"  PDFs   : {len(pdfs)} files")
    print(f"  Input  : {in_dir}")
    print(f"  Output : {out_dir}")
    print("═"*65)

    # ── Check Ollama ──────────────────────────────
    print("\n[1/3] Checking Ollama connection...")
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=10, trust_env=False)
        r.raise_for_status()
        models = [m["name"] for m in r.json().get("models", [])]
        match = next((m for m in models if OLLAMA_MODEL.split(":")[0] in m), None)
        if not match:
            print(f"  ERROR: Model '{OLLAMA_MODEL}' not found!")
            print(f"  Available models: {models}")
            sys.exit(1)
        print(f"  ✅ Ollama OK — model ready: {match}")
    except httpx.ConnectError:
        print(f"  ERROR: Cannot connect to Ollama at {OLLAMA_URL}")
        print("  Make sure: ollama serve (is running)")
        sys.exit(1)
    except Exception as e:
        print(f"  ERROR: {e}"); sys.exit(1)

    # ── Check DB ──────────────────────────────────
    print("\n[2/3] Connecting to database...")
    try:
        engine, Session = setup_db()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print(f"  ✅ DB OK — {DB_URL.split('@')[-1]}")
    except Exception as e:
        print(f"  ERROR: DB connection failed: {e}")
        sys.exit(1)

    print(f"\n[3/3] Processing {len(pdfs)} PDF(s)...\n")

    stats = {"ok": 0, "dup": 0, "err": 0}
    start_all = time.time()

    for idx, pdf in enumerate(pdfs, 1):
        t0  = time.time()
        pc  = page_count(pdf)
        print(f"{'─'*65}")
        print(f"  [{idx}/{len(pdfs)}] {pdf.name}")
        print(f"        Pages : {pc}")

        try:
            # ── Pass 1: extract invoice data ───────────────
            text_pages = get_text_pages(pdf)
            is_scanned = all(len(t) < 50 for t in text_pages[:2])
            print(f"        Type  : {'Scanned image PDF' if is_scanned else 'Text-embedded PDF'}")

            print(f"\n  ◆ Pass 1 — Invoice Extraction")
            data, _ = extract_main(pdf, text_pages)

            # ── Pass 2: GRN stamp ──────────────────────────
            print(f"\n  ◆ Pass 2 — GRN Stamp Search")
            grn_raw, scroll_raw = extract_grn(pdf, pc)

            # ── Normalize ──────────────────────────────────
            header, lines = normalize(data, pdf.name, grn_raw, scroll_raw)

            elapsed = round(time.time() - t0, 1)
            print(f"\n  ┌{'─'*45}")
            print(f"  │ Vendor   : {header['vendor'] or '⚠ NOT FOUND'}")
            print(f"  │ Invoice  : {header['invoice_number'] or '⚠ NOT FOUND'}")
            print(f"  │ Date     : {header['invoice_date'] or '⚠ NOT FOUND'}")
            print(f"  │ PO No    : {header['po_number'] or '⚠ NOT FOUND'}")
            print(f"  │ GSTIN    : {header['gstin'] or '⚠ NOT FOUND'}")
            print(f"  │ Total    : ₹{header['total']:,.2f}")
            print(f"  │ CGST     : ₹{header['cgst']:,.2f}  SGST: ₹{header['sgst']:,.2f}  IGST: ₹{header['igst']:,.2f}")
            print(f"  │ GRN No   : {header['grn_number'] or '(not found)'}")
            print(f"  │ Scroll   : {header['scroll_number'] or '(not found)'}")
            print(f"  │ Items    : {len(lines)}")
            print(f"  │ Confidence: {header['confidence']}%")
            print(f"  └{'─'*45}")

            # ── DB save ────────────────────────────────────
            with Session() as session:
                dup, reason = is_duplicate(
                    session, header["invoice_number"],
                    header["po_number"], header["vendor"])

                if dup:
                    print(f"\n  ⚠️  SKIPPED: {reason}")
                    session.add(AuditLog(invoice_id=None, action="DUPLICATE_SKIPPED",
                        detail={"file": pdf.name,
                                "invoice_number": header["invoice_number"]}))
                    session.commit()
                    stats["dup"] += 1
                else:
                    inv_id = save_to_db(session, header, lines, pdf.name)
                    print(f"\n  ✅ Saved to DB — id={inv_id}")
                    stats["ok"] += 1

            # ── Excel save ─────────────────────────────────
            xls = save_excel(header, lines, out_dir)
            print(f"  📄 Excel: {xls.name}")

        except Exception as e:
            elapsed = round(time.time() - t0, 1)
            print(f"\n  ❌ FAILED ({elapsed}s): {e}")
            import traceback; traceback.print_exc()
            try:
                with Session() as s:
                    s.add(AuditLog(invoice_id=None, action="EXTRACTION_ERROR",
                        detail={"file": pdf.name, "error": str(e)}))
                    s.commit()
            except: pass
            stats["err"] += 1

        print(f"  ⏱  {round(time.time()-t0, 1)}s\n")

    total_time = round(time.time() - start_all, 0)
    print("═"*65)
    print(f"  COMPLETED in {int(total_time//60)}m {int(total_time%60)}s")
    print(f"  ✅ Extracted : {stats['ok']}")
    print(f"  ⚠️  Duplicates: {stats['dup']} (skipped)")
    print(f"  ❌ Errors    : {stats['err']}")
    print(f"  📁 Excel     : {out_dir}")
    print("═"*65)


if __name__ == "__main__":
    main()
