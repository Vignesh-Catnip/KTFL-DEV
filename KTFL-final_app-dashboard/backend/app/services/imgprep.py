"""Conservative image preparation for photographed / scanned invoices (OpenCV).

Principles (approved rules): never sharpen, never binarise, never touch pixel values inside the text - only geometry
(rotation / perspective) and a mild contrast lift for dark or washed-out photos. Everything is OFF unless
settings.image_preprocess is true, and the quality assessment only REPORTS (blur / brightness / contrast) so a person
can be asked to re-photograph instead of the model guessing digits it cannot see.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import cv2
import numpy as np

log = logging.getLogger("clearmatch.imgprep")

BLUR_MIN = 60.0          # variance of the Laplacian below this -> too blurry to read digits reliably
DARK_MAX = 70.0          # mean grey below this -> too dark
BRIGHT_MIN = 235.0       # mean grey above this with no contrast -> washed out
CONTRAST_MIN = 28.0      # grey standard deviation below this -> flat / washed out
MAX_SKEW = 15.0          # larger angles are not trusted (rotation is handled elsewhere)


@dataclass
class Quality:
    blur: float
    brightness: float
    contrast: float
    flags: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.flags


def _gray(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img


def assess_quality(img: np.ndarray) -> Quality:
    g = _gray(img)
    blur = float(cv2.Laplacian(g, cv2.CV_64F).var())
    q = Quality(blur=round(blur, 1), brightness=round(float(g.mean()), 1), contrast=round(float(g.std()), 1))
    if q.blur < BLUR_MIN:
        q.flags.append("BLURRED")
    if q.brightness < DARK_MAX:
        q.flags.append("TOO_DARK")
    if q.contrast < CONTRAST_MIN:
        q.flags.append("LOW_CONTRAST" if q.brightness <= BRIGHT_MIN else "WASHED_OUT")
    return q


def estimate_skew(img: np.ndarray) -> float:
    """Skew in degrees of the text lines (positive = counter-clockwise), 0.0 when it cannot be estimated reliably."""
    g = _gray(img)
    bw = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    bw = cv2.dilate(bw, cv2.getStructuringElement(cv2.MORPH_RECT, (25, 1)))        # merge characters into text lines
    lines = cv2.HoughLinesP(bw, 1, np.pi / 720, threshold=max(80, g.shape[1] // 6),
                            minLineLength=g.shape[1] // 4, maxLineGap=20)
    if lines is None or len(lines) < 5:
        return 0.0
    angles = []
    for x1, y1, x2, y2 in np.asarray(lines).reshape(-1, 4):
        a = np.degrees(np.arctan2(y1 - y2, x2 - x1))
        if abs(a) <= MAX_SKEW:
            angles.append(a)
    if len(angles) < 5:
        return 0.0
    return float(np.median(angles))


def deskew(img: np.ndarray, min_angle: float = 0.4) -> tuple[np.ndarray, float]:
    angle = estimate_skew(img)
    if abs(angle) < min_angle or abs(angle) > MAX_SKEW:
        return img, 0.0
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), -angle, 1.0)
    out = cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    return out, angle


def _order(pts: np.ndarray) -> np.ndarray:
    s, d = pts.sum(axis=1), np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]], dtype="float32")


def perspective_correct(img: np.ndarray, min_area: float = 0.35) -> tuple[np.ndarray, bool]:
    """If a clear 4-corner sheet of paper covers most of the photo, warp it flat. Otherwise return it unchanged."""
    g = cv2.GaussianBlur(_gray(img), (5, 5), 0)
    edges = cv2.dilate(cv2.Canny(g, 50, 150), np.ones((3, 3), np.uint8))
    cnts, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    area_img = img.shape[0] * img.shape[1]
    for c in sorted(cnts, key=cv2.contourArea, reverse=True)[:5]:
        if cv2.contourArea(c) < min_area * area_img:
            break
        approx = cv2.approxPolyDP(c, 0.02 * cv2.arcLength(c, True), True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            quad = _order(approx.reshape(4, 2).astype("float32"))
            tl, tr, br, bl = quad
            w = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
            h = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
            if w < 200 or h < 200:
                continue
            # skip when the sheet is already (nearly) axis aligned: no distortion to correct
            if np.abs(quad - np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype="float32")).max() < 0.01 * max(w, h) and \
               abs(w * h - area_img) < 0.02 * area_img:
                return img, False
            m = cv2.getPerspectiveTransform(quad, np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], dtype="float32"))
            return cv2.warpPerspective(img, m, (w, h), flags=cv2.INTER_CUBIC), True
    return img, False


def lift_contrast(img: np.ndarray) -> tuple[np.ndarray, bool]:
    """Mild CLAHE on lightness only, and only for flat / dark photos. No sharpening, no thresholding."""
    q = assess_quality(img)
    if q.contrast >= CONTRAST_MIN * 1.6 and q.brightness >= DARK_MAX * 1.2:
        return img, False
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB) if img.ndim == 3 else None
    clahe = cv2.createCLAHE(clipLimit=1.5, tileGridSize=(8, 8))
    if lab is None:
        return clahe.apply(img), True
    lab[:, :, 0] = clahe.apply(lab[:, :, 0])
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR), True


def prepare(img: np.ndarray) -> tuple[np.ndarray, dict]:
    """perspective -> deskew -> mild contrast. Returns the image and a report of what was done."""
    report: dict = {"quality_before": assess_quality(img).__dict__}
    img, warped = perspective_correct(img)
    img, angle = deskew(img)
    img, lifted = lift_contrast(img)
    report.update({"perspective_corrected": warped, "deskew_degrees": round(angle, 2), "contrast_lifted": lifted,
                   "quality_after": assess_quality(img).__dict__})
    return img, report
