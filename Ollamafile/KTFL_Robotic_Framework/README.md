# KTFL SAP MIRO Robotic Framework

This project uses Python + Robot Framework + SAP GUI Scripting + PostgreSQL; it does not use IBM RPA.

## Workflow
1. Start/attach to SAP GUI and open the configured SAP Logon connection.
2. Log in using credentials from `.env` if the login screen is present.
3. Read PostgreSQL invoices where `status='REVIEW'`.
4. Open `/nMIRO`, fill configured header fields, enter PO number.
5. If a verified ALV grid ID and column keys are configured, scan its rows for the PO (and GRN if supplied) and select the matching row.
6. Stop before posting. The user must verify and post manually.

## Setup
```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```
Fill `.env` with approved credentials and the exact SAP Logon connection description. Do not commit `.env`.

## SAP GUI Scripting
Scripting must be enabled in SAP GUI options and, where applicable, by SAP Basis on the server. If a system requires SSO/MFA or has a custom login screen, complete login manually and let the robot attach to the active session.

## Discover SAP IDs
Open SAP and MIRO manually, then run:
```powershell
python tools\inspect_sap.py
```
Inspect `reports/sap_control_tree.txt` and verify all IDs with your SAP process owner. SAP field IDs, grid IDs and grid column keys vary by SAP release and screen layout. Do not guess them.

## Run
```powershell
python run.py
```

## Database assumptions
`invoices` must contain `id`, `invoice_number`, `invoice_date`, `po_number`, `currency`, and `status`. If your schema contains `grn_number`, the library reads it dynamically; otherwise it treats GRN as unavailable and logs that exact matching needs configuration/data.

## Important limitations
- This is a configurable starter, not a verified production-ready bot.
- PO/GRN row selection only works when the actual MIRO table/grid control ID and column keys have been verified and configured.
- The code does not click Post and does not mark an invoice as posted.
- Validate on a test SAP client before production.
- Rotate any credentials previously pasted into source/chat if they were real.
