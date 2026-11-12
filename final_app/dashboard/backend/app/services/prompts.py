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
Read the digits directly from the image — NEVER copy example or placeholder digits.

MANDATORY KTFL GRN FORMAT:
- Always EXACTLY 10 numeric digits starting with "5000" (a "5" followed by THREE zeroes: 5000XXXXXX).
- NEVER 9 digits (if you read 9 digits starting with "500", you missed one zero in "5000"). Never append date digits next to the GRN.

WHERE TO LOOK (IN PRIORITY ORDER):
1. TOP MARGIN (TOP-LEFT / TOP-CENTER / TOP-RIGHT):
   - Stores/security staff frequently handwrite a 10-digit "5000XXXXXX" number in pen above the top border of the invoice (often underlined, with a date below or after a slash).
   - If a 10-digit number starting with "5000" is handwritten at the top margin, extract THAT exact 10-digit number! Do NOT replace it with any number from the bottom stamp.
   - Short-prefix shorthand at top/right margin: "5-XXXXXX" or "S-XXXXXX" (6 digits after dash) -> expand to "5000XXXXXX".
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
Return ONLY valid JSON matching the exact schema. Read every PO, GRN, and amount strictly from the attached document images.

================================ FIELD RULES ================================

1. VENDOR & SELLER GSTIN (EXACT REFERENCE):
- vendor: Legal name of the SELLER at top letterhead (NEVER "Kalyani Technoforge", "KTFL", "Kalyani Transmission", or Ship-To party). Remove financial year brackets like "(2026-27)" or "26-27".
- gstin: 15-character GSTIN of the SELLER (NEVER Buyer/Consignee block, NEVER KTFL's "27AABCK0618A1Z7").
  * Known Seller GSTINs (verify against document):
    - SLR METALIKS LIMITED (Karnataka): "29AAJCS4396P1Z9"
    - KALYANI STEELS LIMITED (Karnataka): "29AAACK7315D1ZQ"
    - HINDALCO INDUSTRIES LIMITED (Kerala Alupuram Works): "32AAACH1201R1ZW"
    - BHARAT METALS (Maharashtra): "27AATFB3972H1ZZ" (even if top-left is covered by a weighment slip, PAN at bottom is AATFB3972H)
    - EURO STEEL AND TRACER PRODUCTS (Maharashtra): "27HBFPS2958A1ZG"
    - BMSS STEEL INDUSTRIES PRIVATE LIMITED (Maharashtra): "27AAACG1519K1ZP"
    - VS ENTERPRISES (Maharashtra): "27AAYFV1317R1Z6"
    - K.V. STEEL AND PIPES PRIVATE LIMITED (Maharashtra): "27AADCK8810E1ZN"
    - OM ENGINEERING (Maharashtra): "27ASVPB4182F2ZI"
    - SNEHDEEP PACKAGING PVT LTD (Maharashtra): "27ABCCS2447R1ZQ"
    - ACCUMEASURE TOOLS (Maharashtra): "27AAXPG9799D1ZS"
    - RUDRA PACK SOLUTIONS PVT LTD (Maharashtra): "27AAHCR8563K1ZP"
    - SRN Packagings (Maharashtra): "27AAPPM7293L1ZS"
    - EGEARZ PRIVATE LIMITED (Maharashtra): "27AAFCE6816Q1Z0"
    - Indian Cables & Electricals Pvt. Ltd. (Maharashtra): "27AABCI4883P1ZV"
    - Ocean Induction Solutions (Maharashtra): "27AADFO8995R1Z0"
    - UNI-TECH ENGINEERS (Maharashtra): "27AADFU4266H1ZZ"
    - AURANGABAD AUTO ANCILLARY PVT. LTD. (Maharashtra): "27AACCA3515D1Z8"

2. INVOICE NUMBER & DATE:
- invoice_number: Exact printed string under "Invoice No." / "Tax Invoice No." / "Bill No." Strip any trailing slash at the very end. Preserve "BM" prefix on Bharat Metals. Read digits carefully (e.g. 0640 vs 0840).
- invoice_date: Convert to strict numeric "DD-MM-YYYY" (convert month abbreviations JAN..DEC to 01..12; never output "0CT" with a zero).

3. KTFL PURCHASE ORDER (po_number):
- Valid KTFL SAP POs are 10 digits starting with "510000XXXX", "61000XXXXX", or "810000XXXX".
- PRIORITY 1 — PRINTED PO FIELDS ("CUSTOMER PO:", "PO No:", "P.O. No.", "Buyer's Order No.", "Party P.O No:"):
  * Extract the exact 10-digit PO printed on THIS document! Never substitute a PO from another invoice.
  * Strip text prefix "PO-" or leading "-".
  * 11-DIGIT TYPO FIX: If printed as 11 digits starting with "610000" (four zeroes in the middle, e.g. "610000XXXXX"), remove ONE zero from the middle so ALL 5 trailing digits are preserved as a 10-digit PO ("61000XXXXX") — NEVER chop off the last digit!
- PRIORITY 2 — HANDWRITTEN PO IN MARGIN / ABOVE TABLE (ONLY when printed Order No is vendor's internal code like "PS26Y-...", "RP26Y-...", or "Jul. Schedule"):
  * Read the pen/pencil annotation on the right side just above the line-item table carefully digit by digit:
    - "51-XXXX" or "51/XXXX" or "po-51/XXXX" -> expand to "510000XXXX".
    - "Po-510000XXXX" -> "510000XXXX".
    - 7-digit "512XXXX" on SLR Metaliks -> expand to 10-digit "5100002496".

4. GRN NUMBER (grn_number) & VENDOR CODE:
- grn_number: Exact 10-digit number starting with "5000" (from top-left/top-right margin pen note OR inside KTFL receiving stamp "GRN NO" row below SCROLL NO). Never mix digits from Scroll No (starts with 200 or 202) or date.
- vendor_code: KTFL vendor code if printed on Tax Invoice (e.g. "Vendor Code" / "CUSTOMER CODE" / "SUPPLIER CODE"), else "".

5. LINE ITEMS (lines[]) — PRINTED TABLE ROWS ONLY:
- Extract ONLY numbered printed rows from the main item table on the Tax Invoice page(s).
- NEVER put the Serial Number ("1", "2", etc.) into `"item"`! `"item"` MUST be the actual product text from the `"Description of Goods"` / `"Particulars"` / `"ITEM DESCRIPTION"` column.
- NEVER extract handwritten margin/bundle tally notes (such as handwritten bundle weights in top/bottom margins of Hindalco invoices) into `lines[]`!
- Each line item in `lines[]` MUST have its own row's `hsn_sac` from that row's HSN column (never copy the first row's HSN to other rows). Header `hsn_sac` = primary material item's single 4-8 digit HSN (never concatenate multiple HSNs; never pick service SAC "996511").
- Preserve full multi-line descriptions, dimensions, and KTFL material codes (even where a rubber stamp overlaps text). Combine both description columns on Bharat Metals.
- HINDALCO TABLE RULE:
  * Extract ONLY the numbered printed rows from the main table.
  * `quantity` = `NET WT` column, `uom` = `"KG"`. NEVER use `PCS`, `GROSS WT`, or Page 2 Weighment Slip weight!
  * `rate` = `NET PRICE` column.
  * `amount` = `TAXABLE VALUE` column.
- EUROPEAN 3-DECIMAL NOTATION: Quantities printed with 3 decimal places like `"1.000 nos"`, `"31.390 MT"`, `"322.400 KGS"` mean `1.0`, `31.39`, `322.4` — NEVER `1000`!
- MANDATORY ARITHMETIC CHECK (`quantity * rate == amount`):
  * For EVERY row, multiply `quantity * rate` and compare to `amount`!
  * If `quantity * rate` differs from your read `amount` or `subtotal` (e.g., if a stamp covers a digit so `31.57 * 75199 = 2374032.48` vs `274032.48`, or `34.77 * 70000 = 2433900` vs `2433500`, or `40.40 * 255 = 10302` vs `10322`, or `100.40 * 255 = 25602` vs `25652`, or `55.20 * 210 = 11592` vs `11802`), use `quantity * rate` to verify the exact printed digits!

6. AMOUNTS & TAXES (CRITICAL — NO DROPPED DIGITS & STRICT IGST RULE):
- COUNT ALL DIGITS BEFORE DECIMAL: Whether formatted with Indian commas (`23,74,032.48`) or without commas (`2819783.11`), count every single digit before the decimal point! Never drop a digit!
- NEVER put the IGST amount into `subtotal`, and NEVER put physical weight (`KGS` / `MT`) into `subtotal`.
- Multi-page invoices: Read Page 2 for remaining lines and final totals. If Page 2 of a 2-page invoice is missing from the scan (e.g. `Page 1 of 2` at bottom), set `subtotal = SUM(lines.amount)` and compute 18% GST according to the Seller's state below.
- MANDATORY INTER-STATE IGST vs INTRA-STATE CGST+SGST RULE:
  * Seller in **Karnataka (`29...`)** or **Kerala (`32...`)** (SLR Metaliks, Kalyani Steels, Hindalco):
    `cgst = 0.0`, `sgst = 0.0`, `igst = total_tax` (full 18% GST). NEVER split into `cgst`/`sgst`!
  * Seller in **Maharashtra (`27...`)**:
    `igst = 0.0`, `cgst = total_tax / 2`, `sgst = total_tax / 2`. (If multiple tax columns exist in CGST/SGST rows, sum ALL columns in that row).
- `gst_rate_percent`: Always a standard GST slab (`18.0`, `12.0`, `5.0`, `28.0`). Never output calculated ratios.
- FINAL SANITY CHECK: Verify BOTH equations before returning JSON:
  1) `SUM(lines.amount) ≈ subtotal` (and if loading charges are in the tax summary table, include them in `subtotal` so `subtotal + total_tax ≈ total`).
  2) `subtotal + cgst + sgst + igst ≈ total` (within +/- 2.0 Rs rounding). If `subtotal` does not equal `total - total_tax`, re-read `subtotal` and `total`!

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
