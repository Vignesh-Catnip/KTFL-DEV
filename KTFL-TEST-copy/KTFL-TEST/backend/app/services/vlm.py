"""Vision model (Ollama qwen3-vl) access. Prompts are the tuned ones from the extractor script.

`VlmClient` is a small interface so the pipeline can be tested with a fake model.
"""
from __future__ import annotations

import logging
import json
import re
from typing import Protocol

import httpx

from ..config import get_settings
from .textutil import (clean, clean_grn, clean_po, extract_json, format_date, grn_is_valid, gstin_ok, is_ktfl_po,
                       is_material_po, is_valid_po,
                       repair_gstin, snap_gst_rate, split_double_gstin, to_float)

log = logging.getLogger("clearmatch.vlm")

# ---------------------------------------------------------------------------
# Prompts are managed in prompts.py — edit that file to tune extraction behaviour.
# ---------------------------------------------------------------------------
from .prompts import (GRN_PROMPT, GRN_RETRY_HINT, PAGE_CLASS_PROMPT, UPRIGHT_PROMPT, VENDOR_HINTS,  # noqa: E402
                      build_invoice_prompt)

# ---------------------------------------------------------------------------
# NEW: the model now returns values AS PRINTED (see prompts.py). adapt_invoice() turns that into the keys the rest of
# the pipeline has always received (po_number, po_numbers_all, total_tax, gst_rate_percent, confidence ...),
# so nothing downstream has to change.
# ---------------------------------------------------------------------------
_NO_VENDOR_CODE = {"", "NA", "N/A", "NIL", "NONE", "-", "--"}
_GSTIN_IN_TEXT = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][0-9A-Z]Z[0-9A-Z]\b")


def _guess_seller_gstin(text_layer: str) -> str:
    """First GSTIN in the OCR text that has a vendor note in VENDOR_HINTS (None of KTFL's own is in that table)."""
    for g in _GSTIN_IN_TEXT.findall((text_layer or "").upper()):
        if g in VENDOR_HINTS:
            return g
    return ""


def _row_bad(l: dict) -> bool:
    q, r, a = to_float(l.get("quantity")), to_float(l.get("rate")), to_float(l.get("amount"))
    return bool(q and r and abs(q * r - a) > max(1.0, 0.002 * abs(a)))


def adapt_invoice(d: dict) -> dict:
    d = dict(d or {})
    # PO candidates, best first: printed material PO (51/61) > handwritten material PO > printed service PO (81/82/83)
    # > handwritten service PO. Anything that is not a KTFL PO prefix (phone numbers, e-way bill, CIN pieces) is dropped.
    # Vendors such as SLR print NO PO at all - the PO is written by hand ("PO - 5100002448") - while the model
    # can pick the vendor's phone number from the letterhead as the "printed PO".
    printed: list[str] = []
    hand: list[str] = []
    for field, bucket in (("po_number_raw", printed), ("handwritten_po_raw", hand)):
        for tok in re.split(r"[,;]|\band\b", clean(d.get(field)), flags=re.I):
            po = clean_po(tok)
            if is_ktfl_po(po) and po not in bucket:
                bucket.append(po)
    for m in re.finditer(r"(?<!\d)((?:51|61|81|82|83)\d{8})(?!\d)", clean(d.get("handwritten_references"))):
        if m.group(1) not in hand:
            hand.append(m.group(1))
    pos = ([p for p in printed if is_material_po(p)] + [p for p in hand if is_material_po(p)]
           + [p for p in printed if not is_material_po(p)] + [p for p in hand if not is_material_po(p)])
    pos = list(dict.fromkeys(pos))
    d["po_number"], d["po_numbers_all"] = (pos[0] if pos else ""), ",".join(pos)

    d["invoice_number"] = clean(d.get("invoice_number")).rstrip("/").strip()
    d["invoice_date"] = format_date(clean(d.get("invoice_date")).upper().replace("0CT", "OCT"))   # dd.mm.yyyy
    d["vendor"] = re.sub(r"\s*\(?\d{2,4}\s*-\s*\d{2,4}\)?\s*$", "", clean(d.get("vendor"))).strip()  # drop "(2026-27)"
    vc = clean(d.get("vendor_code"))
    d["vendor_code"] = "" if vc.upper() in _NO_VENDOR_CODE else vc

    sub, c, s, g = (to_float(d.get(k)) for k in ("subtotal", "cgst", "sgst", "igst"))
    total = to_float(d.get("total"))
    tax = round(c + s + g, 2)
    d["total_tax"] = tax
    d["gst_rate_percent"] = snap_gst_rate(tax / sub * 100) if sub > 0 else None
    d.setdefault("grn_number", "")                         # GRN comes from read_grn(), not from the main pass

    # confidence = how many self-consistency checks the read passed (the model's own 0.95 was a constant)
    lines = [l for l in (d.get("lines") or []) if isinstance(l, dict)]
    d["lines"] = lines
    extra = to_float(d.get("round_off")) + to_float(d.get("other_charges"))
    conf = 0.95
    if not gstin_ok(repair_gstin(split_double_gstin(clean(d.get("gstin")).upper().replace(" ", "")))):
        conf -= 0.15
    if not pos:
        conf -= 0.15
    if sub <= 0 or total <= 0 or abs(sub + tax + extra - total) > 2.0:
        conf -= 0.15
    if not lines:
        conf -= 0.15
    else:
        if sub > 0 and abs(sum(to_float(l.get("amount")) for l in lines) - sub) > max(1.5, sub * 0.0005):
            conf -= 0.15
        if any(_row_bad(l) for l in lines):
            conf -= 0.10
    d["confidence"] = round(max(conf, 0.2), 2)           # 0-1 like before
    return d


class VlmClient(Protocol):
    def extract_invoice(self, images: list[str], text_layer: str) -> dict: ...
    def read_grn(self, images: list[str]) -> str: ...
    def read_grn_candidates(self, images: list[str]) -> dict: ...      # NEW: {"grn", "grn_top_margin", "grn_stamp"}
    def page_is_upright(self, image_b64: str) -> bool | None: ...
    def read_totals(self, images: list[str]) -> dict: ...
    def read_handwritten_po(self, images: list[str]) -> str: ...
    def read_region(self, image_b64: str) -> dict: ...
    def number_label(self, images: list[str], number: str) -> str: ...
    def read_seller_gstins(self, images: list[str]) -> list[dict]: ...
    def find_numbers(self, image_b64: str) -> list[dict]: ...
    def health(self) -> tuple[bool, str]: ...


class OllamaVlm:
    """/api/chat with think=False (qwen3-vl is a thinking model: without it the tokens are spent on
    thinking and the answer comes back empty). Retries once without forced JSON format."""

    def __init__(self) -> None:
        self.cfg = get_settings()

    # -- low level -------------------------------------------------------------------------------
    def _post_chat(self, prompt: str, images: list[str], num_predict: int, use_format: bool) -> dict:
        payload = {
            "model": self.cfg.model_vlm,
            "messages": [{"role": "user", "content": prompt, "images": images}],
            "stream": False, "think": False, "keep_alive": "30m",
            "options": {"temperature": 0, "num_ctx": self.cfg.num_ctx, "num_predict": num_predict},
        }
        if use_format:
            payload["format"] = "json"
        r = httpx.post(f"{self.cfg.ollama_url.rstrip('/')}/api/chat", json=payload,
                       timeout=self.cfg.vlm_timeout, trust_env=False)
        r.raise_for_status()
        return r.json()

    def _ask_raw(self, prompt: str, images: list[str], num_predict: int | None = None) -> tuple[str, dict]:
        """The model's answer as TEXT (no JSON parsing) + the Ollama response."""
        num_predict = num_predict or self.cfg.num_predict
        data = self._post_chat(prompt, images, num_predict, use_format=True)
        msg = data.get("message") or {}
        raw = (msg.get("content") or msg.get("thinking") or "").strip()
        if not raw:
            data = self._post_chat(prompt, images, num_predict, use_format=False)
            msg = data.get("message") or {}
            raw = (msg.get("content") or msg.get("thinking") or "").strip()
        if not raw:
            raise ValueError(f"Ollama returned an empty response (done_reason={data.get('done_reason')})")
        return raw, data

    def _ask(self, prompt: str, images: list[str], num_predict: int | None = None) -> dict:
        raw, data = self._ask_raw(prompt, images, num_predict)
        try:
            return extract_json(raw)
        except ValueError:
            log.warning("model answer is not JSON (%d chars, done_reason=%s): %s ... %s", len(raw),
                        data.get("done_reason"), raw[:200].replace("\n", " "), raw[-120:].replace("\n", " "))
            raise

    # -- interface -------------------------------------------------------------------------------
    def classify_pages(self, images: list[str]) -> list[str]:
        """NEW: page_type of every page (PAGE_CLASS_PROMPT). A page the model cannot classify is kept."""
        out = []
        for img in images:
            try:
                out.append(str(self._ask(PAGE_CLASS_PROMPT, [img], 64).get("page_type") or "other"))
            except Exception as exc:
                log.warning("page classification failed: %s", exc)
                out.append("invoice_first")
        return out

    def _invoice_pages(self, images: list[str]) -> list[str]:
        """NEW, opt-in (settings.classify_pages = True): drop weighment slips, debit notes, LRs and challans before the
        main pass. Off by default because it adds one small model call per page."""
        if not getattr(self.cfg, "classify_pages", False) or len(images) <= 1:
            return images
        keep = [im for im, t in zip(images, self.classify_pages(images)) if t in ("invoice_first", "invoice_continuation")]
        return keep or images

    def extract_invoice(self, images: list[str], text_layer: str) -> dict:
        base = build_invoice_prompt(_guess_seller_gstin(text_layer))        # vendor note only when the vendor is known
        prompt = base + ("\nOCR TEXT (may be noisy):\n" + text_layer[:2000] if text_layer else "")
        pages = self._invoice_pages(images)
        try:
            data = self._ask(prompt, pages[: self.cfg.main_pages])
        except Exception as exc:
            log.warning("main pass failed (%s) - retrying compact", exc)
            short = prompt + "\nIMPORTANT: keep it compact. At most 25 line items, item text max 40 characters."
            try:
                data = self._ask(short, pages[:2])
            except Exception as exc2:
                # last resort: header fields only, first page, no line items (they are taken from SQL later)
                log.warning("compact pass failed (%s) - header-only pass", exc2)
                head = base + ("\nIMPORTANT: return NO line items - \"lines\": []. Fill every other field. "
                               "Keep the answer short.")
                data = self._ask(head, pages[:1], 1200)
        return adapt_invoice(data)

    def page_is_upright(self, image_b64: str) -> bool | None:
        """True / False = the model's yes/no on 'is the text upright?'; None = no usable answer."""
        try:
            v = self._ask(UPRIGHT_PROMPT, [image_b64], 24).get("upright")
        except Exception as exc:
            log.warning("page orientation not understood: %s", exc)
            return None
        if isinstance(v, str):
            v = v.strip().lower() in ("true", "yes")
        return bool(v) if v is not None else None

    def read_grn_candidates(self, images: list[str], max_retry: int = 2) -> dict:
        """NEW: reads BOTH places a GRN can be (handwritten top margin, receiving stamp). Pages are scanned until both
        are found, so a top-margin GRN on page 1 and a stamp GRN on page 2 can be compared.
        Returns {"grn": chosen, "grn_top_margin": valid-or-"", "grn_stamp": valid-or-""}; top margin wins (as before)."""
        keys = ("grn_top_margin", "grn_stamp")
        found = {k: "" for k in keys}
        first_bad = ""
        for pno, img in enumerate(images, 1):
            prompt = GRN_PROMPT
            for _ in range(max_retry + 1):
                try:
                    ans = self._ask(prompt, [img], 128)
                except Exception as exc:
                    log.warning("GRN page %s error: %s", pno, exc)
                    break
                bad = None
                for k in keys:
                    raw = clean(ans.get(k))
                    if not raw or found[k]:
                        continue
                    c = clean_grn(raw)
                    if grn_is_valid(c):
                        found[k] = c
                    elif bad is None:
                        bad = (k, raw, c)
                        first_bad = first_bad or raw
                if bad is None:
                    break
                k, raw, c = bad
                where = "the top margin" if k == "grn_top_margin" else "the receiving stamp"
                prompt = GRN_PROMPT + GRN_RETRY_HINT.format(location=where, bad=raw, n=len(c))
            if all(found.values()):
                break
        return {"grn": found["grn_top_margin"] or found["grn_stamp"] or first_bad, **found}

    def read_grn(self, images: list[str], max_retry: int = 2) -> str:
        return self.read_grn_candidates(images, max_retry)["grn"]

    def read_handwritten_po(self, images: list[str]) -> str:
        """Second look for a PO that is written BY HAND (many vendors print none). Returns a material PO (51/61 + 8
        digits) or ''. Only the model reads handwriting; the result is validated, never trusted blindly."""
        from .prompts import HANDWRITTEN_PO_PROMPT
        for img in images:
            try:
                ans = self._ask(HANDWRITTEN_PO_PROMPT, [img], 256)
            except Exception as exc:
                log.warning("handwritten PO read failed: %s", exc)
                continue
            for k in ("handwritten_po", "po"):
                po = clean_po(ans.get(k))
                if is_material_po(po):
                    return po
        return ""

    @staticmethod
    def _enhance(image_b64: str) -> str:
        """Grey + auto-contrast + sharpen: handwriting in pale blue ink on a stamp becomes much easier to read."""
        import base64
        import io
        try:
            from PIL import Image, ImageFilter, ImageOps
            im = Image.open(io.BytesIO(base64.b64decode(image_b64))).convert("L")
            im = ImageOps.autocontrast(im, cutoff=1).filter(ImageFilter.UnsharpMask(radius=2, percent=160, threshold=2))
            buf = io.BytesIO()
            im.save(buf, "PNG")
            return base64.b64encode(buf.getvalue()).decode()
        except Exception as exc:                                    # never let the helper break the scan
            log.warning("lens: image enhancement skipped: %s", exc)
            return image_b64

    @staticmethod
    def _digit_runs(raw: str) -> list[str]:
        text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL | re.I)
        nums: list[str] = []
        for m in re.finditer(r"\d[\d \-/.]{4,}\d|\d{6,}", text):
            d = re.sub(r"\D", "", m.group(0))
            if len(d) >= 6 and d not in nums:
                nums.append(d)
        return nums

    def read_region(self, image_b64: str) -> dict:
        """Lens: the numbers inside a box the person drew on the invoice.
        Works on the model's TEXT (JSON, fenced JSON, a plain number or a sentence all work - every run of 6+ digits counts).
        Handwriting is read up to 3 times: when the first answer is not a 10-digit PO / GRN (the model often drops repeated
        zeros: 5000410882 -> 510882) it is told what it answered and how many digits are expected, and sees a contrast-
        enhanced copy of the crop. 'raw' = what the model said (shown in the panel); 'error' = the call itself failed."""
        from .prompts import LENS_PROMPT
        from .textutil import grn_is_valid, is_material_po, is_service_po, is_valid_po

        def good(n: str) -> bool:
            return grn_is_valid(n) or is_material_po(n) or (is_valid_po(n) and is_service_po(n))

        nums: list[str] = []
        raws: list[str] = []
        label = ""
        enhanced = None
        prompt, img = LENS_PROMPT, image_b64
        for attempt in range(3):
            try:
                raw, _ = self._ask_raw(prompt, [img], 256)
            except Exception as exc:
                log.warning("lens read failed: %s", exc)
                if not raws:
                    return {"numbers": [], "label": "", "raw": f"error: {exc}"[:300], "error": True}
                break
            raws.append(raw)
            found = self._digit_runs(raw)
            for n in found:
                if n not in nums:
                    nums.append(n)
            try:
                label = label or str(extract_json(re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL | re.I)).get("label") or "")
            except Exception:
                pass
            log.info("lens read (try %d): model answered %r -> numbers %s", attempt + 1, raw[:200], found)
            if any(good(n) for n in nums):
                break
            seen = ", ".join(found) if found else "nothing"
            prompt = (LENS_PROMPT + f"\nYour previous answer was: {seen}. That is NOT a complete 10-digit number. "
                      "Look again at every digit from left to right, count them, and do not skip repeated digits "
                      "(handwritten zeros such as 5000 are often missed). The number has exactly 10 digits.")
            if enhanced is None:
                enhanced = self._enhance(image_b64)
            img = enhanced if attempt == 0 else image_b64
        nums.sort(key=lambda n: 0 if good(n) else 1)
        return {"numbers": nums, "label": label, "raw": " | ".join(r.replace("\n", " ") for r in raws)[:300]}

    def number_label(self, images: list[str], number: str) -> str:
        """The label printed beside `number` ('PO No', 'Mo. No.', 'DC No' ...). '' when unknown / not found."""
        from .prompts import NUMBER_LABEL_PROMPT
        for img in images:
            try:
                ans = self._ask(NUMBER_LABEL_PROMPT.format(number=number), [img], 128)
            except Exception as exc:
                log.warning("number label read failed: %s", exc)
                continue
            if ans.get("found") in (True, "true", "True") and str(ans.get("label") or "").strip():
                return str(ans["label"]).strip()
        return ""

    def read_seller_gstins(self, images: list[str]) -> list[dict]:
        """[{'gstin': .., 'role': ..}] from the upper part of the invoice; only checksum-valid GSTINs are returned."""
        from .prompts import GSTIN_PROMPT
        out: list[dict] = []
        for img in images:
            try:
                ans = self._ask(GSTIN_PROMPT, [img], 384)
            except Exception as exc:
                log.warning("GSTIN re-read failed: %s", exc)
                continue
            for it in ans.get("gstins") or []:
                g = re.sub(r"[^0-9A-Z]", "", str((it or {}).get("gstin") or "").upper())
                if gstin_ok(g) and g not in [o["gstin"] for o in out]:
                    out.append({"gstin": g, "role": str(it.get("role") or "").lower()})
        return out

    def find_numbers(self, image_b64: str) -> list[dict]:
        """Lens: PO / GRN numbers on one page with their position (fractions 0..1 of the image), found by the model.
        Qwen3-VL normally answers boxes in 0..1000 relative coordinates; 0..1 fractions are accepted too. Tolerant about
        the JSON shape (list or {"boxes": [...]}, "bbox" / "bbox_2d" / "box", "value" / "number"). [] on any error."""
        from .prompts import LENS_FIND_PROMPT
        try:
            raw, _ = self._ask_raw(LENS_FIND_PROMPT, [image_b64], 768)
        except Exception as exc:
            log.warning("lens find failed: %s", exc)
            return []
        text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL | re.I)
        items: list = []
        try:
            data = json.loads(re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.I).strip())
        except Exception:
            try:
                data = extract_json(text)
            except Exception:
                data = None
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            for k in ("boxes", "numbers", "items", "results", "detections"):
                if isinstance(data.get(k), list):
                    items = data[k]
                    break
            else:
                items = [data] if (data.get("bbox") or data.get("bbox_2d")) else []
        out = []
        for b in items:
            if not isinstance(b, dict):
                continue
            try:
                v = re.sub(r"\D", "", str(b.get("value") or b.get("number") or b.get("text") or b.get("label") or ""))
                box = b.get("bbox") or b.get("bbox_2d") or b.get("box") or b.get("bounding_box")
                x1, y1, x2, y2 = (float(c) for c in box)
            except (TypeError, ValueError):
                continue
            scale = 1.0 if max(x1, y1, x2, y2) <= 1.0 else 1000.0           # fractions or Qwen's 0..1000 grid
            x1, y1, x2, y2 = (max(0.0, min(1.0, c / scale)) for c in (x1, y1, x2, y2))
            if len(v) == 10 and x2 > x1 and y2 > y1:
                out.append({"value": v, "x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1})
        log.info("lens find: model answered %r -> %d box(es)", raw[:200], len(out))
        return out

    def read_totals(self, images: list[str]) -> dict:
        """Focused re-read of the totals box (bottom half of each page, high resolution). {} on any error."""
        from .prompts import TOTALS_PROMPT
        out: dict = {}
        for img in reversed(images):                       # the totals sit on the LAST invoice page
            try:
                ans = self._ask(TOTALS_PROMPT, [img], 512)
            except Exception as exc:
                log.warning("totals re-read failed: %s", exc)
                continue
            vals = {k: to_float(ans.get(k)) for k in ("subtotal", "cgst", "sgst", "igst", "round_off", "other_charges", "total")}
            if vals.get("total") and vals.get("subtotal"):
                return vals
            out = out or vals
        return out

    def health(self) -> tuple[bool, str]:
        try:
            r = httpx.get(f"{self.cfg.ollama_url.rstrip('/')}/api/tags", timeout=8, trust_env=False)
            r.raise_for_status()
            names = [m.get("name") for m in r.json().get("models", [])]
            if self.cfg.model_vlm in names:
                return True, f"{self.cfg.model_vlm} ready"
            return False, f"model {self.cfg.model_vlm} not installed on {self.cfg.ollama_url}"
        except Exception as exc:
            return False, f"Ollama not reachable at {self.cfg.ollama_url}: {exc}"


_client: VlmClient | None = None


def get_vlm() -> VlmClient:
    global _client
    if _client is None:
        _client = OllamaVlm()
    return _client


def set_vlm(client: VlmClient | None) -> None:
    """Tests inject a fake model here."""
    global _client
    _client = client
