from __future__ import annotations

from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    username: str
    password: str


class InvoiceEdit(BaseModel):
    """Fields a person may correct on the reconciliation screen."""
    vendor: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None
    po_number: str | None = None
    grn_number: str | None = None
    plant_code: str | None = None
    vendor_code: str | None = None
    hsn_sac: str | None = None
    subtotal: float | None = None
    cgst: float | None = None
    sgst: float | None = None
    igst: float | None = None
    total_tax: float | None = None
    total: float | None = None


class ResolveIn(BaseModel):
    resolution: str = Field(min_length=2, max_length=40)       # e.g. "Accept variance", "Debit note"
    note: str = ""
    approve_variance: bool = False


class UserIn(BaseModel):
    username: str
    password: str = Field(min_length=8)
    full_name: str = ""
    role: str = "viewer"
