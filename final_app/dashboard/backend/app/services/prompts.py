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
You are an expert OCR and optical inspection specialist. Your ONLY task is to find and extract the exact 10-digit KTFL GRN (Goods Receipt Note) number from this invoice document.

CRITICAL RULE: Extract ONLY what is visually present on the current document. NEVER copy example numbers from this prompt!

SEARCH LOCATIONS (SCAN IN THIS MANDATORY SEQUENCE):

LOCATION 1 — INWARD / RECEIVING RUBBER STAMP:
  - Search across the entire document (typically bottom margins, blank areas, lower-right, or stamped over printed line items).
  - Look for rubber stamp impressions with: "KALYANI TECHNOFORGE LTD", "KTFL", "KTF LTD", "KALYANI TRANSMISSION", "RECEIVED SUBJECT TO VERIFICATION", or plant names "RANJANGAON", "KHED", "MUNDHWA".
  - Inside the stamp, locate the row labeled: "GRN NO", "GRN No.", "GRN", "G.R.N. No.", "Inward No", "MRN No", "M.R.N. No.", "JRN NO", "Goods Receipt No".
  - NOTE: This row is usually directly below or beside "SCROLL NO" / "DATE" / "TIME".

LOCATION 2 — TOP MARGIN & HEADER PEN ANNOTATIONS (CRITICAL):
  - On many delivery invoices and photos, stores/security officers handwrite the 10-digit GRN number with date at the TOP LEFT, TOP RIGHT, or TOP CENTER margin in pen:
    * Format: 10 digits starting with "5000" (often followed by a slash and date, e.g. "5000xxxxxx / DD-MM-YY").
  - Check for short-prefix shorthand written at the top or right margin:
    * "5-xxxxxx" -> expand to 10 digits by inserting "000" after "5": "5000xxxxxx"
    * "S-xxxxxx" -> "5000xxxxxx"
    * "5einvxxxxx" / "Seinvxxxxx" -> "50003xxxxx"
  - If the bottom stamp's GRN row is blank, smudged, or unreadable, you MUST extract the 10-digit "5000xxxxxx" number from the TOP MARGIN.

LOCATION 3 — STAMP-ADJACENT OR TABLE-ADJACENT HANDWRITING:
  - Check pen writing near the inspector/stores signature or alongside the item table.

CHARACTER DISAMBIGUATION RULES:
  - A valid KTFL GRN must be EXACTLY 10 digits and start with "50" (format: 5000xxxxxx).
  - Sometimes the initial "5" is written with a curved top loop that looks like "9" or "S" (e.g. "900038xxxx" or "S00041xxxx") -> Always normalize the leading digit to "5" ("500038xxxx", "500041xxxx").
  - Read every handwritten digit stroke by stroke:
    * 0 vs 8: Check for closed crossing loops in the center (8) vs single open/closed oval (0).
    * 4 vs 1: Check for horizontal bar and vertical crossing leg (4) vs single upright line (1).
    * 5 vs 6: Look for flat horizontal top roof and sharp corner (5) vs continuous curved loop (6).
    * 7 vs 1 vs 2: Check top horizontal bar. Distinct top horizontal bar with diagonal stem is 7; straight vertical is 1; curved top with flat base is 2.
    * 3 vs 8: Open loops on the left is 3; dual enclosed loops is 8.
    * 9 vs 4: Curved/circular top loop is 9; sharp triangular corner with intersecting stems is 4.

STRICT REJECTION RULES (NEVER EXTRACT AS GRN):
  * SCROLL NO: starts with "200" or "202" and has 10 to 13 digits (e.g. 200205xxxx, 202102xxxx) -> REJECT.
  * Gate Entry / Bill / Red ink slash numbers in top-right (e.g. "2728/4533", "2729/4538", "2734/228") -> REJECT.
  * Lorry Receipt (LR) No (e.g. 8821, 8826, 8619), Challan No, E-Way bill number (12 digits), Vehicle No, PO No (starts with 51, 61, 81).
  * Vendor printed invoice number.

If no valid 10-digit KTFL GRN starting with "50" is visible anywhere, return empty string "".
NEVER invent, guess, or copy example digits.

Return strictly a valid JSON object without markdown or code fences:
{"grn_number": "<10-digit number starting with 50 or empty string>"}
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
A valid KTFL GRN has EXACTLY 10 digits and starts with "50" (format: 5000xxxxxx).
Re-read the handwriting stroke by stroke on the current image. Look at BOTH:
1. Inside the KTFL receiving rubber stamp ("GRN NO" line, directly below SCROLL NO).
2. The TOP MARGIN / HEADER where stores/security staff writes the 10-digit number in pen (or "5-xxxxxx" shorthand).
Pay special attention to ambiguous digits: 0 vs 8, 4 vs 1, 5 vs 6, 3 vs 8, 7 vs 1, 9 vs 4.
Do NOT mistake SCROLL NO (starts with 200... or 202...) or PO NO (starts with 51... or 61...) for GRN.
"""

# ---------------------------------------------------------------------------
# Main invoice extraction prompt
# ---------------------------------------------------------------------------
PROMPT = r"""
You are an expert Indian GST purchase-invoice extraction engine for manufacturing supply chains.
The BUYER is Kalyani Technoforge Ltd (KTFL).
Read all provided invoice pages/images carefully. Return ONLY a valid JSON object matching the exact schema.
NEVER copy example numbers from this prompt — extract ONLY what is printed or handwritten on the actual document!

================================ FIELD RULES ================================

── 1. VENDOR & SELLER GSTIN (CRITICAL) ──────────────────────────────────────
- vendor: Full registered legal name of the SELLER / SUPPLIER printed at top letterhead.
  * NEVER extract "Kalyani Technoforge Ltd", "KTFL", "Kalyani Transmission", or "Rosario Investment Pvt Ltd" (Ship-To) — those are BUYER / CONSIGNEE entities.
  * Copy full legal entity suffix: "LIMITED", "PVT. LTD.", "PRIVATE LIMITED", "LLP", "& CO."
  * Drop any financial year brackets or suffixes from the vendor name:
    e.g., "EURO STEEL AND TRACER PRODUCTS (2026-27)" -> "EURO STEEL AND TRACER PRODUCTS"
    e.g., "BHARAT METALS 26-27" -> "BHARAT METALS"
    e.g., "SRN Packagings - (F.Y-2026-27)" -> "SRN Packagings"
  * If a weighment slip at the top-left partially covers the first letters of the letterhead (e.g. "..RO STEEL AND TRACER PRODUCTS"), check the bottom signature block ("For, EURO STEEL AND TRACER PRODUCTS") to get the complete vendor name.

- gstin: Exactly 15-character alphanumeric GSTIN of the SELLER / SUPPLIER.
  * Format: 2 digits (State Code) + 10 chars (Seller PAN: 5 letters, 4 digits, 1 letter) + 1 entity char + "Z" + 1 check char.
  * Look ONLY in the "Details of Supplier", "Seller", "Company GSTIN", or Top Letterhead block (NEVER the Buyer / Consignee / Ship-To block!).
  * NEVER extract KTFL's GSTIN ("27AABCK0618A1Z7") or any corrupted variant ending in "0618A1Z7" (such as "29AABCK0618A1Z7", "27AABCH0618A1ZA", "27AAABK0618A1Z7")!
  * NEVER hallucinate trailing zeroes (e.g. "27AAHCB00000000000000" is INVALID).
  * RECONSTRUCTION RULE WHEN COVERED BY WEIGHMENT SLIP:
    If the top-left Seller GSTIN is partially covered by a weighment slip (e.g. only "...72H1ZZ" is visible), look at the bottom of the invoice for "Company's PAN" (e.g. "AATFB3972H") and Seller State Code ("27"), and combine them: "27" + "AATFB3972H" + "1ZZ" = "27AATFB3972H1ZZ".
  * Verify Seller State Code prefix matches Seller address state:
    - Karnataka = "29" (e.g. SLR Metaliks: "29AAJCS4396P1Z9", Kalyani Steels: "29AAACK7315D1ZQ")
    - Kerala = "32" (e.g. Hindalco Alupuram Works: "32AAACH1201R1ZW" — NEVER "27AABCH...")
    - Maharashtra = "27" (e.g. Bharat Metals: "27AATFB3972H1ZZ", Euro Steel: "27HBFPS2958A1ZG", BMSS Steel: "27AAACG1519K1ZP", VS Enterprises: "27AAYFV1317R1Z6", K.V. Steel: "27AADCK8810E1ZN", OM Engineering: "27ASVPB4182F2ZI", Snehdeep: "27ABCCS2447R1ZQ", Accumeasure: "27AAXPG9799D1ZS", Rudra Pack: "27AAHCR8563K1ZP", SRN Packagings: "27AAPPM7293L1ZS", Egearz: "27AAFCE6816Q1Z0", Indian Cables: "27AABCI4883P1ZV", Ocean Induction: "27AADFO8995R1Z0", Uni-Tech: "27AADFU4266H1ZZ", Aurangabad Auto: "27AACCA3515D1Z8")

── 2. INVOICE NUMBER & DATE ─────────────────────────────────────────────────
- invoice_number: Exact printed alphanumeric string under "Invoice No.", "Tax Invoice No.", "Bill No.", "Inv. No.", "Invoice No:".
  * Copy character by character including internal slashes and dashes.
  * Strip any trailing slash or border artifact at the very end (e.g. "2627017961/" -> "2627017961").
  * For Bharat Metals, if the logo touches "BM", preserve the full prefix ("BM/26-27/1604" or "BM 26-27/0640", not "B26-27/1604").
  * Do NOT confuse 0 with O, 1 with I/L, A with 4, P with R.
  * NEVER extract PO number, GRN, Challan No, or E-way bill as the invoice number.

- invoice_date: The date printed next to the invoice number ("Date", "Dated", "Invoice Date").
  * ALWAYS convert to numeric format: DD-MM-YYYY (e.g. "13-OCT-26" -> "13-10-2026", "05 OCT 2026" -> "05-10-2026", "14-Sep-26" -> "14-09-2026", "26.07.2026" -> "26-07-2026").
  * NEVER output month names with a zero like "13-0CT-26" — always output numeric month "13-10-2026".

── 3. KTFL PURCHASE ORDER (PO NUMBER) ───────────────────────────────────────
- po_number: The official 10-digit purchase order issued by KTFL to the vendor.
  SCAN THE ENTIRE TAX INVOICE PAGE (printed PO fields AND handwritten pen/pencil notes in margins/above tables).

  KTFL HAS THREE VALID 10-DIGIT SAP PO FORMATS:
    1. Direct / Steel / Raw Material PO: 10 digits starting with "51000" (e.g. 510000xxxx).
    2. Indirect / Consumables / Tooling / Gauges / Packaging / Electricals PO: 10 digits starting with "61000" (e.g. 61000xxxxx). THESE ARE 100% VALID SAP POs — NEVER REJECT 61000xxxxx!
    3. Project / Capital PO: 10 digits starting with "81000" (e.g. 810000xxxx).

  STEP A — CHECK PRINTED PO FIELDS FIRST:
  Look at: "PO No.", "P.O. No.", "P. O. No. / Date", "Purchase Order No.", "Buyer's Order No.", "Buyer's Order No./PO NO.-", "Customer PO No.", "CUSTOMER PO:", "Party P.O No:", "FO No:".
  * If a 10-digit number starting with "51000", "61000", or "81000" is printed there, extract it!
  * Strip any text prefix like "PO-" or leading hyphen "-" (e.g. "PO-5100002047" -> "5100002047", "-5100002366" -> "5100002366").
  * 11-DIGIT TYPO NORMALIZATION: If a printed PO has 11 digits starting with "610000" (extra zero after 61000, e.g. "61000021137"), remove one zero to normalize to the 10-digit SAP PO -> "6100021137".

  STEP B — CHECK HANDWRITTEN PO IN MARGINS / ABOVE TABLE (WHEN PRINTED PO IS VENDOR'S OWN ORDER NO):
  On steel invoices (such as SLR Metaliks or Kalyani Steels), the printed "Order No." is the vendor's internal code (e.g. "PS26Y-01549", "RP26Y-01044", "Jul. Schedule", "130019559"). Look for the handwritten KTFL PO written in pen/pencil on the right margin, above the item table, or below the invoice number:
    * "51-xxxx" or "51/xxxx" or "po-51/xxxx" -> Expand to 10 digits by inserting "0000": "510000xxxx" (e.g. "51-2496" -> "5100002496", "51/2448" -> "5100002448", "51-2050" -> "5100002050").
    * "Po-510000xxxx" -> "510000xxxx".
    * If a 7-digit handwritten number like "5122996" appears on SLR Metaliks, check if it represents "5100002496" / "5100002996".

  STRICT PO REJECTION RULES:
  * NEVER invent or substitute a "510000..." PO when a valid "61000..." PO is printed on the invoice!
  * NEVER extract vendor internal order numbers ("PS26Y-...", "RP26Y-...", "WST_EXT_DOM/...", "130019559", "13717").
  * NEVER extract Scroll numbers (200... / 202...), E-Way Bill numbers (12 digits), or GRN numbers (5000...).

── 4. GRN NUMBER (RECEIVING STAMP / TOP MARGIN) ────────────────────────────
- grn_number: The 10-digit KTFL Goods Receipt Note number starting with "50" (format: 5000xxxxxx).
  * Check BOTH:
    1. Inside the KTFL receiving rubber stamp ("GRN NO" row).
    2. At the top-left / top-center / top-right margin where stores staff writes the 10-digit "5000xxxxxx" number (or "5-xxxxxx" -> "5000xxxxxx").
  * Must be 10 digits starting with "50". Never copy a GRN from a prompt example.

── 5. VENDOR CODE ──────────────────────────────────────────────────────────
- vendor_code: Vendor code assigned by KTFL if printed on the Tax Invoice (e.g. "50038", "20166338", "31243", "30941", "310026").
  * Do NOT put Heat No (e.g. "68557"), PO Number, or Weighment Slip Customer Code (e.g. "R10029.1") into `vendor_code`. If "Na" or not printed, return "".

── 6. HSN / SAC CODE ───────────────────────────────────────────────────────
- hsn_sac: Must be a SINGLE 4 to 8 digit numeric code representing the primary material item (e.g. "72142090", "72284000", "72283019", "72286099", "76042930", "73269099", "73181500", "39204900", "44129990", "85446090", "84663020", "87089900", "83052000", "90173021", "90173029", "39202010", "84814000").
  * NEVER concatenate multiple HSN codes together (NEVER output "72284000722830996511" or "4819101039239090").
  * NEVER pick service SAC "996511" (Loading Charges) as the header `hsn_sac` on a material invoice — always pick the primary material HSN (e.g. "72284000").
  * NEVER put Heat No or Drawing No into `hsn_sac`.

── 7. LINE ITEMS & DESCRIPTIONS (CRITICAL FOR SAP GR MATCHING) ─────────────
- Extract line items ONLY from the TAX INVOICE page(s). Ignore Weighment Slips, Counting Charts, Lorry Receipts (LR), Delivery Challans ("DELIVERY CHALLAN"), and Debit Notes!
- FULL MULTI-LINE & MULTI-COLUMN ITEM DESCRIPTION:
  * Never truncate item descriptions! Combine all wrapped text lines inside the description cell AND combine adjacent description/code columns so that **KTFL Material Codes** and **exact dimensions** are preserved in `item`:
    - Indian Cables: Include both `Item Code` and all 3 lines of `ITEM NAME` including the `-S0690700xxxx` code at the end (e.g. `"CF1.5X3C CU.COND.PVC INS FLEXIBLE INDUSTRIAL CABLE 1.5X3C- - S06907001269"`).
    - BMSS Steel: Include all 4 lines of `Description` including dimensions and the `T150...` material code at the bottom (e.g. `"1.2714/DB6 73269099 B L&T Make 710 X 240 X 220 SR NO-18917 T150580710240220S"`).
    - Bharat Metals: Combine BOTH `Description of Goods` AND `Description` columns so the exact grade and dimensions are present (e.g. `"1.2714 ALLOY STEEL ROUND & BLOCK(72284000) 1.2714 320X90X60MM -1NOS"`, `"202/5mm H13 Round Bar 72284000 H13 DIA 205X480 2 NOS"`).
    - VS Enterprises / Uni-Tech / Accumeasure / Rudra Pack: Include the 12-digit Material/Item/Part Code (`014001000814`, `012101012147`, `012301002078`, `P05003000153`, `B05101000918`) along with the item name.
- HINDALCO ALUMINIUM INVOICES (SPECIAL TABLE RULE):
  * In Hindalco invoices (`KLALP...`), the table has columns: `PKGS`, `PCS`, `GROSS WT`, `NET WT`, `UOM`, `RATE`, `TAXABLE VALUE`.
  * For `quantity`, ALWAYS extract `NET WT` (e.g. `1755.4`, `2994.3`, `3076.8`, `2142.9`, `4894.6`, `3560.2`, `1059.4`) with `uom: "KG"`. NEVER extract `PCS` (`130`) or `GROSS WT` (`1768`) or Weighment Slip weight (`11750`) as the line quantity!
  * For `amount`, ALWAYS extract `TAXABLE VALUE` / `NET MATERIAL VALUE` (e.g. `854897.35`), NEVER the post-tax Total Invoice Value (`1008778.88`)!
- LINE ITEM ARITHMETIC VERIFICATION:
  * For every line item, verify: `quantity * rate ≈ amount`!
  * If a rubber stamp overlaps a digit in `quantity` or `amount` (e.g. `29.80` vs `28.80`, or `2,433,900.00` vs `2,433,500.00`), use `quantity * rate = amount` to determine the exact true digit (`29.80 * 255 = 7599.00`, `34.770 * 70000 = 2433900.00`, `31.570 * 75199 = 2374032.48`).
- BHARAT METALS LOADING CHARGES:
  * If a Bharat Metals invoice has a separate row `"LOADING CHARGES SALES GST @ 18%"` (`996511`) below the item subtotal (e.g. `406.00` or `30.00`), do NOT include it as a material row in `lines` (since SAP Goods Receipt only books the steel items), OR if included, ensure `subtotal` is the full `Total Taxable Value` (`225490.00` or `7086.00`) from the bottom HSN Tax Summary table so that `subtotal + total_tax ≈ total`.

── 8. AMOUNTS & TAXES (CRITICAL MATHEMATICAL INTEGRITY) ───────────────────
- All currency values must be numeric floats without symbols (₹, Rs.) or commas.
- DO NOT CONFUSE TOTAL WEIGHT (KGS/MT) WITH TAXABLE VALUE (INR):
  * On multi-page Euro Steel invoices (e.g. Inv `880` `5000373619.pdf`), the Total row shows `20` (Pcs), `969.000 KGS` (Weight), and `1,96,215.00` (Taxable Amount). `969.000 KGS` is WEIGHT! The `subtotal` is `196215.00`!
- MULTI-PAGE TAX INVOICES:
  * When a Tax Invoice continues onto Page 2 (e.g. Euro Steel Inv `880`, Inv `899`, BMSS Steel `BMSS1208//26-27`), read Page 2 to extract the remaining line items AND the final `subtotal`, `cgst`, `sgst`, and `total`! Never return `0.0`!
- MISSING PAGE 2 FALLBACK (e.g. Indian Cables `NE021696/PUN` where only Page 1 of 2 is photographed):
  * If `Page 1 of 2` is printed at the bottom and Page 2 is not provided, compute `subtotal` by summing all line item `amount` values on Page 1 (`296925 + 22200 + 183675 + 67500 + 71000 = 641300.00`), and compute the 18% GST (`cgst = 57717.00`, `sgst = 57717.00`, `total_tax = 115434.00`, `total = 756734.00`).
- INTRA-STATE (CGST+SGST) vs INTER-STATE (IGST) RULE:
  * If Seller GSTIN starts with `27` (Maharashtra) and Buyer is KTFL (`27`, Maharashtra): Tax is ALWAYS **CGST + SGST** (`igst = 0.0`). Never put the tax into `igst` for a `27` Seller!
  * If Seller GSTIN starts with `29` (Karnataka) or `32` (Kerala): Tax is ALWAYS **IGST** (`cgst = 0.0, sgst = 0.0`).
- MULTI-BRACKET TAX TABLE SUMMATION (e.g. Rudra Pack `RPS/26-27/07542`):
  * If the tax summary box has multiple rate columns in the CGST and SGST rows (e.g. `CGST 2.5%: 1530.31 | 9%: 18642.42`), you MUST sum all columns in that row (`cgst = 1530.31 + 18642.42 = 20172.73`, `sgst = 20172.73`, `total_tax = 40345.46`).
- gst_rate_percent: Standard GST rate (`18.0`, `12.0`, `5.0`, `28.0`). Must be a standard slab (usually `18.0`). Never output a calculated ratio like `1.5`, `3.62`, `10.0`, or `18.79`!
- MATHEMATICAL SANITY CHECK:
  * Verify `subtotal + cgst + sgst + igst ≈ total` (within +/- 2.00 Rs rounding).
  * Verify `cgst + sgst + igst == total_tax`.

── 9. EUROPEAN DECIMAL QUANTITY NOTATION (STEEL / METALS) ──────────────────
- In steel/metal invoices, quantities printed with 3 decimal places like `"31.390 MT"`, `"322.400 KGS"`, `"35.900 MT"`, `"969.000 KGS"`, `"1.000 nos"` mean `31.39`, `322.4`, `35.9`, `969.0`, `1.0`. Never multiply by 1000.

============================= REQUIRED JSON FORMAT =============================
Return strictly valid JSON without any markdown formatting, preamble, or code fences:
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
