"""Small parsing / formatting helpers (numbers, PO / GRN / GSTIN clean-up, dates)."""
from __future__ import annotations

import json
import re
from datetime import date, datetime


def to_float(v) -> float:
    if v is None:
        return 0.0
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^0-9.\-]", "", str(v).replace(",", "").replace("₹", ""))
    try:
        return float(s) if s else 0.0
    except ValueError:
        return 0.0


def clean(v) -> str:
    return str(v).strip() if v not in (None, "None", "null") else ""


def clean_po(v) -> str:
    """Handles the full 10-digit PO as printed, and the short hand some people write by the gate /
    transporter stamp: '51/2448', '51-2448', '81.7531' -> 51 + 00 + 2448 = 5100002448 (pad the gap so
    it is 10 digits), same idea as clean_grn. Only expanded when it starts with KTFL's own PO prefixes
    51 or 81 - any other short number is left alone rather than guessed into a PO."""
    s = clean(v)
    s = re.sub(r"^(?:PO[-/.\s]*|P\.O\.[-/.\s]*|P/O[-/.\s]*)", "", s, flags=re.IGNORECASE).strip()
    s = "".join(_OCR_DIGITS.get(c, c) for c in s)
    m = re.search(r"(?<!\d)610000(\d{5})(?!\d)", s)      # NEW: 11-digit typo '610000XXXXX' -> '61000XXXXX' (drop ONE zero,
    if m:                                                #      keep all 5 trailing digits; was done by the old prompt)
        return "61000" + m.group(1)
    m = re.search(r"\d{10}", s)
    if m:
        return m.group(0)
    m = re.match(r"^\s*(\d{2,4})\s*[-/.]\s*(\d{3,8})\s*$", s)
    if m:
        a, b = m.group(1), m.group(2)
        if a[:2] in ("51", "81") and 0 < 10 - len(a) - len(b) < 10:
            return a + "0" * (10 - len(a) - len(b)) + b
    return s


def is_valid_po(po: str) -> bool:
    return bool(re.fullmatch(r"\d{10}", po or ""))


# KTFL PO prefix classification:
#   Material POs : 51xxxxxxxx, 61xxxxxxxx  -> allowed for extraction and SAP posting
#   Service POs  : 81xxxxxxxx, 82xxxxxxxx, 83xxxxxxxx -> NOT allowed; block at extraction
_SERVICE_PO_PREFIXES = ("81", "82", "83")
_MATERIAL_PO_PREFIXES = ("51", "61")


def is_service_po(po: str) -> bool:
    """Returns True if the PO is a KTFL service PO (81/82/83 prefix). These must NOT be extracted or posted."""
    po = (po or "").strip()
    if not is_valid_po(po):
        return False
    return po[:2] in _SERVICE_PO_PREFIXES


def is_material_po(po: str) -> bool:
    """Returns True if the PO is a KTFL material PO (51/61 prefix). Only these are allowed."""
    po = (po or "").strip()
    if not is_valid_po(po):
        return False
    return po[:2] in _MATERIAL_PO_PREFIXES


_OCR_DIGITS = {'S': '5', 's': '5', 'O': '0', 'o': '0', 'I': '1', 'l': '1', 'Z': '2', 'z': '2',
               'B': '8', 'G': '6', 'T': '7', 'A': '4', 'q': '9'}


def clean_grn(v) -> str:
    """'50-343863' -> '5000343863' (pads the gap so the GRN is 10 digits); OCR letters -> digits."""
    s = clean(v)
    if not s:
        return s
    s = "".join(_OCR_DIGITS.get(c, c) for c in s)
    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", s)
    if m:
        a, b = m.group(1), m.group(2)
        out = a + "0" * max(0, 10 - len(a) - len(b)) + b
    else:
        out = re.sub(r"[^\d]", "", s)
    if re.fullmatch(r"9000\d{6}", out):                  # NEW: a leading 9 before 000 is a 5 (old prompt did this)
        out = "5" + out[1:]
    return out


def grn_is_valid(g: str) -> bool:
    return len(g or "") == 10 and g.startswith("50")


def strip_zeros(v) -> str:
    return str(v or "").strip().lstrip("0")


def norm_key(s: str | None) -> str:
    """Upper-case and strip ALL whitespace - OCR spacing noise must never defeat a comparison."""
    return re.sub(r"\s+", "", (s or "")).upper()


def norm_doc(v) -> str:
    return re.sub(r"\s+", "", str(v or "")).lstrip("0")


def norm_hsn(v) -> str:
    return re.sub(r"\D", "", str(v or ""))


def norm_text(v) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(v or "").upper())


def fmt_sap_date(v) -> str:
    if v is None or str(v).strip() in ("", "00000000"):
        return ""
    if isinstance(v, (datetime, date)):
        return v.strftime("%d.%m.%Y")
    s = str(v).strip()
    for f, n in (("%Y%m%d", 8), ("%Y-%m-%d", 10), ("%Y-%m-%d %H:%M:%S", 19)):
        try:
            return datetime.strptime(s[:n], f).strftime("%d.%m.%Y")
        except ValueError:
            pass
    return s


_DATE_FORMATS = ("%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d-%m-%y", "%d/%m/%y",
                 "%d-%b-%y", "%d-%b-%Y", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y")


def format_date(raw: str) -> str:
    """Any invoice date format -> dd.mm.yyyy (unchanged when it cannot be parsed)."""
    if not raw:
        return ""
    raw = raw.strip()
    for f in _DATE_FORMATS:
        try:
            dt = datetime.strptime(raw, f)
            if dt.year < 100:
                dt = dt.replace(year=dt.year + 2000)
            return dt.strftime("%d.%m.%Y")
        except ValueError:
            pass
    return raw


def fix_amounts(sub, cgst, sgst, igst, tax, total):
    if tax == 0:
        tax = round(cgst + sgst + igst, 2)
    if sub == 0 and total > 0:
        sub = round(total - tax, 2)
    if total == 0:
        total = round(sub + tax, 2)
    return sub, cgst, sgst, igst, tax, total


GST_STANDARD_RATES = (0, 0.1, 0.25, 1, 1.5, 3, 5, 6, 7.5, 12, 18, 28)
GST_COMPONENT_RATES = (0, 0.05, 0.125, 0.5, 0.75, 1.5, 2.5, 3, 3.75, 6, 9, 14)


def snap_gst_rate(rate, component: bool = False):
    """17.96 -> 18 ; 8.97 -> 9 (component). Left alone when farther than 0.4 % from a legal rate."""
    if rate is None:
        return None
    table = GST_COMPONENT_RATES if component else GST_STANDARD_RATES
    best = min(table, key=lambda r: abs(r - rate))
    return float(best) if abs(best - rate) <= 0.4 else round(rate, 2)


def split_double_gstin(g: str) -> str:
    """Sometimes OCR reads two GSTINs joined: 'AAHCR8563K/27AAHCR8563K1ZP' or
    '27AABCK0618A1Z7/27AAHCR8563K1ZP'. Pick the one that is NOT KTFL's own GSTIN.
    If both are foreign (or we cannot tell), return the last 15-char segment."""
    parts = re.split(r"[/\s]+", g)
    # keep only parts that look like a GSTIN (≥14 chars, starts with 2 digits + letters)
    candidates = [p for p in parts if re.match(r"^\d{2}[A-Z]", p) and len(p) >= 14]
    if not candidates:
        return g
    if len(candidates) == 1:
        return candidates[0]
    # prefer the valid one; if both valid, return the longer / last one
    valid = [c for c in candidates if gstin_ok(fix_gstin(c[:15]))]
    return valid[-1] if valid else candidates[-1]


def fix_gstin(g: str) -> str:
    """Fix OCR confusion by position: 27ABRP19475K1ZQ -> 27ABRPI9475K1ZQ."""
    if len(g) != 15:
        return g
    to_digit = {"O": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2", "G": "6"}
    to_letter = {"0": "O", "1": "I", "5": "S", "8": "B", "2": "Z", "6": "G"}
    out = []
    for i, ch in enumerate(g):
        if i in (0, 1, 7, 8, 9, 10):
            out.append(to_digit.get(ch, ch))
        elif i in (2, 3, 4, 5, 6, 11):
            out.append(to_letter.get(ch, ch))
        elif i == 13:
            out.append("Z")
        else:
            out.append(ch)
    return "".join(out)


GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z]\d[A-Z][A-Z0-9]$")
_GST_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_LOOKALIKE = {"0": "ODQ", "1": "ILT", "2": "Z", "5": "S", "6": "G", "8": "BR", "7": "T", "4": "A", "9": "Q"}
_LOOKALIKE = {**_LOOKALIKE, **{c: k for k, v in _LOOKALIKE.items() for c in v}}


def gstin_ok(g: str) -> bool:
    """15 characters, right pattern AND the GSTIN check character (last) agrees with the other 14."""
    if not GSTIN_RE.match(g or ""):
        return False
    total = 0
    for i, c in enumerate(g[:14]):
        v = _GST_CHARS.index(c) * (1 if i % 2 == 0 else 2)
        total += v // 36 + v % 36
    return g[14] == _GST_CHARS[(36 - total % 36) % 36]


def repair_gstin(g: str) -> str:
    """A GSTIN the model misread by ONE character (extra character, or a look-alike such as 8<->R, 0<->O, 5<->S)
    is put right when exactly one correction makes it a valid GSTIN (pattern + check character). Otherwise it is
    returned unchanged - a wrong guess is worse than a flagged one."""
    g = (g or "").strip().upper().replace(" ", "")
    if gstin_ok(g) or not g:
        return g
    cands: set[str] = set()
    if len(g) == 16:                                          # one extra character
        cands = {g[:i] + g[i + 1:] for i in range(16)}
    elif len(g) == 15:                                        # one look-alike character
        for i, c in enumerate(g):
            for r in _LOOKALIKE.get(c, ""):
                cands.add(g[:i] + r + g[i + 1:])
    found = {c for c in cands if gstin_ok(c)}
    return found.pop() if len(found) == 1 else g


def extract_json(raw) -> dict:
    """Pull the JSON object out of a model answer (think blocks, code fences, cut-off output)."""
    if isinstance(raw, list):
        raw = "".join(str(x.get("text", "") if isinstance(x, dict) else x) for x in raw)
    t = re.sub(r"<think>.*?</think>", "", str(raw), flags=re.DOTALL | re.I).strip()
    t = re.sub(r"^```(?:json)?", "", t, flags=re.I)
    t = re.sub(r"```$", "", t).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    s, e = t.find("{"), t.rfind("}")
    if s >= 0 and e > s:
        try:
            return json.loads(t[s:e + 1])
        except Exception:
            pass
    return repair_json(t)


def repair_json(t: str) -> dict:
    """Output was cut off / has a broken comma: walk back to the last complete object and close the
    open brackets, so the header and every line item that WAS completed are still kept."""
    s = t.find("{")
    if s < 0:
        raise ValueError("No valid JSON in response")
    body = t[s:]
    cuts = [m.end() for m in re.finditer(r"[}\]]", body)]
    for end in reversed(cuts[-80:]):
        cand = body[:end].rstrip().rstrip(",")
        for closer in ("", "}", "]}", "}]}", "]}}"):
            try:
                out = json.loads(cand + closer)
                if isinstance(out, dict):
                    return out
            except Exception:
                continue
    raise ValueError("No valid JSON in response (repair failed)")


def json_safe(obj):
    """Make SAP rows (Decimal / date / bytes) storable in a JSON column."""
    from decimal import Decimal
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [json_safe(v) for v in obj]
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    return obj


# ---------------------------------------------------------------------------------------------------------------
# Strict parsing (approved implementation rules: unreadable -> None + reason, never 0; no silent JSON repair)
# ---------------------------------------------------------------------------------------------------------------
class IncompleteJson(ValueError):
    """The model answer is not one complete JSON object (cut off, broken, empty or not JSON at all)."""


def parse_amount(v):
    """Strict money parser. Returns (Decimal | None, reason | None). Never returns 0 for something it could not read.

    Accepts: numbers, "1,42,115.00", "1,234,567.89", "-500.00", "(500.00)" (accounting negative), "Rs. 1,000.50".
    Refuses (None + reason): "", "NA", garbage, several decimal points, European "1.234,50" (ambiguous with
    Indian grouping - the printed value is kept for a person to judge), more than two decimals after a comma group.
    The original printed text is never modified: callers keep the raw string themselves.
    """
    from decimal import Decimal, InvalidOperation
    if v is None or isinstance(v, bool):
        return None, "missing"
    if isinstance(v, Decimal):
        return v, None
    if isinstance(v, (int, float)):
        if v != v or v in (float("inf"), float("-inf")):
            return None, "not a finite number"
        return Decimal(str(v)), None
    s = str(v).strip()
    if not s or s.upper() in ("NA", "N/A", "NIL", "NULL", "NONE", "-", "--"):
        return None, "missing"
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1].strip()
    s = re.sub(r"^(?:INR|RS\.?|₹)\s*", "", s, flags=re.I).strip()
    if s.startswith("-"):
        neg, s = True, s[1:].strip()
    if re.fullmatch(r"\d{1,3}(\.\d{3})+,\d+|\d+,\d{1,2}", s) and not re.fullmatch(r"\d{1,3}(,\d{2})*,\d{3}", s):
        return None, f"ambiguous decimal comma / European format: {str(v)[:30]!r}"
    if not re.fullmatch(r"[\d,]*\.?\d*", s) or not re.search(r"\d", s):
        return None, f"not a number: {str(v)[:30]!r}"
    if "," in s and "." in s and s.rfind(",") > s.rfind("."):
        return None, f"ambiguous separators (decimal comma?): {str(v)[:30]!r}"
    if s.count(".") > 1:
        return None, f"several decimal points: {str(v)[:30]!r}"
    whole = s.split(".")[0]
    if "," in whole and not re.fullmatch(r"\d{1,3}(,\d{2})*,\d{3}|\d{1,3}(,\d{3})+|\d{1,2}(,\d{2})+,\d{3}", whole):
        return None, f"irregular digit grouping: {str(v)[:30]!r}"
    try:
        d = Decimal(s.replace(",", ""))
    except InvalidOperation:
        return None, f"not a number: {str(v)[:30]!r}"
    return (-d if neg else d), None


def extract_json_strict(raw) -> dict:
    """Parse ONE complete JSON object. Only removes <think> blocks and markdown fences. A cut-off or broken answer
    raises IncompleteJson - it is never repaired (repair_json used to hide dropped line items)."""
    if isinstance(raw, list):
        raw = "".join(str(x.get("text", "") if isinstance(x, dict) else x) for x in raw)
    t = re.sub(r"<think>.*?</think>", "", str(raw), flags=re.DOTALL | re.I).strip()
    t = re.sub(r"^```(?:json)?", "", t, flags=re.I).strip()
    t = re.sub(r"```$", "", t).strip()
    if not t:
        raise IncompleteJson("empty model response")
    try:
        obj = json.loads(t)
    except json.JSONDecodeError as exc:
        raise IncompleteJson(f"invalid or truncated JSON: {exc.msg} at char {exc.pos} of {len(t)}") from exc
    if not isinstance(obj, dict):
        raise IncompleteJson(f"expected a JSON object, got {type(obj).__name__}")
    return obj


def match_gst_rate(rate, component: bool = False, tol: float = 0.05):
    """Replaces snapping. A computed rate counts as a legal rate only when it differs by paise-level rounding noise
    (default 0.05 percentage points). 17.8 stays 17.8 and is then reported as NON_STANDARD_GST - it is never
    pulled to 18."""
    if rate is None:
        return None
    table = GST_COMPONENT_RATES if component else GST_STANDARD_RATES
    best = min(table, key=lambda r: abs(r - rate))
    return float(best) if abs(best - rate) <= tol else round(rate, 2)
