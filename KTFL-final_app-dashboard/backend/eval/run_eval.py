"""Accuracy evaluation runner.

    python -m eval.run_eval --labels eval/labels --files <folder with the invoice PDFs / photos> --out eval/report_baseline
    python -m eval.run_eval --labels eval/labels --predictions eval/predictions.json --out eval/report_improved

Labels: one JSON per invoice, <file stem>.json, filled in BY A PERSON from the invoice itself (see labels_template.json).
Nothing here writes or edits labels. Predictions come from the live pipeline (needs Ollama; add --sap to also run the
SAP validation and report false-MATCHED cases) or from a saved predictions.json ({stem: {...extracted fields...}}).
Report: field accuracy, line accuracy, exact / partial / missing / false / invented rates, per category, plus timing and
Ollama token use. Two runs (baseline vs improved) are compared with eval.compare - improvement is only claimed from that."""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

from .metrics import aggregate, score_invoice, score_validation

CATEGORIES = ("text_pdf", "scanned_pdf", "mobile_photo", "multi_page", "multi_grn", "multi_po", "handwritten_grn")


def load_labels(folder: Path) -> dict[str, dict]:
    out = {}
    for p in sorted(folder.glob("*.json")):
        if p.name.startswith("labels_template"):
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        if d.get("_verified_by") in (None, "", "PENDING"):
            print(f"skip {p.name}: not marked as manually verified (_verified_by)", file=sys.stderr)
            continue
        out[p.stem] = d
    return out


def predict_live(files: Path, stems: set[str], sap: bool):
    """Runs the real extraction (and optionally SAP validation) on each file. Needs Ollama (and SQL Server for --sap)."""
    from app import database
    from app.services import pdfio
    from app.services.normalize import normalize
    from app.services.vlm import get_vlm
    preds, valid, perf = {}, {}, {}
    vlm = get_vlm()
    for p in sorted(files.iterdir()):
        if p.stem not in stems or not p.is_file():
            continue
        t0 = time.perf_counter()
        images = pdfio.main_page_images(p)
        data = vlm.extract_invoice(images, pdfio.text_layer(p) if p.suffix.lower() == ".pdf" else "")
        runs = data.pop("_runs", [])
        h, lines = normalize(data, p.name)
        preds[p.stem] = {**{k: (None if k in h.get("unread", []) else h.get(k)) for k in h}, "lines": lines,
                         "_page_info": h.get("page_info")}
        perf[p.stem] = {"seconds": round(time.perf_counter() - t0, 1), "pages": len(images),
                        "prompt_tokens": sum(r.get("prompt_eval_count") or 0 for r in runs),
                        "output_tokens": sum(r.get("eval_count") or 0 for r in runs)}
        if sap:
            from app.services.pipeline import check_against_sap
            from app.services.sap import get_sap
            with database.session_factory()() as db:
                check_against_sap(db, h, lines, get_sap())
            valid[p.stem] = h["queue"]
    return preds, valid, perf


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", required=True, type=Path)
    ap.add_argument("--files", type=Path)
    ap.add_argument("--predictions", type=Path)
    ap.add_argument("--sap", action="store_true", help="also run SAP validation (false-MATCHED measurement)")
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    labels = load_labels(a.labels)
    if not labels:
        print("No manually verified labels found - nothing to evaluate. Accuracy measurement is pending.")
        return 2
    valid, perf = {}, {}
    if a.predictions:
        blob = json.loads(a.predictions.read_text(encoding="utf-8"))
        preds, valid, perf = blob.get("predictions", blob), blob.get("validation", {}), blob.get("performance", {})
    elif a.files:
        preds, valid, perf = predict_live(a.files, set(labels), a.sap)
    else:
        ap.error("give --files (run the model) or --predictions (saved output)")
    scored, groups = {}, defaultdict(list)
    for stem, exp in labels.items():
        if stem not in preds:
            print(f"WARNING {stem}: no prediction - counted as all values missing", file=sys.stderr)
        scored[stem] = score_invoice(preds.get(stem, {}), exp.get("expected", exp))
        for cat in exp.get("categories", []):
            groups[cat].append(scored[stem])
    report = {"invoices": sorted(labels), "extraction": aggregate(list(scored.values())),
              "by_category": {c: aggregate(groups[c]) for c in CATEGORIES if groups.get(c)},
              "per_invoice": scored, "performance": perf}
    if valid:
        report["sap_validation"] = score_validation(
            [{"expected": labels[s].get("expected_status", "REVIEW"), "actual_queue": q} for s, q in valid.items()
             if s in labels])
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.with_suffix(".json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    ov = report["extraction"]["overall"]
    a.out.with_suffix(".md").write_text(
        f"# Extraction accuracy ({len(labels)} verified invoices)\n\n"
        f"exact {ov['exact_match_rate']} | exact or partial {ov['exact_or_partial_rate']} | missing {ov['missing_value_rate']} | "
        f"false {ov['false_value_rate']} | invented {ov['invented_value_count']}\n", encoding="utf-8")
    print(a.out.with_suffix(".md").read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
