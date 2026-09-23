# -*- coding: utf-8 -*-
"""
One-off migration (2026-09-24): recompute every stored `deadline` field from its own
`deadline.raw` text using the current _parse_deadline() -- no API calls, no re-
extraction needed, since the raw span was already captured and verified verbatim by
Stage 6; only the deterministic parsing logic changed (see extract_norms.py's
_DEADLINE_PATTERNS note on "onverwijld"/"onmiddellijk" no longer being reduced to a
fake exact zero). Same principle an external review suggested directly: "Recompute
derived fields from stored raw spans when the parser changes; an expensive model
rerun is unnecessary when the raw extraction is already sufficient."
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract_norms import SOURCES, _parse_deadline

ROOT = Path(__file__).resolve().parent.parent


def main():
    total_changed = 0
    for spec in SOURCES:
        path = ROOT / spec.path
        root = json.loads(path.read_text(encoding="utf-8"))
        changed = 0
        for p in spec.get_provisions(root):
            for n in p.get("norms") or []:
                old = n.get("deadline")
                if not old or not old.get("raw"):
                    continue
                new = _parse_deadline(old["raw"])
                if new != old:
                    n["deadline"] = new
                    changed += 1
        if changed:
            path.write_text(json.dumps(root, ensure_ascii=False, indent=1), encoding="utf-8")
        total_changed += changed
        print(f"{spec.label}: {changed} deadline(s) recomputed")
    print(f"\nTOTAL: {total_changed} deadline(s) recomputed from stored raw text, no API calls")


if __name__ == "__main__":
    main()
