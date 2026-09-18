"""
KTFL MIRO Invoice Extractor
===========================

Tech:
    - LangChain
    - Ollama
    - Qwen3-VL vision model
    - PyMuPDF
    - OpenPyXL

Requirements:
    1. Extract one invoice per PDF.
    2. Create ONE Excel file for EACH invoice number.
    3. Extract printed + handwritten PO numbers.
    4. Extract handwritten reference numbers when visible.
    5. Extract HSN/SAC accurately.
    6. If one HSN/SAC is printed for the whole invoice, apply it to
       every line item.
    7. Read handwritten values from the invoice image.
    8. Use a second vision validation pass for PO / HSN-SAC / handwriting.
    9. Never invent values.
"""

import os

# ============================================================
# LOCAL OLLAMA PROXY FIX
# ============================================================
for proxy_key in [
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
]:
    os.environ.pop(proxy_key, None)

os.environ["NO_PROXY"] = "localhost,127.0.0.1"
os.environ["no_proxy"] = "localhost,127.0.0.1"

import base64
import json
import re
import sys
import time
import httpx
from pathlib import Path
from typing import List

import fitz
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

from langchain_core.messages import HumanMessage
from langchain_ollama import ChatOllama
from pydantic import BaseModel, Field


# ============================================================
# CONFIG
# ============================================================

PDF_FOLDER = r"C:\Users\ckts00126\Desktop\KTFL-EXTRACTION\Input-pdf"

OLLAMA_URL = "http://127.0.0.1:11434"
OLLAMA_MODEL = "qwen3-vl:32b"

OUTPUT_FOLDER = (
    r"C:\Users\ckts00126\Desktop\KTFL-EXTRACTION\Output-Excel"
)

MAX_PAGES = 6
IMAGE_ZOOM = 1.5
TIMEOUT = 900

# True = also create a combined summary Excel
CREATE_SUMMARY = True

# ============================================================


# ============================================================
# DATA MODELS
# ============================================================

class LineItem(BaseModel):
    item: str = ""
    quantity: float = 0
    rate: float = 0
    hsn_sac: str = ""
    amount: float = 0


class InvoiceData(BaseModel):
    vendor: str = ""
    invoice_number: str = ""
    invoice_date: str = ""

    po_number: str = ""
    po_numbers: List[str] = Field(default_factory=list)

    handwritten_references: List[str] = Field(default_factory=list)

    gstin: str = ""
    hsn_sac: str = ""

    vendor_code: str = ""
    tax_code: str = ""
    business_place: str = ""
    currency: str = "INR"

    subtotal: float = 0
    cgst: float = 0
    sgst: float = 0
    igst: float = 0
    total_tax: float = 0
    total: float = 0

    confidence: float = 0

    lines: List[LineItem] = Field(default_factory=list)


# ============================================================
# MAIN EXTRACTION PROMPT
# ============================================================

EXTRACTION_PROMPT = r"""
You are an expert Indian invoice/document extraction engine.

Your job is to read the invoice IMAGE very carefully.

============================================================
CRITICAL IMAGE READING RULES
============================================================

1. IMAGE HAS PRIORITY.
   Do NOT depend only on OCR text.

2. Read BOTH:
   - printed text
   - handwritten text

3. Handwritten text is important.
   It may contain:
   - PO number
   - Purchase Order number
   - PO reference
   - reference number
   - document number
   - handwritten invoice/reference annotations

4. Look at the ENTIRE invoice page, especially:
   - top margin above invoice header
   - top-right area
   - beside Invoice/Bill number
   - beside PO references
   - handwritten notes
   - stamps
   - handwritten numbers near tables

============================================================
PO NUMBER RULE
============================================================

Extract PO number even when there is NO printed "PO Number" label.

Examples of possible formats:
- PO-710067998
- PO 710067998
- P.O. 710067998
- PO/710067998
- 710067998 when clearly handwritten as a PO reference

If multiple PO/reference numbers exist:
- put the actual PO in "po_number" when identifiable
- put all clearly visible PO numbers in "po_numbers"
- put other handwritten reference numbers in "handwritten_references"

DO NOT confuse:
- invoice number
- GRN number
- security number
- e-way bill number
- challan number
with PO number.

============================================================
HSN / SAC RULE
============================================================

Search carefully for:
- HSN/SAC
- HSN
- SAC
- SAC Code
- HSN Code

Preserve the exact digits printed.

Do NOT change:
- leading zeros
- digit order
- 4/6/8 digit length

VERY IMPORTANT:

If the invoice has ONE clear invoice-level HSN/SAC and multiple
line items, assume that HSN/SAC applies to ALL line items unless
the invoice explicitly shows different HSN/SAC codes for different
items.

Example:

HSN/SAC: 998519

Items:
1. Mandays
2. Employer PF
3. Employer ESIC
4. MLWF
5. Service Charge

Then return:
line 1 HSN/SAC = 998519
line 2 HSN/SAC = 998519
line 3 HSN/SAC = 998519
line 4 HSN/SAC = 998519
line 5 HSN/SAC = 998519

============================================================
HANDWRITING
============================================================

If handwritten text is visible and readable, extract it EXACTLY. Pay special attention to handwritten PO/reference numbers above the invoice header.

Do NOT ignore handwriting.

If handwriting is unclear:
- do not invent it
- return empty string
- but include clearly readable handwritten values

============================================================
DATE
============================================================

Convert unambiguous dates to:
YYYY-MM-DD

============================================================
AMOUNTS
============================================================

Return numbers only.

Examples:
₹3,16,061 -> 316061
17,550 -> 17550

============================================================
LINE ITEMS
============================================================

Extract ALL visible line items.

For each:
- item
- quantity
- rate
- HSN/SAC
- amount

============================================================
RETURN ONLY JSON
============================================================

{
  "vendor": "",
  "invoice_number": "",
  "invoice_date": "",

  "po_number": "",
  "po_numbers": [],
  "handwritten_references": [],

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
  "total_tax": 0,
  "total": 0,

  "confidence": 0,

  "lines": [
    {
      "item": "",
      "quantity": 0,
      "rate": 0,
      "hsn_sac": "",
      "amount": 0
    }
  ]
}

NEVER add explanation outside JSON.
"""


# ============================================================
# VALIDATION / SECOND VISION PASS
# ============================================================

VALIDATION_PROMPT = r"""
You are doing a SECOND visual verification of an Indian invoice.

Ignore any previous answer.

Look directly at the invoice image and verify ONLY these fields:

1. Invoice / Bill Number
2. PO Number
3. ALL PO numbers visible anywhere on the page
4. Handwritten reference numbers
5. HSN/SAC
6. HSN/SAC applicable to the invoice
7. GSTIN
8. Invoice Date
9. Invoice Total

============================================================
PO CHECK
============================================================

Inspect the TOP MARGIN and TOP-RIGHT area.

If a handwritten PO number is visible above the invoice,
extract it.

Example:
PO-710067998

Do not mistake handwritten invoice references for PO unless
the visual context indicates they are PO related.

============================================================
HSN/SAC CHECK
============================================================

Look specifically at:
- HSN/SAC field
- SAC Code field
- HSN Code field
- line-item table

If ONE HSN/SAC is clearly shown for the invoice and multiple
items exist, return that ONE code.

Preserve exact digits.

============================================================
HANDWRITING CHECK
============================================================

Read handwritten numbers if they are clearly readable.

Do not invent unclear handwriting.

Return ONLY JSON:

{
  "invoice_number": "",
  "invoice_date": "",
  "po_number": "",
  "po_numbers": [],
  "handwritten_references": [],
  "gstin": "",
  "hsn_sac": "",
  "line_hsn_sac": [],
  "total": 0
}
"""


# ============================================================
# TARGETED HANDWRITING / PO CHECK
# ============================================================

HANDWRITING_PROMPT = r"""
Read ONLY the invoice image and carefully inspect the entire top area,
especially the handwritten notes above and around the invoice header.

Extract:
1. Clearly readable handwritten PO number(s).
2. Clearly readable handwritten reference number(s).
3. Invoice/Bill number if visible.
4. HSN/SAC if visible.

Important:
- Handwritten text is required.
- Preserve the exact characters and digits.
- A handwritten value such as PO-710067998 must be captured exactly.
- Do not convert or guess unclear handwriting.
- Do not confuse invoice number, GRN, security number, challan,
  e-way bill, or other references with PO.
- If the image clearly shows a PO reference without a "PO" label,
  return it in po_number only when the visual context identifies it
  as a PO.
- Return ONLY valid JSON.

{
  "invoice_number": "",
  "po_number": "",
  "po_numbers": [],
  "handwritten_references": [],
  "hsn_sac": ""
}
"""


# ============================================================
# IMAGE / PDF HELPERS
# ============================================================

def pdf_to_images(pdf_path: Path):
    doc = fitz.open(str(pdf_path))

    images = []

    zoom = fitz.Matrix(IMAGE_ZOOM, IMAGE_ZOOM)

    page_count = min(doc.page_count, MAX_PAGES)

    for page_index in range(page_count):
        page = doc[page_index]

        pix = page.get_pixmap(
            matrix=zoom,
            alpha=False
        )

        png_bytes = pix.tobytes("png")

        images.append(
            base64.b64encode(png_bytes).decode("utf-8")
        )

    doc.close()

    return images


def get_pdf_text(pdf_path: Path):
    doc = fitz.open(str(pdf_path))

    text_parts = []

    for page_number, page in enumerate(doc, start=1):
        text = page.get_text("text")

        if text:
            text_parts.append(
                f"\n--- PAGE {page_number} ---\n{text}"
            )

    doc.close()

    return "\n".join(text_parts)


# ============================================================
# LANGCHAIN / OLLAMA
# ============================================================

def create_llm():
    # LangChain client is kept for the project architecture.
    # For Qwen3-VL, invoke_model() uses Ollama's native /api/chat
    # with think=False, because some LangChain/Ollama combinations
    # can place the answer in the thinking channel.
    return ChatOllama(
        model=OLLAMA_MODEL,
        base_url=OLLAMA_URL,
        temperature=0,
        num_ctx=16384,
        num_predict=3000,
        reasoning=False,
        format="json",
        client_kwargs={"trust_env": False},
    )

def create_message(prompt, images, pdf_text=""):

    content = []

    content.append({
        "type": "text",
        "text": prompt
    })

    if pdf_text:

        # OCR text is ONLY secondary reference.
        # Image has priority.
        reference = (
            "\n\nSECONDARY OCR REFERENCE.\n"
            "WARNING: OCR CAN BE WRONG.\n"
            "Use this only to cross-check the image.\n\n"
            + pdf_text[:5000]
        )

        content.append({
            "type": "text",
            "text": reference
        })

    for image in images:

        content.append({
            "type": "image_url",
            "image_url": (
                f"data:image/png;base64,{image}"
            )
        })

    return HumanMessage(
        content=content
    )


def clean_json_response(raw):
    if isinstance(raw, list):
        parts = []
        for item in raw:
            if isinstance(item, dict):
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        raw = "".join(parts)

    text = str(raw).strip()

    text = re.sub(
        r"<think>.*?</think>",
        "",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    text = re.sub(
        r"```(?:json)?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = text.replace("```", "").strip()

    # Direct JSON
    try:
        return json.loads(text)
    except Exception:
        pass

    # JSON object embedded in extra text
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass

    # Balanced JSON object fallback
    depth = 0
    object_start = None
    for i, char in enumerate(text):
        if char == "{":
            if object_start is None:
                object_start = i
            depth += 1
        elif char == "}" and object_start is not None:
            depth -= 1
            if depth == 0:
                candidate = text[object_start:i + 1]
                try:
                    return json.loads(candidate)
                except Exception:
                    object_start = None

    raise ValueError(
        "Model did not return valid JSON. Raw response: "
        + text[:3000]
    )


def _ollama_native_chat(prompt, images, pdf_text=""):
    """
    Reliable Qwen3-VL call using Ollama's native API.

    Important:
    - think=False prevents Qwen3-VL from returning only reasoning.
    - format=json requests a JSON object.
    - trust_env=False prevents proxy settings from touching localhost.
    """

    content = prompt

    if pdf_text:
        content += (
            "\n\nSECONDARY OCR REFERENCE.\n"
            "OCR may be wrong. IMAGE HAS PRIORITY.\n"
            "Use OCR only to cross-check printed text.\n\n"
            + pdf_text[:5000]
        )

    # Ollama native vision format: one text message + image base64 list.
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {
                "role": "user",
                "content": content,
                "images": images,
            }
        ],
        "stream": False,
        "format": "json",
        "think": False,
        "options": {
            "temperature": 0,
            "num_ctx": 16384,
            "num_predict": 3000,
        },
    }

    response = httpx.post(
        f"{OLLAMA_URL}/api/chat",
        json=payload,
        timeout=TIMEOUT,
        trust_env=False,
    )
    response.raise_for_status()

    result = response.json()
    message = result.get("message") or {}

    # Normal final answer.
    content_text = message.get("content") or ""

    if content_text.strip():
        return clean_json_response(content_text)

    # Some Ollama/Qwen versions may still place text in thinking.
    # Only use it as a machine-readable fallback if it contains JSON.
    thinking_text = message.get("thinking") or ""

    if thinking_text.strip():
        try:
            return clean_json_response(thinking_text)
        except Exception:
            pass

    raise ValueError(
        "Ollama returned an empty final answer. "
        f"Response keys: {list(message.keys())}"
    )


def invoke_model(
    llm,
    prompt,
    images,
    pdf_text=""
):
    """
    Qwen3-VL extraction:
    1. Native Ollama API with think=False.
    2. One native retry with stricter JSON instruction.
    3. LangChain fallback.
    """

    # --------------------------------------------------------
    # 1. Native Ollama - primary path
    # --------------------------------------------------------
    try:
        return _ollama_native_chat(
            prompt,
            images,
            pdf_text
        )

    except Exception as native_error:
        print(
            f"     Native Ollama attempt failed: {native_error}"
        )

    # --------------------------------------------------------
    # 2. Native retry
    # --------------------------------------------------------
    retry_prompt = (
        prompt
        + "\n\nFINAL OUTPUT RULE:\n"
          "Return exactly ONE valid JSON object and nothing else.\n"
          "Do NOT write analysis, reasoning, markdown, or explanation.\n"
          "Do NOT use ``` fences.\n"
          "If a value is not visible, use an empty string, empty array, "
          "or 0.\n"
          "The invoice IMAGE is the source of truth.\n"
    )

    try:
        return _ollama_native_chat(
            retry_prompt,
            images,
            pdf_text
        )

    except Exception as retry_error:
        print(
            f"     Native retry failed: {retry_error}"
        )

    # --------------------------------------------------------
    # 3. LangChain fallback
    # --------------------------------------------------------
    message = create_message(
        prompt,
        images,
        pdf_text
    )

    response = llm.invoke([message])

    try:
        return clean_json_response(
            response.content
        )

    except Exception:
        raw = str(response.content or "").strip()

        print(
            "     LangChain response was not valid JSON."
        )

        if raw:
            print(
                "     Raw response (first 1000 chars):"
            )
            print(raw[:1000])

        raise ValueError(
            "Qwen3-VL did not return usable JSON "
            "through native Ollama or LangChain."
        )


# ============================================================
# PAGE-BY-PAGE VISION EXTRACTION
# Keeps each Ollama request small enough for the 16K context.
# ============================================================

def extract_all_pages(llm, prompt, images, pdf_text=""):
    merged = {
        "vendor": "",
        "invoice_number": "",
        "invoice_date": "",
        "po_number": "",
        "po_numbers": [],
        "handwritten_references": [],
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
        "total_tax": 0,
        "total": 0,
        "confidence": 0,
        "lines": [],
    }

    for page_index, image in enumerate(images, start=1):
        print(f"     Vision page {page_index}/{len(images)}...")
        page_text = ""
        if pdf_text:
            parts = pdf_text.split("--- PAGE ")
            if page_index < len(parts):
                page_text = parts[page_index][:5000]

        page_prompt = (
            prompt
            + f"\n\nYou are viewing PAGE {page_index}. "
              "Extract values visible on this page. "
              "If a field is not visible on this page, leave it empty. "
              "Return ONLY valid JSON."
        )

        try:
            data = invoke_model(
                llm,
                page_prompt,
                [image],
                page_text,
            )
        except Exception as error:
            print(f"     Page {page_index} skipped: {error}")
            continue

        if not isinstance(data, dict):
            continue

        # Header fields: fill only when currently empty.
        for field in [
            "vendor", "invoice_number", "invoice_date", "po_number",
            "gstin", "hsn_sac", "vendor_code", "tax_code",
            "business_place", "currency",
        ]:
            value = clean_string(data.get(field))
            if value and not merged.get(field):
                merged[field] = value

        for field in ["subtotal", "cgst", "sgst", "igst", "total_tax", "total"]:
            value = to_float(data.get(field))
            if value and not merged.get(field):
                merged[field] = value

        for po in data.get("po_numbers") or []:
            po = clean_string(po)
            if po and po not in merged["po_numbers"]:
                merged["po_numbers"].append(po)

        for ref in data.get("handwritten_references") or []:
            ref = clean_string(ref)
            if ref and ref not in merged["handwritten_references"]:
                merged["handwritten_references"].append(ref)

        page_lines = data.get("lines") or data.get("line_items") or data.get("items") or []
        if isinstance(page_lines, list):
            merged["lines"].extend(
                [x for x in page_lines if isinstance(x, dict)]
            )

        conf = to_float(data.get("confidence"))
        if conf > merged["confidence"]:
            merged["confidence"] = conf

    return merged


def extract_validation_all_pages(llm, prompt, images, pdf_text=""):
    merged = {
        "invoice_number": "",
        "invoice_date": "",
        "po_number": "",
        "po_numbers": [],
        "handwritten_references": [],
        "gstin": "",
        "hsn_sac": "",
        "line_hsn_sac": [],
        "total": 0,
    }

    for page_index, image in enumerate(images, start=1):
        print(f"     Validation page {page_index}/{len(images)}...")
        page_text = ""
        if pdf_text:
            parts = pdf_text.split("--- PAGE ")
            if page_index < len(parts):
                page_text = parts[page_index][:5000]

        page_prompt = (
            prompt
            + f"\n\nThis is PAGE {page_index}. "
              "Return ONLY valid JSON."
        )

        try:
            data = invoke_model(llm, page_prompt, [image], page_text)
        except Exception as error:
            print(f"     Validation page {page_index} skipped: {error}")
            continue

        if not isinstance(data, dict):
            continue

        for field in [
            "invoice_number", "invoice_date", "po_number", "gstin", "hsn_sac"
        ]:
            value = clean_string(data.get(field))
            if value and not merged.get(field):
                merged[field] = value

        total = to_float(data.get("total"))
        if total and not merged["total"]:
            merged["total"] = total

        for po in data.get("po_numbers") or []:
            po = clean_string(po)
            if po and po not in merged["po_numbers"]:
                merged["po_numbers"].append(po)

        for ref in data.get("handwritten_references") or []:
            ref = clean_string(ref)
            if ref and ref not in merged["handwritten_references"]:
                merged["handwritten_references"].append(ref)

        codes = data.get("line_hsn_sac") or []
        if isinstance(codes, list):
            merged["line_hsn_sac"].extend(
                [clean_string(x) for x in codes if clean_string(x)]
            )

    return merged


def extract_handwriting_check(
    llm,
    image,
    pdf_text=""
):
    """
    Extra focused pass for handwritten PO/reference values.
    Uses only the first page because PO notes are normally near
    the invoice header/top margin.
    """

    print("  -> Targeted handwritten PO check...")

    try:
        data = invoke_model(
            llm,
            HANDWRITING_PROMPT,
            [image],
            pdf_text
        )

        return data if isinstance(data, dict) else {}

    except Exception as error:
        print(
            f"     Handwriting check skipped: {error}"
        )
        return {}


# ============================================================
# NORMALIZATION
# ============================================================

def to_float(value):

    if value is None:
        return 0.0

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value)

    text = text.replace(",", "")

    text = re.sub(
        r"[^0-9.\-]",
        "",
        text
    )

    try:
        return float(text)

    except Exception:
        return 0.0


def clean_string(value):

    if value is None:
        return ""

    return str(value).strip()


def normalize_invoice(data, filename):

    if not isinstance(data, dict):
        data = {}

    raw_lines = (
        data.get("lines")
        or data.get("line_items")
        or data.get("items")
        or []
    )

    lines = []

    for raw in raw_lines:

        if not isinstance(raw, dict):
            continue

        line = {
            "item": clean_string(
                raw.get("item")
                or raw.get("description")
                or raw.get("name")
            ),

            "quantity": to_float(
                raw.get("quantity")
                or raw.get("qty")
            ),

            "rate": to_float(
                raw.get("rate")
                or raw.get("unit_price")
            ),

            "hsn_sac": clean_string(
                raw.get("hsn_sac")
                or raw.get("hsn")
                or raw.get("sac")
            ),

            "amount": to_float(
                raw.get("amount")
                or raw.get("total")
            )
        }

        lines.append(line)

    # Remove exact duplicate line items that can occur when
    # page-level extraction repeats a table/header.
    unique_lines = []
    seen_lines = set()

    for line in lines:
        key = (
            line["item"],
            line["quantity"],
            line["rate"],
            line["hsn_sac"],
            line["amount"],
        )
        if key not in seen_lines:
            seen_lines.add(key)
            unique_lines.append(line)

    lines = unique_lines

    cgst = to_float(data.get("cgst"))
    sgst = to_float(data.get("sgst"))
    igst = to_float(data.get("igst"))

    total_tax = to_float(
        data.get("total_tax")
        or data.get("tax")
    )

    if total_tax == 0:

        total_tax = (
            cgst
            + sgst
            + igst
        )

    subtotal = to_float(
        data.get("subtotal")
        or data.get("taxable_value")
    )

    total = to_float(
        data.get("total")
        or data.get("grand_total")
    )

    if subtotal == 0 and lines:

        subtotal = sum(
            line["amount"]
            for line in lines
        )

    if total == 0:

        total = subtotal + total_tax

    po_numbers = data.get("po_numbers") or []

    if not isinstance(po_numbers, list):
        po_numbers = [po_numbers]

    po_numbers = [
        clean_string(x)
        for x in po_numbers
        if clean_string(x)
    ]

    handwritten = (
        data.get("handwritten_references")
        or []
    )

    if not isinstance(handwritten, list):
        handwritten = [handwritten]

    handwritten = [
        clean_string(x)
        for x in handwritten
        if clean_string(x)
    ]

    header = {
        "filename": filename,

        "vendor": clean_string(
            data.get("vendor")
        ),

        "invoice_number": clean_string(
            data.get("invoice_number")
        ),

        "invoice_date": clean_string(
            data.get("invoice_date")
        ),

        "po_number": clean_string(
            data.get("po_number")
        ),

        "po_numbers": po_numbers,

        "handwritten_references": handwritten,

        "gstin": clean_string(
            data.get("gstin")
        ),

        "hsn_sac": clean_string(
            data.get("hsn_sac")
        ),

        "vendor_code": clean_string(
            data.get("vendor_code")
        ),

        "tax_code": clean_string(
            data.get("tax_code")
        ),

        "business_place": clean_string(
            data.get("business_place")
        ),

        "currency": clean_string(
            data.get("currency")
        ) or "INR",

        "subtotal": subtotal,
        "cgst": cgst,
        "sgst": sgst,
        "igst": igst,
        "total_tax": total_tax,
        "total": total,

        "confidence": to_float(
            data.get("confidence")
        )
    }

    return header, lines


# ============================================================
# REGEX FALLBACK
# ============================================================

def regex_fallback(pdf_text):

    result = {}

    text = pdf_text or ""

    # --------------------------------------------------------
    # Invoice / Bill number
    # --------------------------------------------------------

    invoice_patterns = [

        r"(?:invoice|bill)\s*(?:no|number)"
        r"\s*[:#.\-]?\s*"
        r"([A-Za-z0-9][A-Za-z0-9./_-]*)",

        r"(?:tax\s+invoice)"
        r"\s*(?:no|number)?"
        r"\s*[:#.\-]?\s*"
        r"([A-Za-z0-9][A-Za-z0-9./_-]*)",
    ]

    for pattern in invoice_patterns:

        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE
        )

        if match:

            result["invoice_number"] = (
                match.group(1).strip()
            )

            break

    # --------------------------------------------------------
    # PO Number
    # --------------------------------------------------------

    po_patterns = [

        r"\bP\.?\s*O\.?\s*[-:/]?\s*"
        r"([A-Za-z0-9][A-Za-z0-9./_-]*)",

        r"\bPO\s*NO\.?\s*[:#-]?\s*"
        r"([A-Za-z0-9][A-Za-z0-9./_-]*)",

        r"\bPURCHASE\s+ORDER"
        r"\s*(?:NO|NUMBER)?"
        r"\s*[:#-]?\s*"
        r"([A-Za-z0-9][A-Za-z0-9./_-]*)",
    ]

    po_values = []

    for pattern in po_patterns:

        matches = re.findall(
            pattern,
            text,
            flags=re.IGNORECASE
        )

        for value in matches:

            value = value.strip()

            if value and value not in po_values:

                po_values.append(value)

    if po_values:

        result["po_numbers"] = po_values

        # Prefer explicit PO- value
        for value in po_values:

            if value.upper().startswith("PO"):

                result["po_number"] = value
                break

        if "po_number" not in result:

            result["po_number"] = po_values[0]

    # --------------------------------------------------------
    # HSN / SAC
    # --------------------------------------------------------

    hsn_patterns = [

        r"(?:HSN\s*/?\s*SAC|HSN|SAC)"
        r"\s*(?:CODE)?"
        r"\s*[:.\-]?\s*"
        r"([0-9]{4,8})\b",

        r"SAC\s+CODE"
        r"\s*[:.\-]?\s*"
        r"([0-9]{4,8})\b",
    ]

    for pattern in hsn_patterns:

        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE
        )

        if match:

            result["hsn_sac"] = (
                match.group(1)
            )

            break

    # --------------------------------------------------------
    # GSTIN
    # --------------------------------------------------------

    gstin_pattern = (
        r"\b"
        r"[0-9]{2}"
        r"[A-Z]{5}"
        r"[0-9]{4}"
        r"[A-Z]"
        r"[A-Z0-9]Z"
        r"[A-Z0-9]"
        r"\b"
    )

    match = re.search(
        gstin_pattern,
        text.upper()
    )

    if match:

        result["gstin"] = match.group(0)

    # --------------------------------------------------------
    # Total
    # --------------------------------------------------------

    total_patterns = [

        r"(?:total\s+invoice\s+amount"
        r"|total\s+invoice\s+amt"
        r"|invoice\s+total"
        r"|total\s+amount)"
        r"\s*[:\-]?\s*[₹]?\s*"
        r"([0-9,]+(?:\.[0-9]+)?)",

        r"\bTOTAL\b"
        r"\s*[₹]?\s*"
        r"([0-9,]+(?:\.[0-9]+)?)",
    ]

    for pattern in total_patterns:

        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE
        )

        if match:

            result["total"] = to_float(
                match.group(1)
            )

            break

    return result


# ============================================================
# MERGE VALIDATION RESULT
# ============================================================

def apply_validation(
    header,
    lines,
    validation
):

    if not isinstance(validation, dict):
        return header, lines

    # --------------------------------------------------------
    # Fill missing header values
    # --------------------------------------------------------

    simple_fields = [
        "invoice_number",
        "invoice_date",
        "po_number",
        "gstin",
        "hsn_sac",
    ]

    for field in simple_fields:

        value = clean_string(
            validation.get(field)
        )

        if value:

            # Validation is visual second pass.
            # Use it when main extraction is missing.
            if not header.get(field):

                header[field] = value

    # --------------------------------------------------------
    # PO list
    # --------------------------------------------------------

    validation_pos = (
        validation.get("po_numbers")
        or []
    )

    if not isinstance(validation_pos, list):

        validation_pos = [
            validation_pos
        ]

    for po in validation_pos:

        po = clean_string(po)

        if (
            po
            and po not in header["po_numbers"]
        ):

            header["po_numbers"].append(po)

    if (
        not header["po_number"]
        and header["po_numbers"]
    ):

        header["po_number"] = (
            header["po_numbers"][0]
        )

    # --------------------------------------------------------
    # Handwritten references
    # --------------------------------------------------------

    refs = (
        validation.get(
            "handwritten_references"
        )
        or []
    )

    if not isinstance(refs, list):
        refs = [refs]

    for ref in refs:

        ref = clean_string(ref)

        if (
            ref
            and ref
            not in header[
                "handwritten_references"
            ]
        ):

            header[
                "handwritten_references"
            ].append(ref)

    # --------------------------------------------------------
    # Total
    # --------------------------------------------------------

    if not header["total"]:

        value = to_float(
            validation.get("total")
        )

        if value:
            header["total"] = value

    # --------------------------------------------------------
    # Line HSN/SAC
    # --------------------------------------------------------

    line_codes = (
        validation.get(
            "line_hsn_sac"
        )
        or []
    )

    if not isinstance(line_codes, list):
        line_codes = [line_codes]

    for index, line in enumerate(lines):

        if (
            not line["hsn_sac"]
            and index < len(line_codes)
        ):

            code = clean_string(
                line_codes[index]
            )

            if code:
                line["hsn_sac"] = code

    return header, lines


# ============================================================
# APPLY SINGLE HSN/SAC TO ALL ITEMS
# ============================================================

def apply_invoice_hsn_sac(
    header,
    lines
):

    invoice_hsn = (
        header.get("hsn_sac")
        or ""
    ).strip()

    if invoice_hsn:

        for line in lines:

            if not line["hsn_sac"]:

                line["hsn_sac"] = invoice_hsn

    # If header missing but every visible line
    # has exactly the same code, use it as invoice HSN.
    if not invoice_hsn:

        codes = [
            line["hsn_sac"]
            for line in lines
            if line["hsn_sac"]
        ]

        unique_codes = list(
            dict.fromkeys(codes)
        )

        if len(unique_codes) == 1:

            header["hsn_sac"] = (
                unique_codes[0]
            )

    return header, lines


# ============================================================
# APPLY FALLBACKS
# ============================================================

def apply_fallbacks(
    header,
    lines,
    pdf_text
):

    fallback = regex_fallback(
        pdf_text
    )

    for field in [
        "invoice_number",
        "po_number",
        "hsn_sac",
        "gstin",
    ]:

        if (
            not header.get(field)
            and fallback.get(field)
        ):

            header[field] = fallback[field]

    if (
        not header["po_numbers"]
        and fallback.get("po_numbers")
    ):

        header["po_numbers"] = (
            fallback["po_numbers"]
        )

    if (
        not header["po_number"]
        and header["po_numbers"]
    ):

        header["po_number"] = (
            header["po_numbers"][0]
        )

    if (
        not header["total"]
        and fallback.get("total")
    ):

        header["total"] = (
            fallback["total"]
        )

    return header, lines


# ============================================================
# FILE NAME
# ============================================================

def safe_filename(value):

    value = str(value)

    value = re.sub(
        r'[<>:"/\\|?*]+',
        "_",
        value
    )

    value = re.sub(
        r"\s+",
        "_",
        value
    )

    value = value.strip(
        " ._"
    )

    return value or "UNKNOWN_INVOICE"


# ============================================================
# EXCEL FORMATTING
# ============================================================

def style_header(
    worksheet,
    row,
    fill_color
):

    fill = PatternFill(
        "solid",
        fgColor=fill_color
    )

    font = Font(
        bold=True,
        color="FFFFFF",
        size=10
    )

    for cell in worksheet[row]:

        cell.fill = fill

        cell.font = font

        cell.alignment = Alignment(
            horizontal="center",
            vertical="center",
            wrap_text=True
        )


def auto_width(worksheet):

    for column in worksheet.columns:

        max_length = 0

        column_letter = get_column_letter(
            column[0].column
        )

        for cell in column:

            value = str(
                cell.value or ""
            )

            max_length = max(
                max_length,
                len(value)
            )

        worksheet.column_dimensions[
            column_letter
        ].width = min(
            max(max_length + 2, 10),
            55
        )


# ============================================================
# CREATE INDIVIDUAL EXCEL
# ============================================================

def create_invoice_excel(
    header,
    lines,
    output_folder
):

    output_folder = Path(
        output_folder
    )

    output_folder.mkdir(
        parents=True,
        exist_ok=True
    )

    invoice_number = (
        header["invoice_number"]
        or Path(
            header["filename"]
        ).stem
    )

    filename = (
        safe_filename(invoice_number)
        + ".xlsx"
    )

    output_path = (
        output_folder
        / filename
    )

    workbook = Workbook()

    # --------------------------------------------------------
    # SHEET 1
    # --------------------------------------------------------

    ws = workbook.active

    ws.title = "Invoice Data"

    ws.append([
        "Field",
        "Value"
    ])

    style_header(
        ws,
        1,
        "1F4E79"
    )

    fields = [

        ("File Name",
         header["filename"]),

        ("Vendor",
         header["vendor"]),

        ("Invoice Number",
         header["invoice_number"]),

        ("Invoice Date",
         header["invoice_date"]),

        ("PO Number",
         header["po_number"]),

        ("All PO Numbers",
         ", ".join(
             header["po_numbers"]
         )),

        ("Handwritten References",
         ", ".join(
             header[
                 "handwritten_references"
             ]
         )),

        ("GSTIN",
         header["gstin"]),

        ("HSN/SAC",
         header["hsn_sac"]),

        ("Vendor Code",
         header["vendor_code"]),

        ("Tax Code",
         header["tax_code"]),

        ("Business Place",
         header["business_place"]),

        ("Currency",
         header["currency"]),

        ("Subtotal",
         header["subtotal"]),

        ("CGST",
         header["cgst"]),

        ("SGST",
         header["sgst"]),

        ("IGST",
         header["igst"]),

        ("Total Tax",
         header["total_tax"]),

        ("Invoice Total",
         header["total"]),

        ("Confidence %",
         header["confidence"]),
    ]

    for field, value in fields:

        ws.append([
            field,
            value
        ])

    ws.column_dimensions[
        "A"
    ].width = 28

    ws.column_dimensions[
        "B"
    ].width = 60

    ws.freeze_panes = "A2"

    # --------------------------------------------------------
    # SHEET 2
    # --------------------------------------------------------

    wi = workbook.create_sheet(
        "Line Items"
    )

    wi.append([
        "Sl.No",
        "Item / Description",
        "Quantity",
        "Rate",
        "HSN/SAC",
        "Amount"
    ])

    style_header(
        wi,
        1,
        "145A32"
    )

    for index, line in enumerate(
        lines,
        start=1
    ):

        wi.append([
            index,
            line["item"],
            line["quantity"],
            line["rate"],
            line["hsn_sac"],
            line["amount"]
        ])

    widths = [
        10,
        60,
        12,
        15,
        18,
        18
    ]

    for index, width in enumerate(
        widths,
        start=1
    ):

        wi.column_dimensions[
            get_column_letter(index)
        ].width = width

    wi.freeze_panes = "A2"

    # --------------------------------------------------------
    # SHEET 3 - Raw AI JSON
    # --------------------------------------------------------

    wr = workbook.create_sheet(
        "Extraction Info"
    )

    wr.append([
        "Field",
        "Value"
    ])

    style_header(
        wr,
        1,
        "7F6000"
    )

    wr.append([
        "Extraction Method",
        "LangChain + Ollama Qwen3-VL"
    ])

    wr.append([
        "HSN/SAC Rule",
        "Single invoice HSN/SAC applied to all items"
    ])

    wr.append([
        "Handwriting Check",
        "Vision validation pass enabled"
    ])

    auto_width(wr)

    workbook.save(
        output_path
    )

    print(
        f"  Excel created: {output_path}"
    )

    return output_path


# ============================================================
# SUMMARY EXCEL
# ============================================================

def create_summary_excel(
    invoices,
    output_folder
):

    output_path = (
        Path(output_folder)
        / "invoice_summary.xlsx"
    )

    workbook = Workbook()

    ws = workbook.active

    ws.title = "Invoice Summary"

    columns = [
        "File Name",
        "Vendor",
        "Invoice Number",
        "Invoice Date",
        "PO Number",
        "All PO Numbers",
        "Handwritten References",
        "GSTIN",
        "HSN/SAC",
        "Subtotal",
        "CGST",
        "SGST",
        "IGST",
        "Total Tax",
        "Invoice Total",
        "Confidence %"
    ]

    ws.append(columns)

    style_header(
        ws,
        1,
        "1F4E79"
    )

    for invoice in invoices:

        ws.append([
            invoice["filename"],
            invoice["vendor"],
            invoice["invoice_number"],
            invoice["invoice_date"],
            invoice["po_number"],
            ", ".join(
                invoice["po_numbers"]
            ),
            ", ".join(
                invoice[
                    "handwritten_references"
                ]
            ),
            invoice["gstin"],
            invoice["hsn_sac"],
            invoice["subtotal"],
            invoice["cgst"],
            invoice["sgst"],
            invoice["igst"],
            invoice["total_tax"],
            invoice["total"],
            invoice["confidence"],
        ])

    auto_width(ws)

    ws.freeze_panes = "A2"

    workbook.save(
        output_path
    )

    print(
        f"  Summary created: {output_path}"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    input_folder = Path(
        PDF_FOLDER
    )

    output_folder = Path(
        OUTPUT_FOLDER
    )

    if not input_folder.exists():

        print(
            f"ERROR: Input folder not found:\n"
            f"{input_folder}"
        )

        sys.exit(1)

    pdfs = sorted(
        [
            path
            for path in input_folder.iterdir()
            if (
                path.is_file()
                and path.suffix.lower()
                == ".pdf"
            )
        ],
        key=lambda x: x.name.lower()
    )

    if not pdfs:

        print(
            f"ERROR: No PDF files found:\n"
            f"{input_folder}"
        )

        sys.exit(1)

    output_folder.mkdir(
        parents=True,
        exist_ok=True
    )

    print("=" * 70)
    print("KTFL MIRO INVOICE EXTRACTOR")
    print("=" * 70)
    print(
        f"PDF count : {len(pdfs)}"
    )
    print(
        f"Model     : {OLLAMA_MODEL}"
    )
    print(
        f"Output    : {output_folder}"
    )
    print("=" * 70)

    # --------------------------------------------------------
    # 0. CHECK LOCAL OLLAMA BEFORE PROCESSING PDFs
    # --------------------------------------------------------
    try:
        check = httpx.get(
            f"{OLLAMA_URL}/api/tags",
            timeout=10,
            trust_env=False,
        )
        check.raise_for_status()

        models = check.json().get("models", [])
        model_names = [
            m.get("name", "")
            for m in models
        ]

        print(
            "Local Ollama : OK"
        )
        print(
            "Available models: "
            + (
                ", ".join(model_names)
                if model_names
                else "NONE"
            )
        )

        if OLLAMA_MODEL not in model_names:
            print()
            print(
                f"ERROR: {OLLAMA_MODEL} is not installed."
            )
            print(
                f"Run: ollama pull {OLLAMA_MODEL}"
            )
            sys.exit(1)

    except Exception as error:
        print()
        print(
            "ERROR: Cannot connect to local Ollama."
        )
        print(
            f"Ollama URL: {OLLAMA_URL}"
        )
        print(
            f"Details: {error}"
        )
        sys.exit(1)

    llm = create_llm()

    all_headers = []

    for index, pdf in enumerate(
        pdfs,
        start=1
    ):

        print()
        print(
            f"[{index}/{len(pdfs)}] "
            f"Processing: {pdf.name}"
        )

        try:

            # ------------------------------------------------
            # 1. PDF -> high resolution images
            # ------------------------------------------------

            images = pdf_to_images(
                pdf
            )

            # ------------------------------------------------
            # 2. PDF text as secondary reference
            # ------------------------------------------------

            text = get_pdf_text(
                pdf
            )

            # ------------------------------------------------
            # 3. MAIN LANGCHAIN VISION EXTRACTION
            # ------------------------------------------------

            print(
                "  -> Main vision extraction..."
            )

            data = extract_all_pages(
                llm,
                EXTRACTION_PROMPT,
                images,
                text
            )

            header, lines = (
                normalize_invoice(
                    data,
                    pdf.name
                )
            )

            # ------------------------------------------------
            # 4. SECOND VISUAL VALIDATION
            # ------------------------------------------------

            print(
                "  -> PO / HSN / handwriting "
                "validation..."
            )

            validation = extract_validation_all_pages(
                llm,
                VALIDATION_PROMPT,
                images,
                text
            )

            header, lines = (
                apply_validation(
                    header,
                    lines,
                    validation
                )
            )

            # ------------------------------------------------
            # 5. TARGETED HANDWRITING / PO CHECK
            # ------------------------------------------------
            if images:
                handwriting = extract_handwriting_check(
                    llm,
                    images[0],
                    text[:5000]
                )

                # Fill missing invoice-level values.
                for field in [
                    "invoice_number",
                    "po_number",
                    "hsn_sac",
                ]:
                    value = clean_string(
                        handwriting.get(field)
                    )
                    if value and not header.get(field):
                        header[field] = value

                for po in handwriting.get("po_numbers") or []:
                    po = clean_string(po)
                    if (
                        po
                        and po not in header["po_numbers"]
                    ):
                        header["po_numbers"].append(po)

                for ref in (
                    handwriting.get(
                        "handwritten_references"
                    )
                    or []
                ):
                    ref = clean_string(ref)
                    if (
                        ref
                        and ref not in header[
                            "handwritten_references"
                        ]
                    ):
                        header[
                            "handwritten_references"
                        ].append(ref)

                if (
                    not header["po_number"]
                    and header["po_numbers"]
                ):
                    header["po_number"] = (
                        header["po_numbers"][0]
                    )

            # ------------------------------------------------
            # 6. Regex fallback
            # ------------------------------------------------

            header, lines = (
                apply_fallbacks(
                    header,
                    lines,
                    text
                )
            )

            # ------------------------------------------------
            # 7. SINGLE HSN/SAC -> ALL ITEMS
            # ------------------------------------------------

            header, lines = (
                apply_invoice_hsn_sac(
                    header,
                    lines
                )
            )

            # ------------------------------------------------
            # 8. Confidence
            # ------------------------------------------------

            header["confidence"] = round(
                max(
                    0,
                    min(
                        100,
                        header["confidence"]
                        * (
                            100
                            if header[
                                "confidence"
                            ] <= 1
                            else 1
                        )
                    )
                ),
                1
            )

            # ------------------------------------------------
            # 9. Create one Excel per invoice
            # ------------------------------------------------

            create_invoice_excel(
                header,
                lines,
                output_folder
            )

            all_headers.append(
                header
            )

            print(
                "  --------------------------------"
            )

            print(
                f"  Invoice No : "
                f"{header['invoice_number'] or '?'}"
            )

            print(
                f"  PO Number  : "
                f"{header['po_number'] or '?'}"
            )

            print(
                f"  HSN/SAC    : "
                f"{header['hsn_sac'] or '?'}"
            )

            print(
                f"  GSTIN      : "
                f"{header['gstin'] or '?'}"
            )

            print(
                f"  Date       : "
                f"{header['invoice_date'] or '?'}"
            )

            print(
                f"  Total      : "
                f"{header['total']}"
            )

            print(
                f"  Line Items : "
                f"{len(lines)}"
            )

            print(
                "  --------------------------------"
            )

        except Exception as error:

            print(
                f"  ERROR: {error}"
            )

        if index < len(pdfs):

            time.sleep(1)

    # --------------------------------------------------------
    # 10. Combined summary
    # --------------------------------------------------------

    if (
        CREATE_SUMMARY
        and all_headers
    ):

        create_summary_excel(
            all_headers,
            output_folder
        )

    print()
    print("=" * 70)
    print("DONE")
    print("=" * 70)
    print(
        "Each invoice has been saved as a separate Excel file."
    )


if __name__ == "__main__":
    main()
