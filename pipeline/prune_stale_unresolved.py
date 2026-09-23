# -*- coding: utf-8 -*-
"""
Cleanup for stage6_unresolved_extractions.json (2026-09-24) -- this file is append-only
(resolve_queue_conservatively() adds to it, never removes), so it accumulates two kinds
of noise: (1) a paragraph dropped as unresolved in an earlier run and later
successfully resolved in a subsequent (e.g. gpt-5.6-terra) escalation leaves a stale
record behind, and (2) the SAME still-genuinely-unresolved paragraph gets logged again
every time resolve_queue_conservatively() is re-run on it, producing duplicates.

Rewritten after two earlier, hand-rolled versions of this script gave results that
directly contradicted the real extract_norms.py --dry-run count (88 "unresolved" vs.
26 actually pending) -- both were bugs in the REIMPLEMENTED matching logic (index-vs-
number bookkeeping differs between old and new log entries), not in the underlying
data. Fixed by using _iter_units() ITSELF as the ground truth (the same function
extract_norms.py actually runs against), matched by (instrument, article,
paragraph_number) -- the one identifier that's stable across both old (pre-
paragraph_index) and new log entries -- rather than re-deriving the same logic a
third time and risking a third bug.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract_norms import SOURCES, _iter_units

ROOT = Path(__file__).resolve().parent.parent


def find_pending_keys() -> set:
    """(instrument_id, article, paragraph_number) for every unit _iter_units() would
    actually offer right now -- the real, authoritative "still needs work" set."""
    pending = set()
    for spec in SOURCES:
        root = json.loads((ROOT / spec.path).read_text(encoding="utf-8"))
        in_scope = [p for p in spec.get_provisions(root) if spec.in_scope(p)]
        for unit in _iter_units(in_scope):
            iid = unit.provision.get("instrument_id") or (
                "BWBR0051796" if "uitvoeringswet" in spec.path.lower() else
                "BWBR0049497" if "bijlage35" in spec.path.lower() else None)
            art = str(unit.provision.get("article") or unit.provision.get("number"))
            pending.add((iid, art, str(unit.paragraph_number)))
    return pending


def main():
    path = ROOT / "data" / "stage6_unresolved_extractions.json"
    entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    pending = find_pending_keys()

    seen = set()
    kept = []
    for e in entries:
        key = (e["instrument_id"], e["article"], str(e.get("paragraph_number")))
        if key not in pending:
            continue  # stale -- _iter_units() no longer considers this unresolved
        if key in seen:
            continue  # duplicate log entry for the same still-pending paragraph
        seen.add(key)
        kept.append(e)

    removed = len(entries) - len(kept)
    path.write_text(json.dumps(kept, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{removed} stale/duplicate entr{'y' if removed == 1 else 'ies'} removed, "
          f"{len(kept)} genuinely unresolved remain (should match --dry-run's pending count)")


if __name__ == "__main__":
    main()
