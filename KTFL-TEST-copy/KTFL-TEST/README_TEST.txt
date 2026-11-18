TEST COPY (does not change or touch the live app)
1. Unzip anywhere NEW (not over the live folder).
2. Open run_test.bat in Notepad, check the LIVE_VENV path (venv of your live app), save, double click it.
3. Open http://localhost:7100  -> login admin / test1234
4. Jobs page -> Upload a PDF (e.g. the SLR invoice). Wait until it is DONE (Qwen is shared with the live app, so it can queue).
5. Open the invoice -> "Digital Invoice" tab -> toolbar -> 🔍 Scan: drag a box around the PO / GRN number.
   Also try: rotate buttons, Fit / Width, zoom, "Detect PO / GRN numbers with AI".
Stop it with Ctrl+C in the black window. Delete the folder when done - nothing else is changed.
