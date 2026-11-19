# Full flow and how to run it

```
 PDF -> WATCH_FOLDER --> agent (Ollama qwen3-vl) reads it --> SQL Server check (MSEG / EKKO / EKPO)
                                                                   |
        +--------------------- MATCHED ----------------------------+---- RECONCILE / BLOCKED
        |                                                                      |
 robot starts by itself (API)                                  Dashboard: person edits + Save -> Push
        |                                                                      |
 MIRO posting in SAP GUI <------------------ robot starts (API) <--------------+
        |
 result (POSTED / PARKED / REVIEW / FAILED + SAP doc no) is written to PostgreSQL -> Auto Flow SAP Logs
```

* **Matched** – everything agrees with SQL. Goes to the robot automatically (ROBOT_AUTO_RUN=true).
* **SQL is a checker.** Taken from SQL: GRN, invoice number, PO (only when not read) and the plant code (GRN plant, else PO plant). Everything else (vendor, GSTIN, HSN, plant, currency, taxable value, lines) comes from the invoice only; SQL values are shown next to it for comparison.
* **Reconcile** – data present but differs / part of the fields not read (70 % - < 100 %). Person opens it, corrects, Save, then **Push**. Push starts the robot.
* **Blocked** – less than 70 % read or PO / GRN not in SQL. Fix PO / GRN number and re-check.
* **Error Flow** – every invoice: what the invoice says, what SQL says, the difference, what to do, and the line items that do not agree.
* Duplicate (same invoice no + PO) is never processed twice. Multi-PO invoices are not blocked; the robot picks lines by the invoice PO.

## PDF folders
```
1 input   (WATCH_FOLDER)  -> drop PDFs here
2 processing              -> agent is reading it / checking SQL
3 output\matched | reconcile | blocked   -> extracted; follows the dashboard queue (moves when you fix a Reconcile)
4 finish\posted | parked | duplicate    -> robot posted / parked it; duplicates go here
  error                   -> PDF could not be read
```
Folders 2-4 and `error` are created next to the input folder automatically (or set PROCESSING_FOLDER, OUTPUT_FOLDER, FINISH_FOLDER, ERROR_FOLDER in .env). Files are moved, never deleted; the dashboard keeps its own copy for the viewer. The Auto Extraction page shows the folders.

## MIRO document type (Details tab)
The dashboard has a master `doc_type_master` (client, doc type, number range, description, plant). It is filled at start-up
with your table; admins can add / delete rows via `/api/doc-types`. Every invoice gets `doc_type` + `doc_type_desc` from
its plant code (plant 2002 -> X1 "Invoice - R'gaon R1"). The robot selects that **description** in the SAP Doc. Type drop-down.
Plant 2022 has two types (X4 AMBT1 / XD INMT1) -> the invoice goes to Reconcile and a person picks one in the invoice screen.
A plant that is not in the master also goes to Reconcile.

## NCM / HSN column
HSN is read **only from the invoice** and only **compared** with SQL (never copied). The SQL column is auto-detected in MSEG / EKPO / MARC.
To see which one was found: log in as approver and open `/api/sap/hsn-columns`. To list candidates yourself run in SSMS on BRONZE_DB:

```sql
SELECT TABLE_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_NAME IN ('MSEG','EKPO','MARC')
  AND (COLUMN_NAME LIKE '%HSN%' OR COLUMN_NAME LIKE '%NBM%' OR COLUMN_NAME LIKE '%NCM%'
       OR COLUMN_NAME LIKE '%STEUC%' OR COLUMN_NAME LIKE '%STAWN%' OR COLUMN_NAME LIKE '%SAC%');
```
If auto-detect picks the wrong one, set `SAP_HSN_COLUMNS=MSEG.<column>` in `backend/.env`.

## One-time setup (Windows, the PC that has SAP GUI)
1. `backend\.env` from `.env.example`: DATABASE_URL, SAP_* (SQL Server read-only user), OLLAMA_*, WATCH_FOLDER, `ROBOT_ENABLED=true`, `ROBOT_DIR=<robot folder>`, `ROBOT_PYTHON=<robot venv>\Scripts\python.exe`.
2. The robot keeps its **own** `.env` (PG + SAP GUI login, `MIRO_MODE`). Start with `MIRO_MODE=CHECK` (dry run – nothing is posted; results show as "dry run"), then `PARK`, then `POST`.
3. Backend: `cd backend && venv\Scripts\activate && pip install -r requirements.txt && uvicorn app.main:app --host 0.0.0.0 --port 7000`
4. Frontend (once): `cd frontend && npm install && npx vite build` – the backend serves `frontend/dist`.
5. Open `http://<server>:7000`, login, then drop PDFs into WATCH_FOLDER.

SAP GUI scripting needs a **logged-in interactive Windows session**. Run uvicorn in that session (not as a Windows service); keep the screen unlocked while the robot works.

## Daily use
1. Drop invoice PDFs in the watch folder (Auto Extraction page shows each file).
2. Invoices tab: Matched / Reconcile / Blocked tabs. Matched ones go to the robot by themselves after ~60 s.
3. Reconcile: open the invoice (3-way view: invoice, GRN, PO), correct values, Save, **Push to SAP MIRO** – the robot starts immediately.
4. Auto Flow SAP Logs: robot panel (mode, waiting, Run robot now, run history, log), and per-invoice SAP result.
5. Only one robot runs at a time; a Push while it is running is queued and runs right after.

## Tests
`cd backend && pytest` (SQLite SAP simulator) – and the real robot test:
`PG_TEST_URL=postgresql+psycopg://user:pw@host/db ROBOT_TEST_DIR=<robot folder> pytest tests/test_robot_integration.py`
`cd frontend && npx vitest run`
