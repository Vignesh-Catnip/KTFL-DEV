from datetime import date, datetime

class InvoiceValidator:
    ROBOT_LIBRARY_SCOPE = "SUITE"

    def validate_invoice(self, invoice):
        errors = []
        for field in ("id", "invoice_number", "invoice_date", "po_number", "currency"):
            value = invoice.get(field)
            if value is None or str(value).strip() == "":
                errors.append(f"Missing required field: {field}")
        if invoice.get("invoice_date") not in (None, ""):
            try:
                value = invoice["invoice_date"]
                if not isinstance(value, (date, datetime)):
                    date.fromisoformat(str(value)[:10])
            except (ValueError, TypeError):
                errors.append("invoice_date must be a valid ISO date (YYYY-MM-DD)")
        if invoice.get("currency") and len(str(invoice["currency"]).strip()) > 5:
            errors.append("currency value looks invalid")
        return {"valid": not errors, "errors": errors}
