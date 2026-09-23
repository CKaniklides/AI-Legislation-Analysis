# -*- coding: utf-8 -*-
"""
One-off reset (2026-09-24, item 3 full re-extraction): clears norms[] and
_empty_paragraph_indices on every IN-SCOPE provision across all SOURCES, so
_iter_units() treats the whole corpus as unprocessed and extract_norms.py re-runs
under the new multi-norm-per-paragraph schema everywhere -- not just on paragraphs
that were previously queued or never attempted.

Why this is necessary, not optional: a paragraph already holding a norms[] entry from
the OLD one-norm-per-paragraph schema is marked "done" by paragraph_index, so the new
multi-norm prompt would never get a chance to check it for a second bundled duty.
Re-deriving everything from scratch under one unified schema also avoids mixing old
entries (no norm_index) with new ones.

Run once, locally, no API calls, no cost. The pre-reset files are backed up separately
(data/backup_pre_item3_reextraction/) before this runs, so nothing here is
unrecoverable even without git.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract_norms import SOURCES

ROOT = Path(__file__).resolve().parent.parent


def main():
    for spec in SOURCES:
        path = ROOT / spec.path
        root = json.loads(path.read_text(encoding="utf-8"))
        provisions = spec.get_provisions(root)
        n_reset = 0
        for p in provisions:
            if not spec.in_scope(p):
                continue
            had = bool(p.get("norms")) or bool(p.get("_empty_paragraph_indices"))
            p["norms"] = []
            p.pop("_empty_paragraph_indices", None)
            if had:
                n_reset += 1
        path.write_text(json.dumps(root, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{spec.label}: reset {n_reset} in-scope provision(s) -> {spec.path}")


if __name__ == "__main__":
    main()
