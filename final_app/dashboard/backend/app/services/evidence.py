"""OCR evidence: an independent reading of the printed text, used to CHECK / correct what the vision model returned.

The model reads layout and handwriting well but sometimes mis-reads small printed IDs (GSTIN digits, PO digits) or picks
a number from the wrong box (SLR prints the vendor's phone number 8394294215 in the letterhead - it starts with 83 and
looks like a service PO). Tesseract on a 3000 px render reads printed IDs reliably, and a GSTIN carries its own
checksum, so the two readings can be compared:

  * seller GSTIN invalid / missing  -> snap to the checksum-valid GSTIN printed on the page (never the buyer's)
  * PO not printed anywhere but one OCR candidate is 1 digit away -> snap
  * a number printed after "Mo. No. / Ph. No. / Tel" is a phone number, never a PO
  * a service PO / SAC 99 reading is accepted for the exception folder only when the page really contains it

Optional: needs the `tesseract` binary; without it every function degrades to "no evidence" (nothing is changed).
"""
from __future__ import annotations

import base64
import io
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from ..config import get_settings
from . import orient, pdfio
from .textutil import KTFL_PO_PREFIXES, gstin_ok

log = logging.getLogger("clearmatch.evidence")

_GSTIN = re.compile(r"(?<![0-9A-Z])\d{2}[A-Z]{5}\d{4}[A-Z][0-9A-Z]Z[0-9A-Z](?![0-9A-Z])")
_PO = re.compile(r"(?<!\d)(?:51|61|81|82|83)\d{8}(?!\d)")
_PHONE_CTX = re.compile(r"(mo\.?\s*no|mobile|ph\.?\s*no|phone|tel\b|telephone|contact|fax|cell|whatsapp)", re.I)
_TEN = re.compile(r"(?<!\d)\d{10}(?!\d)")
# lines that carry OTHER 10-digit ids (delivery challan, e-way bill, IRN, LR, shipment ...): a number there is not a PO
_OTHER_CTX = re.compile(r"(\bDC\b|d\.?c\.?\s*no|delivery|challan|way\s*bill|e-?way|\birn\b|\back\b|\blr\b|shipment|weighment|"
                        r"\bslip\b|\bso\b|sales\s*order|vehicle|lorry|\bgr\b|\brr\b|docket|consignment)", re.I)


@dataclass
class Evidence:
    available: bool = False
    gstins: list[str] = field(default_factory=list)          # checksum-valid GSTINs, order of appearance
    gstins_bad: list[str] = field(default_factory=list)      # GSTIN-shaped but checksum fails (OCR slip)
    pos: list[str] = field(default_factory=list)             # KTFL-prefix 10-digit numbers that are NOT phone numbers
    phones: set[str] = field(default_factory=set)
    others: set[str] = field(default_factory=set)            # KTFL-prefix numbers printed as DC / e-way / shipment ... ids
    text: str = ""

    def digits(self) -> str:
        return re.sub(r"\D", "", self.text)


def available() -> bool:
    return orient.available()


def _ocr(png: bytes, psm: int = 6) -> str:
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "p.png")
        Path(p).write_bytes(png)
        try:
            return subprocess.run([orient.cmd(), p, "-", "--psm", str(psm)], capture_output=True, text=True,
                                  timeout=120).stdout
        except Exception as exc:                                   # missing binary / timeout
            log.warning("tesseract failed: %s", exc)
            return ""


def analyse(text: str) -> Evidence:
    """Pure function (testable without tesseract): text -> candidate IDs."""
    ev = Evidence(available=True, text=text)
    up = text.upper()
    for g in _GSTIN.findall(up):
        bucket = ev.gstins if gstin_ok(g) else ev.gstins_bad
        if g not in bucket:
            bucket.append(g)
    for line in text.splitlines():
        if _PHONE_CTX.search(line):
            ev.phones.update(_TEN.findall(line))
    for line in text.splitlines():
        if _OTHER_CTX.search(line) and not re.search(r"\bP\.?\s?O\.?\b|purchase", line, re.I):
            ev.others.update(_PO.findall(line))
    for m in _PO.findall(text):
        if m not in ev.phones and m not in ev.pos:
            ev.pos.append(m)
    return ev


def collect(path: str | Path) -> Evidence:
    """OCR of the invoice pages (as the pipeline sees them: PDF page rotation already applied)."""
    if not available():
        return Evidence()
    cfg = get_settings()
    chunks: list[str] = []
    try:
        with pdfio._open(path) as doc:
            for i in range(min(doc.page_count, cfg.main_pages)):
                pg = doc[i]
                z = 3000 / max(pg.rect.width, pg.rect.height)
                chunks.append(_ocr(pg.get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=False).tobytes("png")))
    except Exception:
        log.exception("OCR evidence failed")
        return Evidence()
    return analyse("\n".join(chunks))


def _dist(a: str, b: str) -> int:
    if len(a) != len(b):
        return 99
    return sum(x != y for x, y in zip(a, b))


def apply(h: dict, ev: Evidence, buyer_prefixes: tuple[str, ...], known: tuple[str, ...] = ()) -> list[str]:
    """Correct h from the evidence. Returns notes (for the audit log). Never invents a value that is not on the page."""
    notes: list[str] = []
    src = h.setdefault("field_sources", {})

    # ---- a GSTIN that fails its checksum but is 1-2 characters from a vendor we know -> the known GSTIN (no OCR needed)
    cur0 = h.get("gstin") or ""
    if cur0 and not gstin_ok(cur0):
        near = [k for k in known if _dist(cur0, k) <= 2]
        if len(near) == 1:
            notes.append(f"GSTIN {cur0} -> {near[0]} (known vendor, checksum fixed)")
            h["gstin"], src["gstin"] = near[0], "VENDOR_MASTER"
    if not ev.available:
        return notes

    # ---- seller GSTIN
    sellers = [g for g in ev.gstins if not any(g.startswith(p) for p in buyer_prefixes)]
    for bad in ev.gstins_bad:                    # an OCR slip next to a vendor we know (2 characters at most) -> the known GSTIN
        near = [k for k in known if _dist(bad, k) <= 2]
        if len(near) == 1 and near[0] not in sellers:
            sellers.append(near[0])
    cur = h.get("gstin") or ""
    if not cur or not gstin_ok(cur):
        pick = ""
        if cur:                                   # the model's reading is wrong by a few characters -> nearest printed GSTIN
            near = sorted(sellers, key=lambda g: _dist(cur, g))
            if near and _dist(cur, near[0]) <= 4:
                pick = near[0]
        elif len(sellers) == 1:
            pick = sellers[0]
        if pick:
            notes.append(f"GSTIN {cur or '(empty)'} -> {pick} (checksum-valid GSTIN printed on the page)")
            h["gstin"], src["gstin"] = pick, "OCR"

    # ---- PO
    po = h.get("po_number") or ""
    if po and po in ev.phones:
        notes.append(f"PO {po} is a phone number on the page - dropped")
        h["po_number"], po = "", ""
        h["po_numbers_printed"] = [p for p in h.get("po_numbers_printed", []) if p not in ev.phones]
    if po and po not in ev.pos and ev.pos:
        near = [p for p in ev.pos if _dist(po, p) == 1 and p[:2] == po[:2]]
        if len(near) == 1:
            notes.append(f"PO {po} -> {near[0]} (printed PO one digit away)")
            h["po_number"], src["po_number"] = near[0], "OCR"
            po = near[0]
    if not po:
        mat = [p for p in ev.pos if p[:2] in ("51", "61")]
        if len(mat) == 1:
            notes.append(f"PO taken from the printed text: {mat[0]}")
            h["po_number"], src["po_number"] = mat[0], "OCR"
    return notes


def corroborates_po(ev: Evidence, po: str) -> bool:
    """A service PO may send an invoice to the exception folder unless the page shows that very number as a phone
    number, delivery-challan / e-way / shipment id (SLR phone 83942..., Siporex 'DC. No 83700...')."""
    return po not in ev.phones and po not in ev.others


def corroborates_sac(ev: Evidence, code: str) -> bool:
    return (not ev.available) or (code and code in ev.digits())


# ----------------------------------------------------------------------------------------------- Lens: where are the numbers?
_BOX_CACHE: dict[tuple, list[dict]] = {}
_LENS_PREFIX = ("50", "51", "61", "81", "82", "83")


def find_numbers(path: str | Path, page: int) -> list[dict]:
    """Printed 10-digit GRN / PO candidates on one page with their position (fractions of the page) so the dashboard
    can draw a box around each. [] without tesseract. Handwriting is not found here - the person draws a box for that."""
    if not available():
        return []
    path = Path(path)
    key = (str(path), path.stat().st_mtime_ns, page)
    if key in _BOX_CACHE:
        return _BOX_CACHE[key]
    out: list[dict] = []
    try:
        with pdfio._open(path) as doc:
            pg = doc[page - 1]
            z = 3000 / max(pg.rect.width, pg.rect.height)
            png = pg.get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=False).tobytes("png")
            W, H = int(pg.rect.width * z), int(pg.rect.height * z)
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "p.png")
            Path(f).write_bytes(png)
            tsv = subprocess.run([orient.cmd(), f, "-", "--psm", "6", "tsv"], capture_output=True, text=True, timeout=180).stdout
        seen: set[tuple] = set()
        for line in tsv.splitlines()[1:]:
            c = line.split("\t")
            if len(c) < 12 or not c[11].strip():
                continue
            digits = re.sub(r"\D", "", c[11])
            if len(digits) != 10 or digits[:2] not in _LENS_PREFIX or len(re.sub(r"[\d\W_]", "", c[11])) > 1:
                continue
            l, t, w, h = (int(c[i]) for i in (6, 7, 8, 9))
            k = (digits, round(l / W, 2), round(t / H, 2))
            if k in seen:
                continue
            seen.add(k)
            out.append({"value": digits, "x": l / W, "y": t / H, "w": w / W, "h": h / H})
    except Exception:
        log.exception("number detection failed")
        return []
    if len(_BOX_CACHE) > 64:
        _BOX_CACHE.clear()
    _BOX_CACHE[key] = out
    return out
