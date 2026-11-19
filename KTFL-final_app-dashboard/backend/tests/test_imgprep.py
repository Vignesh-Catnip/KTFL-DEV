"""Image preparation: geometry only, honest quality flags, multi-page TIFF."""
import cv2
import numpy as np
import pytest
from PIL import Image

from app.services import imgprep, pdfio


def page(w=1000, h=1400, lines=24):
    img = np.full((h, w, 3), 255, np.uint8)
    for i in range(lines):
        y = 80 + i * 52
        cv2.putText(img, f"ITEM {i:03d} EJECTION PIN 82073000 QTY 30 RATE 209.00 AMOUNT 6270.00", (60, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 2, cv2.LINE_AA)
    return img


def rotate(img, deg):
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), deg, 1.0)
    return cv2.warpAffine(img, m, (w, h), borderValue=(255, 255, 255))


def test_sharp_page_is_ok_and_blurred_page_is_flagged():
    assert imgprep.assess_quality(page()).ok
    blurred = cv2.GaussianBlur(page(), (0, 0), 6)
    assert "BLURRED" in imgprep.assess_quality(blurred).flags


def test_dark_and_flat_photos_are_flagged():
    assert "TOO_DARK" in imgprep.assess_quality((page() * 0.2).astype(np.uint8)).flags
    flat = (200 + page().astype(np.float32) * 0.05).astype(np.uint8)
    assert any(f in imgprep.assess_quality(flat).flags for f in ("LOW_CONTRAST", "WASHED_OUT"))


@pytest.mark.parametrize("deg", [3.0, -4.0, 7.0])
def test_skew_is_estimated_and_corrected(deg):
    img = rotate(page(), deg)
    est = imgprep.estimate_skew(img)
    assert abs(est - deg) < 0.8
    fixed, angle = imgprep.deskew(img)
    assert abs(imgprep.estimate_skew(fixed)) < 0.8 and abs(angle - deg) < 0.8


def test_straight_page_is_left_alone():
    img = page()
    out, angle = imgprep.deskew(img)
    assert angle == 0.0 and out is img


def test_perspective_flattens_a_tilted_sheet_and_ignores_plain_pages():
    sheet = page(800, 1100)
    canvas = np.full((1400, 1200, 3), 90, np.uint8)
    src = np.float32([[0, 0], [799, 0], [799, 1099], [0, 1099]])
    dst = np.float32([[170, 120], [1020, 200], [980, 1300], [130, 1230]])
    m = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(sheet, m, (1200, 1400), dst=canvas, borderMode=cv2.BORDER_TRANSPARENT)
    out, done = imgprep.perspective_correct(warped)
    assert done and 0.6 < out.shape[1] / out.shape[0] < 0.95
    same, done = imgprep.perspective_correct(page())
    assert not done and same is not None


def test_text_pixels_are_not_sharpened_or_thresholded():
    img = page()
    out, report = imgprep.prepare(img)
    assert out.shape == img.shape and set(np.unique(out)) - {0, 255} and not report["contrast_lifted"]
    # no binarisation: anti-aliased grey levels of the glyph edges survive
    assert len(np.unique(_gray(out))) > 10


def _gray(i):
    return cv2.cvtColor(i, cv2.COLOR_BGR2GRAY)


def test_multipage_tiff_opens_every_page(tmp_path):
    frames = [Image.fromarray(cv2.cvtColor(page(600, 800, 8 + n), cv2.COLOR_BGR2RGB)) for n in range(3)]
    p = tmp_path / "scan.tif"
    frames[0].save(p, save_all=True, append_images=frames[1:])
    assert pdfio.page_count(p) == 3
    assert len(pdfio.main_page_images(p)) == 3
