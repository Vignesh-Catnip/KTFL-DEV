"""ClearMatch API - run with:  uvicorn app.main:app --host 0.0.0.0 --port 8000"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .database import ensure_schema, get_engine, session_factory
from .routers import auth, doctypes, flows, invoices, robot as robot_router
from .security import seed_admin
from .services import robot, worker
from .services.gst import load_gst_master

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("clearmatch")


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_settings()
    if cfg.app_env == "production" and (cfg.jwt_secret == "change-me" or len(cfg.jwt_secret) < 24):
        raise RuntimeError("Set a long random JWT_SECRET before running in production")
    ensure_schema(get_engine())
    with session_factory()() as db:
        seed_admin(db)
        if Path(cfg.gst_rate_xlsx).is_file():
            n = load_gst_master(db, cfg.gst_rate_xlsx, cfg.sap_client)
            log.info("GST master loaded: %s codes", n)
        else:
            log.warning("GST master file not found: %s", cfg.gst_rate_xlsx)
    with session_factory()() as db:
        from .services.doctype import load_master
        log.info("Document type master: %s rows added", load_master(db))
    if cfg.worker_enabled:
        worker.start()
    robot.start()
    yield
    worker.stop()
    robot.stop()


def create_app() -> FastAPI:
    cfg = get_settings()
    app = FastAPI(title="ClearMatch", version="1.0.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=cfg.cors_list, allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])
    for r in (auth.router, invoices.router, flows.router, robot_router.router, doctypes.router):
        app.include_router(r)
    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if dist.is_dir():                                # one-process deployment: API + built dashboard
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            f = dist / path
            return FileResponse(f if path and f.is_file() else dist / "index.html")
    return app


app = create_app()
