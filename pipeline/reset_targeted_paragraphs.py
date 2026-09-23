# -*- coding: utf-8 -*-
"""
Targeted reset (2026-09-24) -- clears norms[] ONLY for paragraphs matching a specific
"likely under-extracted" heuristic: >=3 lettered sub-points (a), b), c)...) in the
paragraph text, but <=1 actionable norm currently extracted from it. Confirmed against
a real case before trusting this: AI Act art. 10(5) has 7 lettered points and genuinely
bundles a PERMISSION, a PROHIBITION (data must not be transferred to other parties) and
an OBLIGATION (erasure once bias is corrected) -- the old one-norm-per-paragraph
extraction only ever captured the first. That missing prohibition/obligation pair is
exactly what was needed to make two of the historical blueprint's candidate findings
(AG3, AD2) even possible to detect.

This is a bounded, justified subset (190 paragraphs corpus-wide, checked directly),
NOT the full-corpus re-extraction that was deliberately deferred as too costly for
speculative benefit. Every one of these 190 already shows the concrete symptom
(dense, lettered, under-extracted), so the cost here buys confirmed value, not a blind
re-check.

Unlike reset_norms_for_reextraction.py, this does NOT touch _empty_paragraph_indices
or norms[] for any OTHER paragraph in the same article -- only the flagged ones.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract_norms import SOURCES

ROOT = Path(__file__).resolve().parent.parent
ACTIONABLE = {"OBLIGATION", "PROHIBITION", "PERMISSION", "COMPETENCE"}
LETTERED_RE = re.compile(r"[a-z]\)\s")


def find_targets():
    targets = []  # (spec, path, root, provision, paragraph_index)
    for spec in SOURCES:
        path = ROOT / spec.path
        root = json.loads(path.read_text(encoding="utf-8"))
        for p in spec.get_provisions(root):
            if not spec.in_scope(p):
                continue
            for idx, para in enumerate(p.get("paragraphs") or []):
                n_letters = len(LETTERED_RE.findall(para["text"]))
                if n_letters < 3:
                    continue
                norms_here = [n for n in (p.get("norms") or [])
                              if n.get("paragraph_index") == idx or
                              (n.get("paragraph_index") is None and str(n.get("number")) == str(para["number"]))]
                n_actionable = len([n for n in norms_here if n["deontic"] in ACTIONABLE])
                if n_actionable <= 1:
                    targets.append((spec, path, root, p, idx))
    return targets


def main():
    targets = find_targets()
    print(f"{len(targets)} target paragraph(s) found")

    dirty = {}
    for spec, path, root, p, idx in targets:
        para = (p.get("paragraphs") or [])[idx]
        before = len(p.get("norms") or [])
        p["norms"] = [n for n in (p.get("norms") or [])
                      if not (n.get("paragraph_index") == idx or
                              (n.get("paragraph_index") is None and str(n.get("number")) == str(para["number"])))]
        removed = before - len(p["norms"])
        if p.get("_empty_paragraph_indices") and idx in p["_empty_paragraph_indices"]:
            p["_empty_paragraph_indices"].remove(idx)
        dirty[path] = root
        print(f"  {spec.label} art.{p.get('article') or p.get('number')} para {para['number']} "
              f"(idx {idx}): removed {removed} old norm(s)")

    for path, root in dirty.items():
        path.write_text(json.dumps(root, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nwrote back {len(dirty)} file(s)")


if __name__ == "__main__":
    main()
