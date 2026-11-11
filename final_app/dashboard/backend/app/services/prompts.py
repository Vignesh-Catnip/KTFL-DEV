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

SEARCH LOCATIONS (SCAN IN THIS MANDATORY SEQUENCE):

LOCATION 1 — INWARD / RECEIVING RUBBER STAMP:
  - Search across the entire document (typically bottom margins, blank areas, lower-right, or stamped over printed line items).
  - Look for rubber stamp impressions with: "KALYANI TECHNOFORGE LTD", "KTFL", "KTF LTD", "KALYANI TRANSMISSION", "RECEIVED SUBJECT TO VERIFICATION", or plant names "RANJANGAON", "KHED", "MUNDHWA".
  - Inside the stamp, locate the row labeled: "GRN NO", "GRN No.", "GRN", "G.R.N. No.", "Inward No", "MRN No", "M.R.N. No.", "JRN NO", "Goods Receipt No".
  - NOTE: This row is usually directly below or beside "SCROLL NO" / "DATE" / "TIME".

LOCATION 2 — TOP MARGIN & HEADER PEN ANNOTATIONS (CRITICAL):
  - On many delivery invoices and photos, stores/security officers handwrite the 10-digit GRN number with date at the TOP LEFT, TOP RIGHT, or TOP CENTER margin in pen:
    * Examples: "5000346213", "5000345215", "5000346220", "5000346227", "5000346407", "5000357265", "5000352298", "5000352298/17-8-26".
  - Check for short-prefix shorthand:
    * "5-341287" -> expand to "5000341287"
    * "5-340791" -> expand to "5000340791"
    * "5-340793" -> expand to "5000340793"
    * "5einv40790" / "5000340790" -> "5000340790"
  - If the bottom stamp's GRN row is blank, smudged, or unreadable, check the TOP MARGIN.

LOCATION 3 — STAMP-ADJACENT OR TABLE-ADJACENT HANDWRITING:
  - Check pen writing near the inspector/stores signature or alongside the item table.

CHARACTER DISAMBIGUATION RULES:
  - A valid KTFL GRN must be EXACTLY 10 digits and start with "50" (format: 50xxxxxxxx).
  - Read every handwritten digit stroke by stroke:
    * 0 vs 8: Check for closed crossing loops in the center (8) vs single open/closed oval (0).
    * 4 vs 1: Check for horizontal bar and vertical crossing leg (4) vs single upright line (1).
    * 5 vs 6: Look for flat horizontal top roof and sharp corner (5) vs continuous curved loop (6).
    * 3 vs 8: Check left side. Open loops on the left is 3; dual enclosed loops is 8.
    * 7 vs 1: Check top horizontal bar. Distinct top horizontal bar is 7; straight vertical is 1.
    * 9 vs 4: Check if top is curved/circular (9) vs sharp triangular corner with intersecting stems (4).

STRICT REJECTION RULES (NEVER EXTRACT AS GRN):
  * SCROLL NO: starts with "200" or "202" and has 10 to 13 digits (e.g. 2002056677, 2021022856, 2002052304, 2002051288) -> REJECT.
  * Lorry Receipt (LR) No, Challan No, E-Way bill number, Vehicle No, Gate Pass No, Delivery Note No.
  * Vendor printed invoice number.

If no valid 10-digit KTFL GRN starting with "50" is visible anywhere, return empty string "".
NEVER invent, guess, or pad digits.

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
A valid KTFL GRN has EXACTLY 10 digits and starts with "50".
Re-read the handwriting stroke by stroke. Look at BOTH:
1. Inside the KTFL receiving rubber stamp ("GRN NO" line).
2. The TOP MARGIN / HEADER where stores/security staff writes the 10-digit number in pen (e.g. 500034xxxx, 500035xxxx).
Pay special attention to ambiguous digits: 0 vs 8, 4 vs 1, 5 vs 6, 3 vs 8, 7 vs 1, 9 vs 4.
Do NOT mistake SCROLL NO (starts with 200... or 202...) for GRN.
"""

# ---------------------------------------------------------------------------
# Main invoice extraction prompt
# ---------------------------------------------------------------------------
PROMPT = r"""
You are an expert Indian GST purchase-invoice extraction engine for manufacturing supply chains.
The BUYER is Kalyani Technoforge Ltd (KTFL).
Read all provided invoice pages/images carefully. Return ONLY a valid JSON object matching the exact schema.

================================ FIELD RULES ================================

── 1. VENDOR & GSTIN ────────────────────────────────────────────────────────
- vendor: Full registered legal name of the SELLER printed at top letterhead.
  * NEVER "Kalyani Technoforge", "KTFL", or "Kalyani Transmission" — that is the BUYER / CONSIGNEE.
  * Copy full legal entity suffix: "LIMITED", "PVT. LTD.", "PRIVATE LIMITED", "LLP", "& CO."
  * Drop any financial year brackets from the vendor name:
    e.g., "EURO STEEL AND TRACER PRODUCTS (2026-27)" -> "EURO STEEL AND TRACER PRODUCTS"
    e.g., "BHARAT METALS 26-27" -> "BHARAT METALS"
    e.g., "SLR METALIKS LIMITED" -> "SLR METALIKS LIMITED"
    e.g., "AURANGABAD AUTO ANCILLARY PVT. LTD." -> "AURANGABAD AUTO ANCILLARY PVT. LTD."

- gstin: Exactly 15-character alphanumeric GSTIN of the SELLER / SUPPLIER.
  * Look specifically in the "Details of Supplier", "Seller", or Top Letterhead block (NEVER the Buyer block!).
  * State code prefixes match seller state:
    - Karnataka: 29 (e.g. "29AAJCS4396P1Z9", "29AAACK7315D1ZQ")
    - Kerala: 32 (e.g. "32AAACH1201R1ZW")
    - Maharashtra: 27 (e.g. "27AATFB3972H1ZZ", "27HBFPS2958A1ZG", "27AAACG1519K1ZP", "27AAYFV1317R1Z6", "27AADCK8810E1ZN", "27ASVPB4182F2ZI", "27ABCCS2447R1ZQ", "27AAXPG9799D1ZS", "27AAHCR8563K1ZP", "27AAAFCE6816Q1Z0", "27AABCI4883P1ZV", "27AADFO8995R1Z0", "27AADFU4266H1ZZ", "27AACCA3515D1Z8")
  * NEVER copy KTFL's GSTIN (which starts with "27AABCK0618A...").
  * NEVER hallucinate zeroes (e.g. "27AAHCB00000000000000" is INVALID). If blurry, verify character by character from the PAN / Supplier box.

── 2. INVOICE NUMBER & DATE ─────────────────────────────────────────────────
- invoice_number: Exact printed alphanumeric string under "Invoice No.", "Tax Invoice No.", "Bill No.", "Inv. No.", "Invoice No:".
  * Copy character by character including all slashes, dashes, and zeroes (e.g. "BM 26-27/0640", "BM/26-27/1453", "BM/26-27/1603", "BM/26-27/1604", "SE26Y-14765", "SE26Y-14834", "SE26Y-14835", "SE26Y-13425", "SE26Y-13426", "SE26Y-14368", "KLALP2610003915", "KLALP2610003722", "KLALP2610003781", "KLALP2610003782", "KT-82/2026-27", "KT-83/2026-27", "KT-70/2026-27", "OME/26-27/450", "1702/2026-2027", "4679/26-27", "4678/26-27", "2627017961", "879", "880", "899", "979", "9043043595").
  * Do NOT confuse 0 with O, 1 with I/L, A with 4, P with R.
  * NEVER extract PO number, GRN, Challan No, or E-way bill as the invoice number.
- invoice_date: The date printed next to the invoice number ("Date", "Dated", "Invoice Date").
  * Format: DD-MM-YYYY or DD/MM/YYYY. Convert letter months: e.g. "13-OCT-26" -> "13-10-2026", "05 OCT 2026" -> "05-10-2026".

── 3. KTFL PURCHASE ORDER (PO NUMBER) ───────────────────────────────────────
- po_number: The official purchase order issued by KTFL to the vendor.
  SCAN THE ENTIRE PAGE (printed header fields, order reference boxes, tables, AND all pen/pencil margin notes).

  KTFL HAS THREE VALID 10-DIGIT PO FORMATS IN SAP:
    1. Direct Production / Steel PO: 10 digits starting with "51" (e.g. 5100002503, 5100002363, 5100002512, 5100002047, 5100002145, 5100002466, 5100002390, 5100002426, 5100002428, 5100002467, 5100002366, 5100002429, 5100001992, 5100002049, 5100002202, 5100002448, 5100002496, 5100002050).
    2. Indirect / Consumables / Tooling / Packaging PO: 10 digits starting with "61" (e.g. 6100015411, 6100019846, 6100020918, 6100020592, 6100023920, 6100020050, 6100014717, 6100021995, 6100021626, 6100022556). THESE ARE 100% VALID SAP POs FOR NON-STEEL/SERVICE VENDORS.
    3. Project / Capital PO: 10 digits starting with "81" (e.g. 8100007531).

  PRIORITY 1 — PRINTED PO LABELS:
  Search: "PO No.", "P.O. No.", "Purchase Order No.", "Buyer's Order No.", "Buyer's Order No./PO NO.-", "Customer PO No.", "Customer PO", "Order Ref.", "Our PO", "FO No.", "Party P.O No:".
  * If the printed field contains a valid 10-digit number starting with 51, 61, or 81, extract it.
  * Strip prefixes: e.g. "PO-5100002047" -> "5100002047", "PO-5100002145" -> "5100002145", "PO-5100002351" -> "5100002351".

  PRIORITY 2 — HANDWRITTEN KTFL PO IN MARGINS / OVERPRINT:
  If no valid printed PO is present, or if vendor writes KTFL's SAP PO in pen/pencil across margins/headers/above tables:
    * "51-2496", "51/2496", "PO-51/2496" -> Expand to 10-digit: "5100002496"
    * "51/2448", "Po-5100002448" -> "5100002448"
    * "51-2050" -> "5100002050"
    * "5122996" -> "5100002996"
    * "Po-5100002202", "51/2150" -> "5100002202"
    * "81-7531" -> "8100007531"

  STRICT REJECTION RULES (DO NOT extract as KTFL PO):
  * Vendor internal sales order IDs with letters: "PS26Y-01549", "SE26Y-14765", "RP26Y-01044", "WST_EXT_DOM/...", "130019559" -> Vendor order IDs, NOT KTFL PO.
  * Scroll numbers (starting with 200 or 202).
  * E-Way Bill numbers (12 digits, e.g. 182586455637, 242255297677, 252277817641).
  * Vehicle numbers (e.g. DD01N9450, MH14EM0792, MH12WX2564).
  * CIN (21 chars starting with U or L).

  If multiple valid KTFL POs exist, put the primary in "po_number" and all comma-separated in "po_numbers_all".
  If no valid PO is found anywhere, return "".

── 4. GRN NUMBER (RECEIVING STAMP / ANNOTATION) ────────────────────────────
- grn_number: The 10-digit receiving Goods Receipt Note number starting with "50".
  * CHECK BOTH:
    1. Inside the KTFL receiving rubber stamp ("GRN NO" row): e.g. 5000412955, 5000377914, 5000392138, 5000407239, 5000407251, 5000410006, 5000410881, 5000410882, 5000298627, 5000393525, 5000395483, 5000344355, 5000365809, 5000370742, 5000373614, 5000373619, 5000376005, 5000377736, 5000377743, 5000387769.
    2. At top margins where stores/security staff writes the GRN in pen: e.g. "5000346213", "5000345215", "5000346220", "5000346227", "5000346407", "5000357265", "5000360088", "5000360144", "5000340789", "5000340790", "5000340791", "5000340793", "5000341287", "5000352298".
  * Must be exactly 10 digits starting with "50".
  * DO NOT confuse with SCROLL NO (which starts with 200... / 202...).

── 5. VENDOR CODE ──────────────────────────────────────────────────────────
- vendor_code: Vendor code assigned by KTFL if printed (e.g. "50038", "20166338", "31243", "30941", "310026").

── 6. HSN / SAC CODE ───────────────────────────────────────────────────────
- hsn_sac: Must be a single 4 to 8 digit code (e.g. "72142090", "72284000", "76042930", "73269099", "73181500", "39204900", "44129990", "85446090", "84663020", "87089900", "83052000").
  * Pick the primary HSN of the main/highest value line item.
  * NEVER concatenate multiple line item HSNs into a single long string (e.g. NEVER "4819101039239090", NEVER "72284000722830996511").

── 7. AMOUNTS & TAXES (CRITICAL MATHEMATICAL INTEGRITY) ───────────────────
- All currency values must be numeric floats without symbols (₹, Rs.) or commas.
- CRITICAL DISTINCTION — DO NOT CONFUSE WEIGHT (KGS/MT) WITH TAXABLE VALUE (INR):
  * Example: "Total: 969.000 KGS" is the physical weight! Look at the currency amount column: "1,96,215.00" is the TAXABLE VALUE! Never put weight into subtotal!
- CRITICAL — INCLUDE ALL TAXABLE SURCHARGES (LOADING / FREIGHT / PACKING):
  * If the invoice includes "LOADING CHARGES", "FREIGHT", or "PACKING & FORWARDING" on which GST is charged (e.g., Bharat Metals Loading charges ₹30.00 or ₹406.00):
    Include it as a line item AND include it in the subtotal!
    Example: Material (7056.00) + Loading (30.00) = Subtotal (7086.00)!
- subtotal: Base taxable value before GST (Total Taxable Value / Assessable Value).
  * If an invoice spans multiple pages (e.g. BMSS Steel 5000377914, Euro Steel 5000376005, 5000373619), EXTRACT AND SUM ALL LINE ITEMS ACROSS ALL PAGES! Never return 0.0 if line items exist.
- cgst: Central GST amount in Rupees (sum across all rate brackets, e.g. 2.5% + 9% -> total CGST).
- sgst: State GST amount in Rupees (sum across all rate brackets, e.g. 2.5% + 9% -> total SGST).
- igst: Integrated GST amount in Rupees (Full tax for interstate transactions, e.g. Karnataka to Maharashtra or Kerala to Maharashtra).
- gst_rate_percent: Applicable GST rate percentage (e.g. 5.0, 12.0, 18.0, 28.0). If multiple rates, put predominant rate (e.g. 18.0).
- total_tax: Total tax in rupees (cgst + sgst + igst).
- total: Final net payable invoice total (Grand Total after round off).
- MATHEMATICAL SANITY CHECK:
  subtotal + total_tax must equal total (+/- round-off of up to 2.0 Rs).
  If subtotal + total_tax != total, re-verify subtotal, taxes, and total immediately!

── 8. EUROPEAN DECIMAL QUANTITY NOTATION (STEEL / METALS) ──────────────────
In steel/metal invoices:
  "31.390 MT" means 31.39 MT (not 31,390).
  "322.400 KGS" means 322.4 KGS.
  "35.900 MT" means 35.9 MT.
  "969.000 KGS" means 969 KGS.
Never multiply by 1000 when ".000" or ".xxx" represents metric decimal tonnes or kilograms.

── 9. PLANT HINT ───────────────────────────────────────────────────────────
- plant_hint: Specify KTFL plant location if indicated: "RANJANGAON", "KHED", "MUNDHWA", or "CHAKAN".

── 10. MULTI-PAGE & ATTACHED DOCUMENTS ─────────────────────────────────────
- If an invoice package contains Tax Invoice + Weighment Slip + Counting Chart + Lorry Receipt (LR) + Debit Note:
  * Extract financial amounts and official invoice metadata ONLY from the TAX INVOICE pages.
  * Do NOT extract amounts or quantities from Debit Notes, Tare Weight slips, or Transporter LR copies.

── 11. LINE ITEMS COMPLETENESS ─────────────────────────────────────────────
- lines: Extract EVERY SINGLE line item printed on the tax invoice (across all pages if multi-page).
  * If an invoice has 9, 12, or 19 line items, extract every single line item accurately.
  * Sum of all line item amounts MUST EQUAL the invoice subtotal exactly.

── 12. HANDWRITTEN REFERENCES ─────────────────────────────────────────────
- handwritten_references: Comma-separated list of all handwritten notations found on the page with their visual locations (e.g. "top-left: 5000346213 08/08/2026, stamp: 2021022856 8/8/26, margin: 51-2496").

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
  "gst_rate_percent": 0.0,
  "total_tax": 0.0,
  "total": 0.0,
  "grn_number": "",
  "handwritten_references": "",
  "confidence": 0.0,
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
