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
    validators.validate_invoice() derives them.
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
# Main invoice extraction prompt
# ---------------------------------------------------------------------------
PROMPT = r"""
You are an extraction engine for Indian GST purchase invoices. The BUYER is Kalyani Technoforge Ltd (KTFL).
You are given the pages of ONE tax invoice (first page plus any continuation pages). Combine them into one result.
Ignore weighment slips, counting charts, lorry receipts, delivery challans, debit notes and any other attachment.

GENERAL RULES
- Copy values exactly as printed. Do NOT calculate, correct, round, expand shorthand or reformat. A separate program checks them.
- If a value is not visible or not printed, return "" (text) or 0.0 (number). Never guess and never invent digits.
- Indian number formats: "1,42,115.00" and "142115.00" are the same number. Count every digit before the decimal point.
  A dot is always a decimal point ("31.390" is thirty-one point three nine).
- Numbers inside these instructions are placeholders. Read every number from the document.

FIELDS
- vendor: legal name of the SELLER, from the letterhead. Never KTFL / Kalyani Technoforge / Kalyani Transmission.
- gstin: the SELLER's 15-character GSTIN (letterhead or "GST No" beside the seller block). Never the buyer's
  (the buyer is KTFL, GSTIN starts "27AABCK0618").
- invoice_number: the text under "Invoice No." / "Tax Invoice No." / "Bill No." Copy it fully, including prefixes and suffixes.
- invoice_date: the invoice date exactly as printed.
- po_number_raw: the KTFL purchase order number(s) PRINTED in a box labelled "PO No", "PO NO & Date", "Customer PO",
  "Cust. PO No", "P.O. No." or "Buyer's Order No.". A KTFL PO is exactly 10 digits and starts 51, 61, 81, 82 or 83
  (for example 5100001234). If several, separate with commas. NEVER take: phone / mobile numbers ("Mo. No.", "Ph. No.",
  "Tel", "Contact Detail"), the vendor's own order code ("Order No.: PS26Y-01408", "VERBAL ORDER"), "Customer Ref No.",
  E-way bill (12 digits), IRN / Ack no., LR no., vehicle no., SO / delivery / shipment numbers, CIN, PAN, GSTIN.
  If no printed PO exists return "" - many vendors print none.
- handwritten_po_raw: a PO WRITTEN BY HAND in pen (blue / black) or pencil anywhere on the page - often in the white
  space beside "Transporter Details", near the stamps, in a margin or above the item table - e.g. "PO - 5100002448",
  "po-51/2448", "PO 5100002448". Copy the digits as written. Look for it on EVERY invoice even when a printed PO box exists
  but is empty. Do not mix it up with the handwritten GRN (starts 50..., written in the stamp / top margin).
- vendor_code: the KTFL vendor code ONLY if a value is printed after "Vendor Code" / "Customer Code" / "Supplier Code".
  If the value is blank, "Na", "NA" or "-", return "".
- plant_hint: the KTFL plant named in the Ship To / Dispatch To block, copied as printed (for example the plant name
  or place line).
- hsn_sac: the HSN of the highest-value material row (one code only).
- handwritten_references: short note (under 40 characters) of handwritten PO/GRN notes only. Never bundle or heat numbers.

LINE ITEMS (lines[])
- One entry per numbered row of the main item table, across all invoice pages, in original serial order.
- item: the product description text, never the serial number. Keep the full multi-line description and material codes.
  If two description columns exist, join them.
- quantity: the quantity in the QTY / NET WT column (the number next to the UOM). Not the box / package / pieces-count
  column and not a total-weight field.
- uom: copy ONLY from the row's UOM column or the unit printed next to the quantity. If the table has no unit for the
  quantity, return "". Never infer it from a total weight, box count or any other part of the page.
- rate, amount: the row's rate and its line amount (taxable value of the row).
- hsn_sac: that row's own HSN.
- Skip service / loading-charge rows (HSN 9965...) and handwritten tallies.

TOTALS (from the printed summary at the bottom, not from your own sums)
- subtotal: the printed "Taxable Value" / "Taxable Amount" / "Total Taxable Value". Not a weight, not the grand total.
- cgst, sgst, igst: the printed rupee amount of each tax. Read each one independently. A tax that is not charged is 0.0.
  Do not copy the taxable value into a tax field, and do not split one tax into two.
- round_off: printed "Round Off" amount, with its sign if shown, else 0.0.
- other_charges: freight / packing / loading charges that are outside the taxable value and added to the total, else 0.0.
- total: the printed final "Grand Total" / "Total Invoice Value" (the rounded payable amount).
- currency: "INR" unless another currency is printed.

HINDALCO-STYLE TABLES: columns run "Sr & Description | HSN | Alloy/Section | PKGS | PCS | GROSS WT | NET WT | UOM | RATE | ...
| TAXABLE VALUE". quantity is the NET WT value immediately left of "KG" (a decimal weight, not PCS).

Return strictly valid JSON with exactly these keys, no markdown:
{
  "vendor": "",
  "gstin": "",
  "invoice_number": "",
  "invoice_date": "",
  "po_number_raw": "",
  "handwritten_po_raw": "",
  "vendor_code": "",
  "plant_hint": "",
  "hsn_sac": "",
  "currency": "INR",
  "subtotal": 0.0,
  "cgst": 0.0,
  "sgst": 0.0,
  "igst": 0.0,
  "round_off": 0.0,
  "other_charges": 0.0,
  "total": 0.0,
  "lines": [
    {"item": "", "quantity": 0.0, "uom": "", "rate": 0.0, "hsn_sac": "", "amount": 0.0}
  ],
  "handwritten_references": ""
}
"""

# ---------------------------------------------------------------------------
# Optional per-vendor hints. After the vendor is identified (GSTIN -> vendor master),
# append the matching text to PROMPT for the extraction call. Keep each hint short.
# Keys are seller GSTINs.
# ---------------------------------------------------------------------------
VENDOR_HINTS = {
    # SLR METALIKS (Karnataka) - prints NO KTFL PO; the PO is handwritten; letterhead carries phone numbers
    "29AAJCS4396P1Z9": r"""
VENDOR NOTE (SLR Metaliks): this vendor does NOT print the KTFL purchase order. Leave po_number_raw "" - "Order No.: PS26Y-..."
is SLR's own sales order, "Customer Ref No." is not a PO, and the numbers after "Mo. No." / "Ph. No." (839429...) are
SLR's phone numbers, never a PO. The KTFL PO is written BY HAND in blue / black pen as "PO - 51XXXXXXXX", usually to the right
of the "Transporter Details" block or near the stamps: copy it into handwritten_po_raw. The goods go to Maharashtra from
Karnataka, so the tax is IGST (cgst = sgst = 0). The "Total :" row under the item table shows net quantity, taxable value
and IGST amount; "Total Invoice Value (INR)" at the bottom right is the total; "ROUND OFF" is a small signed amount.
Read the buyer / consignee GSTIN only as the buyer. The seller GSTIN is the one under "Details of Supplier".
""",
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


# Focused second read of the tax / totals box (used only when the first read does not add up).
TOTALS_PROMPT = """This image is the LOWER HALF of an Indian GST tax invoice. Read ONLY the amounts block (taxable value,
tax lines and grand total). Return JSON only:
{"subtotal": number, "cgst": number, "sgst": number, "igst": number, "round_off": number, "other_charges": number, "total": number}

Rules:
- subtotal = total TAXABLE value before tax (sum of the line amounts / "Taxable Amount" / "Assessable Value"). It is NOT the grand total.
- cgst, sgst, igst = the tax AMOUNTS in rupees (not the % rates). Only one of (cgst+sgst) or igst is normally non-zero; use 0 for the other.
- other_charges = freight / packing / insurance / TCS added outside the taxable value (0 if none). round_off = signed rounding (0 if none).
- total = final payable amount ("Grand Total" / "Invoice Total" / "Net Amount"), the number in the box that is also written in words.
- Copy digits exactly, remove thousand separators, keep decimals. Indian grouping 13,25,688.00 -> 1325688.00.
- If a value is not printed use 0. Do not calculate or guess: read."""


# Focused look for a handwritten PO (used only when no valid material PO was found by the main read).
HANDWRITTEN_PO_PROMPT = """This is a page of an Indian tax invoice addressed to Kalyani Technoforge Ltd. The invoice does NOT
print the buyer's purchase order number, so someone wrote it BY HAND with a pen (blue or black ink) or pencil, usually as
"PO - 51XXXXXXXX" / "PO 61XXXXXXXX" / "P.O. 5100002448", in white space beside "Transporter Details", near the stamps,
in a margin or above the item table.
Find that handwritten PO number. A KTFL PO has exactly 10 digits and starts with 51 or 61 (for example 5100002448).
Return JSON only: {"handwritten_po": "<10 digits or empty>", "where": "<short place description>"}
Do NOT return: printed numbers, phone / mobile numbers, E-way bill numbers, invoice numbers, dates, vehicle numbers,
or the handwritten GRN (the GRN starts with 50 and is written inside the receiving stamp or the top margin).
If you cannot see a handwritten PO, return {"handwritten_po": "", "where": ""}. Never guess digits."""
