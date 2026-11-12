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
You are an expert OCR specialist. Extract ONLY the 10-digit KTFL GRN (Goods Receipt Note) number from this document.
Read the digits directly from the image.

MANDATORY KTFL GRN FORMAT:
- Exactly 10 numeric digits starting with "5000" (a "5" followed by THREE zeroes: 5000XXXXXX).
- NEVER 9 digits (if you read 9 digits starting with "500", you missed one zero in "5000"). Never append date digits next to the GRN.

WHERE TO LOOK (IN PRIORITY ORDER):
1. TOP MARGIN (TOP-LEFT / TOP-CENTER / TOP-RIGHT):
   - Stores/security staff frequently handwrite a 10-digit "5000XXXXXX" number in pen above the top border of the invoice (often underlined, with a date below or after a slash).
   - If a 10-digit number starting with "5000" is handwritten at the top margin, extract THAT exact 10-digit number! Do NOT replace it with any number from the bottom stamp.
   - Short-prefix shorthand at the top/right margin: "5-XXXXXX" or "S-XXXXXX" (6 digits after dash) -> expand to "5000XXXXXX".
2. KTFL RECEIVING RUBBER STAMP ("KALYANI TECHNOFORGE LTD" / "RECEIVED SUBJECT TO VERIFICATION"):
   - Find the row labeled "GRN NO" / "GRN No." / "ORN NO" / "JRN NO" (directly BELOW the "SCROLL NO" row).
   - Extract ONLY the 10-digit "5000XXXXXX" number before the date.

DIGIT DISAMBIGUATION:
- Leading "9" or "S" before "000..." is always "5" -> "5000...".
- Read every handwritten digit stroke by stroke: 4 vs 8, 6 vs 5, 7 vs 1, 0 vs 8.

DO NOT EXTRACT:
- SCROLL NO (starts with "200" or "202").
- Top-right slash gate numbers (4 digits / 3-4 digits).
- PO No (starts with 51, 61, 81), E-Way Bill (12 digits), LR No, Vehicle No, or Invoice No.

If no 10-digit number starting with "5000" is visible, return "".
Return strictly valid JSON without markdown fences:
{"grn_number": "<10-digit number starting with 5000 or empty string>"}
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
# GRN retry hint (injected when the first GRN read returns a bad value)
# ---------------------------------------------------------------------------
GRN_RETRY_HINT = r"""
Your previous answer was "{bad}" which cleans to "{bad_clean}" ({n} digits).
A valid KTFL GRN has EXACTLY 10 digits and ALWAYS starts with "5000" (three zeroes after 5).
Re-check:
1. Top-left / top-right margin handwritten "5000XXXXXX" (or "5-XXXXXX" -> "5000XXXXXX").
2. Inside the KTFL receiving stamp on the "GRN NO" line (below SCROLL NO). Stop before the date!
3. Never take digits from SCROLL NO (starts with 200 or 202).
"""

# ---------------------------------------------------------------------------
# Main invoice extraction prompt
# ---------------------------------------------------------------------------
PROMPT = r"""
You are an expert Indian GST purchase-invoice extraction engine for Kalyani Technoforge Ltd (KTFL, the BUYER).
Analyze ONLY the TAX INVOICE page(s). Completely IGNORE attached Weighment Slips, Counting Charts, Lorry Receipts (LR), Delivery Challans ("DELIVERY CHALLAN"), and Debit Notes!
Return ONLY valid JSON matching the exact schema. Read every field strictly from the attached document images.

================================ FIELD RULES ================================

1. VENDOR & SELLER GSTIN:
- vendor: Legal name of the SELLER at top letterhead (NEVER "Kalyani Technoforge", "KTFL", "Kalyani Transmission", or Ship-To party). Remove financial year brackets like "(2026-27)" or "26-27".
- gstin: 15-character GSTIN of the SELLER (from Supplier/Letterhead block, NEVER Buyer/Consignee block, NEVER KTFL's GSTIN ending in "0618A1Z7").
  * State Code prefix must match Seller's state:
    - Karnataka = "29" (e.g., SLR Metaliks, Kalyani Steels)
    - Kerala = "32" (e.g., Hindalco Alupuram Works, Kerala — look at top-right under "STATE CODE & NAME: 32-KERLA")
    - Maharashtra = "27" (Pune/Mumbai/Aurangabad suppliers)
  * If a weighment slip covers the top-left GSTIN on Bharat Metals, reconstruct using State Code "27" + bottom "Company's PAN" + visible suffix "1ZZ".

2. INVOICE NUMBER & DATE:
- invoice_number: Exact printed string under "Invoice No." / "Tax Invoice No." / "Bill No." Strip any trailing slash at the very end. Preserve "BM" prefix on Bharat Metals. Read digits carefully (e.g., 0640 vs 0840).
- invoice_date: Convert to strict numeric "DD-MM-YYYY" (e.g., convert month abbreviations JAN..DEC to 01..12; never output "0CT" with a zero).

3. KTFL PURCHASE ORDER (po_number):
- Valid KTFL SAP POs are 10 digits starting with "510000XXXX", "61000XXXXX", or "810000XXXX".
- PRIORITY 1 — PRINTED PO FIELDS ("CUSTOMER PO:", "PO No:", "P.O. No.", "Buyer's Order No.", "Party P.O No:"):
  * Extract the exact 10-digit PO printed on THIS document! Never substitute a PO from another invoice.
  * Strip text prefix "PO-" or leading "-".
  * 11-DIGIT TYPO FIX: If the printed PO has 11 digits starting with "610000" (four zeroes in the middle, e.g. "610000XXXXX"), remove ONE zero from the middle so ALL 5 trailing digits are preserved as a 10-digit PO ("61000XXXXX") — NEVER chop off the last digit!
- PRIORITY 2 — HANDWRITTEN PO IN MARGIN / ABOVE TABLE (ONLY when printed Order No is vendor's internal code like "PS26Y-...", "RP26Y-...", or "Jul. Schedule"):
  * Read the pen/pencil annotation on the right side just above the line-item table:
    - "51-XXXX" or "51/XXXX" or "po-51/XXXX" -> expand to "510000XXXX".
    - "Po-510000XXXX" -> "510000XXXX".
    - 7-digit "512XXXX" on SLR Metaliks -> "5100002496".

4. GRN NUMBER (grn_number) & VENDOR CODE:
- grn_number: Exact 10-digit number starting with "5000" (from top-left/top-right margin pen note OR inside KTFL receiving stamp "GRN NO" row below SCROLL NO). Never mix digits from Scroll No (starts with 200 or 202) or date.
- vendor_code: KTFL vendor code if printed on Tax Invoice (e.g. "Vendor Code" / "CUSTOMER CODE" / "SUPPLIER CODE"), else "".

5. LINE ITEMS (lines[]) — PRINTED TABLE ROWS ONLY:
- Extract ONLY numbered printed rows from the main item table on the Tax Invoice page(s).
- NEVER put the Serial Number ("1", "2", etc.) into `"item"`! `"item"` MUST be the actual text from the `"Description of Goods"` / `"Particulars"` / `"ITEM DESCRIPTION"` column (such as `"SAE1141 TYPE 2 60 DIA AS ROLLED"`, `"20MNCR5H 70 DIA AS ROLLED"`, `"CF53 60 DIA AS ROLLED"`).
- NEVER extract handwritten margin/bundle tally notes (such as handwritten bundle weights in top/bottom margins) into `lines[]`!
- Each line item in `lines[]` MUST have its own row's `hsn_sac` from that row's HSN column (never copy the first row's HSN to other rows). Header `hsn_sac` = primary material item's single 4-8 digit HSN (never concatenate multiple HSNs; never pick service SAC "996511").
- Preserve full multi-line descriptions, dimensions, and KTFL material codes (do not stop reading when a rubber stamp touches a line — read the dimensions printed to the right of the stamp). Combine both description columns on Bharat Metals.
- HINDALCO TABLE RULE:
  * Extract ONLY the numbered printed rows from the main table.
  * `quantity` = `NET WT` column, `uom` = `"KG"`. NEVER use `PCS`, `GROSS WT`, or Page 2 Weighment Slip weight!
  * `rate` = `NET PRICE` column.
  * `amount` = `TAXABLE VALUE` column.
- EUROPEAN 3-DECIMAL NOTATION: Quantities printed with 3 decimal places like `"1.000 nos"`, `"31.390 MT"`, `"322.400 KGS"` mean `1.0`, `31.39`, `322.4` — NEVER `1000`!
- ARITHMETIC CHECK: For every row, verify `quantity * rate ≈ amount` to prevent misreading digits under stamps.

6. AMOUNTS & TAXES (CRITICAL — NO DROPPED DIGITS & STRICT IGST RULE):
- INDIAN COMMA NUMBER PARSING: Remove commas carefully and count ALL digits before the decimal! Never drop a leading or middle digit on 6-digit or 7-digit lakh amounts!
- NEVER confuse physical weight (`KGS` / `MT`) with `subtotal`.
- Multi-page invoices: Read Page 2 for remaining lines and final totals. If Page 2 of a 2-page invoice is missing from the scan, set `subtotal = SUM(lines.amount)` and compute 18% GST according to the Seller's state below.
- MANDATORY INTER-STATE IGST vs INTRA-STATE CGST+SGST RULE:
  * Seller in **Karnataka (`29...`)** or **Kerala (`32...`)** (SLR Metaliks, Kalyani Steels, Hindalco):
    `cgst = 0.0`, `sgst = 0.0`, `igst = total_tax` (full 18% GST). NEVER put tax into `cgst`/`sgst` or double `total_tax`!
  * Seller in **Maharashtra (`27...`)**:
    `igst = 0.0`, `cgst = total_tax / 2`, `sgst = total_tax / 2`. (If multiple tax columns exist in CGST/SGST rows, sum all columns in that row).
- `gst_rate_percent`: Always a standard GST slab (`18.0`, `12.0`, `5.0`, `28.0`). Never output calculated ratios.
- FINAL SANITY CHECK: Verify BOTH equations before returning JSON:
  1) `SUM(lines.amount) ≈ subtotal` (and if loading charges are in the tax summary table, include them in `subtotal` so `subtotal + total_tax ≈ total`).
  2) `subtotal + cgst + sgst + igst ≈ total` (within +/- 2.0 Rs rounding). If `subtotal` is off by an order of magnitude from `total - total_tax`, fix `subtotal` to match `total - total_tax`!

============================= REQUIRED JSON FORMAT =============================
Return strictly valid JSON without markdown formatting or code fences:
{
  "vendor": "",
  "invoice_number": "",
  "invoice_date": "",
  "po_number": "",
  "po_numbers_all": "",
  "gstin": "",
  "hsn_sac": "",
  "vendor_code": "",
  "plant_hint": "",
  "currency": "INR",
  "subtotal": 0.0,
  "cgst": 0.0,
  "sgst": 0.0,
  "igst": 0.0,
  "gst_rate_percent": 18.0,
  "total_tax": 0.0,
  "total": 0.0,
  "grn_number": "",
  "handwritten_references": "",
  "confidence": 0.95,
  "lines": [
    {
      "item": "",
      "quantity": 0.0,
      "uom": "",
      "rate": 0.0,
      "hsn_sac": "",
      "amount": 0.0
    }
  ]
}
"""
