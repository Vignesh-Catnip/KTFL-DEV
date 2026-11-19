"""
VLM Prompt Management - all prompts live here, independent of Python code.

Design rules for this version
-----------------------------
1. The model READS; Python NORMALIZES and VALIDATES (see validators.py).
   Prompts ask for values exactly as printed/written. They do not ask the model to
   expand shorthand, fix typos, do arithmetic, derive totals or estimate confidence.
2. No literal example numbers inside rules (small models copy them). Use XXXX placeholders.
3. One field, one source of truth: GRN is extracted ONLY by GRN_PROMPT.
4. Vendor-specific quirks live in VENDOR_HINTS and are appended only when the vendor is known,
   so the main prompt stays short.

Contract changes vs the old file (update vlm.py accordingly):
  - PROMPT no longer returns grn_number, po_numbers_all, gst_rate_percent, total_tax, confidence.
    validate.validate() derives them.
  - PROMPT returns po_number_raw / handwritten_po_raw instead of a normalized po_number.
  - GRN_PROMPT returns {"grn_top_margin", "grn_stamp"} (raw) instead of {"grn_number"}.
  - New PAGE_CLASS_PROMPT (run once per page before extraction).
"""

# ---------------------------------------------------------------------------
# Page classification prompt (run per page BEFORE extraction)
# ---------------------------------------------------------------------------
PAGE_CLASS_PROMPT = r"""
Classify this single scanned page from a purchase-invoice bundle.

page_type must be exactly one of:
- "invoice_first"        : first page of a TAX INVOICE (letterhead, invoice number, buyer/seller blocks, item table).
- "invoice_continuation" : a later page of the SAME tax invoice (more numbered item rows, taxable value, CGST/SGST/IGST
                           summary, totals, "Page 2 of N", or the receiving stamp), even without a "TAX INVOICE" heading.
- "weighment_slip"
- "debit_note"           : a GST debit note / credit note.
- "lorry_receipt"
- "delivery_challan"
- "other"

Also read, exactly as printed (empty string if absent):
- invoice_number: the invoice / bill number printed on this page.
- page_marker: text such as "Page 1 of 2" if printed.

Return ONLY valid JSON:
{"page_type": "", "invoice_number": "", "page_marker": ""}
"""

# ---------------------------------------------------------------------------
# Page orientation check prompt
# ---------------------------------------------------------------------------
UPRIGHT_PROMPT = r"""
Look at this scanned page. Is the printed text UPRIGHT - horizontal lines of text that read normally from left
to right, with the tops of the letters pointing to the top of the picture?
Answer false if the text runs vertically (page turned sideways 90/270 degrees) or is upside down (180 degrees).
Return ONLY: {"upright": true} or {"upright": false}
"""

# ---------------------------------------------------------------------------
# GRN extraction prompt - returns BOTH candidate locations, raw. Python decides.
# Best results: send a high-resolution crop of the top ~20% of the page for the
# top-margin read and a crop of the receiving stamp for the stamp read.
# ---------------------------------------------------------------------------
GRN_PROMPT = r"""
You are an OCR specialist. Find the KTFL GRN (Goods Receipt Note) number on this document.
A valid KTFL GRN has 10 digits and starts with "5000". Read the digits from THIS image only.

There are two places it can appear. Report each separately, exactly as you see it:

1. grn_top_margin: a number handwritten in pen in the TOP MARGIN (top-left, top-centre or top-right), usually above
   the printed header, often underlined with a date below it or after a slash. Report only the number, not the date.
   It may be written in a short form such as "5-XXXXXX" or "S-XXXXXX": copy it as written, do not expand it.
2. grn_stamp: the number on the "GRN NO" row (may be misprinted as "ORN NO" / "JRN NO") of the rubber receiving stamp
   ("KALYANI TECHNOFORGE" / "RECEIVED SUBJECT TO VERIFICATION"). That row sits directly BELOW the "SCROLL NO" row.
   Report only the number before the date.

If a location has no GRN, return "" for it. Never copy a number from one location into the other.

Never report these as a GRN:
- SCROLL NO (starts with "20", e.g. 200..., 202...).
- Gate numbers written as two numbers with a slash (e.g. 4 digits / 3-4 digits), usually in red at the top right.
- PO numbers (start with 51, 61 or 81).
- E-way bill (12 digits), LR number, vehicle number, invoice number, dates.

Reading handwriting: go stroke by stroke. 1 has no long top bar (7 does). 4 has an open top; 8 is closed.
6 has a closed lower loop and a tail; 5 has a flat top bar; 0 is an unbroken oval.
After reading, re-read each digit left to right once more against the image. The most common error is swapping two
adjacent digits, or running a digit of the date onto the end of the number.
Every document has its own GRN: do not reuse a number you remember from another invoice.

Return strictly valid JSON, no markdown:
{"grn_top_margin": "", "grn_stamp": ""}
"""

# ---------------------------------------------------------------------------
# GRN retry hint (appended when Python validation rejects the first read)
# Placeholders: {location}, {bad}, {n}
# ---------------------------------------------------------------------------
GRN_RETRY_HINT = r"""
Your previous read of {location} was "{bad}" ({n} digits), which is not a valid GRN.
A valid KTFL GRN has EXACTLY 10 digits and starts with "5000" (a 5 followed by THREE zeroes).
Re-check, in this order:
1. Top margin: handwritten number above the printed header. Stop before the date.
2. Receiving stamp: the "GRN NO" row, below "SCROLL NO". Stop before the date.
3. Do not use SCROLL NO (starts with 20), gate numbers (with a slash) or PO numbers (start with 51/61/81).
4. If a digit is unclear, do not guess a digit to make the number fit: return "" for that location.
5. Do not reuse a number from memory or from another invoice. Watch for adjacent-digit swaps.
Return the same JSON shape as before.
"""

# ---------------------------------------------------------------------------
# Main invoice extraction prompt  (v2: null policy, page labels, source pages, every printed row)
# ---------------------------------------------------------------------------
PROMPT_VERSION = "v2.0-null-pages"

PROMPT = r"""
You are an extraction engine for Indian GST purchase invoices. The BUYER is Kalyani Technoforge Ltd (KTFL).
You receive some pages of ONE document, each image preceded by its page label (for example "PAGE 2 of 5").
Extract only what is printed or written on THE PAGES SHOWN. Other pages are read in separate calls.
Ignore weighment slips, counting charts, lorry receipts, delivery challans and other attachments: do not extract
values from them (list them in "pages" with kind "attachment").

ABSOLUTE RULES
1. NULL POLICY: if a value is not visible or not readable, return null. Never return "" , 0 or 0.0 for something you
   could not read. Return 0.00 only when the document itself prints a zero or a dash for that amount.
2. Never guess, never invent, never infer, never calculate. Do not compute totals, tax, rates or quantities.
   Do not copy a value from another field, another page or another invoice. Do not use any outside knowledge.
3. Copy values exactly as printed. Do not correct spelling, do not reformat, do not convert letters to digits
   (never turn O into 0, S into 5, A into 4), do not pad or expand short numbers.
4. All amounts, quantities, rates and percentages are returned as TEXT exactly as printed, for example the digits,
   commas and dots you see. Do not strip separators and do not convert.
5. Every printed row of the item table is one entry in "lines", in printed order, including service, loading, freight
   and other charge rows. NEVER skip a row and NEVER merge rows, even when two rows are identical.
   Keep the full description, never shorten it.
6. For every line and for each header block say on which page you saw it ("page" / "header_page" / "totals_page").
7. Handwriting is not printed text. Put handwritten values only in the handwritten_* fields; never use them in
   printed fields.
8. If a page is blurred, cut off, rotated or otherwise hard to read, say so in "pages" (legible false) and return
   null for the values you cannot read. Do not fill the gap.

FIELDS
- vendor: legal name of the SELLER from the letterhead. Never KTFL / Kalyani Technoforge / Kalyani Transmission.
- gstin: the SELLER's 15-character GSTIN. Never the buyer's (the buyer GSTIN starts with "27AABCK0618").
- invoice_number: the text under "Invoice No." / "Tax Invoice No." / "Bill No.". Copy fully with prefixes and suffixes.
- invoice_date: the invoice date as printed. due_date: the due / payment date as printed if any (never mix the two).
- po_number_raw: the KTFL purchase order number(s) printed in a box such as "PO No", "Customer PO", "Buyer's Order No."
  Several -> separate with commas. Not the vendor's own order code, not SO / delivery / shipment numbers.
- handwritten_po_raw: a PO written by hand, only if it is not printed. Copy as written.
- vendor_code: only if a value is printed after "Vendor Code" / "Supplier Code". Blank, "NA" or "-" -> null.
- plant_hint: the KTFL plant named in the Ship To / Dispatch To block, as printed.
- hsn_sac: an HSN/SAC printed in the HEADER or summary of the invoice. Do not copy it into the lines.
- currency: as printed (for example INR) or null.
- subtotal: printed "Taxable Value" total. cgst, sgst, igst: printed rupee amount of each tax, each read separately.
  gst_rate_printed: the GST rate % printed in the tax summary, as text, or null.
  round_off: printed Round Off with its sign. other_charges: charges outside the taxable value that are added to the total.
  total: printed final Grand Total / Total Invoice Value.
- handwritten_references: short note of handwritten PO/GRN remarks (not heat or bundle numbers).

LINES (every printed row)
- line_no: the printed serial number as text. material_code: code printed in the row, else null.
- item: full description as printed. hsn_sac: THIS row's own HSN, or null if the row has none.
- quantity: the quantity next to the unit (not box count, not total weight). uom: the unit printed for the row, or null.
- rate, discount, amount (taxable value of the row), tax_rate, cgst, sgst, igst: as printed for the row, else null.
- page: page number (from the label) where the row is printed. raw_text: the row exactly as read, on one line.

Return strictly valid JSON, no markdown, with exactly these keys:
{
  "vendor": null, "gstin": null, "invoice_number": null, "invoice_date": null, "due_date": null,
  "po_number_raw": null, "handwritten_po_raw": null, "vendor_code": null, "plant_hint": null, "hsn_sac": null,
  "currency": null, "subtotal": null, "cgst": null, "sgst": null, "igst": null, "gst_rate_printed": null,
  "round_off": null, "other_charges": null, "total": null,
  "header_page": null, "totals_page": null,
  "lines": [
    {"line_no": null, "material_code": null, "item": null, "hsn_sac": null, "quantity": null, "uom": null,
     "rate": null, "discount": null, "amount": null, "tax_rate": null, "cgst": null, "sgst": null, "igst": null,
     "page": null, "raw_text": null}
  ],
  "pages": [ {"page": null, "kind": "invoice", "legible": true} ],
  "handwritten_references": null
}
"""


def prompt_hash(text: str | None = None) -> str:
    """Short fingerprint stored with every extraction run so a result can be tied to the exact prompt text."""
    import hashlib
    return hashlib.sha256((text or PROMPT).encode("utf-8")).hexdigest()[:16]


def page_header(first: int, last: int, total: int) -> str:
    """Text placed in front of the images of one call so the model knows which pages it is looking at."""
    rng = f"PAGES {first} to {last}" if last > first else f"PAGE {first}"
    return (f"The images below are {rng} of {total} pages of this document, in order. "
            f"Image 1 is PAGE {first}" + (f", image {last - first + 1} is PAGE {last}." if last > first else ".") + "\n")


# ---------------------------------------------------------------------------
# Optional per-vendor hints. After the vendor is identified (GSTIN -> vendor master),
# append the matching text to PROMPT for the extraction call. Keep each hint short.
# Keys are seller GSTINs.
# ---------------------------------------------------------------------------
VENDOR_HINTS = {
    # HINDALCO INDUSTRIES (Alupuram)
    "32AAACH1201R1ZW": r"""
VENDOR NOTE (Hindalco): the KTFL PO is the 10-digit number beside "PO No:" on the RIGHT of the header (below
TIME OF ISSUE / DELIVERY NO), also repeated beside "CUSTOMER PO:". Never take SO NO, DELIVERY NO or SHIPMENT NO.
Ignore the summary banner row at the top of the item table; extract only the numbered rows.
""",
    # BHARAT METALS
    "27AATFB3972H1ZZ": r"""
VENDOR NOTE (Bharat Metals): keep the "BM" prefix of the invoice number. Join the two description columns.
If the top-left of the letterhead is covered by a slip, the seller PAN at the bottom is AATFB3972H.
""",
    # BMSS STEEL
    "27AAACG1519K1ZP": r"""
VENDOR NOTE (BMSS Steel): the item table can continue on page 2. The taxable value, tax summary and receiving stamp
are on the last page. subtotal is the printed Taxable Value box, even if item rows are adjusted by discounts.
""",
    # EGEARZ
    "27AAFCE6816Q1Z0": r"""
VENDOR NOTE (EGEARZ): the item table has a Box column and a Qty column and NO unit column: uom is "".
"Total Weight (In Kg)" is not a quantity. Vendor Code prints "Na": return "".
""",
}


def build_invoice_prompt(seller_gstin: str = "") -> str:
    """Main prompt plus the vendor note, if the vendor is already known."""
    hint = VENDOR_HINTS.get((seller_gstin or "").strip().upper(), "")
    return PROMPT + hint if hint else PROMPT
