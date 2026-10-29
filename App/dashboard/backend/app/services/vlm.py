"""Vision model (Ollama qwen3-vl) access. Prompts are the tuned ones from the extractor script.

`VlmClient` is a small interface so the pipeline can be tested with a fake model.
"""
from __future__ import annotations

import logging
from typing import Protocol

import httpx

from ..config import get_settings
from .textutil import clean, clean_grn, extract_json, grn_is_valid

log = logging.getLogger("clearmatch.vlm")

GRN_PROMPT = r"""
You are looking at ONE invoice image. Your ONLY job is to find the GRN number from the receiving/inward stamp.

STEP 1 — Find the receiving stamp (usually a "KALYANI TECHNOFORGE LTD." rubber stamp, bottom of page).
STEP 2 — Find the row labelled "GRN NO", "GRN No.", "GRN", "JRN NO", "Inward No", "MRN No".
         It is normally the row BELOW "SCROLL NO".
STEP 3 — Extract the handwritten number EXACTLY as written (keep dash / dots / spaces).
STEP 4 — A GRN always starts with "50" and has EXACTLY 10 digits.
         "50-343863" means 50 + 00 + 343863 = 5000343863. Count the digits carefully.

NEVER extract from: SCROLL NO (starts with 200...), CH NO, Challan No, E-way Bill No,
Vehicle No, Gate Pass No, Delivery Note No, Buyer's Order No.

Return ONLY: {"grn_number": "<value or empty string>"}
"""

GRN_RETRY_HINT = """
Your previous answer was "{bad}" which cleans to "{bad_clean}" ({n} digits).
A valid GRN has EXACTLY 10 digits and starts with "50". Re-read the handwriting stroke by stroke.
"""

PROMPT = r"""
You are an Indian GST purchase-invoice extraction engine. The BUYER is Kalyani Technoforge Ltd (KTFL).
Read the invoice IMAGES. Return ONLY valid JSON.

FIELD RULES
- vendor         : the SELLER company printed at the top. NEVER "Kalyani Technoforge".
- invoice_number : value under "Invoice No." (e.g. "428/26-27", "AB2627143", "BM/26-27/1328").
- invoice_date   : the "Dated" value next to the invoice number.
- po_number      : "Buyer's Order No." — KTFL POs are 10 digits starting 81 or 51 (e.g. 8100007531).
                   Strip any "PO-" prefix. If MORE THAN ONE different PO is printed anywhere on the
                   invoice, list every one of them comma-separated in "po_numbers_all".
- vendor_code    : "Vendor Code" printed on the invoice, if present.
- gstin          : the SELLER's GSTIN (15 chars). Never KTFL's own GSTIN (starts 27AABCK0618A).
- hsn_sac        : the HSN/SAC code of the goods (4–8 digits) EXACTLY as printed on the invoice.
                   If the invoice does not print an HSN/SAC code, return "" - never guess one.
- plant / ship-to: if a KTFL plant code or unit is printed (e.g. "Plant: 1200"), put it in "plant_hint".
- Amounts        : ₹6,19,763.14 -> 619763.14  (remove ₹ and thousands separators, keep the decimal).
- subtotal       : total taxable value BEFORE GST.
- cgst/sgst/igst : the GST AMOUNTS in rupees, not the percentages.
- gst_rate_percent: the GST RATE printed on the invoice as a number (18 for 9%+9% or 18%). 0 if none.
- total          : the final payable invoice total.
- grn_number     : always "" (it is extracted separately).
- Ignore Delivery Challan / E-way bill / weighment slip pages when collecting line items.

LINE ITEMS: only the invoice's own goods rows that carry an amount. No duplicate rows.

Return exactly:
{
  "vendor": "", "invoice_number": "", "invoice_date": "", "po_number": "",
  "po_numbers_all": "", "gstin": "", "hsn_sac": "", "vendor_code": "", "plant_hint": "",
  "currency": "INR", "subtotal": 0.0, "cgst": 0.0, "sgst": 0.0, "igst": 0.0,
  "gst_rate_percent": 0.0, "total_tax": 0.0, "total": 0.0,
  "grn_number": "", "handwritten_references": "", "confidence": 0.0,
  "lines": [{"item": "", "quantity": 0.0, "uom": "", "rate": 0.0, "hsn_sac": "", "amount": 0.0}]
}
"""


class VlmClient(Protocol):
    def extract_invoice(self, images: list[str], text_layer: str) -> dict: ...
    def read_grn(self, images: list[str]) -> str: ...
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

    def _ask(self, prompt: str, images: list[str], num_predict: int | None = None) -> dict:
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
        return extract_json(raw)

    # -- interface -------------------------------------------------------------------------------
    def extract_invoice(self, images: list[str], text_layer: str) -> dict:
        prompt = PROMPT + ("\nOCR TEXT (may be noisy):\n" + text_layer[:2000] if text_layer else "")
        try:
            return self._ask(prompt, images[: self.cfg.main_pages])
        except Exception as exc:
            log.warning("main pass failed (%s) - retrying compact", exc)
            short = prompt + "\nIMPORTANT: keep it compact. At most 25 line items, item text max 40 characters."
            return self._ask(short, images[:2])

    def read_grn(self, images: list[str], max_retry: int = 2) -> str:
        best = ""
        for pno, img in enumerate(images, 1):
            prompt = GRN_PROMPT
            for _ in range(max_retry + 1):
                try:
                    grn = clean(self._ask(prompt, [img], 96).get("grn_number"))
                except Exception as exc:
                    log.warning("GRN page %s error: %s", pno, exc)
                    grn = ""
                if not grn:
                    break
                c = clean_grn(grn)
                if grn_is_valid(c):
                    return grn
                best = best or grn
                prompt = GRN_PROMPT + GRN_RETRY_HINT.format(bad=grn, bad_clean=c, n=len(c))
        return best

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
