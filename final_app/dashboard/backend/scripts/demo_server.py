"""DEMO / UAT server: real API + real PDFs, but a simulated SAP (SQLite) and a scripted vision model,
so the whole dashboard can be tried without SAP, Ollama or PostgreSQL.   python -m scripts.demo_server"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.update({"DATABASE_URL": f"sqlite:///{ROOT}/demo.db", "ADMIN_PASSWORD": "admin-pass-1",
                   "WORKER_ENABLED": "true", "GST_RATE_XLSX": str(ROOT / "tests/fixtures/GST_RATE.xlsx"),
                   "UPLOAD_DIR": str(ROOT / "demo_uploads"), "SAP_ENABLED": "true", "WATCH_FOLDER": str(ROOT / "demo_in"),
                   "WATCHER_POLL_SECONDS": "3", "WATCH_SETTLE_SECONDS": "1"})
(ROOT / "demo_in").mkdir(exist_ok=True)
from tests.conftest import FakeVlm, dsk_vision, make_sap                      # noqa: E402
from app.services import sap, vlm                                              # noqa: E402

sap.set_sap(make_sap(qty_override={3: 18}))
vlm.set_vlm(FakeVlm(dsk_vision()))
import uvicorn                                                                 # noqa: E402
uvicorn.run("app.main:app", host="127.0.0.1", port=int(os.environ.get("PORT", 8000)))
