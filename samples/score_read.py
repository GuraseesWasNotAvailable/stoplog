"""Score a `stoplog extract --out read.json` result against a truth file.

    python samples/score_read.py read.json samples/line3-shiftB.truth.json

Exact match per field after light normalisation (case, spaces, 4:10 == 04:10, 6 == 6.0).
Free-text fields (reason, action) also report a similarity score, since a model may
legitimately read "tea break + cleaning" as "tea break and cleaning".
"""

from __future__ import annotations

import json
import re
import sys
from difflib import SequenceMatcher

FIELDS = ["machine", "start_time", "end_time", "duration_min", "category_as_written",
          "reason_as_written", "action_taken", "recorded_by"]
FREE_TEXT = {"reason_as_written", "action_taken"}


def norm(field: str, v) -> str:
    if v is None:
        return ""
    s = str(v).strip().lower()
    if field in ("start_time", "end_time"):
        m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", s)
        return f"{int(m.group(1)):02d}:{m.group(2)}" if m else s
    if field == "duration_min":
        try:
            f = float(s)
            return str(int(f)) if f.is_integer() else str(f)
        except ValueError:
            return s
    return re.sub(r"\s+", " ", s).rstrip(".;, ")  # "Cleared film." == "cleared film"


def main(read_path: str, truth_path: str) -> int:
    read = json.load(open(read_path, encoding="utf-8"))["read"]
    truth = json.load(open(truth_path, encoding="utf-8"))
    rows_t, rows_r = truth["rows"], read["stops"]

    header = {"sheet_date": (truth["header"]["sheet_date"], read.get("sheet_date")),
              "shift": (truth["header"]["shift"], read.get("shift")),
              "line": (truth["header"]["line"], read.get("line")),
              "sku": (truth["header"]["sku"], read.get("sku"))}
    ok_h = sum(norm(k, a) == norm(k, b) for k, (a, b) in header.items())
    print(f"header: {ok_h}/{len(header)}", {k: v[1] for k, v in header.items() if norm(k, v[0]) != norm(k, v[1])} or "")
    print(f"rows: read {len(rows_r)}, truth {len(rows_t)}")

    total = right = 0
    sims = []
    misses = []
    for i, t in enumerate(rows_t):
        r = rows_r[i] if i < len(rows_r) else {}
        for f in FIELDS:
            a, b = norm(f, t.get(f)), norm(f, r.get(f))
            total += 1
            if a == b:
                right += 1
            else:
                misses.append(f"  row {i + 1} {f}: wrote {t.get(f)!r}, read {r.get(f)!r}")
            if f in FREE_TEXT and a:
                sims.append(SequenceMatcher(None, a, b).ratio())
    tally = [r for r in rows_r if (r.get("occurrences") or 1) > 1]
    print(f"fields exact: {right}/{total} ({100 * right / total:.1f}%)")
    if sims:
        print(f"free-text similarity (reason, action): mean {sum(sims) / len(sims):.2f}")
    print(f"tally rows detected: {[(r.get('reason_as_written'), r.get('occurrences')) for r in tally]}")
    print("\n".join(misses) if misses else "no field mismatches")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
