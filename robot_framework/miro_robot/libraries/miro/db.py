"""
PostgreSQL access for the MIRO robot (same database the extractor writes to).

Adds MIRO tracking columns to `invoices` (idempotent) and records every outcome in
`invoices` + `audit_logs`, so a re-run never posts the same invoice twice.

miro_status lifecycle
    NULL / ''      -> not yet processed
    IN_PROGRESS    -> claimed by a robot run
    CHECKED        -> CHECK mode: filled + balance verified, nothing saved in SAP
    PARKED         -> parked in SAP (miro_doc_number filled)
    POSTED         -> posted in SAP (miro_doc_number filled)
    REVIEW         -> data does not agree with SAP (GRN missing, amount gap...) - not retried
    FAILED         -> technical error BEFORE saving - retried up to MIRO_MAX_ATTEMPTS
    UNKNOWN        -> error DURING save - may or may not be posted, check in SAP; never retried
"""
from __future__ import annotations

import json
from typing import Any

import psycopg
from psycopg.rows import dict_row

MIGRATIONS = [
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS miro_status VARCHAR(20)",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS miro_doc_number VARCHAR(20)",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS miro_message TEXT",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS miro_attempts INTEGER DEFAULT 0",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS miro_selected_rows TEXT",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS miro_updated_at TIMESTAMPTZ",
    "CREATE INDEX IF NOT EXISTS ix_invoices_miro_status ON invoices (miro_status)",
]

FINAL_STATUSES = ("CHECKED", "PARKED", "POSTED", "REVIEW", "FAILED", "UNKNOWN")


class MiroDb:
    def __init__(self, conninfo: str):
        self.conninfo = conninfo
        self.conn: psycopg.Connection | None = None

    # ------------------------------------------------------------ connection
    def connect(self) -> None:
        self.conn = psycopg.connect(self.conninfo, row_factory=dict_row, autocommit=False)
        with self.conn.cursor() as cur:
            for sql in MIGRATIONS:
                cur.execute(sql)
        self.conn.commit()

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def _cur(self):
        if self.conn is None or self.conn.closed:
            self.connect()
        return self.conn.cursor()

    # ------------------------------------------------------------ reading
    def pending_invoices(self, source_status: str, mode: str, max_attempts: int, limit: int) -> list[dict]:
        """MATCHED invoices that still need MIRO work in this mode.

        CHECK mode is a dry run: it takes EVERY MATCHED invoice on every run (no reset needed),
        except ones already saved in SAP (POSTED / PARKED) or in doubt (UNKNOWN / IN_PROGRESS)."""
        if mode == "CHECK":
            sql = """
            SELECT id, invoice_number, invoice_date, po_number, grn_number, vendor, vendor_code,
                   currency, subtotal, tax, total, gst_tax_code, sap_tax_code, plant_code,
                   COALESCE(miro_status, '') AS miro_status, COALESCE(miro_attempts, 0) AS miro_attempts
              FROM invoices
             WHERE status = %(st)s
               AND COALESCE(miro_status, '') NOT IN ('POSTED', 'PARKED', 'UNKNOWN', 'IN_PROGRESS')
             ORDER BY id
             LIMIT %(lim)s"""
            with self._cur() as cur:
                cur.execute(sql, {"st": source_status, "lim": limit})
                rows = cur.fetchall()
            self.conn.commit()
            return [self._clean(r) for r in rows]

        redo = ["FAILED"]                    # CHECKED is re-picked only when mode is PARK/POST
        sql = """
            SELECT id, invoice_number, invoice_date, po_number, grn_number, vendor, vendor_code,
                   currency, subtotal, tax, total, gst_tax_code, sap_tax_code, plant_code,
                   COALESCE(miro_status, '') AS miro_status, COALESCE(miro_attempts, 0) AS miro_attempts
              FROM invoices
             WHERE status = %(st)s
               AND (miro_status IS NULL OR miro_status = ''
                    OR (miro_status = ANY(%(redo)s) AND COALESCE(miro_attempts, 0) < %(max)s)
                    OR (miro_status = 'CHECKED' AND %(mode)s <> 'CHECK'))
             ORDER BY id
             LIMIT %(lim)s"""
        with self._cur() as cur:
            cur.execute(sql, {"st": source_status, "redo": redo, "max": max_attempts,
                              "mode": mode, "lim": limit})
            rows = cur.fetchall()
        self.conn.commit()
        return [self._clean(r) for r in rows]

    def invoice_lines(self, invoice_id: int) -> list[dict]:
        with self._cur() as cur:
            cur.execute("""SELECT item, quantity, rate, amount, uom, source
                             FROM invoice_lines WHERE invoice_id = %s ORDER BY id""", (invoice_id,))
            rows = cur.fetchall()
        self.conn.commit()
        return [self._clean(r) for r in rows]

    # ------------------------------------------------------------ claiming / results
    def claim(self, invoice_id: int, mode: str, max_attempts: int) -> bool:
        """Atomically mark the invoice IN_PROGRESS. False if another run took it or it is done.
        CHECK mode never writes to `invoices` (dry run) - nothing to claim."""
        if mode == "CHECK":
            return True
        with self._cur() as cur:
            cur.execute("""
                UPDATE invoices
                   SET miro_status = 'IN_PROGRESS',
                       miro_attempts = COALESCE(miro_attempts, 0) + 1,
                       miro_updated_at = NOW()
                 WHERE id = %(id)s
                   AND (miro_status IS NULL OR miro_status = ''
                        OR (miro_status = 'FAILED' AND COALESCE(miro_attempts, 0) < %(max)s)
                        OR (miro_status = 'CHECKED' AND %(mode)s <> 'CHECK'))
                RETURNING id""", {"id": invoice_id, "max": max_attempts, "mode": mode})
            ok = cur.fetchone() is not None
        self.conn.commit()
        return ok

    def finish(self, invoice_id: int, status: str, message: str = "", doc_number: str = "",
               selected_rows: str = "", detail: dict[str, Any] | None = None,
               write_invoice: bool = True) -> None:
        """Store the result. write_invoice=False (CHECK mode): invoices table untouched,
        only a history row in audit_logs."""
        if status not in FINAL_STATUSES:
            raise ValueError(f"Unknown MIRO status {status}")
        with self._cur() as cur:
            if write_invoice:
                cur.execute("""
                    UPDATE invoices
                       SET miro_status = %s, miro_message = %s,
                           miro_doc_number = COALESCE(NULLIF(%s, ''), miro_doc_number),
                           miro_selected_rows = COALESCE(NULLIF(%s, ''), miro_selected_rows),
                           miro_updated_at = NOW()
                     WHERE id = %s""", (status, message[:4000], doc_number, selected_rows, invoice_id))
            cur.execute("""INSERT INTO audit_logs (invoice_id, action, detail, created_at)
                           VALUES (%s, %s, %s, NOW())""",
                        (invoice_id, (f"MIRO_{status}" if write_invoice else
                                      "MIRO_CHECK_" + ("OK" if status == "CHECKED" else status)),
                         json.dumps({"message": message, "doc_number": doc_number,
                                     "selected_rows": selected_rows, **(detail or {})}, default=str)))
        self.conn.commit()

    def release_stale(self) -> int:
        """IN_PROGRESS left behind by a crashed run -> UNKNOWN (must be checked in SAP by a person)."""
        with self._cur() as cur:
            cur.execute("""UPDATE invoices SET miro_status = 'UNKNOWN',
                                  miro_message = 'Previous robot run stopped while processing - '
                                                 'check in SAP (MIR4 / MIR6) before re-running',
                                  miro_updated_at = NOW()
                            WHERE miro_status = 'IN_PROGRESS' RETURNING id""")
            n = len(cur.fetchall())
        self.conn.commit()
        return n

    def rollback(self) -> None:
        if self.conn is not None and not self.conn.closed:
            self.conn.rollback()

    @staticmethod
    def _clean(row: dict) -> dict:
        out = {}
        for k, v in row.items():
            if v is None:
                out[k] = ""
            elif hasattr(v, "is_finite"):            # Decimal
                out[k] = float(v)
            else:
                out[k] = v
        return out
