"""Before / after comparison of two eval reports. 'Improved' is claimed only for what this prints."""
import json
import sys
from pathlib import Path


def main(before: str, after: str) -> None:
    b, a = (json.loads(Path(p).read_text()) for p in (before, after))
    print(f"{'field':<22}{'exact before':>14}{'exact after':>13}{'false before':>14}{'false after':>13}")
    for grp in ("header_fields", "line_fields"):
        for f, rb in b["extraction"][grp].items():
            ra = a["extraction"][grp].get(f, {})
            print(f"{f:<22}{rb['exact_match_rate']!s:>14}{ra.get('exact_match_rate')!s:>13}"
                  f"{rb['false_value_rate']!s:>14}{ra.get('false_value_rate')!s:>13}")
    for k in ("sap_validation",):
        if k in b and k in a:
            print("false_matched:", b[k].get("false_matched"), "->", a[k].get("false_matched"))


if __name__ == "__main__":
    main(*sys.argv[1:3])
