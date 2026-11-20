# ClearMatch – SAP MIRO invoice verification dashboard

React + FastAPI dashboard on top of the existing extractor and the MIRO robot. All three share the same PostgreSQL
database (`miro_invoice`); the app only ever **adds** columns – nothing existing is changed or dropped.

```
PDF ──> Ollama qwen3-vl (header, lines, GRN stamp) ──> SAP SQL check (MSEG / EKKO / EKPO, read-only)
    ──> GST tax code (GST_RATE.xlsx) ──> queue ──> PostgreSQL ──> dashboard ──> MIRO robot ──> SAP
```

## Queue rules
*Multi-PO is information only, not a block:* when SAP books the invoice against several POs/GRNs, the invoice's PO, GRN and every line are
checked against all those goods-receipt rows. All correct → **Matched** (blue “Multi-PO” tag; `invoice_lines.gr_row` = `MBLNR/item` per line
so the robot selects the right GR lines). A wrong line → Reconcile.

| Queue | Meaning | Examples |
|---|---|---|
| **Matched** | everything present and equal to SAP – robot picks it (`status='MATCHED'`) | all lines qty/amount = GR, tax code, HSN agree |
| **Reconcile** | data present but differs from SAP → *Error Flow* screen | line qty / amount ≠ GRN, GRN value, tax code, **HSN missing in invoice / SAP or different**, arithmetic |
| **Blocked** | too little was read, or SAP cannot be matched | **less than 70 % of the 10 mandatory fields read**, PO or GRN not in SAP |

* **HSN** is stored **only from the invoice**; SAP's HSN (`EKPO.STAWN`) is kept in `sap_hsn` for comparison, never copied.
* Same invoice number + PO can never be stored twice (`dedupe_key` + unique index).
* **Auto Flow SAP Logs** = what the robot wrote back (`miro_status`, `miro_doc_number`, `miro_message`).
* **Push to SAP MIRO** only releases a *Matched* invoice to the robot (clears `miro_status`; robot runs on its schedule).
  An approved variance ("Accept variance") waives Reconcile issues and moves the invoice to Matched.
  Known limit: the robot has its own amount check (selected GR value vs invoice taxable ±1) – an accepted variance can
  still end as `miro_status=REVIEW` in the robot; that shows in Auto Flow.

**Multi-PO invoices are NOT blocked.** If SAP books one vendor invoice against several POs / GRNs, the invoice's PO, GRN and
every line are checked against *all* its goods-receipt rows. All agree → **Matched** (tag "Multi-PO" is information only).
Any line wrong → Reconcile. The robot needs `miro_robot_multi_grn_patch.zip` (copy over the robot folder) to tick the rows of
every GRN; entering a *second PO* inside the MIRO screen is not automated – such an invoice ends as `REVIEW` in Auto Flow.

**Intake is folder-only.** Users drop PDFs into `WATCH_FOLDER`; the agent waits until the file is fully copied, extracts it,
moves it to `<folder>\processed`, and the invoice shows on the dashboard (Matched ones too). Browser upload is off
(`ALLOW_UPLOAD=false`). **Completeness**: 10 mandatory fields (invoice no, date, vendor, PO, GRN, taxable value, total, GST tax code,
plant, vendor code). All read → normal rules. 70–90 % read (1–3 missing) → **Reconcile** ("Data missing"), a person completes
the fields on the screen and Save re-checks against SAP. Below 70 % → Blocked. Missing data can never be waived by "Accept variance".

## Run it (production-style, one process)
```bash
# 1. backend
cd backend
python -m venv venv && venv\Scripts\activate        # Windows  (Linux: source venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env                                # fill DATABASE_URL, SAP_*, JWT_SECRET, ADMIN_PASSWORD
copy ..\GST_RATE.xlsx data\GST_RATE.xlsx
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 2. frontend (build once; the backend serves frontend/dist on :8000)
cd ../frontend && npm install && npm run build
```
Open `http://<server>:8000`, sign in with `ADMIN_USERNAME` / `ADMIN_PASSWORD`. For development: `npm run dev` (port 5173, proxies `/api`).
`SAP_DRIVER` must match the driver installed on that machine (`SQL Server` = built-in, no reboot needed).
Ollama must be reachable at `OLLAMA_URL` with `qwen3-vl:32b` – the top bar shows "AI model offline" otherwise.
Invoices already extracted by the old script appear automatically; press **Re-check** (or edit a field) to run them through the new SAP rules.

## Tests / UAT
```bash
cd backend && pip install -r requirements-dev.txt && pytest -q          # 35 tests (+1 PostgreSQL, see below)
PG_TEST_URL=postgresql+psycopg://user:pw@host/scratch_db pytest tests/test_postgres.py   # legacy-schema upgrade on real PG
cd frontend && npm test && npm run build
python -m scripts.demo_server      # real API + real PDF, simulated SAP + scripted model → UAT without SAP/Ollama (port 8000, admin / admin-pass-1)
```
UAT checklist (invoice DSK Engineers 428/26-27, PO 8100007531, GRN 5000343863, 11 lines):
1. Copy the PDF into the watch folder → within ~25 s job DONE → **Matched**, tax code **I3**, HSN OK, 11 lines OK.
2. Upload again → **Duplicate**, nothing new stored.
3. Change a GR qty in SAP / simulated → **Reconcile**, line shows in *Error Flow* with invoice vs GR qty.
4. Blank PO → **Blocked**; type the PO on the screen → Save → re-checked against SAP → Matched.
5. Remove HSN from invoice / SAP → **Reconcile** with HSN reason.
6. Matched → *Push to SAP MIRO* → robot runs → *Auto Flow* shows Posted / Parked / Failed + message; Failed can be re-queued.
7. Viewer role cannot edit/push (403); every action appears in *Audit Trail* (CSV export).
