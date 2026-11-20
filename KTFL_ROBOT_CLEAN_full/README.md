# KTFL MIRO Robot (Robot Framework + SAP GUI Scripting)

Takes invoices with `status = 'MATCHED'` from the extractor's PostgreSQL (`invoices`,
`invoice_lines`), and enters them in MIRO:

1. `/nMIRO`, company code 2000 popup
2. Invoice date (`INVFO-BLDAT`), invoice number (`INVFO-XBLNR`), gross amount, currency,
   GST tax code + *Calculate tax*
3. PO number (`RM08M-EBELN`) → Enter
4. Reads **every row** of the item table `SAPLMR1MTC_MR1M` (scrolling page by page), finds the
   **Reference Document** column (`DRSEG-LFBNR` = GRN number)
5. *Deselect all* → ticks **only** rows whose Reference Document = `invoices.grn_number`
   (and same PO), then re-reads the table to confirm SAP has exactly those rows selected
6. Checks: selected GR value = invoice taxable value (±`MIRO_AMOUNT_TOLERANCE`), MIRO balance = 0
7. `MIRO_MODE`: **CHECK** (leave without saving) / **PARK** / **POST** → document number saved to DB

## Folder

```
tasks/miro_posting.robot     Robot task (the flow)
libraries/MiroLibrary.py     Robot keywords
libraries/miro/
   config.py                 all settings (env vars / .env)
   db.py                     PostgreSQL: pick, claim, results, audit_logs
   sapgui.py                 SAP GUI Scripting wrapper (FindByName, status bar, popups)
   miro_screen.py            logon + MIRO steps
   matching.py               GRN row selection + amount checks (pure logic)
   fake_sap.py               SAP simulator used only by tests
tests/                       pytest: unit + end-to-end (Robot + Postgres + simulator)
run_miro.bat                 start script for Task Scheduler
```

## Setup (robot PC)

1. Python 3.10+ → `pip install -r requirements.txt`
2. SAP GUI → Options → Accessibility & Scripting → Scripting: **Enable scripting** ON,
   *Notify when a script attaches* OFF, *Notify when a script opens a connection* OFF
3. Basis: profile parameter `sapgui/user_scripting = TRUE` for the system
4. Copy `.env.example` → `.env`, fill passwords (or set them as Windows env variables)
5. MIRO item-list layout must show the **Reference Document** column (layout *All information*
   or a user layout saved as default). If missing, the robot stops with a clear message.

## Go-live sequence

| Step | `MIRO_MODE` | What happens |
|---|---|---|
| UAT | `CHECK` | Fills MIRO, selects GRN rows, verifies balance, screenshot, **exits without saving** → `miro_status = CHECKED` |
| Pilot | `PARK` | Parks documents; AP team reviews in MIR4 and posts |
| Production | `POST` | Posts; SAP doc no. stored in `invoices.miro_doc_number` |

CHECKED invoices are automatically taken again when you switch to PARK/POST.

## Result columns added to `invoices`

`miro_status`, `miro_doc_number`, `miro_message`, `miro_attempts`, `miro_selected_rows`, `miro_updated_at`
(plus one `audit_logs` row per outcome: `MIRO_POSTED`, `MIRO_REVIEW` …)

| miro_status | Meaning | Retried? |
|---|---|---|
| POSTED / PARKED | SAP document created | no |
| CHECKED | CHECK mode, nothing saved | yes, when mode = PARK/POST |
| REVIEW | Data problem: GRN not in MIRO, amount gap, duplicate-invoice warning, balance ≠ 0 | no — fix data, then set `miro_status = NULL` |
| FAILED | SAP/technical error before saving (PO not found, screen error) | yes, up to `MIRO_MAX_ATTEMPTS` |
| UNKNOWN | Error **during** save, or robot crashed mid-invoice | **no** — check MIR4/MIR6 first |

Screenshots: `output/screenshots/`; Robot log: `output/run_*/log.html`.

## Tests

```
# needs a throw-away PostgreSQL (TEST_PG_HOST / TEST_PG_PORT / TEST_PG_PASSWORD)
pytest -q tests
```
28 tests: number/date parsing, GRN row selection, full Robot runs in POST / CHECK→POST / PARK,
GRN missing, amount gap, duplicate warning, unknown PO, retry rules, crashed run → UNKNOWN,
wrong SAP password, empty queue.

## Points to confirm on the real system (first CHECK run)

* `RM08M-DIFFERENZ` (balance) and `DRSEG-LFBNR` field names — if your release differs, change
  `MIRO_BALANCE_FIELD` / `MIRO_COL_REF_DOC` (use SAP GUI recorder or F1 → Technical info).
* Park menu text (`MIRO_PARK_MENU`) if your SAP language/menu differs.
* Withholding tax (TDS) tab is not filled — add if your vendors need it at MIRO.
