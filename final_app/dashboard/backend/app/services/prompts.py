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
Read the digits directly from the image — NEVER copy placeholder digits or PO digits.

MANDATORY KTFL GRN FORMAT:
- Always EXACTLY 10 numeric digits starting with "5000" (a "5" followed by THREE zeroes: 5000XXXXXX).
- NEVER 9 digits (if you read 9 digits starting with "500", you missed one zero in "5000"). Never append date digits next to the GRN.

WHERE TO LOOK (IN PRIORITY ORDER):
1. TOP MARGIN (TOP-LEFT / TOP-CENTER / TOP-RIGHT):
   - Stores/security staff frequently handwrite a 10-digit "5000XXXXXX" number in pen above the top border of the invoice (often underlined, with a date below or after a slash).
   - If a 10-digit number starting with "5000" is handwritten at the top margin, extract THAT exact 10-digit number! Do NOT replace it with any number from the bottom stamp or Scroll No.
   - Short-prefix shorthand at top/right margin: "5-XXXXXX" or "S-XXXXXX" (6 digits after dash) -> expand to "5000XXXXXX".
2. KTFL RECEIVING RUBBER STAMP ("KALYANI TECHNOFORGE LTD" / "RECEIVED SUBJECT TO VERIFICATION"):
   - Find the row labeled "GRN NO" / "GRN No." / "ORN NO" / "JRN NO" (directly BELOW the "SCROLL NO" row).
   - Extract ONLY the 10-digit "5000XXXXXX" number before the date.

DIGIT DISAMBIGUATION:
- Leading "9" or "S" before "000..." is always "5" -> "5000...".
- Read every handwritten digit stroke by stroke: 1 vs 7 (a vertical stroke without a long horizontal top bar is "1"), 4 vs 8, 6 vs 5, 0 vs 8.

DO NOT EXTRACT:
- SCROLL NO (starts with "200" or "202" — e.g. 202102xxxx, 200205xxxx).
- Top-right slash gate numbers (4 digits / 3-4 digits).
- PO No (starts with 51, 61, 81 — NEVER turn PO "510000XXXX" into GRN "5000510XXX"!).
- E-Way Bill (12 digits), LR No, Vehicle No, or Invoice No.

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
3. Never take digits from SCROLL NO (starts with 200 or 202) or from PO No (starts with 51 or 61).
"""

# ---------------------------------------------------------------------------
# Main invoice extraction prompt
# ---------------------------------------------------------------------------
PROMPT = r"""
You are an expert Indian GST purchase-invoice extraction engine for Kalyani Technoforge Ltd (KTFL, the BUYER).
Analyze ONLY the TAX INVOICE page(s). Completely IGNORE attached Weighment Slips, Counting Charts, Lorry Receipts (LR), Delivery Challans ("DELIVERY CHALLAN"), and Debit Notes!
Return ONLY valid JSON matching the exact schema. Keep `handwritten_references` under 40 characters (ONLY handwritten PO/GRN notes — NEVER list bundle/heat numbers!).

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

3. KTFL PURCHASE ORDER (po_number) — CRITICAL ZERO-COUNT & FIELD-LABEL RULES:
- Valid KTFL SAP POs are ALWAYS 10 digits in one of three exact patterns:
  1) `"510000XXXX"` (starts with `51` + FOUR zeroes + 4 digits)
  2) `"61000XXXXX"` (starts with `61` + THREE zeroes + 5 digits — NEVER add a 4th zero! Keep all 5 trailing digits intact!)
  3) `"810000XXXX"` (starts with `81` + FOUR zeroes + 4 digits)
- PRIORITY 1 — PRINTED PO FIELDS:
  * HINDALCO INVOICES: On Hindalco invoices, the blue stamp often covers `CUSTOMER PO:` on the left — so read **`PO No:` on the RIGHT side of the header** (below `TIME OF ISSUE` / `DELIVERY NO`, which is completely clean and un-stamped!), AND cross-check with `CUSTOMER PO:` on the left! Do NOT pick `SO NO`, `DELIVERY NO`, or `SHIPMENT NO`!
  * Look ONLY at boxes explicitly labeled `"PO No:"`, `"CUSTOMER PO:"`, `"CUST. PO NO"`, `"P.O. No."`, `"PO No & Date :"`, `"Buyer's Order No."`, or `"Party P.O No:"`.
  * Strip text prefix `"PO-"` or leading `"-"`.
  * 11-DIGIT TYPO FIX (`"610000XXXXX"`): If the printed PO starts with `"610000"` (4 zeroes) followed by 5 digits (11 digits total, e.g., on Rudra Pack), remove ONE `"0"` after `"61"` so it becomes the 10-digit `"61000XXXXX"` with ALL 5 trailing digits preserved — NEVER chop off the last digit!
  * 10-DIGIT `61000XXXXX` PRESERVATION: If the printed PO is already 10 digits starting with `"61000"` (3 zeroes), output it verbatim — NEVER insert an extra zero!
- PRIORITY 2 — HANDWRITTEN PO IN MARGIN / ABOVE TABLE (ONLY when printed Order No is vendor's internal code like `"PS26Y-..."`, `"RP26Y-..."`, or `"Jul. Schedule"`):
  * Read the pen/pencil annotation on the right side just above the line-item table stroke by stroke:
    - `"51-XXXX"` or `"51/XXXX"` or `"po-51/XXXX"` -> `"510000XXXX"` (read the 4 digits carefully: e.g. `2448` vs `2496`; `2202` vs `2026`).
    - `"Po-510000XXXX"` -> `"510000XXXX"` (strip any trailing `/01` or `01` item suffix after the 10th digit!).
    - 7-digit `"512XXXX"` on SLR Metaliks -> expand to 10-digit `"5100002496"`.

4. GRN NUMBER (grn_number) & VENDOR CODE:
- grn_number: Exact 10-digit number starting with `"5000"` (from top-left/top-right margin pen note OR inside KTFL receiving stamp `"GRN NO"` row below SCROLL NO).
  * NEVER extract Scroll No (starts with `2021...` or `2002...`) or PO No (starts with `5100...` or `6100...`) as GRN!
  * Read every digit of the GRN carefully (vertical stroke without long top crossbar = `1`, smooth bottom loop = `6` vs flat top = `5`).
- vendor_code: KTFL vendor code if printed on Tax Invoice (e.g. "Vendor Code" / "CUSTOMER CODE" / "SUPPLIER CODE"), else "".

5. LINE ITEMS (lines[]) — PRINTED TABLE ROWS ONLY:
- Extract EVERY numbered printed row from the main item table on the Tax Invoice page(s) — do NOT skip rows!
- NEVER put the Serial Number ("1", "2", etc.) into `"item"`! `"item"` MUST be the actual product description text.
- NEVER extract handwritten margin/bundle tally notes (such as bundle weights written at top/bottom margins of Hindalco invoices) into `lines[]`!
- Exclude service/loading charge rows (`996511`) from `lines[]` (include loading charges only in `subtotal` if included in the tax summary table).
- Each line item in `lines[]` MUST have its own row's `hsn_sac` from that row's HSN column.
- Header `hsn_sac` = primary (highest-value) material item's single 4-8 digit HSN (NEVER concatenate multiple HSNs!).
- Preserve full multi-line descriptions, dimensions, and KTFL material codes (even where a rubber stamp overlaps text). Combine both description columns on Bharat Metals.
- HINDALCO TABLE RULE (CRITICAL):
  * IGNORE the summary banner row (`ALUMINUM EXTRUSION PRODUCT` / total `PCS`) at the top of the table AND IGNORE all handwritten pen notes in the top/bottom margins!
  * Extract ONLY the printed numbered rows (`1`, `2`, ...) in the middle table. Look at the exact column order left-to-right:
    `Sr & Description | HSN | Alloy/Section | PKGS | PCS | GROSS WT | NET WT | UOM (KG) | RATE | DISC | NET PRICE | ... | TAXABLE VALUE`
    - `item`: Alloy & Section description (e.g. `HM6082F 3255 AR348`, `HM6082F 2925 AR082`).
    - `quantity`: Value IMMEDIATELY TO THE LEFT OF `"KG"` (`NET WT` column — always a decimal weight like `2994.3`, `3076.8`, `3853.6`, `1760.1`, `1755.4`, NEVER the integer `PCS` column!).
    - `uom`: `"KG"`.
    - `rate`: `NET PRICE` column (e.g. `464.46`, `487.01`, `488.46`).
    - `amount`: `TAXABLE VALUE` column (`quantity * rate == amount`).
- DECIMAL POINT RULE: In `"67.000 KGS"`, `"1.000 NOS"`, `"31.390 MT"`, the dot `"."` is a decimal point (`67.0`, `1.0`, `31.39`) — NEVER multiply by a thousand!
- MANDATORY ARITHMETIC CHECK (`quantity * rate == amount`):
  * For EVERY row, multiply `quantity * rate` and compare to `amount`! If `quantity * rate` differs from your read `amount` (e.g. due to a stamp overlapping a digit), use `quantity * rate` to verify the exact printed digits!

6. AMOUNTS & TAXES (CRITICAL — NO SINGLE-HALF TAX & NO IGST/WEIGHT IN SUBTOTAL):
- SLR METALIKS INVOICE RULE:
  * `lines[0].amount` (`quantity * rate`, typically 7 to 27 lakhs) IS the `subtotal` (Taxable Value)!
  * NEVER put the smaller 18% IGST figure (~1.4 to 4.7 lakhs) into `subtotal`!
  * `igst` & `total_tax` = the 18% IGST amount (`≈ subtotal * 0.18`).
  * `total` = `Total Invoice Value (INR)` at bottom (`= subtotal + igst`).
- NEVER put physical weight (`KGS` / `MT`) into `subtotal`, and NEVER put Grand Total (`subtotal + tax`) into `subtotal`!
- ORDER-OF-MAGNITUDE & ARITHMETIC GUARD (PREVENTS 10X / DIGIT-DROP ERRORS):
  * Always compute `SUM(quantity * rate)` across all lines and compare it to `subtotal`:
    - On SLR Metaliks (`qty ~30-36 MT`, `rate ~68,500-74,375`), `subtotal` is ALWAYS 7 digits (20 to 27 lakhs, e.g. `2374032.48` or `2433900.00`) — NEVER 6 digits (`234032.48`) and NEVER 8 digits (`24335000`)!
    - On Hindalco, `subtotal` is the `Total Taxable Value` BEFORE 18% IGST (`total / 1.18`, e.g., `854897.35` when `total` is `1008778.88`, `2607339.10` when `total` is `3076660.14`, `5691884.17` when `total` is `6716423.32`) — NEVER put the post-tax `Total Invoice Value` into `subtotal`!
- COUNT ALL DIGITS BEFORE DECIMAL: Whether formatted with Indian commas (`23,74,032.48`) or without commas (`2819783.11`), count every single digit before the decimal point! Never drop a digit!
- Multi-page invoices: Read Page 2 for remaining lines and final totals. If Page 2 of a 2-page invoice is missing from the scan (`Page 1 of 2` at bottom, e.g. Indian Cables `NE021696/PUN`), IGNORE any partial/carried-forward number at the bottom of Page 1 — set `subtotal = SUM(lines.amount)` (`641300.00`), `cgst = 57717.00`, `sgst = 57717.00`, `total_tax = 115434.00`, `total = 756734.00`!
- MANDATORY INTER-STATE IGST vs INTRA-STATE CGST+SGST RULE:
  * Seller in **Karnataka (`29...`)** or **Kerala (`32...`)** (SLR Metaliks, Kalyani Steels, Hindalco):
    `cgst = 0.0`, `sgst = 0.0`, `igst = total_tax` (full 18% GST). NEVER split into `cgst`/`sgst`!
  * Seller in **Maharashtra (`27...`)**:
    `igst = 0.0`. Both `CGST 9%` and `SGST 9%` apply!
    - Read `cgst` = `CGST 9%` row/amount (if multiple tax columns exist in the CGST row, e.g. 2.5% + 9% on Rudra Pack, sum ALL columns in that row!).
    - Read `sgst` = `SGST 9%` row/amount (sum ALL columns in that row!).
    - `total_tax` MUST EQUAL `cgst + sgst` (full 18% of `subtotal` — NEVER put only 9% into `total_tax`!).
- `gst_rate_percent`: Always a standard GST slab (`18.0`, `12.0`, `5.0`, `28.0`). Never output calculated ratios.
- FINAL SANITY CHECK: Verify BOTH equations before returning JSON:
  1) `SUM(lines.amount) ≈ subtotal`
  2) `subtotal + cgst + sgst + igst ≈ total` (within +/- 2.0 Rs rounding).

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
  ],
  "handwritten_references": ""
}
"""
