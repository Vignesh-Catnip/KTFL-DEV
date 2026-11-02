"""PDF -> images / text, and finding the original PDF of an invoice for the dashboard viewer."""
from __future__ import annotations

import base64
import io
import logging
import re
from pathlib import Path

import pymupdf

from ..config import get_settings


log = logging.getLogger("clearmatch.pdfio")

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif", ".webp")


def is_image_name(name: str) -> bool:
    return str(name).lower().endswith(IMAGE_EXTS)


def _open(path: str | Path):
    """A PDF opens as it is. A photo / scan (jpg, png, tif ...) is first made readable: EXIF turn applied, a
    sideways phone photo (landscape - invoices are portrait) turned upright, and shrunk to IMAGE_MAX_SIDE
    pixels (a 12 MP photo at zoom 2 is a 100 MB picture the model cannot use). The file on disk is not changed."""
    p = str(path)
    if not is_image_name(p):
        return pymupdf.open(p)
    from PIL import Image, ImageOps
    cfg = get_settings()
    with Image.open(p) as raw:
        im = ImageOps.exif_transpose(raw)
        if cfg.image_autorotate and im.width > im.height * 1.1:
            # still landscape after the EXIF turn (the tag is missing or wrong, e.g. 180 on a sideways shot):
            # trust the pixels, not the tag - a phone held sideways saves the page turned 90 degrees anticlockwise
            im = raw.rotate(-90, expand=True)                 # 90 degrees clockwise
        im = im.convert("RGB")
        im.thumbnail((cfg.image_max_side, cfg.image_max_side))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=90)
    return pymupdf.open(stream=buf.getvalue(), filetype="jpg")


def _encode(pix, path) -> bytes:
    """PNG for PDFs (crisp text); JPEG for photos (a photo as PNG is 4 MB per page for nothing)."""
    return pix.tobytes("jpeg", jpg_quality=88) if is_image_name(str(path)) else pix.tobytes("png")


def page_count(path: str | Path) -> int:
    with _open(path) as doc:
        return doc.page_count


def main_page_images(path: str | Path) -> list[str]:
    """First pages of the invoice (header + line items) as base64 PNG."""
    cfg = get_settings()
    out = []
    with _open(path) as doc:
        zoom = pymupdf.Matrix(cfg.image_zoom, cfg.image_zoom)
        for i in range(min(doc.page_count, cfg.main_pages)):
            out.append(base64.b64encode(_encode(doc[i].get_pixmap(matrix=zoom, alpha=False), path)).decode())
    return out


def grn_stamp_images(path: str | Path) -> list[str]:
    """Bottom 55 % of the first pages - that is where the KTFL receiving stamp sits. Half the pixels of
    a full page (faster) and the higher zoom makes the handwriting clearer."""
    cfg = get_settings()
    out = []
    with _open(path) as doc:
        for i in range(min(doc.page_count, cfg.grn_pages)):
            pg = doc[i]
            r = pg.rect
            clip = pymupdf.Rect(0, r.height * 0.45, r.width, r.height)
            pix = pg.get_pixmap(matrix=pymupdf.Matrix(cfg.grn_zoom, cfg.grn_zoom), clip=clip, alpha=False)
            out.append(base64.b64encode(_encode(pix, path)).decode())
    return out


def text_layer(path: str | Path, per_page: int = 2000) -> str:
    parts = []
    with _open(path) as doc:
        for i, p in enumerate(doc, 1):
            t = p.get_text("text")
            q = text_quality(t)
            if t.strip() and q is not None and q >= 0.08:          # a sideways scan's OCR text is noise - do not feed it on
                parts.append(f"--- PAGE {i} ---\n{t[:per_page]}")
    return "\n".join(parts)


def render_page_png(path: str | Path, page: int = 1, zoom: float = 1.6) -> bytes:
    """One page as PNG bytes for the dashboard's 'Digital invoice' viewer (page is 1-based)."""
    with _open(path) as doc:
        if page < 1 or page > doc.page_count:
            raise IndexError(f"page {page} out of range (1..{doc.page_count})")
        return doc[page - 1].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).tobytes("png")


def locate_pdf(filename: str | None, source_path: str | None = None) -> Path | None:
    """The stored path when it still exists, otherwise search the configured folders by file name
    (invoices extracted earlier by the stand-alone script only have the file name)."""
    if source_path and Path(source_path).is_file():
        return Path(source_path)
    if not filename:
        return None
    for folder in get_settings().search_dirs:
        if not folder.is_dir():
            continue
        direct = folder / filename
        if direct.is_file():
            return direct
        for hit in folder.rglob(Path(filename).name):
            if hit.is_file():
                return hit
    return None


def b64_png_from_bytes(data: bytes) -> str:
    return base64.b64encode(data).decode()


# ------------------------------------------------------------------ scanned pages that were fed in sideways
def text_quality(text: str) -> float | None:
    """Share of real words in a page's text layer. ~0.2-0.45 for a normal invoice, ~0.02 when the page was scanned
    sideways and the scanner's OCR produced gibberish. None = no text layer at all."""
    w = text.split()
    if not w:
        return None
    good = [x for x in w if re.fullmatch(r"[A-Za-z]{4,}", x) and re.search(r"[aeiouAEIOU]", x)]
    return len(good) / len(w)


def _unreadable(page) -> bool:
    q = text_quality(page.get_text())
    return q is None or q < 0.08


def fix_orientation(path: str | Path, probe) -> list[tuple[int, int]]:
    """Scanned pages whose text layer is gibberish / missing are checked by the model with a plain yes/no
    question - "is the text upright?". Upright (the usual case) -> left alone. Otherwise the page is shown turned
    90 / 270 / 180 degrees and the model is asked the same; it is turned only when EXACTLY ONE of the three is
    upright (unclear -> left as it is). Done with the PDF's own page rotation, so reading, GRN stamp and the
    dashboard viewer all see the page upright; only the stored working copy is changed.
    probe(image_b64) -> True / False / None.  Returns [(page_no, degrees)]."""
    if probe is None or is_image_name(str(path)):
        return []
    from PIL import Image
    cfg = get_settings()
    done: list[tuple[int, int]] = []
    doc = pymupdf.open(str(path))

    def b64(im) -> str:
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode()

    try:
        for i in range(min(doc.page_count, max(cfg.main_pages, cfg.grn_pages))):
            pg = doc[i]
            if pg.rotation or not _unreadable(pg):
                continue
            side = max(pg.rect.width, pg.rect.height)
            pix = pg.get_pixmap(matrix=pymupdf.Matrix(1000 / side, 1000 / side), alpha=False)
            base = Image.open(io.BytesIO(pix.tobytes("jpeg", jpg_quality=80)))
            try:
                if probe(b64(base)) is not False:            # upright, or no answer -> do not touch
                    continue
                ok = [deg for deg in (90, 270, 180) if probe(b64(base.rotate(-deg, expand=True))) is True]
            except Exception as exc:
                log.warning("orientation check of page %s failed: %s", i + 1, exc)
                continue
            if len(ok) == 1:
                pg.set_rotation(ok[0])
                done.append((i + 1, ok[0]))
            else:
                log.info("%s page %s looks sideways but the direction is unclear %s - left as it is", Path(path).name,
                         i + 1, ok)
        if done:
            data = doc.tobytes()
    finally:
        doc.close()
    if done:
        Path(path).write_bytes(data)
        log.info("%s: pages turned upright %s", Path(path).name, done)
    return done
