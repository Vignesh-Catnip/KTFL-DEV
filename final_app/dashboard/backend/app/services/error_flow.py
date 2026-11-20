"""Keeps invoices.error_flow_status in step with what the Error flow page shows.

The label is exactly validate.exception_type(): Blocked / Data missing / Quantity mismatch / Price variance /
Tax / HSN / Other mismatch - and '' for an invoice with no open exception (MATCHED, or not in RECONCILE/BLOCKED).
Importing this module registers the listeners; backfill() fills rows that existed before the column did.
"""
from __future__ import annotations

import logging

from sqlalchemy import event, select, update
from sqlalchemy.orm import Session

from ..models import QUEUE_BLOCKED, QUEUE_RECONCILE, Invoice
from .validate import exception_type

log = logging.getLogger("clearmatch.error_flow")


def error_flow_label(queue: str | None, issues: list | None) -> str:
    if queue not in (QUEUE_RECONCILE, QUEUE_BLOCKED):
        return ""
    return exception_type(issues or [])


def _stamp(_mapper, _conn, inv: Invoice) -> None:
    inv.error_flow_status = error_flow_label(inv.queue, inv.issues)


event.listen(Invoice, "before_insert", _stamp)
event.listen(Invoice, "before_update", _stamp)


def backfill(db: Session) -> int:
    """Fill error_flow_status for invoices that do not have it yet (updated_at is left untouched)."""
    rows = db.execute(select(Invoice.id, Invoice.queue, Invoice.issues)
                      .where(Invoice.error_flow_status.is_(None))).all()
    for rid, queue, issues in rows:
        db.execute(update(Invoice).where(Invoice.id == rid)
                   .values(error_flow_status=error_flow_label(queue, issues), updated_at=Invoice.updated_at)
                   .execution_options(synchronize_session=False))
    db.commit()
    if rows:
        log.info("error_flow_status filled for %s existing invoices", len(rows))
    return len(rows)
