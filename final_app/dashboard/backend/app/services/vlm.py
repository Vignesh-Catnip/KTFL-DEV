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

# ---------------------------------------------------------------------------
# Prompts are managed in prompts.py — edit that file to tune extraction behaviour.
# ---------------------------------------------------------------------------
from .prompts import GRN_PROMPT, GRN_RETRY_HINT, PROMPT, UPRIGHT_PROMPT  # noqa: E402


class VlmClient(Protocol):
    def extract_invoice(self, images: list[str], text_layer: str) -> dict: ...
    def read_grn(self, images: list[str]) -> str: ...
    def page_is_upright(self, image_b64: str) -> bool | None: ...
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
        try:
            return extract_json(raw)
        except ValueError:
            log.warning("model answer is not JSON (%d chars, done_reason=%s): %s ... %s", len(raw),
                        data.get("done_reason"), raw[:200].replace("\n", " "), raw[-120:].replace("\n", " "))
            raise

    # -- interface -------------------------------------------------------------------------------
    def extract_invoice(self, images: list[str], text_layer: str) -> dict:
        prompt = PROMPT + ("\nOCR TEXT (may be noisy):\n" + text_layer[:2000] if text_layer else "")
        try:
            return self._ask(prompt, images[: self.cfg.main_pages])
        except Exception as exc:
            log.warning("main pass failed (%s) - retrying compact", exc)
            short = prompt + "\nIMPORTANT: keep it compact. At most 25 line items, item text max 40 characters."
            try:
                return self._ask(short, images[:2])
            except Exception as exc2:
                # last resort: header fields only, first page, no line items (they are taken from SQL later)
                log.warning("compact pass failed (%s) - header-only pass", exc2)
                head = PROMPT + ("\nIMPORTANT: return NO line items - \"lines\": []. Fill every other field. "
                                 "Keep the answer short.")
                return self._ask(head, images[:1], 1200)

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
