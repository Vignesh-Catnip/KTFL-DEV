"""
KTFL Invoice Extractor
======================
- Ollama qwen3-vl:32b  (vision model)
- PostgreSQL  miro_invoice DB
- Extracts: vendor, invoice_no, date, PO, GSTIN, amounts, GRN, line items
- Saves to DB + Excel
"""

import os, base64, json, re, sys, time
from datetime import datetime, timezone
from pathlib import Path

# Remove proxy env vars — Ollama is local
for k in ["HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","http_proxy","https_proxy","all_proxy"]:
    os.environ.pop(k, None)
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
#  CONFIG  — change these if needed
# ─────────────────────────────────────────
PDF_FOLDER    = r"C:\Users\ckts00126\Desktop\KTFL-EXTRACTION\Input-pdf"
OUTPUT_FOLDER = r"C:\Users\ckts00126\Desktop\KTFL-EXTRACTION\Output-Excel"
OLLAMA_URL    = "http://127.0.0.1:11434"
OLLAMA_MODEL  = "qwen3-vl:32b"
DB_URL        = "postgresql+psycopg://appuser:appuser123@10.7.11.22:5432/miro_invoice"

ZOOM          = 1.2    # image quality (1.0=original, 1.5=high but slow)
MAIN_PAGES    = 2      # pages for invoice extraction (header+items on pg 1-2)
GRN_PAGES     = 6      # pages to scan for GRN stamp
NUM_CTX       = 16384
NUM_PREDICT   = 8192   # must be large — thinking tokens eat budget
TIMEOUT       = 900    # seconds per Ollama call
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
    tax_code   : Mapped[str]   = mapped_column(String(30), default="")
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
    # safe column migrations
    cols = [
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
        for sql in cols:
            try: conn.execute(text(sql))
            except: pass
        conn.commit()
    print("  DB ready ✅")
    return engine, Session


# ══════════════════════════════════════════
#  PROMPTS
# ══════════════════════════════════════════

MAIN_PROMPT = """You are an Indian invoice OCR engine. Read the invoice image carefully.
Return ONLY a single JSON object. No explanation. No markdown. No extra text.

Rules:
- vendor: company name on letterhead at TOP (NOT Kalyani Technoforge / KTFL — they are the buyer)
- invoice_number: value next to "Invoice No" / "Bill No" / "Tax Invoice No"
- invoice_date: date next to "Invoice Date" / "Dated"
- po_number: value next to "PO No" / "Purchase Order" / "Our Order No" — remove prefix like "PO-"
- gstin: vendor's 15-char GST number (NOT buyer's GSTIN)
- hsn_sac: HSN code from line items table (4-8 digits)
- vendor_code: number next to "Vendor Code" / "Supplier Code" — if not found use ""
- subtotal: taxable amount before tax (remove ₹ and commas)
- cgst: CGST amount
- sgst: SGST amount
- igst: IGST amount (0 if CGST+SGST present)
- total_tax: cgst + sgst + igst
- total: final payable amount (Grand Total)
- grn_number: always ""
- handwritten_references: any handwritten notes/numbers visible (comma-separated)
- confidence: 0.0 to 1.0 based on image clarity
- lines: array of invoice line items only (NOT tax rows, NOT total rows)

JSON format:
{"vendor":"","invoice_number":"","invoice_date":"","po_number":"","gstin":"","hsn_sac":"","vendor_code":"","currency":"INR","subtotal":0.0,"cgst":0.0,"sgst":0.0,"igst":0.0,"total_tax":0.0,"total":0.0,"grn_number":"","handwritten_references":"","confidence":0.0,"lines":[{"item":"","quantity":0.0,"rate":0.0,"hsn_sac":"","amount":0.0}]}"""

GRN_PROMPT = """Look at this invoice image. Find the RECEIVING STAMP (rubber stamp at bottom/corner added by the receiving company).

Inside the stamp find a line labelled "GRN NO", "GRN No", "GRN", "Inward No", or "MRN No".
Extract ONLY the number next to that label.

IMPORTANT — DO NOT extract from:
- SCROLL NO (most common mistake)
- CH NO, Challan No, E-way Bill No, Vehicle No, Gate Pass No, LR No

Example stamp:
  SCROLL NO: 020040285   <- IGNORE THIS
  GRN NO: 50-35689       <- EXTRACT THIS -> return "50-35689"

If no GRN label found -> return ""
If only SCROLL NO found (no GRN NO) -> return ""

Return ONLY this JSON (nothing else):
{"grn_number":""}"""


# ══════════════════════════════════════════
#  OLLAMA CALL  — /api/generate streaming
# ══════════════════════════════════════════

def ollama_call(prompt: str, images: list[str], num_predict: int = NUM_PREDICT) -> str:
    """
    Stream from Ollama /api/generate.
    Returns raw text (caller parses JSON).
    Uses /api/generate (not /api/chat) — more reliable with vision models.
    """
    payload = {
        "model":   OLLAMA_MODEL,
        "prompt":  prompt,
        "images":  images,
        "stream":  True,
        "think":   False,
        "options": {
            "temperature": 0,
            "num_ctx":     NUM_CTX,
            "num_predict": num_predict,
        },
    }
    collected = []
    with httpx.stream("POST", f"{OLLAMA_URL}/api/generate",
                      json=payload, timeout=TIMEOUT, trust_env=False) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line.strip():
                continue
            try:
                chunk = json.loads(line)
            except Exception:
                continue
            token = chunk.get("response", "")
            if token:
                collected.append(token)
            if chunk.get("done"):
                reason = chunk.get("done_reason", "")
                if reason == "length":
                    print(f"\n    ⚠️  Response cut at {num_predict} tokens — increase NUM_PREDICT")
                break
    return "".join(collected).strip()


def parse_json(raw: str) -> dict:
    """Extract JSON from model output — strips thinking blocks and markdown."""
    # remove <think>...</think>
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL | re.IGNORECASE).strip()
    # remove markdown fences
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw).strip()
    # try direct parse
    try:
        return json.loads(raw)
    except Exception:
        pass
    # find outermost { }
    s, e = raw.find("{"), raw.rfind("}")
    if s >= 0 and e > s:
        try:
            return json.loads(raw[s:e+1])
        except Exception:
            pass
    raise ValueError(f"No JSON in response. First 400 chars:\n{raw[:400]}")


# ══════════════════════════════════════════
#  PDF UTILITIES
# ══════════════════════════════════════════

def pdf_pages_as_b64(path: Path, n: int) -> list[str]:
    """Render first n pages as base64 PNG."""
    doc  = fitz.open(str(path))
    mat  = fitz.Matrix(ZOOM, ZOOM)
    imgs = []
    for i in range(min(n, doc.page_count)):
        pix = doc[i].get_pixmap(matrix=mat, alpha=False)
        imgs.append(base64.b64encode(pix.tobytes("png")).decode())
    doc.close()
    return imgs

def pdf_text(path: Path) -> str:
    """Embedded text from PDF (empty for scanned PDFs)."""
    doc = fitz.open(str(path))
    out = []
    for i, pg in enumerate(doc, 1):
        t = pg.get_text("text").strip()
        if len(t) > 30:
            out.append(f"[PAGE {i}]\n{t[:2000]}")
    doc.close()
    return "\n".join(out)

def page_count(path: Path) -> int:
    doc = fitz.open(str(path))
    n = doc.page_count
    doc.close()
    return n


# ══════════════════════════════════════════
#  EXTRACTION PASSES
# ══════════════════════════════════════════

def extract_invoice(path: Path) -> tuple[dict, list]:
    """Pass 1: extract invoice header + line items."""
    imgs    = pdf_pages_as_b64(path, MAIN_PAGES)
    txt     = pdf_text(path)
    scanned = len(txt.strip()) < 50

    prompt = MAIN_PROMPT
    if not scanned and txt:
        prompt += f"\n\nEMBEDDED TEXT (reference only):\n{txt[:2000]}"

    print(f"    Calling Ollama for main extraction...", flush=True)
    raw = ollama_call(prompt, imgs, NUM_PREDICT)
    if not raw:
        raise ValueError("Ollama returned empty response for main extraction")

    data = parse_json(raw)
    return data, scanned


def extract_grn(path: Path) -> str:
    """Pass 2: scan each page for GRN stamp, return first hit."""
    imgs = pdf_pages_as_b64(path, GRN_PAGES)
    for i, img in enumerate(imgs, 1):
        print(f"    GRN page {i}/{len(imgs)}...", end=" ", flush=True)
        try:
            raw = ollama_call(GRN_PROMPT, [img], num_predict=512)
            if raw:
                data = parse_json(raw)
                grn  = (data.get("grn_number") or "").strip()
                if grn:
                    print(f"✅ found: '{grn}'")
                    return grn
                else:
                    print("(none)")
            else:
                print("(empty)")
        except Exception as e:
            print(f"(err: {e})")
    return ""


# ══════════════════════════════════════════
#  DATA CLEANING
# ══════════════════════════════════════════

def to_f(v) -> float:
    if v is None: return 0.0
    if isinstance(v, (int, float)): return float(v)
    s = re.sub(r"[^\d.\-]", "", str(v).replace(",", ""))
    try: return float(s)
    except: return 0.0

def clean_str(v) -> str:
    return str(v).strip() if v else ""

def clean_po(v) -> str:
    s = clean_str(v)
    return re.sub(r"^(PO|P\.O\.?|P/O)[\-/\s]*", "", s, flags=re.IGNORECASE).strip()

def clean_grn(v) -> str:
    s = clean_str(v)
    if not s: return s
    s = re.sub(r"\s*-\s*", "00", s)   # dash = "00"
    s = re.sub(r"[\s\.]", "", s)
    MAP = {'S':'5','s':'5','O':'0','o':'0','I':'1','l':'1',
           'Z':'2','z':'2','B':'8','G':'6','T':'7','A':'4','q':'9'}
    return "".join(MAP.get(c, c) for c in s)

def fmt_date(raw: str) -> str:
    if not raw: return ""
    raw = raw.strip()
    MONTHS = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
              "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}
    for fmt in ["%d.%m.%Y","%d/%m/%Y","%Y-%m-%d","%d-%m-%Y",
                "%d-%m-%y","%d/%m/%y","%d %b %Y","%d %B %Y","%b %d, %Y"]:
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

def normalize(data: dict, filename: str, grn_raw: str) -> tuple[dict, list]:
    sub  = to_f(data.get("subtotal"))
    cgst = to_f(data.get("cgst"))
    sgst = to_f(data.get("sgst"))
    igst = to_f(data.get("igst"))
    ttax = to_f(data.get("total_tax")) or round(cgst+sgst+igst, 2)
    tot  = to_f(data.get("total"))
    if tot == 0: tot = round(sub + ttax, 2)
    if sub == 0 and tot > 0: sub = round(tot - ttax, 2)

    hsn_top = clean_str(data.get("hsn_sac"))
    hw = data.get("handwritten_references") or ""
    if isinstance(hw, list): hw = ", ".join(str(x) for x in hw if x)

    seen, lines = set(), []
    for row in (data.get("lines") or []):
        if not isinstance(row, dict): continue
        item = clean_str(row.get("item") or row.get("description") or "")
        if not item: continue
        qty  = to_f(row.get("quantity") or row.get("qty"))
        rate = to_f(row.get("rate") or row.get("unit_price"))
        hsn  = clean_str(row.get("hsn_sac") or row.get("hsn") or "") or hsn_top
        amt  = to_f(row.get("amount") or row.get("total"))
        key  = (item.lower()[:40], round(amt, 0))
        if key in seen: continue
        seen.add(key)
        lines.append({"item": item, "quantity": qty, "rate": rate, "hsn_sac": hsn, "amount": amt})

    header = {
        "filename":   filename,
        "vendor":     clean_str(data.get("vendor")),
        "invoice_number": clean_str(data.get("invoice_number")),
        "invoice_date":   fmt_date(clean_str(data.get("invoice_date"))),
        "po_number":      clean_po(data.get("po_number")),
        "gstin":          clean_str(data.get("gstin")),
        "hsn_sac":        hsn_top,
        "vendor_code":    clean_str(data.get("vendor_code")),
        "currency":       clean_str(data.get("currency")) or "INR",
        "subtotal":   round(sub, 2),
        "cgst":       round(cgst, 2),
        "sgst":       round(sgst, 2),
        "igst":       round(igst, 2),
        "total_tax":  round(ttax, 2),
        "total":      round(tot, 2),
        "grn_number": clean_grn(grn_raw),
        "handwritten_references": clean_str(hw),
        "confidence": round(max(0.0, min(1.0, to_f(data.get("confidence")))) * 100, 1),
    }
    return header, lines


# ══════════════════════════════════════════
#  DB SAVE
# ══════════════════════════════════════════

def is_duplicate(session, inv_no, po_no, vc):
    if not inv_no: return False, "No invoice number — no dup check"
    q = [Invoice.invoice_number == inv_no]
    q.append(Invoice.po_number == po_no if po_no else Invoice.po_number == None)
    q.append(Invoice.vendor_code == vc if vc else Invoice.vendor_code == None)
    row = session.scalar(select(Invoice).where(*q))
    if row:
        return True, f"DUP inv={inv_no} po={po_no} vc={vc} (id={row.id})"
    return False, "New — OK"

def save_to_db(session, h: dict, lines: list, fname: str) -> int:
    inv = Invoice(
        filename=fname, vendor=h["vendor"],
        invoice_number=h["invoice_number"], invoice_date=h["invoice_date"],
        po_number=h["po_number"], gstin=h["gstin"], hsn_sac=h["hsn_sac"],
        vendor_code=h["vendor_code"], tax_code="", business_place="",
        currency=h["currency"], subtotal=h["subtotal"],
        cgst=h["cgst"], sgst=h["sgst"], igst=h["igst"],
        tax=h["total_tax"], total=h["total"],
        confidence=h["confidence"]/100,
        grn_number=h["grn_number"],
        handwritten_references=h["handwritten_references"],
        status="REVIEW",
    )
    session.add(inv); session.flush()

    for l in lines:
        session.add(InvoiceLine(
            invoice_id=inv.id, item=l["item"],
            quantity=l["quantity"], rate=l["rate"],
            tax_code=l["hsn_sac"], amount=l["amount"],
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

    def hdr(ws, row_n, color):
        fill = PatternFill("solid", fgColor=color)
        for c in ws[row_n]:
            c.fill = fill
            c.font = Font(bold=True, color="FFFFFF", size=10)
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    # Sheet 1 — header
    ws1 = wb.active; ws1.title = "Invoice Data"
    ws1.append(["Field", "Value"]); hdr(ws1, 1, "1F4E79")
    rows = [
        ("File Name",              h["filename"]),
        ("Vendor",                 h["vendor"]),
        ("Invoice Number",         h["invoice_number"]),
        ("Invoice Date",           h["invoice_date"]),
        ("PO Number",              h["po_number"]),
        ("Vendor GSTIN",           h["gstin"]),
        ("HSN / SAC",              h["hsn_sac"]),
        ("Vendor Code",            h["vendor_code"]),
        ("Currency",               h["currency"]),
        ("Subtotal",               h["subtotal"]),
        ("CGST",                   h["cgst"]),
        ("SGST",                   h["sgst"]),
        ("IGST",                   h["igst"]),
        ("Total Tax",              h["total_tax"]),
        ("Invoice Total",          h["total"]),
        ("GRN / Inward Number",    h["grn_number"]),
        ("Handwritten References", h["handwritten_references"]),
        ("AI Confidence %",        h["confidence"]),
    ]
    for i, (f, v) in enumerate(rows):
        ws1.append([f, v])
        if i % 2 == 1:
            for c in ws1[i+2]: c.fill = alt
    ws1.column_dimensions["A"].width = 28
    ws1.column_dimensions["B"].width = 65
    ws1.freeze_panes = "A2"

    # Sheet 2 — line items
    ws2 = wb.create_sheet("Line Items")
    ws2.append(["Sl.No","Description","Quantity","Rate","HSN/SAC","Amount"])
    hdr(ws2, 1, "145A32")
    for i, l in enumerate(lines, 1):
        ws2.append([i, l["item"], l["quantity"], l["rate"], l["hsn_sac"], l["amount"]])
        if i % 2 == 0:
            for c in ws2[i+1]: c.fill = alt
    for i, w in enumerate([7,55,12,15,15,18], 1):
        ws2.column_dimensions[get_column_letter(i)].width = w
    ws2.freeze_panes = "A2"

    out = out_dir / f"{safe}.xlsx"
    wb.save(out)
    print(f"  📄 Excel saved: {out.name}")


# ══════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════

def main():
    in_dir  = Path(PDF_FOLDER)
    out_dir = Path(OUTPUT_FOLDER)

    if not in_dir.exists():
        print(f"ERROR: Input folder not found: {in_dir}"); sys.exit(1)

    # collect unique PDFs
    seen, pdfs = set(), []
    for p in sorted(in_dir.iterdir()):
        if p.is_file() and p.suffix.lower() == ".pdf":
            if p.name.lower() not in seen:
                seen.add(p.name.lower()); pdfs.append(p)

    if not pdfs:
        print("ERROR: No PDF files found in input folder"); sys.exit(1)

    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 62)
    print("  KTFL Invoice Extractor")
    print(f"  Model : {OLLAMA_MODEL}")
    print(f"  PDFs  : {len(pdfs)}")
    print(f"  DB    : {DB_URL.split('@')[-1]}")
    print("=" * 62)

    # DB
    print("\nConnecting to DB...")
    try:
        engine, Session = setup_db()
    except Exception as e:
        print(f"ERROR: DB connect failed: {e}"); sys.exit(1)

    # Ollama
    print("Checking Ollama...")
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=15, trust_env=False)
        models = [m["name"] for m in r.json().get("models", [])]
        if OLLAMA_MODEL not in models:
            print(f"ERROR: Model '{OLLAMA_MODEL}' not found in Ollama.")
            print(f"  Available: {models}"); sys.exit(1)
        print(f"  Ollama OK — model '{OLLAMA_MODEL}' loaded\n")
    except Exception as e:
        print(f"ERROR: Cannot reach Ollama at {OLLAMA_URL}: {e}"); sys.exit(1)

    stats = {"ok": 0, "dup": 0, "err": 0}

    for idx, pdf in enumerate(pdfs, 1):
        t0 = time.time()
        pc = page_count(pdf)
        print(f"\n{'─'*62}")
        print(f"[{idx}/{len(pdfs)}] {pdf.name}  ({pc} pages)")
        print(f"{'─'*62}")

        try:
            # ── Pass 1: invoice fields ──────────────────────────
            data, scanned = extract_invoice(pdf)
            print(f"  Type   : {'SCANNED image' if scanned else 'Text PDF'}")

            # ── Pass 2: GRN stamp ───────────────────────────────
            print(f"  GRN scan ({min(pc, GRN_PAGES)} pages):")
            grn_raw = extract_grn(pdf)

            # ── Normalise ───────────────────────────────────────
            header, lines = normalize(data, pdf.name, grn_raw)

            print(f"\n  ── Extracted ──────────────────────────────")
            print(f"  Vendor  : {header['vendor'] or '(not found)'}")
            print(f"  Invoice : {header['invoice_number'] or '(not found)'}")
            print(f"  Date    : {header['invoice_date'] or '(not found)'}")
            print(f"  PO      : {header['po_number'] or '(not found)'}")
            print(f"  Total   : {header['total']}")
            print(f"  CGST    : {header['cgst']}  SGST: {header['sgst']}  IGST: {header['igst']}")
            print(f"  GRN     : {header['grn_number'] or '(not found)'}")
            print(f"  Lines   : {len(lines)} items")
            print(f"  Confid  : {header['confidence']}%")

            # ── DB duplicate check ──────────────────────────────
            with Session() as session:
                dup, reason = is_duplicate(
                    session, header["invoice_number"],
                    header["po_number"], header["vendor_code"])
                print(f"  Dup chk : {reason}")

                if dup:
                    print(f"  ⚠️  SKIPPED (duplicate)")
                    session.add(AuditLog(invoice_id=None, action="DUPLICATE_SKIPPED",
                        detail={"file": pdf.name,
                                "invoice_number": header["invoice_number"],
                                "reason": reason}))
                    session.commit()
                    stats["dup"] += 1
                else:
                    inv_id = save_to_db(session, header, lines, pdf.name)
                    print(f"  ✅ DB id={inv_id}")
                    stats["ok"] += 1

            save_excel(header, lines, out_dir)

        except Exception as e:
            elapsed = round(time.time() - t0, 1)
            print(f"\n  ❌ ERROR ({elapsed}s): {e}")
            import traceback; traceback.print_exc()
            try:
                with Session() as s:
                    s.add(AuditLog(invoice_id=None, action="ERROR",
                        detail={"file": pdf.name, "error": str(e)}))
                    s.commit()
            except: pass
            stats["err"] += 1

        print(f"  ⏱  {round(time.time()-t0, 1)}s")

    print(f"\n{'='*62}")
    print(f"  DONE")
    print(f"  ✅ Pushed    : {stats['ok']}")
    print(f"  ⚠️  Duplicate : {stats['dup']} (skipped)")
    print(f"  ❌ Errors    : {stats['err']}")
    print(f"  📁 Excel     : {out_dir}")
    print(f"{'='*62}")


if __name__ == "__main__":
    main()
