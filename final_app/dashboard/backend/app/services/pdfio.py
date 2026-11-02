"""PDF -> images / text, and finding the original PDF of an invoice for the dashboard viewer."""
from __future__ import annotations

import base64
import io
from pathlib import Path

import pymupdf

from ..config import get_settings


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
            if t.strip():
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
