#!/usr/bin/env python3
"""Summarise data/trials-<network>.csv: count, median, p95, min, max per metric."""
import csv
import statistics
import sys
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1 else "../data/trials-devnet.csv")
rows = [r for r in csv.DictReader(open(path)) if r.get("validated") == "True"]
if not rows:
    sys.exit(f"no validated rows in {path}")

def pct(xs, q):
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)

print(f"{path}  n={len(rows)}  alpenglow={rows[-1]['alpenglow']}")
for col in ("ms_tap_to_seen", "ms_seen_to_finalized", "ms_tap_to_fired"):
    xs = [float(r[col]) for r in rows]
    print(f"{col:22s} median {statistics.median(xs):7.1f}  p95 {pct(xs, .95):7.1f}  "
          f"min {min(xs):7.1f}  max {max(xs):7.1f}  (ms)")
