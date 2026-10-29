"""
ORM models.

The `invoices`, `invoice_lines`, `invoice_master`, `gst_tax_codes` and `audit_logs` tables are the SAME
tables the extractor script and the MIRO robot already use - column names are unchanged, new columns
are only ever ADDED (see database.ensure_schema), so all three programs keep working side by side.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# Dashboard queues (what the AP team sees)
QUEUE_MATCHED = "MATCHED"        # everything agrees with SAP -> goes to the robot automatically
QUEUE_RECONCILE = "RECONCILE"    # data present but does not agree with SAP -> a person reconciles
QUEUE_BLOCKED = "BLOCKED"        # mandatory data missing / not in SAP / multiple POs -> cannot be matched
QUEUES = (QUEUE_MATCHED, QUEUE_RECONCILE, QUEUE_BLOCKED)

# invoices.status - the value the MIRO robot reads (MATCHED = ready for MIRO)
STATUS_MATCHED = "MATCHED"
STATUS_REVIEW = "REVIEW"
STATUS_MANUAL = "MANUAL"         # SAP links the invoice to several POs


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    vendor: Mapped[str | None] = mapped_column(String(255))
    invoice_number: Mapped[str | None] = mapped_column(String(100), index=True)
    invoice_date: Mapped[str | None] = mapped_column(String(30))
    po_number: Mapped[str | None] = mapped_column(String(100))
    gstin: Mapped[str | None] = mapped_column(String(50))
    hsn_sac: Mapped[str | None] = mapped_column(String(50))          # ONLY what the invoice prints
    vendor_code: Mapped[str | None] = mapped_column(String(50))
    tax_code: Mapped[str | None] = mapped_column(String(30))
    business_place: Mapped[str | None] = mapped_column(String(100))
    currency: Mapped[str] = mapped_column(String(10), default="INR")
    subtotal: Mapped[float] = mapped_column(Float, default=0)
    cgst: Mapped[float] = mapped_column(Float, default=0)
    sgst: Mapped[float] = mapped_column(Float, default=0)
    igst: Mapped[float] = mapped_column(Float, default=0)
    tax: Mapped[float] = mapped_column(Float, default=0)
    total: Mapped[float] = mapped_column(Float, default=0)
    confidence: Mapped[float] = mapped_column(Float, default=0)
    grn_number: Mapped[str | None] = mapped_column(String(100))
    handwritten_references: Mapped[str | None] = mapped_column(Text)
    plant_code: Mapped[str | None] = mapped_column(String(20))
    plant_source: Mapped[str | None] = mapped_column(String(20))
    doc_type: Mapped[str | None] = mapped_column(String(10))                    # MIRO Doc. Type key of the plant (Details tab)
    doc_type_desc: Mapped[str | None] = mapped_column(String(120))              # its description = the text SAP shows in the drop-down
    po_date: Mapped[str | None] = mapped_column(String(30))
    grn_date: Mapped[str | None] = mapped_column(String(30))
    grn_amount: Mapped[float | None] = mapped_column(Float)
    po_list: Mapped[str | None] = mapped_column(Text)
    multi_po_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    red_flag_reason: Mapped[str | None] = mapped_column(Text)
    gst_percent: Mapped[float | None] = mapped_column(Float)
    gst_percent_component: Mapped[float | None] = mapped_column(Float)
    gst_nature: Mapped[str | None] = mapped_column(String(20))
    gst_tax_code: Mapped[str | None] = mapped_column(String(10))
    gst_tax_code_desc: Mapped[str | None] = mapped_column(String(120))
    sap_tax_code: Mapped[str | None] = mapped_column(String(10))
    tax_code_match: Mapped[bool | None] = mapped_column(Boolean)
    validation_status: Mapped[str | None] = mapped_column(String(20), index=True)
    validation_errors: Mapped[dict] = mapped_column(JSON, default=dict)
    dedupe_key: Mapped[str | None] = mapped_column(String(220), index=True)
    field_sources: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(40), default=STATUS_REVIEW, index=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)

    # ---- ClearMatch additions ------------------------------------------------------------------
    queue: Mapped[str | None] = mapped_column(String(20), index=True)           # MATCHED/RECONCILE/BLOCKED
    issues: Mapped[list] = mapped_column(JSON, default=list)                    # structured issues
    company_code: Mapped[str | None] = mapped_column(String(10))
    sap_hsn: Mapped[str | None] = mapped_column(String(200))                    # HSN in SAP - compare only
    hsn_match: Mapped[bool | None] = mapped_column(Boolean)
    variance_amount: Mapped[float | None] = mapped_column(Float)
    completeness: Mapped[float | None] = mapped_column(Float)                   # share of mandatory fields found (0-1)
    sap_checked: Mapped[bool] = mapped_column(Boolean, default=False)
    grn_in_sap: Mapped[bool] = mapped_column(Boolean, default=False)
    sap_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)              # what SAP showed at check time
    source_path: Mapped[str | None] = mapped_column(String(500))                # original PDF on disk
    page_count: Mapped[int | None] = mapped_column(Integer)
    resolution: Mapped[str | None] = mapped_column(String(40))
    resolution_note: Mapped[str | None] = mapped_column(Text)
    resolved_by: Mapped[str | None] = mapped_column(String(80))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_variance: Mapped[bool] = mapped_column(Boolean, default=False)
    pushed_by: Mapped[str | None] = mapped_column(String(80))
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ---- written by the MIRO robot (see robot db.py) ---------------------------------------------
    miro_status: Mapped[str | None] = mapped_column(String(20), index=True)
    miro_doc_number: Mapped[str | None] = mapped_column(String(20))
    miro_message: Mapped[str | None] = mapped_column(Text)
    miro_attempts: Mapped[int | None] = mapped_column(Integer, default=0)
    miro_selected_rows: Mapped[str | None] = mapped_column(Text)
    miro_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lines = relationship("InvoiceLine", back_populates="invoice", cascade="all, delete-orphan",
                         order_by="InvoiceLine.id")


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"))
    item: Mapped[str] = mapped_column(String(255))
    quantity: Mapped[float] = mapped_column(Float, default=0)
    rate: Mapped[float] = mapped_column(Float, default=0)
    tax_code: Mapped[str] = mapped_column(String(30), default="")
    amount: Mapped[float] = mapped_column(Float, default=0)
    hsn_sac: Mapped[str | None] = mapped_column(String(50))
    po_number: Mapped[str | None] = mapped_column(String(100))
    po_item: Mapped[str | None] = mapped_column(String(10))
    plant_code: Mapped[str | None] = mapped_column(String(20))
    uom: Mapped[str | None] = mapped_column(String(10))
    source: Mapped[str] = mapped_column(String(20), default="VISION")
    # ClearMatch additions: what SAP's goods receipt shows for this line (compare only)
    gr_quantity: Mapped[float | None] = mapped_column(Float)
    gr_amount: Mapped[float | None] = mapped_column(Float)
    gr_row: Mapped[str | None] = mapped_column(String(60))                      # "MBLNR/EBELP"
    line_status: Mapped[str | None] = mapped_column(String(20))                 # OK / QTY / AMOUNT / MISSING

    invoice = relationship("Invoice", back_populates="lines")


class GstTaxCode(Base):
    __tablename__ = "gst_tax_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    client: Mapped[str] = mapped_column(String(10))
    procedure: Mapped[str | None] = mapped_column(String(20))
    tax_code: Mapped[str] = mapped_column(String(10))
    tax_type: Mapped[str | None] = mapped_column(String(5))
    description: Mapped[str | None] = mapped_column(String(160))
    rate: Mapped[float] = mapped_column(Float, default=0)
    rate_component: Mapped[float] = mapped_column(Float, default=0)
    nature: Mapped[str | None] = mapped_column(String(20))
    category: Mapped[str | None] = mapped_column(String(20))


class InvoiceMaster(Base):
    __tablename__ = "invoice_master"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_number: Mapped[str] = mapped_column(String(100), unique=True)
    vendor: Mapped[str | None] = mapped_column(String(255))
    vendor_code: Mapped[str | None] = mapped_column(String(50))
    po_number: Mapped[str | None] = mapped_column(String(100))
    first_seen_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"))


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(100))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    actor: Mapped[str | None] = mapped_column(String(80))                      # ClearMatch addition


class User(Base):
    __tablename__ = "app_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    full_name: Mapped[str | None] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(20), default="viewer")            # viewer / approver / admin
    password_hash: Mapped[str] = mapped_column(String(300))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class RobotRun(Base):
    """One start of the MIRO robot (Robot Framework process)."""
    __tablename__ = "robot_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="RUNNING", index=True)
    # RUNNING / DONE / FAILED / TIMEOUT / INTERRUPTED
    trigger: Mapped[str] = mapped_column(String(10), default="MANUAL")          # AUTO / MANUAL
    triggered_by: Mapped[str | None] = mapped_column(String(80))
    mode: Mapped[str | None] = mapped_column(String(10))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    log_path: Mapped[str | None] = mapped_column(String(500))
    report_path: Mapped[str | None] = mapped_column(String(500))
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


class Job(Base):
    """One PDF going through extraction."""
    __tablename__ = "extraction_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(String(500))
    folder_path: Mapped[str | None] = mapped_column(String(1000))               # where the PDF is now in the folder workflow
    source: Mapped[str] = mapped_column(String(20), default="UPLOAD")          # UPLOAD / WATCH
    status: Mapped[str] = mapped_column(String(20), default="QUEUED", index=True)
    # QUEUED / RUNNING / DONE / DUPLICATE / FAILED
    stage: Mapped[str | None] = mapped_column(String(60))
    invoice_id: Mapped[int | None] = mapped_column(Integer)
    queue: Mapped[str | None] = mapped_column(String(20))
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DocTypeMaster(Base):
    """MIRO document type per plant (client, doc type, number range, description, plant code)."""
    __tablename__ = "doc_type_master"

    id: Mapped[int] = mapped_column(primary_key=True)
    client: Mapped[str] = mapped_column(String(10), default="150")
    doc_type: Mapped[str] = mapped_column(String(10))
    number_range: Mapped[str | None] = mapped_column(String(10))
    description: Mapped[str | None] = mapped_column(String(120))
    plant_code: Mapped[str] = mapped_column(String(20), index=True)
