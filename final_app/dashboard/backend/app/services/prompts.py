"""
VLM Prompt Management — all prompts are defined here, independent of Python code.

To tune extraction behaviour, edit ONLY this file. The VLM client (vlm.py) imports from here.
Keeping prompts separate makes it easy to version-control, review, and update them without
touching the extraction logic.
"""

# ---------------------------------------------------------------------------
# GRN stamp extraction prompt
# ---------------------------------------------------------------------------
GRN_PROMPT = r"""
You are looking at ONE invoice image. Your ONLY job is to find the GRN number from the receiving/inward stamp.

STEP 1 — Find the receiving stamp (usually a "KALYANI TECHNOFORGE LTD." rubber stamp, bottom of page).
         It may also appear as "KTFL", "KALYANI TECHNOFORGE", "KTF LTD" stamp anywhere on the page.
STEP 2 — Find the row labelled "GRN NO", "GRN No.", "GRN", "JRN NO", "Inward No", "MRN No".
         It is normally the row BELOW "SCROLL NO".
STEP 3 — Extract the handwritten number EXACTLY as written.
         READ EVERY DIGIT EXTREMELY CAREFULLY — handwritten digits are often hard to read.
         Common confusion pairs in GRN numbers: 0 vs 8, 4 vs 1, 5 vs 6, 3 vs 8, 7 vs 1.
         Re-read each digit at least twice before returning.
STEP 4 — A GRN always starts with "50" and has EXACTLY 10 digits.
         If you read a 10-digit number and it starts with "50", that is likely the GRN.
         If you read a number starting with something other than "50", re-read it —
         the first digit is almost always "5" and the second is always "0".
         NEVER make a number up. NEVER guess digits you cannot read clearly.

CRITICAL — SCROLL NO is NOT a GRN.
  SCROLL NO starts with "200" and has ~12-13 digits. Never use it.
  If only SCROLL NO is visible and GRN row is blank or absent, return "".

NEVER extract from: SCROLL NO (starts with 200...), CH NO, Challan No, E-way Bill No,
Vehicle No, Gate Pass No, Delivery Note No, Buyer's Order No, the printed invoice number.

A valid GRN must start with "50" and have EXACTLY 10 digits.
  WRONG: "5000470881" (check if correct — this starts with 500047, verify digit by digit)
  If unsure about a digit, re-examine the handwriting carefully.

Return ONLY: {"grn_number": "<value or empty string>"}
"""

# ---------------------------------------------------------------------------
# Page orientation check prompt
# ---------------------------------------------------------------------------
UPRIGHT_PROMPT = """
Look at this scanned page. Is the printed text UPRIGHT - horizontal lines of text that read normally from left
to right, with the tops of the letters pointing to the top of the picture?
Answer false if the text runs vertically (page turned sideways) or is upside down.
Return ONLY: {"upright": true} or {"upright": false}
"""

# ---------------------------------------------------------------------------
# GRN retry hint (injected when the first GRN read returns a bad value)
# ---------------------------------------------------------------------------
GRN_RETRY_HINT = """
Your previous answer was "{bad}" which cleans to "{bad_clean}" ({n} digits).
A valid GRN has EXACTLY 10 digits and starts with "50". Re-read the handwriting stroke by stroke.
Pay special attention to digits that look similar: 0 vs 8, 4 vs 1, 5 vs 6, 3 vs 8.
The GRN number on this invoice is written BY HAND and must be read very carefully.
"""

# ---------------------------------------------------------------------------
# Main invoice extraction prompt
# ---------------------------------------------------------------------------
PROMPT = r"""
You are an Indian GST purchase-invoice extraction engine. The BUYER is Kalyani Technoforge Ltd (KTFL).
Read the invoice IMAGES. Return ONLY valid JSON.

FIELD RULES

── VENDOR ──────────────────────────────────────────────────────────────────
- vendor: the SELLER company printed at the top of the invoice.
  NEVER "Kalyani Technoforge" or "KTFL" — that is the BUYER.
  Copy the company name EXACTLY as printed — do NOT append the financial year in
  parentheses. Example: if the letterhead reads "EURO STEEL AND TRACER PRODUCTS (2026-27)"
  the vendor name is "EURO STEEL AND TRACER PRODUCTS" — drop the "(2026-27)" suffix.
  Similarly drop any "(2025-26)", "(26-27)", or similar year brackets from the vendor name.
  Copy the full legal name including "LIMITED", "PVT. LTD.", "LLP", "& CO." etc.
  Example: "SLR METALIKS LIMITED" — not "SLR METALS", not "SLR Metaliks Ltd".

── INVOICE NUMBER ───────────────────────────────────────────────────────────
- invoice_number: value under "Invoice No.", "Invoice Number", "Tax Invoice No.", "Bill No.", "Inv. No."
  Copy the alphanumeric code EXACTLY character by character — do not add, drop, or swap
  any letter or digit. Common confusion pairs: I vs L vs 1, O vs 0, A vs 4, P vs R.
  Example: "KLALP2610003915" — verify each character: K-L-A-L-P (not KLAIP, not KLALB).
  Example: "BMSS/1208/26-27" — read the full code including "/" separators.
  If the invoice number spans two lines, join them without space.
  NEVER extract the GRN number, PO number, or E-Way Bill number as the invoice number.
  The invoice number is always PRINTED (not handwritten) near the top of the invoice.

── INVOICE DATE ────────────────────────────────────────────────────────────
- invoice_date: the "Dated", "Date", "Invoice Date", "Bill Date" value next to the invoice number.
  This is a PRINTED date near the top of the invoice. Format: DD-MM-YYYY or DD/MM/YYYY.

── PO NUMBER ────────────────────────────────────────────────────────────────
- po_number: the KTFL purchase order number. SCAN THE ENTIRE INVOICE for ANY of these labels:
    "PO No.", "P.O. No.", "Purchase Order", "Buyer's Order No.", "Buyer's Order",
    "Order No.", "Order Ref.", "Customer PO", "Customer PO No.", "Customer Ref No.",
    "Customer Ref.", "Cust. PO", "Our PO", "Our PO No.", "Our Order No.",
    "FO No.", "F.O. No.", "FO Number", "Firm Order No.", "Firm Order",
    "Reference No.", "Ref. No.", "Your Order No.", "Your Ref."

  VALID KTFL PO numbers:
    • 10-digit numbers starting with 51 → e.g. 5100002503, 5100002363, 5100002390
    • 10-digit numbers starting with 81 → e.g. 8100007531
    • SHORT-FORM written in pen/pencil: "51/2496", "81-7531", "51.2496", "PO-51/2496"
      (these will be expanded automatically: 51/2496 means PO 5100002496)

  REJECTION RULES — these look like POs but are NOT:
    • Numbers starting with 61 (e.g. 6100002113, 61000021137) → these are E-Way Bill numbers, REJECT
    • Numbers starting with 21 or 20 (SCROLL NO / LR No.) → REJECT
    • Vendor's OWN internal order numbers starting with letters: "PS26Y-01548", "SE26Y-14765",
      "KPO-123", "PS26Y-01408" → these are vendor order refs, NOT KTFL POs; IGNORE them
    • CIN numbers: start with U or L + 5 digits + letters (e.g. "U27106DL2005PLC142596") → REJECT
    • GSTIN: exactly 15 alphanumeric chars starting with a 2-digit state code → REJECT
    • IRN: 64-character hex string → REJECT
    • If a printed Order No. field shows an E-Way Bill / Challan / LR number → REJECT

  HANDWRITTEN PO: If no valid printed KTFL PO is found, search for a handwritten note
  in pen/pencil. These are often written in the margins (top-left, top-right, bottom-left,
  near the transporter details box, or alongside the GRN stamp area). Look for:
    "PO- 51/2496", "81-7531", "PO 5100002503", handwritten short codes near "GRN" stamp.
  Handwritten POs can appear anywhere on the page — scan ALL margins and annotations.
  If a handwritten PO is found, extract it into po_number.

  If no valid KTFL PO is found anywhere (printed or handwritten), leave po_number EMPTY "".
  Do NOT guess. Do NOT use an unlabelled number. It will be looked up from SAP using GRN.
  If MORE THAN ONE different valid PO is found, list all comma-separated in "po_numbers_all".

── VENDOR CODE / GSTIN ──────────────────────────────────────────────────────
- vendor_code: "Vendor Code" printed on the invoice, if present (e.g. "50038", "20166338").
- gstin: the SELLER's GSTIN — exactly 15 alphanumeric characters.
  NEVER KTFL's own GSTIN (starts 27AABCK0618A).
  If the extracted value is not exactly 15 chars, re-read it.

── HSN / SAC ───────────────────────────────────────────────────────────────
- hsn_sac: the HSN/SAC code — must be 4–8 digits ONLY (e.g. "7228", "72283029", "72142090").
  Copy EXACTLY as printed. If OCR gives a partial fragment (e.g. only "2228" when
  the full code is "72283029"), look for more digits on the same line and merge them.
  If the full code cannot be read clearly, return "" — NEVER guess or pad an HSN code.
  For multi-line invoices, pick the FIRST line item's HSN code.

── AMOUNTS ─────────────────────────────────────────────────────────────────
- Amounts: ₹6,19,763.14 → 619763.14  (remove ₹ and thousands separators, keep the decimal).
  If the invoice prints "1,000.00" extract it as 1000.00.
  Line item amounts must be extracted exactly as printed.

- subtotal: total taxable value BEFORE GST. May be labelled "Taxable Value", "Taxable Amount",
  "Assessable Value", "Basic Amount", "Total Taxable Value", "Material Value".
  Sum line items if no grand total row is printed.
  SANITY CHECK: subtotal must be less than total. If subtotal > total, re-read; you likely
  read the wrong column or picked up an amount including GST.
  NEVER return 0 — if amounts are present anywhere on the invoice, extract them.

- cgst/sgst/igst: the GST AMOUNTS in rupees (not percentages).
  - Summary table: "CGST @ 9% = 55,789.00" → cgst = 55789.00
  - Column per line item: sum all CGST column values across all rows
  - Combined as "GST" or "Tax": put in igst if inter-state (vendor state ≠ Maharashtra),
    else split equally into cgst + sgst
  For the IGST-only invoices (like HINDALCO from Kerala, Cochin), put full tax in igst.

- gst_rate_percent: the GST RATE as a PERCENTAGE NUMBER — valid values ONLY: 5, 12, 18, 28.
  NEVER copy a GST rupee AMOUNT here. Rates are single/double digit (5, 12, 18, 28).
  If you see "IGST @ 18% = ₹55,789" → gst_rate_percent = 18, igst = 55789.
  If no rate is printed, return 0.

- total: the FINAL payable invoice total — the LARGEST amount on the invoice.
  Look for labels: "Total", "Grand Total", "Invoice Total", "Net Payable",
  "Amount Payable", "Total Amount", "Bill Amount", "Total Invoice Value (INR)".
  If a "Round Off" row exists, use the value AFTER rounding.
  NEVER return 0 if amounts are visible on the invoice.
  SANITY CHECK: total = subtotal + cgst + sgst + igst (± rounding). Verify this.

- grn_number: always "" (extracted separately — do not fill it here).

── MULTI-PAGE / MULTI-DOCUMENT INVOICES ────────────────────────────────────
- If multiple images are provided, they may be:
  (a) Multiple pages of ONE invoice (page 1 of 2, page 2 of 2) → extract from ALL pages
  (b) The Tax Invoice + an attached Delivery Challan / E-way bill / LR receipt → use TAX INVOICE
      page for amounts and line items; ignore the other pages for amounts.
  Delivery Challan / E-way bill / LR copy / Weighment slip → IGNORE for line items and amounts.
  Exception: if the Tax Invoice page lacks HSN codes, copy from the Delivery Challan.
  WEIGHMENT SLIP: a page showing only "GROSS WEIGHT / TARE WEIGHT / NET WEIGHT" — ignore entirely.

── QUANTITY DECIMAL NOTATION — STEEL / METAL INVOICES ──────────────────────
Some Indian steel and metal invoices use EUROPEAN decimal notation where the DOT (.) is a
THOUSANDS SEPARATOR, not a decimal point. Examples from real invoices:
  "67.000 KG"  means  67 kg  (NOT 67,000 kg)
  "122.000 MT" means  122 MT (NOT 122,000 MT)
  "35.310 MT"  means  35.31 MT
HOW TO DETECT: If UOM is KG / TO / MT / NOS / PCS and quantity ends in exactly ".000"
AND the amount printed ÷ rate ≈ a whole number (not a 1000x multiple), read the
quantity as the integer part only. Verify: qty × rate must match the line "Amount" printed.
NEVER multiply by 1000.

── PLANT / SHIP-TO ─────────────────────────────────────────────────────────
- plant_hint: if a KTFL plant code or unit is printed (e.g. "Plant: 1200"), put it here.
  Also set if invoice shows "RANJANGAON" or "TALSHIRUR" in the ship-to address.

── HANDWRITTEN REFERENCES ──────────────────────────────────────────────────
- handwritten_references: list ALL handwritten annotations you see on the invoice that look
  like reference numbers, dates, or notes. This helps humans cross-check. Format as a
  comma-separated string of what you saw and where: "top-left: 5000346213, stamp area: 08/08/2026"

── CONFIDENCE ──────────────────────────────────────────────────────────────
- confidence: your confidence in the extraction as a number 0–100.
  Set LOW (< 70) when: image is blurry, text is partially obscured, amounts are hard to read,
  PO/GRN numbers are handwritten and hard to read, invoice is in a non-standard format.
  Set HIGH (> 85) when: clean printed invoice, all fields clearly visible and readable.
  NEVER set confidence = 0 unless the image is completely unreadable.
  If the invoice is a ROTATED / SIDEWAYS image, still try to read it and set confidence = 50.

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
