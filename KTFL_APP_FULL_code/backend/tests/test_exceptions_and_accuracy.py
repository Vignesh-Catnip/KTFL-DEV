"""Exception folder (service PO / SAC 99), filename-GRN priority and the totals second read."""
import shutil

from app.models import Invoice, Job
from app.services import workflow, worker
from app.services.pipeline import process_pdf
from tests.conftest import FIX, FakeVlm, dsk_vision, make_sap

PDF = FIX / "inv_dsk_428.pdf"


def test_service_po_goes_to_exception_and_is_not_saved(db):
    d = dsk_vision(po_number="8100007531", po_numbers_all="8100007531")
    r = process_pdf(db, PDF, "inv_dsk_428.pdf", vlm=FakeVlm(d), gw=make_sap())
    assert r.outcome == "EXCEPTION" and r.reason == "service_po" and r.invoice_id is None
    assert db.query(Invoice).count() == 0


def test_sac_99_goes_to_exception(db):
    d = dsk_vision(hsn_sac="998313")
    d["lines"] = [dict(l, hsn_sac="998313") for l in d["lines"]]
    r = process_pdf(db, PDF, "inv_dsk_428.pdf", vlm=FakeVlm(d), gw=make_sap())
    assert r.outcome == "EXCEPTION" and r.reason == "sac_99" and db.query(Invoice).count() == 0


def test_material_invoice_is_not_an_exception(db):
    r = process_pdf(db, PDF, "inv_dsk_428.pdf", vlm=FakeVlm(), gw=make_sap())
    assert r.outcome == "CREATED"


def test_filename_grn_beats_model_grn(db):
    r = process_pdf(db, PDF, "5000343863.pdf", vlm=FakeVlm(dsk_vision(), grn="5000999999"), gw=make_sap())
    inv = db.get(Invoice, r.invoice_id)
    assert inv.grn_number == "5000343863"


def test_totals_second_read_fixes_inconsistent_amounts(db):
    class V(FakeVlm):
        def read_totals(self, images):
            return {"subtotal": 64240.0, "cgst": 5781.6, "sgst": 5781.6, "igst": 0, "round_off": 0,
                    "other_charges": 0, "total": 75803.2}
    bad = dsk_vision(subtotal=11563.2, igst=64240.0, total=1234.0, cgst=0, sgst=0, total_tax=0)
    r = process_pdf(db, PDF, "inv_dsk_428.pdf", vlm=V(bad), gw=make_sap())
    inv = db.get(Invoice, r.invoice_id)
    assert inv.subtotal == 64240.0 and inv.total == 75803.2


def test_totals_second_read_never_replaces_with_inconsistent(db):
    class V(FakeVlm):
        def read_totals(self, images):
            return {"subtotal": 1, "cgst": 0, "sgst": 0, "igst": 0, "total": 999}
    bad = dsk_vision(total=1234.0)
    r = process_pdf(db, PDF, "inv_dsk_428.pdf", vlm=V(bad), gw=make_sap())
    assert db.get(Invoice, r.invoice_id).subtotal == 64240.0


def test_worker_moves_exception_pdf(db, tmp_path, monkeypatch):
    from app.config import get_settings
    cfg = get_settings()
    monkeypatch.setattr(cfg, "watch_folder", str(tmp_path / "input"))
    workflow.ensure_folders()
    f = workflow.folders()
    pdf = f["processing"] / "svc.pdf"
    shutil.copyfile(PDF, pdf)
    job = Job(filename="svc.pdf", stored_path=str(pdf), source="FOLDER", folder_path=str(pdf))
    workflow.place_exception(job, "service_po")
    assert (f["exception"] / "service_po" / "svc.pdf").is_file() and not pdf.exists()


# ---------------------------------------------------------------- SLR-style invoice: no printed PO, phone number in the letterhead
from app.services import evidence as evmod                      # noqa: E402
from app.services.vlm import adapt_invoice                       # noqa: E402

SLR_TEXT = """Details of Supplier Order No. : PS26Y-01408
GSTIN : 29AAJCS4396P1Z9
Contact Detail : SLR METALIKS LIMITED, Mo. No. : 8394294215, Ph. No. : 8394294061, Email : x
GSTIN : 27AABCK0618A1Z7 GSTIN : 27AABCK0618A1Z7
"""


def test_phone_number_is_never_a_po():
    # model returned the vendor's phone number as the printed PO - it must not become the PO, handwritten one wins
    d = adapt_invoice({"po_number_raw": "8394294061", "handwritten_po_raw": "PO - 5100002448"})
    assert d["po_number"] == "5100002448"


def test_material_po_beats_service_po():
    d = adapt_invoice({"po_number_raw": "8100001234", "handwritten_po_raw": "5100002448"})
    assert d["po_number"] == "5100002448"


def test_evidence_finds_phones_and_seller_gstin():
    ev = evmod.analyse(SLR_TEXT)
    assert {"8394294215", "8394294061"} <= ev.phones and not ev.pos
    assert ev.gstins[0] == "29AAJCS4396P1Z9"


def test_evidence_drops_phone_po_and_snaps_gstin():
    ev = evmod.analyse(SLR_TEXT)
    h = {"po_number": "8394294061", "po_numbers_printed": ["8394294061"], "gstin": "29AAJCS4396P1Z8", "field_sources": {}}
    notes = evmod.apply(h, ev, ("27AABCK0618A",))
    assert h["po_number"] == "" and h["gstin"] == "29AAJCS4396P1Z9" and len(notes) == 2


def test_evidence_snaps_po_one_digit_away():
    ev = evmod.analyse("PO No 5100002448 dated 12.10.2026")
    h = {"po_number": "5100002446", "po_numbers_printed": [], "gstin": "", "field_sources": {}}
    evmod.apply(h, ev, ())
    assert h["po_number"] == "5100002448"


def test_phone_po_does_not_trigger_service_exception(db):
    """83xxxxxxxx phone number read as the PO + OCR knows it is a phone -> invoice is processed, not moved away."""
    from app.services import pipeline
    ev = evmod.analyse(SLR_TEXT)
    h = {"po_number": "8394294061", "po_numbers_printed": ["8394294061"], "po_numbers_all": "8394294061", "hsn_sac": "72283019"}
    assert pipeline._exception_reason(h, [], ev) is None


def test_handwritten_po_pass_fills_missing_po(db):
    class V(FakeVlm):
        def read_handwritten_po(self, images):
            return "5100002448"
    d = dsk_vision(po_number="", po_numbers_all="")
    r = process_pdf(db, PDF, "5000343863.pdf", vlm=V(d), gw=make_sap())
    assert db.get(Invoice, r.invoice_id).po_number == "5100002448"


def test_delivery_challan_number_is_not_a_service_po():
    ev = evmod.analyse("Your PO No: Jul.Schedule Invoice No: 9043\nOur OA No : 130019559 DC. No : 8370059954\n")
    assert "8370059954" in ev.others
    from app.services import pipeline
    h = {"po_number": "8370059954", "po_numbers_printed": ["8370059954"], "hsn_sac": "72283019"}
    assert pipeline._exception_reason(h, [], ev) is None


def test_real_service_po_still_goes_to_exception():
    ev = evmod.analyse("P.O. No : 8100007531 Date 01.10.2026\n")
    from app.services import pipeline
    h = {"po_number": "8100007531", "po_numbers_printed": ["8100007531"], "hsn_sac": "998313"}
    assert pipeline._exception_reason(h, [], ev)[0] == "service_po"


def test_known_vendor_gstin_snaps_ocr_slip():
    ev = evmod.analyse("GSTIN : 29AAICS4396P1Z9\n")          # OCR read J as I -> checksum fails
    h = {"po_number": "", "po_numbers_printed": [], "gstin": "", "field_sources": {}}
    evmod.apply(h, ev, ("27AABCK0618A",), ("29AAJCS4396P1Z9",))
    assert h["gstin"] == "29AAJCS4396P1Z9"


def test_slr_prompt_has_vendor_note():
    from app.services.prompts import build_invoice_prompt
    assert "handwritten_po_raw" in build_invoice_prompt("29AAJCS4396P1Z9") and "Mo. No." in build_invoice_prompt("29AAJCS4396P1Z9")


def test_known_vendor_snap_works_without_ocr():
    h = {"po_number": "", "po_numbers_printed": [], "gstin": "29AAJCS4396P1Z8", "field_sources": {}}
    evmod.apply(h, evmod.Evidence(), ("27AABCK0618A",), ("29AAJCS4396P1Z9",))
    assert h["gstin"] == "29AAJCS4396P1Z9"
