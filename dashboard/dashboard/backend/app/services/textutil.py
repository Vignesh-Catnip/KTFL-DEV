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
    s = clean(v)
    s = re.sub(r"^(?:PO[-/.\s]*|P\.O\.[-/.\s]*|P/O[-/.\s]*)", "", s, flags=re.IGNORECASE).strip()
    m = re.search(r"\d{10}", s)
    return m.group(0) if m else s


def is_valid_po(po: str) -> bool:
    return bool(re.fullmatch(r"\d{10}", po or ""))


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
        return a + "0" * max(0, 10 - len(a) - len(b)) + b
    return re.sub(r"[^\d]", "", s)


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
