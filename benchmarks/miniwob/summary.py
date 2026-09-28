"""Pass counts per results file: `python benchmarks/miniwob/summary.py *.jsonl`."""
import json
import sys
from pathlib import Path

for path in sys.argv[1:]:
    records = [json.loads(line) for line in Path(path).read_text().splitlines()]
    passed = sum(r["ok"] for r in records)
    errors = sum(1 for r in records if r.get("error"))
    unseeded = sum(1 for r in records if r.get("seeded") is False)
    note = f"  ({unseeded} unseeded)" if unseeded else ""
    print(f"{path:40} {passed:>4}/{len(records):<4} {passed / max(1, len(records)):>4.0%}"
          f"  errors {errors}{note}")
