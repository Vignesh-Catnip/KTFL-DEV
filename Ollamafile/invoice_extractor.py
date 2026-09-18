"""
Invoice Extractor — Batch PDF extraction using Ollama
======================================================
Usage:
    python invoice_extractor.py

Config (edit below):
    PDF_FOLDER  : folder containing invoice PDFs
    OLLAMA_URL  : Ollama server URL
    OLLAMA_MODEL: model name (must support vision)
    OUTPUT_FILE : output Excel file name
"""

import base64
import json
import re
import sys
import time
from pathlib import Path

import httpx
import fitz  # PyMuPDF
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

# ============================================================
# CONFIG — change these as needed
# ============================================================
PDF_FOLDER   = r"C:\Users\ckts00126\Desktop\KTFL-EXTRACTION\Input-pdf"
OLLAMA_URL   = "http://localhost:11434"
OLLAMA_MODEL = "qwen3-vl:32b"
OUTPUT_FILE  = r"C:\Users\ckts00126\Desktop\KTFL-EXTRACTION\Output-Excel\invoice_extract.xlsx"
MAX_PAGES    = 3
TIMEOUT      = 600
# ============================================================

PROMPT = """You are an invoice data extraction engine.

Read the invoice image carefully and extract ALL visible fields.

Return ONLY a valid JSON object. No markdown, no code fences, no explanation.
If a value is not visible, return empty string "" for text or 0 for numbers.
Never invent values. Dates must be YYYY-MM-DD format. Amounts must be plain numbers only.

Return exactly this JSON structure:
{
  "vendor": "",
  "invoice_number": "",
  "invoice_date": "",
  "po_number": "",
  "gstin": "",
  "hsn_sac": "",
  "vendor_code": "",
  "tax_code": "",
  "business_place": "",
  "currency": "INR",
  "subtotal": 0,
  "cgst": 0,
  "sgst": 0,
  "igst": 0,
  "tax": 0,
  "total": 0,
  "confidence": 0,
  "lines": [
    {"item": "", "quantity": 0, "rate": 0, "hsn_sac": "", "amount": 0}
  ]
}"""


def to_float(v):
    if v is None: return 0.0
    if isinstance(v, (int, float)): return float(v)
    cleaned = re.sub(r'[^0-9.\-]', '', str(v).replace(',', ''))
    try: return float(cleaned) if cleaned else 0.0
    except: return 0.0


def extract_json(raw):
    text = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL | re.IGNORECASE).strip()
    text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*```$', '', text).strip()
    try: return json.loads(text)
    except: pass
    s, e = text.find('{'), text.rfind('}')
    if s != -1 and e > s:
        try: return json.loads(text[s:e+1])
        except: pass
    raise ValueError("No valid JSON in response")


def pdf_to_images(pdf_path):
    doc = fitz.open(str(pdf_path))
    images = []
    zoom = fitz.Matrix(1.6, 1.6)
    for i in range(min(doc.page_count, MAX_PAGES)):
        pix = doc[i].get_pixmap(matrix=zoom, alpha=False)
        images.append(base64.b64encode(pix.tobytes("png")).decode())
    doc.close()
    return images


def call_ollama(images):
    url = f"{OLLAMA_URL.rstrip('/')}/api/generate"
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": PROMPT,
        "stream": False,
        "think": False,
        "images": images,
        "options": {"num_ctx": 8192, "num_predict": 4096},
    }
    resp = httpx.post(url, json=payload, timeout=TIMEOUT, trust_env=False)
    resp.raise_for_status()
    body = resp.json()
    raw = (body.get("response") or body.get("thinking") or "").strip()
    if not raw:
        raise ValueError(f"Ollama returned empty response (done_reason={body.get('done_reason')})")
    return raw


def normalize(data, filename):
    if not isinstance(data, dict): data = {}
    inv = data.get("invoice") if isinstance(data.get("invoice"), dict) else data
    lines_raw = inv.get("lines") or inv.get("line_items") or inv.get("items") or []

    cgst     = to_float(inv.get("cgst"))
    sgst     = to_float(inv.get("sgst"))
    igst     = to_float(inv.get("igst"))
    tax      = to_float(inv.get("tax")) or cgst + sgst + igst
    subtotal = to_float(inv.get("subtotal") or inv.get("taxable_value"))
    total    = to_float(inv.get("total") or inv.get("grand_total"))
    if subtotal == 0 and lines_raw:
        subtotal = sum(to_float(l.get("amount")) for l in lines_raw if isinstance(l, dict))
    if total == 0:
        total = subtotal + tax

    lines = []
    for l in lines_raw:
        if not isinstance(l, dict): continue
        lines.append({
            "filename":       filename,
            "invoice_number": str(inv.get("invoice_number") or "").strip(),
            "item":           str(l.get("item") or l.get("description") or l.get("name") or "").strip(),
            "quantity":       to_float(l.get("quantity") or l.get("qty")),
            "rate":           to_float(l.get("rate") or l.get("unit_price")),
            "hsn_sac":        str(l.get("hsn_sac") or l.get("hsn") or l.get("sac") or "").strip(),
            "amount":         to_float(l.get("amount") or l.get("total")),
        })

    header = {
        "filename":       filename,
        "vendor":         str(inv.get("vendor") or "").strip(),
        "invoice_number": str(inv.get("invoice_number") or "").strip(),
        "invoice_date":   str(inv.get("invoice_date") or "").strip(),
        "po_number":      str(inv.get("po_number") or "").strip(),
        "gstin":          str(inv.get("gstin") or "").strip(),
        "hsn_sac":        str(inv.get("hsn_sac") or "").strip(),
        "vendor_code":    str(inv.get("vendor_code") or "").strip(),
        "tax_code":       str(inv.get("tax_code") or "").strip(),
        "business_place": str(inv.get("business_place") or "").strip(),
        "currency":       str(inv.get("currency") or "INR").strip() or "INR",
        "subtotal":       subtotal,
        "cgst":           cgst,
        "sgst":           sgst,
        "igst":           igst,
        "total_tax":      tax,
        "total":          total,
        "confidence":     round(max(0.0, min(1.0, to_float(inv.get("confidence")))) * 100, 1),
    }
    return header, lines


def style_header_row(ws, row, fill_color="1F4E79"):
    fill = PatternFill("solid", fgColor=fill_color)
    font = Font(bold=True, color="FFFFFF", size=10)
    for cell in ws[row]:
        cell.fill = fill
        cell.font = font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def auto_width(ws):
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            try:
                val = str(cell.value or "")
                max_len = max(max_len, len(val))
            except: pass
        ws.column_dimensions[col_letter].width = min(max(max_len + 2, 10), 40)


def build_excel(headers, all_lines, output_path):
    # Make sure output folder exists
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()

    # ── Sheet 1: Header Data ──────────────────────────────
    ws1 = wb.active
    ws1.title = "Header Data"

    h_cols = [
        "File Name", "Vendor", "Invoice Number", "Invoice Date",
        "PO Number", "GSTIN", "HSN/SAC", "Vendor Code", "Tax Code",
        "Business Place", "Currency", "Subtotal", "CGST", "SGST",
        "IGST", "Total Tax", "Invoice Total", "Confidence %"
    ]
    h_keys = [
        "filename", "vendor", "invoice_number", "invoice_date",
        "po_number", "gstin", "hsn_sac", "vendor_code", "tax_code",
        "business_place", "currency", "subtotal", "cgst", "sgst",
        "igst", "total_tax", "total", "confidence"
    ]

    ws1.append(h_cols)
    style_header_row(ws1, 1)

    alt_fill = PatternFill("solid", fgColor="EBF3FB")
    num_keys = {"subtotal","cgst","sgst","igst","total_tax","total","confidence"}

    for idx, h in enumerate(headers):
        row = [h.get(k, "") for k in h_keys]
        ws1.append(row)
        if idx % 2 == 1:
            for cell in ws1[idx + 2]:
                cell.fill = alt_fill
        for ci, key in enumerate(h_keys, 1):
            if key in num_keys:
                ws1.cell(row=idx+2, column=ci).alignment = Alignment(horizontal="right")

    auto_width(ws1)
    ws1.freeze_panes = "A2"

    # ── Sheet 2: Line Items ───────────────────────────────
    ws2 = wb.create_sheet("Line Items")

    l_cols = ["File Name", "Invoice Number", "Item / Description",
              "Quantity", "Rate", "HSN/SAC", "Amount"]
    l_keys = ["filename", "invoice_number", "item",
              "quantity", "rate", "hsn_sac", "amount"]

    ws2.append(l_cols)
    style_header_row(ws2, 1, fill_color="145A32")

    for idx, line in enumerate(all_lines):
        row = [line.get(k, "") for k in l_keys]
        ws2.append(row)
        if idx % 2 == 1:
            for cell in ws2[idx + 2]:
                cell.fill = alt_fill
        for ci, key in enumerate(l_keys, 1):
            if key in ("quantity", "rate", "amount"):
                ws2.cell(row=idx+2, column=ci).alignment = Alignment(horizontal="right")

    auto_width(ws2)
    ws2.freeze_panes = "A2"

    wb.save(output_path)
    print(f"\n✅ Excel saved: {output_path}")
    print(f"   Sheet 1 'Header Data' : {len(headers)} invoices")
    print(f"   Sheet 2 'Line Items'  : {len(all_lines)} line items")


def main():
    folder = Path(PDF_FOLDER)
    if not folder.exists():
        print(f"❌ Folder not found: {folder}")
        sys.exit(1)

    # Fix: deduplicate — Windows glob returns .pdf and .PDF as same file
    seen = set()
    pdfs = []
    for p in sorted(folder.glob("*")):
        if p.suffix.lower() == ".pdf" and p.name.lower() not in seen:
            seen.add(p.name.lower())
            pdfs.append(p)

    if not pdfs:
        print(f"❌ No PDF files found in: {folder}")
        sys.exit(1)

    print(f"📁 Found {len(pdfs)} PDF(s) in: {folder}")
    print(f"🤖 Model : {OLLAMA_MODEL}")
    print(f"📄 Output: {OUTPUT_FILE}\n")

    all_headers = []
    all_lines   = []

    for i, pdf in enumerate(pdfs, 1):
        print(f"[{i}/{len(pdfs)}] Processing: {pdf.name} ...", end=" ", flush=True)
        try:
            images        = pdf_to_images(pdf)
            raw           = call_ollama(images)
            data          = extract_json(raw)
            header, lines = normalize(data, pdf.name)
            all_headers.append(header)
            all_lines.extend(lines)
            print(f"✅  vendor={header['vendor'] or '?'}  total={header['total']}")
        except Exception as e:
            print(f"❌  Error: {e}")
            all_headers.append({
                "filename":       pdf.name,
                "vendor":         "EXTRACTION FAILED",
                "invoice_number": "", "invoice_date":   "",
                "po_number":      "", "gstin":          "",
                "hsn_sac":        "", "vendor_code":    "",
                "tax_code":       "", "business_place": "",
                "currency":       "INR",
                "subtotal": 0, "cgst": 0, "sgst": 0,
                "igst": 0, "total_tax": 0, "total": 0, "confidence": 0,
            })
        if i < len(pdfs):
            time.sleep(1)

    build_excel(all_headers, all_lines, OUTPUT_FILE)


if __name__ == "__main__":
    main()
