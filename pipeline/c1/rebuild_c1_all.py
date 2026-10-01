# -*- coding: utf-8 -*-
"""
Rebuilds data/findings_c1.json from every cache, in the ONLY safe order. Added 2026-09-28
after a real mistake: rebuild_findings_c1_from_cache.py writes the whole file fresh from the
strict duty-conflict cache and knows nothing about the findings other detectors append
(definitional_mismatch, legal_tension/missing_context), so running it alone silently wiped
109 definitional findings. This runs all three steps, in order, each a pure cache replay
(no new API calls when the caches are warm):

  1. rebuild_findings_c1_from_cache.py   -- strict duty/threshold/competence/standard findings
                                            (minus those the graded pass superseded)
  2. detect_c1_definitional_mismatch.py  -- appends definitional_mismatch findings
  3. detect_c1_legal_tension.py --append-findings-only -- appends legal_tension / missing_context
  4. detect_c1_legal_basis.py --append-findings-only   -- appends permission_prohibition_conflict

Always use this instead of running step 1 by hand.
"""
import subprocess
import sys
from collections import Counter
from pathlib import Path
_HERE = Path(__file__).resolve().parent
_PIPELINE = _HERE.parent
for _d in (_PIPELINE, _PIPELINE / "c1", _PIPELINE / "preprocessing", _PIPELINE / "c2"):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

HERE = Path(__file__).resolve().parent
STEPS = [
    ["rebuild_findings_c1_from_cache.py"],
    ["detect_c1_definitional_mismatch.py"],
    ["detect_c1_legal_tension.py", "--append-findings-only"],
    ["detect_c1_legal_basis.py", "--append-findings-only"],
]

for step in STEPS:
    print(f"\n=== {' '.join(step)} ===", flush=True)
    subprocess.run([sys.executable, str(HERE / step[0]), *step[1:]], cwd=HERE, check=True)

import json
data = json.loads((HERE.parent / "data" / "results" / "c1" / "findings_c1.json").read_text(encoding="utf-8"))
print(f"\nfindings_c1.json: {len(data['findings'])} findings; "
      f"{len(data.get('resolved_by_graded_pass', []))} strict findings resolved by the graded pass")
print(dict(Counter(f["subtype"] for f in data["findings"])))
