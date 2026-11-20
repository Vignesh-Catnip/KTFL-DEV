"""Engine / session handling and the idempotent schema upgrade."""
from __future__ import annotations

import logging
from collections.abc import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import get_settings
from .models import Base

log = logging.getLogger("clearmatch.db")

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        kwargs: dict = {"connect_args": {"check_same_thread": False}}
        if ":memory:" in url or url in ("sqlite://", "sqlite:///"):
            kwargs["poolclass"] = StaticPool
        return create_engine(url, **kwargs)
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=10)


def init_engine(url: str | None = None) -> Engine:
    """Create the global engine (called once at start-up; tests call it with their own URL)."""
    global _engine, _SessionLocal
    _engine = make_engine(url or get_settings().database_url)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, autoflush=False)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        init_engine()
    return _engine  # type: ignore[return-value]


def session_factory() -> sessionmaker[Session]:
    if _SessionLocal is None:
        init_engine()
    return _SessionLocal  # type: ignore[return-value]


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    db = session_factory()()
    try:
        yield db
    finally:
        db.close()


# partial unique indexes / helper indexes that create_all() cannot express
_INDEXES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_invoices_dedupe_key ON invoices (dedupe_key) "
    "WHERE dedupe_key IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_gst_tax_codes ON gst_tax_codes (client, tax_code)",
    "CREATE INDEX IF NOT EXISTS ix_invoices_queue ON invoices (queue)",
    "CREATE INDEX IF NOT EXISTS ix_invoices_miro_status ON invoices (miro_status)",
    "CREATE INDEX IF NOT EXISTS ix_audit_logs_invoice ON audit_logs (invoice_id)",
    "CREATE INDEX IF NOT EXISTS ix_audit_logs_created ON audit_logs (created_at)",
]


def ensure_schema(engine: Engine | None = None) -> list[str]:
    """Create missing tables and ADD missing columns to tables the extractor already created.
    Never drops or alters anything that exists. Returns the list of columns it added."""
    engine = engine or get_engine()
    Base.metadata.create_all(engine)
    added: list[str] = []
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ctype = col.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {ctype}'))
                added.append(f"{table.name}.{col.name}")
        for sql in _INDEXES:
            try:
                conn.execute(text(sql))
            except Exception as exc:             # an index that cannot be built must not stop the app
                log.warning("index skipped: %s (%s)", sql[:70], exc)
    if added:
        log.info("schema upgraded, added columns: %s", ", ".join(added))
    return added
