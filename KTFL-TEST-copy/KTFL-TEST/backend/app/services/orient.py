"""Page orientation from OCR evidence (replaces tesseract OSD, which was wrong on ~20% of these scans).

best_rotation(image_or_path) -> (clockwise_degrees, confident)
  Rotates the page 0/90/180/270, counts confidently-read words (>=3 letters, tesseract conf >= 70) for each,
  and keeps the best. confident=False when the page has too little text (weighment slip, blank back page) or the
  winner is not clearly ahead - then keep the page as it is, or ask the model (prompts.UPRIGHT_PROMPT).
Needs the `tesseract` binary and Pillow. ~3 s per page (4 OCR passes at 1500 px).
"""
from __future__ import annotations

import csv
import io
import os
import subprocess
import shutil
import tempfile

from PIL import Image

MIN_WORDS = 8        # fewer confident words than this on the best rotation -> not enough evidence
MIN_MARGIN = 2.0     # best must have at least this many times the words of the runner-up


def cmd() -> str:
    from ..config import get_settings
    return get_settings().tesseract_cmd.strip() or "tesseract"


def available() -> bool:
    """True when the tesseract binary can be found (optional - without it the model's yes/no probe is used)."""
    from ..config import get_settings
    return get_settings().ocr_evidence and shutil.which(cmd()) is not None


def _words(img: Image.Image) -> int:
    img = img.convert("L")
    img.thumbnail((1500, 1500))
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "p.png")
        img.save(p)
        out = subprocess.run([cmd(), p, "-", "--psm", "6", "tsv"], capture_output=True, text=True).stdout
    n = 0
    for row in csv.DictReader(io.StringIO(out), delimiter="\t", quoting=csv.QUOTE_NONE):
        t = (row.get("text") or "").strip()
        try:
            conf = float(row.get("conf") or -1)
        except ValueError:
            conf = -1
        if conf >= 70 and len(t) >= 3 and sum(c.isalpha() for c in t) >= 3:
            n += 1
    return n


def best_rotation(image) -> tuple[int, bool]:
    img = Image.open(image) if isinstance(image, (str, os.PathLike)) else image
    scores = {r: _words(img.rotate(-r, expand=True)) for r in (0, 90, 180, 270)}   # PIL rotates counter-clockwise
    best = max(scores, key=scores.get)
    runner = sorted(scores.values(), reverse=True)[1]
    if scores[best] >= MIN_WORDS and scores[best] >= MIN_MARGIN * max(runner, 1):
        return best, True
    return 0, False


def upright(image) -> Image.Image:
    """Convenience: return the page rotated upright (unchanged when not confident)."""
    img = Image.open(image) if isinstance(image, (str, os.PathLike)) else image
    r, ok = best_rotation(img)
    return img.rotate(-r, expand=True) if ok and r else img
