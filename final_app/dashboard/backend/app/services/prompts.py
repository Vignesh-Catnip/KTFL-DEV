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
STEP 2 — Find the row labelled "GRN NO", "GRN No.", "GRN", "JRN NO", "Inward No", "MRN No".
         It is normally the row BELOW "SCROLL NO".
STEP 3 — Extract the handwritten number EXACTLY as written (keep dash / dots / spaces).
STEP 4 — A GRN always starts with "50" and has EXACTLY 10 digits. People often write it short:
         "50" then a dash then the last 6 digits - the two missing zeros are added later, so copy
         the handwriting exactly as written. Count the digits carefully.
         If you cannot find a GRN written on the page, return an empty string. NEVER make a number up.

CRITICAL — SCROLL NO is NOT a GRN. SCROLL NO starts with "200" and has ~12-13 digits.
If the stamp shows "SCROLL NO: 2001234567890" and "GRN NO: 5012345678", extract ONLY the GRN row.
If only SCROLL NO is visible and GRN row is blank or absent, return "".

NEVER extract from: SCROLL NO (starts with 200...), CH NO, Challan No, E-way Bill No,
Vehicle No, Gate Pass No, Delivery Note No, Buyer's Order No.
A valid GRN must start with "50" — reject any extracted value that does not start with 50.

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
"""

# ---------------------------------------------------------------------------
# Main invoice extraction prompt
# ---------------------------------------------------------------------------
PROMPT = r"""
You are an Indian GST purchase-invoice extraction engine. The BUYER is Kalyani Technoforge Ltd (KTFL).
Read the invoice IMAGES. Return ONLY valid JSON.

FIELD RULES
- vendor         : the SELLER company printed at the top. NEVER "Kalyani Technoforge".
- invoice_number : value under "Invoice No.", "Invoice Number", "Tax Invoice No.", "Bill No.", "Inv. No."
                   Copy the alphanumeric code EXACTLY character by character — do not add, drop, or swap
                   any letter or digit. Common confusion pairs: I vs L vs 1, O vs 0, A vs 4, P vs R.
                   Example: "KLALP2610003915" — verify each character: K-L-A-L-P (not KLAIP, not KLALB).
                   If the invoice number spans two lines, join them without space.
- invoice_date   : the "Dated", "Date", "Invoice Date", "Bill Date" value next to the invoice number.
- po_number      : ONLY take this from a field that is actually LABELLED as the purchase order. Accepted
                   labels (look for ALL of these on every invoice):
                     "PO No.", "P.O. No.", "Purchase Order", "Buyer's Order No.", "Order No.", "Order Ref.",
                     "Customer PO", "Customer PO No.", "Customer Ref No.", "Customer Ref.", "Cust. PO",
                     "Our PO", "Our PO No.", "Our Order No.",
                     "FO No.", "F.O. No.", "FO Number", "Firm Order No.", "Firm Order",
                     "Reference No.", "Ref. No.", "Your Order No.", "Your Ref."
                   Never pick a bare 10-digit (or short-form) number that has no such label next to it -
                   it could be an invoice no., GSTIN digits, a vehicle/gate/LR/scroll number etc.
                   KTFL POs are 10 digits starting 81 or 51 (e.g. 8100007531, 5100002503). Strip any "PO-" prefix.
                   REJECT the labelled value if it does NOT start with 51 or 81 and is not a short-form
                   (e.g. "51/2496", "81-7531"). Vendor's own internal order numbers often appear under
                   "Order No." but start with letters or non-51/81 patterns (e.g. "PS26Y-01548",
                   "SE26Y-14765", "KPO-123") — these are NOT KTFL POs; ignore them and look for a
                   handwritten KTFL PO instead.
                   NEVER use the CIN number (Company Identification Number) as a PO. CIN always starts
                   with U or L followed by 5 digits and letters (e.g. "U27106DL2005PLC142596") — ignore it.
                   If MORE THAN ONE different PO is printed anywhere on the invoice, list every one of
                   them comma-separated in "po_numbers_all".
                   If the LABELLED Order No. is missing or is not a valid KTFL PO, also look for a
                   HANDWRITTEN PO note written in pen/pencil near the TRANSPORTER / "To Place" box or in
                   the RIGHT MARGIN / BOTTOM LEFT of the invoice - people scribble it there in SHORT FORM
                   such as "po- 51/2496", "51-2496", "PO 81.7531". Read it exactly as written (keep the
                   "/" "-" ".") into "po_number" - it will be expanded automatically (51/2496 means PO
                   5100002496, 81-7531 means 8100007531).
                   If there is no such label anywhere, leave po_number empty - do not guess
                   from an unlabelled number; it will be looked up from SAP using the GRN / invoice no. /
                   line items instead.
- vendor_code    : "Vendor Code" printed on the invoice, if present.
- gstin          : the SELLER's GSTIN — exactly 15 alphanumeric characters. Never KTFL's own GSTIN
                   (starts 27AABCK0618A). If the extracted value is not exactly 15 chars, re-read it.
- hsn_sac        : the HSN/SAC code of the goods — must be 4–8 digits ONLY (e.g. "7228", "72283029").
                   Copy EXACTLY as printed. If OCR gives a partial fragment (e.g. only "2228" when
                   the full code is "72283029"), look for more digits on the same line and merge them.
                   If the full code cannot be read clearly, return "" — never guess or pad an HSN code.
- plant / ship-to: if a KTFL plant code or unit is printed (e.g. "Plant: 1200"), put it in "plant_hint".
- Amounts        : ₹6,19,763.14 -> 619763.14  (remove ₹ and thousands separators, keep the decimal).
                   If the invoice prints "1,000.00" extract it as 1000.00 (preserve .00, do not round to 1000).
                   Line item amounts must be extracted exactly as printed — if the invoice shows "5,250.00" return 5250.00, not 5250.
- subtotal       : total taxable value BEFORE GST. This may be labelled "Taxable Value", "Taxable Amount",
                   "Assessable Value", "Basic Amount", "Total Taxable Value", or be the SUM of all line
                   "Taxable Value" / "Material Value" column cells. Sum them if no grand total row is printed.
                   SANITY CHECK: subtotal must be less than total. If subtotal > total, re-read; you likely
                   read the wrong column or picked up an amount including GST.
                   NEVER return 0 — if amounts are present anywhere on the invoice, extract them.
- cgst/sgst/igst : the GST AMOUNTS in rupees (not percentages). These may appear as:
                   - A summary table at the bottom: "CGST @ 9% = 55,789.00" → cgst = 55789.00
                   - A separate column per line item: add up all CGST column values across all rows
                   - Combined as "GST" or "Tax" with no CGST/SGST split: put the full amount in igst
                     if the vendor's state ≠ Maharashtra (inter-state = IGST), else split equally into cgst+sgst
- gst_rate_percent: the GST RATE as a PERCENTAGE NUMBER — valid values are 5, 12, 18, or 28 only.
                   NEVER copy the GST rupee AMOUNT into this field. If the invoice shows
                   "IGST @ 18% = ₹55,789" → gst_rate_percent = 18, igst = 55789.
                   If you are unsure whether a number is a rate or an amount, check: rates are always
                   single or double digit (5, 12, 18, 28); amounts are hundreds or thousands.
                   If no rate is printed, return 0.
- total          : the final payable invoice total. Look for labels: "Total", "Grand Total", "Invoice Total",
                   "Net Payable", "Amount Payable", "Total Amount", "Bill Amount". If a "Round Off" row
                   exists, use the value AFTER rounding. NEVER return 0 if amounts are visible on the invoice.
- grn_number     : always "" (it is extracted separately).
- Ignore Delivery Challan / E-way bill / weighment slip / lorry receipt / LR copy pages when collecting
                   line items and amounts — extract from the TAX INVOICE pages only.
                   WEIGHMENT SLIP: a page showing only "GROSS WEIGHT / TARE WEIGHT / NET WEIGHT" is a
                   weighment slip — it has no line items, amounts, or GRN; ignore it entirely.
                   Exception: if the Tax Invoice page does not print HSN codes but the Delivery Challan does,
                   copy the HSN code from the Challan into hsn_sac.

LINE ITEMS: only the invoice's own goods rows that carry an amount. No duplicate rows.

QUANTITY DECIMAL NOTATION — CRITICAL FOR STEEL/MATERIAL INVOICES:
Some Indian steel and metal invoices use EUROPEAN decimal notation where the DOT (.) is a
THOUSANDS SEPARATOR, not a decimal point. Examples from real invoices:
  "67.000 KG"  means  67 kg  (NOT 67,000 kg)
  "122.000 MT" means  122 MT (NOT 122,000 MT)
  "30.000 NOS" means  30 nos (NOT 30,000 nos)
HOW TO DETECT: If UOM is KG / TO / MT / NOS / PCS and quantity ends in exactly ".000" (three zeros
after the dot) AND the amount printed ÷ rate ≈ a whole number (not a 1000x multiple), then read
the quantity as the integer part only. Verify: qty × rate must be close to the line "Amount" printed.
NEVER multiply by 1000. If qty × rate matches the printed amount, your reading is correct.

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
