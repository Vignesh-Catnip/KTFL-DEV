"""
Robot Framework keyword library for KTFL MIRO posting.

Used by tasks/miro_posting.robot. Each keyword is one business step, so the Robot log
shows exactly where an invoice stopped.
"""
from __future__ import annotations

import functools
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from robot.api import logger                       # noqa: E402
from robot.api.deco import keyword, library        # noqa: E402

from miro.config import load_config                # noqa: E402
from miro.db import MiroDb                         # noqa: E402
from miro.matching import MiroDataError, plan_hsn, plan_selection   # noqa: E402
from miro.miro_screen import MiroScreen, connect_sap       # noqa: E402
from miro.sapgui import SapError                   # noqa: E402


def _track(fn):
    """Remember what kind of error a keyword raised, so Record Failure can classify it."""
    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        try:
            return fn(self, *a, **kw)
        except MiroDataError:
            self._error_kind = "DATA"
            raise
        except SapError:
            self._error_kind = self._error_kind if self._error_kind == "SAVE" else "SAP"
            raise
        except Exception:
            if self._error_kind != "SAVE":
                self._error_kind = "TECH"
            logger.debug(traceback.format_exc())
            raise
    return wrapper


@library(scope="SUITE", auto_keywords=False)
class MiroLibrary:

    def __init__(self):
        self.cfg = None
        self.db: MiroDb | None = None
        self.sap = None
        self.miro: MiroScreen | None = None
        self._error_kind = ""
        self._plan = None
        self._run_stats = {"CHECKED": 0, "PARKED": 0, "POSTED": 0, "REVIEW": 0,
                           "FAILED": 0, "UNKNOWN": 0, "SKIPPED": 0}

    # ================================================================ setup
    @keyword("Initialize MIRO Robot")
    def initialize(self):
        self.cfg = load_config()
        logger.info(f"MIRO mode: {self.cfg.mode} | company code {self.cfg.company_code} | "
                    f"source status {self.cfg.source_status} | SAP backend {self.cfg.sap_backend}",
                    also_console=True)
        if self.cfg.mode == "POST":
            logger.warn("MIRO_MODE=POST - invoices WILL be posted in SAP")

    @keyword("Connect To Invoice Database")
    def connect_db(self):
        self.db = MiroDb(self.cfg.pg_conninfo)
        self.db.connect()
        stale = self.db.release_stale()
        if stale:
            logger.warn(f"{stale} invoice(s) were IN_PROGRESS from a crashed run -> marked UNKNOWN "
                        f"(check them in SAP MIR4 before resetting miro_status)")

    @keyword("Get Invoices Ready For MIRO")
    def get_pending(self):
        rows = self.db.pending_invoices(self.cfg.source_status, self.cfg.mode,
                                        self.cfg.max_attempts, self.cfg.batch_limit)
        logger.info(f"{len(rows)} invoice(s) with status {self.cfg.source_status} waiting for MIRO",
                    also_console=True)
        return rows

    @keyword("Connect To SAP")
    @_track
    def connect_to_sap(self):
        self.sap = connect_sap(self.cfg)
        self.sap.say = self._say
        self.miro = MiroScreen(self.sap, self.cfg)
        logger.info(f"SAP session ready ({self.sap.screen_info()})", also_console=True)

    # ================================================================ per invoice
    @keyword("Claim Invoice")
    def claim(self, invoice):
        self._error_kind = ""
        self._plan = None
        self._unplanned = 0.0
        if self.miro:
            self.miro.log = []
        ok = self.db.claim(int(invoice["id"]), self.cfg.mode, self.cfg.max_attempts)
        if not ok:
            self._run_stats["SKIPPED"] += 1
            logger.info(f"Invoice id={invoice['id']} already taken / finished - skipped")
        else:
            invoice["lines"] = self.db.invoice_lines(int(invoice["id"]))
            logger.info(f"=== Invoice {invoice['invoice_number']} | PO {invoice['po_number']} | "
                        f"GRN {invoice['grn_number']} | total {invoice['total']} ===", also_console=True)
        return ok

    @keyword("Open MIRO")
    @_track
    def open_miro(self):
        self.miro.open()

    @keyword("Enter Invoice Header")
    @_track
    def enter_header(self, invoice):
        self.miro.fill_header(invoice)

    @keyword("Enter Purchase Order")
    @_track
    def enter_po(self, invoice):
        self.miro.enter_po(invoice["po_number"])

    @keyword("Select GRN Lines")
    @_track
    def select_grn_lines(self, invoice):
        """Reads the MIRO item table, keeps only rows whose Reference Document = GRN number,
        checks them against the invoice from the DB, then ticks exactly those rows."""
        rows = self.miro.read_rows()
        # GRN value may differ from the invoice taxable value by freight etc. that is later
        # booked as unplanned delivery costs -> allow that much difference here
        plan = plan_selection(rows, invoice, self.cfg.amount_tolerance + self.cfg.max_unplanned_cost)
        for w in plan.warnings:
            logger.warn(f"{invoice['invoice_number']}: {w}")
            self.miro.note(w)
        self.miro.select_rows(plan.indexes)
        self._plan = plan
        msg = (f"Selected {len(plan.rows)} row(s) {[r.index + 1 for r in plan.rows]} for GRN "
               f"{invoice['grn_number']} = {plan.selected_amount:,.2f} "
               f"(invoice taxable {float(invoice.get('subtotal') or 0):,.2f})")
        if plan.other_grns:
            msg += f"; other GRNs left unselected: {plan.other_grns}"
        self.miro.note(msg)
        logger.info(msg, also_console=True)

    @keyword("Check And Fix HSN")
    @_track
    def check_hsn(self, invoice):
        """HSN/SAC column of the selected GRN rows must equal the HSN in the database;
        different -> the cell is overwritten with the DB value."""
        if not self.cfg.check_hsn or not self._plan:
            return
        rows = {r.index: r for r in self.miro.read_rows()}
        selected = [rows[r.index] for r in self._plan.rows if r.index in rows]
        changes, notes = plan_hsn(selected, invoice, self.cfg.amount_tolerance)
        for n in notes:
            self.miro.note(n)
            logger.info(n, also_console=True)
        self.miro.update_hsn(changes)
        if changes:
            logger.info(f"HSN corrected on {len(changes)} row(s)", also_console=True)

    @keyword("Enter Amount And Settle Balance")
    @_track
    def amount_and_balance(self, invoice):
        """Amount = invoice total. Balance 0 -> OK. Balance > 0 (up to MIRO_MAX_UNPLANNED_COST) ->
        Details tab: Doc. Type by plant + Unpl. Del. Csts = balance. Balance must end at 0."""
        bal = self.miro.enter_amount(invoice)
        logger.info(f"Balance after amount: {bal:,.2f}", also_console=True)
        unpl = self.miro.settle_balance(invoice, bal)
        self._unplanned = unpl
        if unpl:
            logger.info(f"Unpl. Del. Csts {unpl:,.2f} entered - balance 0", also_console=True)
        else:
            logger.info("Balance OK (0.00)", also_console=True)

    @keyword("Simulate Invoice")
    @_track
    def simulate(self, invoice):
        if not self.cfg.simulate:
            return
        info = self.miro.simulate()
        self._screenshot(invoice, "simulated")
        logger.info(f"Simulate OK {('- ' + info) if info else ''}", also_console=True)

    @keyword("Complete Invoice")
    @_track
    def complete(self, invoice):
        """CHECK -> leave without saving | PARK -> park | POST -> post. Records the result."""
        inv_id = int(invoice["id"])
        shot = self._screenshot(invoice, "before_save")
        sel = ",".join(str(r.index + 1) for r in (self._plan.rows if self._plan else []))
        detail = {"mode": self.cfg.mode, "screenshot": shot, "sap_log": self.miro.log,
                  "selected_amount": self._plan.selected_amount if self._plan else None,
                  "unplanned_delivery_costs": getattr(self, "_unplanned", 0.0)}

        if self.cfg.mode == "CHECK":
            self.miro.leave()
            self._finish(inv_id, "CHECKED", "Filled and verified in MIRO - not saved (CHECK mode)",
                         "", sel, detail)
            return "CHECKED"

        self._error_kind = "SAVE"                      # from here an exception may mean "saved?"
        try:
            doc, txt = self.miro.park() if self.cfg.mode == "PARK" else self.miro.post()
        except (SapError, MiroDataError):
            self._error_kind = "SAP"                   # SAP explicitly refused -> nothing saved
            raise
        status = "PARKED" if self.cfg.mode == "PARK" else "POSTED"
        if not doc:
            self._error_kind = "SAVE"
            raise SapError(f"No document number after {self.cfg.mode}: '{txt}'")
        self._error_kind = ""
        self._finish(inv_id, status, txt, doc, sel, detail)
        logger.info(f"{status}: SAP document {doc}", also_console=True)
        return status

    @keyword("Record Failure")
    def record_failure(self, invoice, error):
        """Classifies the error and stores it:
             data problem           -> REVIEW  (not retried)
             error while saving     -> UNKNOWN (check in SAP, not retried)
             other SAP / tech error -> FAILED  (retried next run)"""
        kind = self._error_kind or "TECH"
        status = {"DATA": "REVIEW", "SAVE": "UNKNOWN"}.get(kind, "FAILED")
        shot = self._screenshot(invoice, "error")
        detail = {"mode": self.cfg.mode, "error_kind": kind, "screenshot": shot,
                  "sap_log": self.miro.log if self.miro else []}
        try:
            self.db.rollback()
            self._finish(int(invoice["id"]), status, str(error), "", "", detail)
        except Exception as e:                          # DB down: never hide the SAP error
            logger.error(f"Could not write failure to DB: {e}")
        logger.warn(f"{invoice.get('invoice_number')}: {status} - {error}")
        if self.miro and status != "UNKNOWN":
            self.miro.leave()
        return status

    @keyword("Reset SAP Screen")
    def reset_screen(self):
        if self.miro:
            self.miro.leave()

    # ================================================================ teardown
    @keyword("Write Run Summary")
    def summary(self):
        s = self._run_stats
        line = " | ".join(f"{k} {v}" for k, v in s.items())
        logger.info(f"MIRO run finished: {line}", also_console=True)
        out = self.cfg.output_dir / f"run_summary_{datetime.now():%Y%m%d_%H%M%S}.json"
        out.write_text(json.dumps(s, indent=2), encoding="utf-8")
        return s

    @keyword("Close Connections")
    def close(self):
        if self.db:
            self.db.close()

    # ================================================================ helpers
    def _say(self, msg):
        """Every SAP step: always in log.html; on the console too in watch mode."""
        logger.info(msg)
        if self.cfg.step_delay > 0:
            logger.console(msg)

    def _finish(self, inv_id, status, message, doc, sel, detail):
        prefix = "[CHECK] " if self.cfg.mode == "CHECK" else ""
        self.db.finish(inv_id, status, prefix + message, doc, sel, detail,
                       write_invoice=self.cfg.mode != "CHECK")
        self._run_stats[status] = self._run_stats.get(status, 0) + 1

    def _screenshot(self, invoice, tag):
        if not self.sap:
            return ""
        safe = "".join(ch if ch.isalnum() else "_" for ch in str(invoice.get("invoice_number", "")))
        path = self.cfg.output_dir / "screenshots" / f"{invoice['id']}_{safe}_{tag}_{datetime.now():%H%M%S}.png"
        p = self.sap.screenshot(path)
        if p:
            logger.info(f"Screenshot: {p}")
        return p
