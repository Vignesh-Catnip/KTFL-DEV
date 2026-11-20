"""'Lens': read a PO / GRN number from a box drawn on the digital invoice."""
from app.routers import invoices as invrouter
from app.services import lens, pdfio
from tests.conftest import FIX
from tests.test_api import client, upload  # noqa: F401  (fixture + helper)


def test_classify():
    assert lens.classify("5000396179") == "GRN"
    assert lens.classify("5100002448") == "PO"
    assert lens.classify("6100002448") == "PO"
    assert lens.classify("8100002448") == "SERVICE_PO"
    assert lens.classify("8394294061") == "SERVICE_PO"      # looks like 83... - the page context (phone) is checked elsewhere
    assert lens.classify("123456789") == "OTHER"


def test_build_candidates_merges_ocr_and_ai():
    c = lens.build_candidates("PO - 5100 002448 dated 28-09-2026", ["5100002448", "9000396179"])
    by = {x["value"]: x for x in c}
    assert by["5100002448"]["sources"] == ["OCR", "AI"] and by["5100002448"]["kind"] == "PO"
    assert by["5000396179"]["kind"] == "GRN" and by["5000396179"]["corrected_from"] == "9000396179"   # 9000... -> 5000...
    assert c[0]["kind"] in ("PO", "GRN")                                                              # valid ones first


def test_crop_follows_page_rotation(tmp_path):
    import io

    import pymupdf
    from PIL import Image
    src = tmp_path / "r.pdf"
    d = pymupdf.open(str(FIX / "inv_dsk_428.pdf"))
    d[0].set_rotation(90)
    d.save(str(src))
    full = Image.open(io.BytesIO(pdfio.render_page_png(src, 1, 1.0))).convert("L")
    crop = Image.open(io.BytesIO(pdfio.render_crop_png(src, 1, 0.0, 0.0, 0.5, 0.5, min_width=100))).convert("L")
    assert crop.width > 0 and crop.height > 0
    ratio = crop.width / crop.height
    assert abs(ratio - (full.width / 2) / (full.height / 2)) < 0.1          # crop has the shape of the displayed quarter


def test_lens_endpoint_value_and_region(client, monkeypatch):   # noqa: F811
    upload(client)
    inv = client.get("/api/invoices").json()["items"][0]["id"]

    class AiReader:
        def read_region(self, b64):
            assert len(b64) > 100
            return {"numbers": ["5100007531"], "label": "PO"}
    monkeypatch.setattr(invrouter, "get_vlm", lambda: AiReader())

    r = client.post(f"/api/invoices/{inv}/lens", json={"page": 1, "x": 0.1, "y": 0.1, "w": 0.3, "h": 0.05})
    assert r.status_code == 200, r.text
    c = r.json()["candidates"]
    assert c and c[0]["value"] == "5100007531" and c[0]["kind"] == "PO" and c[0]["sources"] == ["AI"]
    assert c[0]["sap"]["checked"] and c[0]["sap"]["in_sap"] is True

    assert r.json()["crop"].startswith("iVBOR")                                    # captured area (PNG) comes back for comparison

    r = client.post(f"/api/invoices/{inv}/lens", json={"page": 1, "value": "5000343863"})
    assert r.json()["candidates"][0]["kind"] == "GRN" and r.json()["crop"] == ""
    r = client.post(f"/api/invoices/{inv}/lens", json={"page": 1, "value": "5000343863", "x": 0.2, "y": 0.2, "w": 0.2, "h": 0.04})
    assert r.json()["candidates"][0]["kind"] == "GRN" and r.json()["crop"]         # outlined number: value + the area picture

    assert client.post(f"/api/invoices/{inv}/lens", json={"page": 1, "x": 0.1, "y": 0.1, "w": 0.0001, "h": 0.0001}).status_code == 422
    assert client.post(f"/api/invoices/{inv}/lens", json={"page": 9, "value": "5100007531"}).status_code == 404
    assert client.get(f"/api/invoices/{inv}/page/1/numbers").json() == {"available": False, "ai": False, "boxes": []}


# ------------------------------------------------------------------ Qwen-only mode (no Tesseract)
def test_ai_number_finder_endpoint(client, monkeypatch):   # noqa: F811
    upload(client)
    inv = client.get("/api/invoices").json()["items"][0]["id"]

    class Finder:
        def find_numbers(self, b64):
            assert len(b64) > 100
            return [{"value": "5100007531", "x": 0.6, "y": 0.3, "w": 0.2, "h": 0.03},
                    {"value": "9876543210", "x": 0.1, "y": 0.1, "w": 0.1, "h": 0.02}]       # not a PO / GRN pattern -> dropped
    monkeypatch.setattr(invrouter, "get_vlm", lambda: Finder())
    r = client.get(f"/api/invoices/{inv}/page/1/numbers?ai=true").json()
    assert r["ai"] is True and [b["value"] for b in r["boxes"]] == ["5100007531"] and r["boxes"][0]["kind"] == "PO"


def test_qwen_vlm_helpers_parse_answers(monkeypatch):
    from app.services import vlm as vlmmod
    v = vlmmod.OllamaVlm()
    answers = iter([{"boxes": [{"value": "5100002448", "bbox": [610, 300, 830, 340]}, {"value": "12", "bbox": [0, 0, 1, 1]}]},
                    {"found": True, "label": "Mo. No."},
                    {"gstins": [{"gstin": "29AAJCS4396P1Z9", "role": "seller"}, {"gstin": "27AABCK0618A1Z7", "role": "buyer"},
                                {"gstin": "29AAJCS4396P1Z8", "role": "seller"}]}])
    monkeypatch.setattr(v, "_ask", lambda prompt, images, n=None: next(answers))
    b = v.find_numbers("x")
    assert len(b) == 1 and abs(b[0]["x"] - 0.61) < 1e-9 and abs(b[0]["w"] - 0.22) < 1e-9
    assert v.number_label(["x"], "8394294061") == "Mo. No."
    g = v.read_seller_gstins(["x"])
    assert [x["gstin"] for x in g] == ["29AAJCS4396P1Z9", "27AABCK0618A1Z7"]          # checksum-invalid one dropped


def test_no_ocr_phone_number_po_is_vetoed_by_model_label(db):
    from app.services import evidence as evmod, pipeline
    class V:
        def read_handwritten_po(self, images): return ""
        def number_label(self, images, number): return "Mo. No."
    h = {"po_number": "8394294061", "po_numbers_printed": ["8394294061"], "po_numbers_all": "8394294061", "hsn_sac": "72283019",
         "field_sources": {}}
    ev = evmod.Evidence()                                   # no OCR
    pipeline._veto_service_po_by_model(V(), ["img"], h, ev, "x.pdf")
    assert h["po_number"] == "" and pipeline._exception_reason(h, [], ev) is None


def test_no_ocr_real_service_po_still_goes_to_exception(db):
    from app.services import evidence as evmod, pipeline
    class V:
        def read_handwritten_po(self, images): return ""
        def number_label(self, images, number): return "PO No"
    h = {"po_number": "8100007531", "po_numbers_printed": ["8100007531"], "po_numbers_all": "8100007531", "hsn_sac": "998313",
         "field_sources": {}}
    ev = evmod.Evidence()
    pipeline._veto_service_po_by_model(V(), ["img"], h, ev, "x.pdf")
    assert pipeline._exception_reason(h, [], ev)[0] == "service_po"


def test_gstin_header_reread_without_ocr(db, monkeypatch):
    from app.services import pipeline
    class V:
        def read_seller_gstins(self, images): return [{"gstin": "29AAJCS4396P1Z9", "role": "seller"}, {"gstin": "27AABCK0618A1Z7", "role": "buyer"}]
    h = {"gstin": "29AAJCS4396P1Z8", "field_sources": {}}
    monkeypatch.setattr(pipeline.pdfio, "band_images", lambda *a, **k: ["img"])
    pipeline._regstin_by_model(V(), "x.pdf", h, ("27AABCK0618A",), "x.pdf")
    assert h["gstin"] == "29AAJCS4396P1Z9"
