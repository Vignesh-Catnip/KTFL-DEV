"""Vision model (Ollama qwen3-vl) access. Prompts are the tuned ones from the extractor script.

`VlmClient` is a small interface so the pipeline can be tested with a fake model.
"""
from __future__ import annotations

import logging
import re
from typing import Protocol

import httpx

from ..config import get_settings
from .textutil import (IncompleteJson, clean, clean_grn, clean_po, extract_json, extract_json_strict, format_date,
                       grn_is_valid, gstin_ok, is_valid_po, match_gst_rate, parse_amount, repair_gstin,
                       split_double_gstin, to_float)

log = logging.getLogger("clearmatch.vlm")

# ---------------------------------------------------------------------------
# Prompts are managed in prompts.py — edit that file to tune extraction behaviour.
# ---------------------------------------------------------------------------
from .prompts import (GRN_PROMPT, GRN_RETRY_HINT, PAGE_CLASS_PROMPT, PROMPT_VERSION, UPRIGHT_PROMPT,  # noqa: E402
                      VENDOR_HINTS, build_invoice_prompt, page_header, prompt_hash)

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


_AMOUNT_KEYS = ("subtotal", "cgst", "sgst", "igst", "round_off", "other_charges", "total")
_LINE_AMOUNT_KEYS = ("quantity", "rate", "discount", "amount", "cgst", "sgst", "igst")


def _read_amount(raw, label: str, unread: list, errors: dict):
    """strict parse -> float, or None (unread) with the reason recorded. Never 0 for 'could not read'."""
    val, why = parse_amount(raw)
    if val is None:
        unread.append(label)
        if why != "missing":
            errors[label] = why
        return None
    return float(val)


def adapt_invoice(d: dict) -> dict:
    """Model JSON (values as printed, null when unreadable) -> the keys normalize() reads.
    No snapping, no calculating of missing values, no silent coercion: unreadable amounts stay None and are listed in
    `unread`; parse failures are listed in `parse_errors` with the reason; the printed text is kept in `raw_values`."""
    d = dict(d or {})
    unread: list[str] = []
    errors: dict[str, str] = {}
    pos: list[str] = []
    for field in ("po_number_raw", "handwritten_po_raw"):
        for tok in re.split(r"[,;]|\band\b", clean(d.get(field)), flags=re.I):
            po = clean_po(tok)
            if is_valid_po(po) and po not in pos:
                pos.append(po)
        if pos:
            break
    d["po_number"], d["po_numbers_all"] = (pos[0] if pos else ""), ",".join(pos)

    d["invoice_number"] = clean(d.get("invoice_number")).rstrip("/").strip()
    d["invoice_date"] = format_date(clean(d.get("invoice_date")).upper().replace("0CT", "OCT"))   # dd.mm.yyyy
    d["vendor"] = re.sub(r"\s*\(?\d{2,4}\s*-\s*\d{2,4}\)?\s*$", "", clean(d.get("vendor"))).strip()  # drop "(2026-27)"
    vc = clean(d.get("vendor_code"))
    d["vendor_code"] = "" if vc.upper() in _NO_VENDOR_CODE else vc

    d["raw_values"] = {k: d.get(k) for k in _AMOUNT_KEYS if d.get(k) is not None}
    vals = {k: _read_amount(d.get(k), k, unread, errors) for k in _AMOUNT_KEYS}
    d.update(vals)
    taxes = [vals[k] for k in ("cgst", "sgst", "igst") if vals[k] is not None]
    d["total_tax"] = round(sum(taxes), 2) if taxes else None          # sum of PRINTED taxes only, never a guess
    if d["total_tax"] is None:
        unread.append("total_tax")
    gp = _read_amount(d.get("gst_rate_printed"), "gst_rate_printed", [], errors)
    d["gst_rate_percent"] = gp                                        # as printed; never derived or snapped here
    d.setdefault("grn_number", "")                         # GRN comes from read_grn(), not from the main pass

    lines = []
    for n, l in enumerate([l for l in (d.get("lines") or []) if isinstance(l, dict)], 1):
        l = dict(l)
        lu: list[str] = []
        for k in _LINE_AMOUNT_KEYS:
            l[k] = _read_amount(l.get(k), f"line{n}.{k}", [], errors) if l.get(k) is not None else None
            if l[k] is None and k in ("quantity", "amount"):
                lu.append(k)
        l["unread"] = lu
        lines.append(l)
    d["lines"] = lines
    d["unread"] = sorted(set(unread))
    d["parse_errors"] = errors

    # confidence = how many self-consistency checks the read passed (the model's own 0.95 was a constant)
    sub, tax, total = vals["subtotal"], d["total_tax"], vals["total"]
    extra = (vals["round_off"] or 0.0) + (vals["other_charges"] or 0.0)
    conf = 0.95
    if not gstin_ok(repair_gstin(split_double_gstin(clean(d.get("gstin")).upper().replace(" ", "")))):
        conf -= 0.15
    if not pos:
        conf -= 0.15
    if None in (sub, tax, total) or abs(sub + tax + extra - total) > 2.0:
        conf -= 0.15
    if not lines:
        conf -= 0.15
    elif sub is not None and abs(sum((l.get("amount") or 0.0) for l in lines) - sub) > max(1.5, sub * 0.0005):
        conf -= 0.15
    d["confidence"] = round(max(conf, 0.2), 2)           # 0-1 like before
    return d


class VlmClient(Protocol):
    def extract_invoice(self, images: list[str], text_layer: str) -> dict: ...
    def read_grn(self, images: list[str]) -> str: ...
    def read_grn_candidates(self, images: list[str]) -> dict: ...      # {"grn", "grn_top_margin", "grn_stamp"}
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

    # -- strict structured call ------------------------------------------------------------------
    def _ask_raw(self, prompt: str, images: list[str], num_predict: int | None = None) -> tuple[dict | None, dict]:
        """One model call that never hides a problem. Returns (parsed | None, meta). meta carries the raw answer, the
        Ollama metrics and a status: OK / EMPTY / INCOMPLETE (done_reason=length or context full) / INVALID_JSON /
        TIMEOUT / ERROR. A truncated or broken answer is NOT repaired."""
        num_predict = num_predict or self.cfg.num_predict
        meta: dict = {"raw": "", "status": "OK", "error": None, "done_reason": None}
        data: dict = {}
        try:
            for use_format in (True, False):                  # an empty answer is retried once without forced JSON
                data = self._post_chat(prompt, images, num_predict, use_format=use_format)
                msg = data.get("message") or {}
                meta["raw"] = (msg.get("content") or msg.get("thinking") or "").strip()
                if meta["raw"]:
                    break
        except httpx.TimeoutException as exc:
            meta.update(status="TIMEOUT", error=f"model timeout after {self.cfg.vlm_timeout}s: {exc}")
            return None, meta
        except Exception as exc:
            meta.update(status="ERROR", error=f"{type(exc).__name__}: {exc}")
            return None, meta
        meta["done_reason"] = data.get("done_reason")
        ms = lambda k: round(data[k] / 1e6) if isinstance(data.get(k), (int, float)) else None     # ns -> ms
        meta["metrics"] = {"prompt_eval_count": data.get("prompt_eval_count"), "eval_count": data.get("eval_count"),
                           "total_duration_ms": ms("total_duration"), "load_duration_ms": ms("load_duration"),
                           "prompt_eval_duration_ms": ms("prompt_eval_duration"),
                           "eval_duration_ms": ms("eval_duration")}
        pec = data.get("prompt_eval_count")
        if isinstance(pec, int) and pec >= self.cfg.num_ctx - 8:
            meta.update(status="INCOMPLETE", error=f"context window full ({pec} of {self.cfg.num_ctx} tokens): "
                                                    f"the input may have been cut")
            return None, meta
        if not meta["raw"]:
            meta.update(status="EMPTY", error=f"empty response (done_reason={meta['done_reason']})")
            return None, meta
        if meta["done_reason"] == "length":
            meta.update(status="INCOMPLETE", error=f"output hit the token limit ({num_predict}) - answer is incomplete")
            return None, meta
        try:
            return extract_json_strict(meta["raw"]), meta
        except IncompleteJson as exc:
            meta.update(status="INVALID_JSON", error=str(exc))
            return None, meta

    def extract_invoice(self, images: list[str], text_layer: str) -> dict:
        """Reads EVERY page. Pages go to the model in labelled groups of `vlm_group_pages`; a group that comes back
        incomplete is retried page by page; a page that still fails is reported (page_info.pages_failed) and the
        invoice goes to REVIEW. Nothing is truncated or repaired to make it fit."""
        cfg = self.cfg
        total = len(images)
        base = build_invoice_prompt(_guess_seller_gstin(text_layer))
        phash = prompt_hash(base)
        runs: list[dict] = []
        results: list[tuple[int, int, dict]] = []
        failed: list[int] = []
        groups_info: list[dict] = []

        def run(first: int, last: int, extra: str = "") -> dict | None:
            prompt = page_header(first, last, total) + base + extra
            if text_layer and total <= 3:                     # the OCR hint is only a hint; pages are labelled by the images
                prompt += "\nOCR TEXT (may be noisy, may belong to other pages):\n" + text_layer[:2000]
            obj, meta = self._ask_raw(prompt, images[first - 1:last])
            runs.append({"call_kind": "INVOICE", "page_from": first, "page_to": last, "pages_total": total,
                         "model": cfg.model_vlm, "prompt_version": PROMPT_VERSION, "prompt_hash": phash,
                         "raw_response": meta["raw"], "parsed_json": obj, "done_reason": meta["done_reason"],
                         "status": meta["status"], "error": meta["error"], **(meta.get("metrics") or {})})
            if obj is None:
                log.warning("pages %s-%s of %s: %s - %s", first, last, total, meta["status"], meta["error"])
            return obj

        step = max(1, int(cfg.vlm_group_pages))
        for first in range(1, total + 1, step):
            last = min(total, first + step - 1)
            obj = run(first, last)
            attempts = 1
            if obj is None and last > first:                  # group failed -> smaller pieces, one page at a time
                for pno in range(first, last + 1):
                    sub = run(pno, pno)
                    attempts += 1
                    if sub is None:
                        sub = run(pno, pno)                   # one more try for a single page
                        attempts += 1
                    if sub is None:
                        failed.append(pno)
                    else:
                        results.append((pno, pno, sub))
            elif obj is None:
                obj = run(first, last)                        # single page: one more try
                attempts += 1
                if obj is None:
                    failed.append(first)
                else:
                    results.append((first, last, obj))
            else:
                results.append((first, last, obj))
            groups_info.append({"from": first, "to": last, "attempts": attempts})

        data, conflicts, legibility = merge_group_results(results)
        data = adapt_invoice(data)
        data["_page_info"] = {
            "pages_total": total, "pages_read": sorted({p for f, l, _ in results for p in range(f, l + 1)}),
            "pages_failed": sorted(failed), "groups": groups_info, "conflicts": conflicts,
            "illegible_pages": legibility, "prompt_version": PROMPT_VERSION, "prompt_hash": phash,
            "model": cfg.model_vlm,
        }
        data["_runs"] = runs
        return data

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


# ----------------------------------------------------------------------------------------------------------------
# Merging the answers of several page groups
# ----------------------------------------------------------------------------------------------------------------
_HEADER_TEXT_KEYS = ("vendor", "gstin", "invoice_number", "invoice_date", "due_date", "po_number_raw",
                     "handwritten_po_raw", "vendor_code", "plant_hint", "hsn_sac", "currency", "gst_rate_printed",
                     "handwritten_references")
_TOTAL_KEYS = ("subtotal", "cgst", "sgst", "igst", "round_off", "other_charges", "total")


def _same(a, b) -> bool:
    n = lambda v: re.sub(r"\s+", "", str(v)).casefold()
    return n(a) == n(b)


def merge_group_results(results: list[tuple[int, int, dict]]) -> tuple[dict, list[dict], list[int]]:
    """Combine the per-group answers into one invoice dict. Header text: first value read, a different later value is a
    conflict (-> REVIEW). Totals: all taken from ONE group - the last one that printed a grand total - never mixed.
    Lines: concatenated in page order, none removed. Returns (data, conflicts, illegible page numbers)."""
    out: dict = {k: None for k in _HEADER_TEXT_KEYS + _TOTAL_KEYS}
    out["lines"] = []
    conflicts: list[dict] = []
    illegible: list[int] = []
    totals_groups: list[tuple[int, dict]] = []
    for first, last, obj in sorted(results, key=lambda r: r[0]):
        for k in _HEADER_TEXT_KEYS:
            v = obj.get(k)
            if v in (None, ""):
                continue
            if out[k] in (None, ""):
                out[k] = v
            elif not _same(out[k], v) and k not in ("hsn_sac", "gst_rate_printed", "handwritten_references"):
                conflicts.append({"field": k, "first": str(out[k]), "later": str(v), "pages": f"{first}-{last}"})
        if any(obj.get(k) not in (None, "") for k in _TOTAL_KEYS):
            totals_groups.append((first, obj))
        for n, l in enumerate(obj.get("lines") or []):
            if isinstance(l, dict):
                l = dict(l)
                if l.get("page") in (None, "") and first == last:
                    l["page"] = first
                out["lines"].append(l)
        for pg in obj.get("pages") or []:
            if isinstance(pg, dict) and pg.get("legible") is False and isinstance(pg.get("page"), int):
                illegible.append(pg["page"])
    if totals_groups:
        with_total = [g for g in totals_groups if g[1].get("total") not in (None, "")]
        _, chosen = (with_total or totals_groups)[-1]
        for k in _TOTAL_KEYS:
            out[k] = chosen.get(k)
        distinct = {str(g[1].get("total")).replace(",", "").strip() for g in with_total}
        if len(distinct) > 1:
            conflicts.append({"field": "total", "first": str(with_total[0][1].get("total")),
                              "later": str(with_total[-1][1].get("total")),
                              "pages": ", ".join(str(g[0]) for g in with_total)})
    return out, conflicts, sorted(set(illegible))
