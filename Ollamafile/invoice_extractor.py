"""
KTFL Invoice Extractor — LangChain + Ollama (Fast Version)
===========================================================
Optimizations:
  - Single pass extraction (no second validation call)
  - First 2 pages only (header + line items usually on page 1-2)
  - Smarter context window (8192 sufficient for single page)
  - No summary Excel
  - Parallel-ready structure (one PDF at a time, fast)
  - LangChain ChatOllama with raw Ollama fallback
"""

import os
for k in ["HTTP_PROXY","HTTPS_PROXY","ALL_PROXY",
          "http_proxy","https_proxy","all_proxy"]:
    os.environ.pop(k, None)
os.environ["NO_PROXY"] = "localhost,127.0.0.1"
os.environ["no_proxy"] = "localhost,127.0.0.1"

import base64, json, re, sys, time
from pathlib import Path

import fitz
import httpx
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

from langchain_core.messages import HumanMessage
from langchain_ollama import ChatOllama

# ============================================================
# CONFIG
# ============================================================
PDF_FOLDER    = r"C:\Users\ckts00126\Desktop\KTFL-EXTRACTION\Input-pdf"
OLLAMA_URL    = "http://127.0.0.1:11434"
OLLAMA_MODEL  = "qwen3-vl:32b"
OUTPUT_FOLDER = r"C:\Users\ckts00126\Desktop\KTFL-EXTRACTION\Output-Excel"

MAX_PAGES     = 2      # 2 pages sufficient — faster than 3
IMAGE_ZOOM    = 1.4    # slightly lower res — faster, still readable
NUM_CTX       = 8192   # sufficient for single invoice + prompt
NUM_PREDICT   = 2048   # enough for invoice JSON
TIMEOUT       = 900
# ============================================================

PROMPT = r"""
You are an Indian invoice extraction engine.

Read the invoice IMAGE. Return ONLY valid JSON. No markdown. No explanation.

AMOUNT RULES:
  ₹94,540.14  → 94540.14
  ₹6,19,763.14 → 619763.14
  ₹3,16,061   → 316061.0
  Remove ₹ and commas. Keep decimal point exactly.

GRN NUMBER:
  Look for receiving stamp: "GRN", "Inward No", "INWARD NO", "MRN"
  Extract ONLY that stamp number.
  NOT vehicle/challan/e-way bill/security numbers.

HANDWRITTEN REFERENCES:
  Extract ONLY meaningful handwritten PO or reference numbers.
  NOT counting chart data, vehicle numbers, weighment numbers.
  Return as short comma-separated string.

LINE ITEMS:
  Extract ONLY invoice line items with amounts.
  Remove duplicates.
  Apply invoice-level HSN/SAC to all items if only one HSN shown.

Return this JSON:
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

# ── Helpers ──────────────────────────────────────────────────

def to_float(v) -> float:
    if v is None: return 0.0
    if isinstance(v, (int, float)): return float(v)
    s = re.sub(r"[^0-9.\-]", "", str(v).replace(",","").replace("₹",""))
    try: return float(s) if s else 0.0
    except: return 0.0

def clean(v) -> str:
    return str(v).strip() if v else ""

def extract_json(raw) -> dict:
    if isinstance(raw, list):
        raw = "".join(str(x.get("text","") if isinstance(x,dict) else x) for x in raw)
    text = str(raw).strip()
    text = re.sub(r"<think>.*?</think>","",text,flags=re.DOTALL|re.IGNORECASE).strip()
    text = re.sub(r"^```(?:json)?","",text,flags=re.IGNORECASE)
    text = re.sub(r"```$","",text).strip()
    try: return json.loads(text)
    except: pass
    s, e = text.find("{"), text.rfind("}")
    if s>=0 and e>s:
        try: return json.loads(text[s:e+1])
        except: pass
    raise ValueError("No valid JSON in response")

def pdf_to_images(path: Path):
    doc  = fitz.open(str(path))
    imgs = []
    zoom = fitz.Matrix(IMAGE_ZOOM, IMAGE_ZOOM)
    for i in range(min(doc.page_count, MAX_PAGES)):
        pix = doc[i].get_pixmap(matrix=zoom, alpha=False)
        imgs.append(base64.b64encode(pix.tobytes("png")).decode())
    doc.close()
    return imgs

def get_pdf_text(path: Path) -> str:
    doc   = fitz.open(str(path))
    parts = []
    for i, page in enumerate(doc, 1):
        t = page.get_text("text")
        if t: parts.append(f"--- PAGE {i} ---\n{t[:2000]}")
    doc.close()
    return "\n".join(parts)

def fix_amounts(subtotal, cgst, sgst, igst, total_tax, total):
    """Fix missing decimal points (e.g. 9454014 → 94540.14)"""
    if total > 0 and total_tax > total:
        candidate = round(total_tax / 100, 2)
        if abs(subtotal + candidate - total) < total * 0.02:
            total_tax = candidate
            cgst = round(cgst/100, 2) if cgst else 0
            sgst = round(sgst/100, 2) if sgst else 0
            igst = round(igst/100, 2) if igst else 0
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

# ── LangChain call ────────────────────────────────────────────

def create_llm():
    return ChatOllama(
        model=OLLAMA_MODEL,
        base_url=OLLAMA_URL,
        temperature=0,
        num_ctx=NUM_CTX,
        num_predict=NUM_PREDICT,
        format="json",
        client_kwargs={"trust_env": False},
    )

def invoke(llm, images, pdf_text="") -> dict:
    """LangChain call with raw Ollama fallback."""

    # ── LangChain ──
    try:
        content = [{"type":"text","text":PROMPT}]
        if pdf_text:
            content.append({"type":"text","text":
                "\nOCR (secondary):\n"+pdf_text[:2000]})
        for img in images:
            content.append({"type":"image_url",
                "image_url":f"data:image/png;base64,{img}"})
        resp = llm.invoke([HumanMessage(content=content)])
        return extract_json(resp.content)
    except Exception as e:
        print(f"     LangChain failed: {e} — trying raw Ollama...")

    # ── Raw Ollama fallback ──
    body = PROMPT
    if pdf_text: body += "\nOCR:\n" + pdf_text[:2000]
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [{"role":"user","content":body,"images":images}],
        "stream": False, "format": "json", "think": False,
        "options": {"temperature":0,"num_ctx":NUM_CTX,"num_predict":NUM_PREDICT},
    }
    r = httpx.post(f"{OLLAMA_URL}/api/chat",
                   json=payload, timeout=TIMEOUT, trust_env=False)
    r.raise_for_status()
    msg = r.json().get("message",{})
    raw = (msg.get("content") or msg.get("thinking") or "").strip()
    if not raw: raise ValueError("Ollama returned empty response")
    return extract_json(raw)

# ── Normalize ─────────────────────────────────────────────────

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
    seen    = set()
    lines   = []

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
        if amt==0 and qty==0 and rate==0 and len(item)<6: continue
        lines.append({"item":item,"quantity":qty,"rate":rate,
                      "hsn_sac":hsn,"amount":amt})

    for line in lines:
        if not line["hsn_sac"] and hsn_inv:
            line["hsn_sac"] = hsn_inv

    hw = data.get("handwritten_references") or ""
    if isinstance(hw, list): hw = ", ".join(str(x) for x in hw if x)

    return {
        "filename":    filename,
        "vendor":      clean(data.get("vendor")),
        "invoice_number": clean(data.get("invoice_number")),
        "invoice_date":   clean(data.get("invoice_date")),
        "po_number":      clean(data.get("po_number")),
        "gstin":          clean(data.get("gstin")),
        "hsn_sac":        hsn_inv,
        "vendor_code":    clean(data.get("vendor_code")),
        "currency":       clean(data.get("currency")) or "INR",
        "subtotal":   round(subtotal,2),
        "cgst":       round(cgst,2),
        "sgst":       round(sgst,2),
        "igst":       round(igst,2),
        "total_tax":  round(total_tax,2),
        "total":      round(total,2),
        "grn_number": clean(data.get("grn_number") or data.get("grn") or ""),
        "handwritten_references": clean(hw),
        "confidence": round(max(0,min(1,to_float(data.get("confidence"))))*100,1),
    }, lines

# ── Excel ─────────────────────────────────────────────────────

def style_hdr(ws, row, color):
    fill = PatternFill("solid", fgColor=color)
    font = Font(bold=True, color="FFFFFF", size=10)
    for c in ws[row]:
        c.fill=fill; c.font=font
        c.alignment=Alignment(horizontal="center",vertical="center",wrap_text=True)

def safe_filename(v):
    v = re.sub(r'[<>:"/\\|?*]',"_",str(v))
    return re.sub(r"\s+","_",v).strip(" ._") or "UNKNOWN"

def create_excel(header, lines, out_folder):
    Path(out_folder).mkdir(parents=True, exist_ok=True)
    fname = safe_filename(header["invoice_number"] or
                          Path(header["filename"]).stem) + ".xlsx"
    path  = Path(out_folder)/fname
    wb    = Workbook()
    alt   = PatternFill("solid",fgColor="EBF3FB")

    # Sheet 1
    ws1 = wb.active; ws1.title="Invoice Data"
    ws1.append(["Field","Value"]); style_hdr(ws1,1,"1F4E79")
    rows = [
        ("File Name",               header["filename"]),
        ("Vendor",                  header["vendor"]),
        ("Invoice Number",          header["invoice_number"]),
        ("Invoice Date",            header["invoice_date"]),
        ("PO Number",               header["po_number"]),
        ("Vendor GSTIN",            header["gstin"]),
        ("HSN / SAC",               header["hsn_sac"]),
        ("Vendor Code",             header["vendor_code"]),
        ("Currency",                header["currency"]),
        ("Subtotal (Taxable Value)",header["subtotal"]),
        ("CGST",                    header["cgst"]),
        ("SGST",                    header["sgst"]),
        ("IGST",                    header["igst"]),
        ("Total Tax",               header["total_tax"]),
        ("Invoice Total",           header["total"]),
        ("GRN / Inward Number",     header["grn_number"]),
        ("Handwritten References",  header["handwritten_references"]),
        ("AI Confidence %",         header["confidence"]),
    ]
    for i,(f,v) in enumerate(rows):
        ws1.append([f,v])
        if i%2==1:
            for c in ws1[i+2]: c.fill=alt
    ws1.column_dimensions["A"].width=28
    ws1.column_dimensions["B"].width=65
    ws1.freeze_panes="A2"

    # Sheet 2
    ws2 = wb.create_sheet("Line Items")
    ws2.append(["Sl.No","Item / Description","Quantity","Rate","HSN/SAC","Amount"])
    style_hdr(ws2,1,"145A32")
    for i,l in enumerate(lines,1):
        ws2.append([i,l["item"],l["quantity"],l["rate"],l["hsn_sac"],l["amount"]])
        if i%2==0:
            for c in ws2[i+1]: c.fill=alt
    for i,w in enumerate([8,60,12,15,18,18],1):
        ws2.column_dimensions[get_column_letter(i)].width=w
    ws2.freeze_panes="A2"

    wb.save(path)
    print(f"  ✅ {fname}")

# ── Main ──────────────────────────────────────────────────────

def main():
    folder = Path(PDF_FOLDER)
    out    = Path(OUTPUT_FOLDER)
    if not folder.exists():
        print(f"ERROR: {folder}"); sys.exit(1)

    seen=set(); pdfs=[]
    for p in sorted(folder.iterdir()):
        if p.is_file() and p.suffix.lower()==".pdf":
            if p.name.lower() not in seen:
                seen.add(p.name.lower()); pdfs.append(p)

    if not pdfs: print("ERROR: No PDFs found"); sys.exit(1)

    out.mkdir(parents=True, exist_ok=True)

    print("="*55)
    print("KTFL Invoice Extractor (Fast) — LangChain+Ollama")
    print(f"PDFs  : {len(pdfs)} | Model: {OLLAMA_MODEL}")
    print(f"Pages : {MAX_PAGES} per PDF | Ctx: {NUM_CTX}")
    print("="*55)

    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags",timeout=10,trust_env=False)
        models = [m["name"] for m in r.json().get("models",[])]
        if OLLAMA_MODEL not in models:
            print(f"ERROR: {OLLAMA_MODEL} not installed.")
            sys.exit(1)
        print(f"Ollama: OK\n")
    except Exception as e:
        print(f"ERROR: Ollama connect failed: {e}"); sys.exit(1)

    llm = create_llm()

    for idx, pdf in enumerate(pdfs, 1):
        t0 = time.time()
        print(f"[{idx}/{len(pdfs)}] {pdf.name}")
        try:
            images   = pdf_to_images(pdf)
            pdf_text = get_pdf_text(pdf)
            data     = invoke(llm, images, pdf_text)
            header, lines = normalize(data, pdf.name)
            create_excel(header, lines, out)
            elapsed = round(time.time()-t0, 1)
            print(f"  Invoice : {header['invoice_number'] or '?'}")
            print(f"  Vendor  : {header['vendor'] or '?'}")
            print(f"  Total   : {header['total']}  Tax: {header['total_tax']}")
            print(f"  GRN     : {header['grn_number'] or '-'}")
            print(f"  Lines   : {len(lines)}  Time: {elapsed}s")
        except Exception as e:
            print(f"  ❌ {e}")
        if idx < len(pdfs): time.sleep(0.5)

    print("\n✅ Done — Excels saved to:", out)

if __name__ == "__main__":
    main()
