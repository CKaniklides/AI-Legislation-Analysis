# -*- coding: utf-8 -*-
"""
One-off migration (2026-09-24): backfill `paragraph_index` (the paragraph's position in
its article's own `paragraphs` list) onto every already-extracted norms[] entry, and onto
the `paragraphs[]` list itself as `numbering_status` where a displayed number repeats
within one article.

Why this is needed, confirmed directly against the data (not assumed): the DISPLAYED
paragraph number ("number", read from the source HTML/XML text) is not always unique
within an article. Three real cases found by scanning the whole corpus:
  - AI Act art. 73: two lid-divs (HTML ids 073.010, 073.011) both display "11" -- the
    second is almost certainly a numbering error in the source Official Journal HTML
    itself (there is no displayed "10"), not a parser bug, but the parser has no way to
    know that and correctly recorded what the source actually says.
  - Telecommunicatiewet art. 3.10 and art. 12.8: a <lid> with a real `jci` (a genuine
    JuriConnect legal identifier) is immediately followed by a second <lid> with the SAME
    displayed number and NO jci at all -- this is a genuine duplicate/amendment artifact
    in the BWB XML itself.

Every downstream consumer that has ever built `{number: text}` (a plain dict) -- Stage 6's
_iter_units() already-done tracking, and detect_c1_contradiction.py's evidence-text
lookup -- silently let the second paragraph's entry overwrite the first's in exactly
these 3 places, which meant one norm's stored evidence text was actually the OTHER
paragraph's text. `paragraph_index` (the list position, always present, always unique
within its article -- no data can make two paragraphs occupy the same position in a
Python list) is what those consumers should have keyed on, not the displayed number.

Run once, locally, no API calls, no cost:
    python migrate_paragraph_index.py            # apply
    python migrate_paragraph_index.py --dry-run  # report only
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SOURCES = [
    ("data/BWBR0052872_2026-08-15_provisions.json", lambda r: r),
    ("data/BWBR0040940_2026-09-01_provisions.json", lambda r: r),
    ("data/BWBR0048156_2025-11-11_provisions.json", lambda r: r),
    ("data/BWBR0009950_2026-08-15_provisions.json", lambda r: r),
    ("data/BWBR0051796_2025-11-21_provisions.json", lambda r: r),
    ("data/32016R0679_original_provisions.json", lambda r: r),
    ("data/32022L2555_original_provisions.json", lambda r: r),
    ("data/32022R2554_original_provisions.json", lambda r: r),
    ("data/32024R1689_original_provisions.json", lambda r: r),
    ("Datasets/Dutch Laws/bijlage35_dataset.json",
     lambda r: r["bijlage_35"]["substantive_text"]["provisions"]),
]


def _disambiguate_by_verbatim(norm: dict, candidate_paragraphs: list[tuple[int, dict]]) -> int:
    """When >1 paragraph shares the norm's displayed number, pick the one whose text
    actually contains the norm's verbatim action/trigger_event span -- the same
    verbatim-checking discipline Stage 6 itself uses, applied here to repair the
    ambiguity rather than trusting position order, which is not guaranteed to match
    extraction order."""
    for field in ("action", "trigger_event", "addressee"):
        span = norm.get(field)
        if not span:
            continue
        span_norm = " ".join(span.split()).lower()
        hits = [idx for idx, para in candidate_paragraphs
                if span_norm in " ".join(para["text"].split()).lower()]
        if len(hits) == 1:
            return hits[0]
    return None  # genuinely can't tell -- left unresolved, reported, not guessed


def migrate(path: str, get_provisions, dry_run: bool) -> tuple[int, int, int]:
    full_path = ROOT / path
    root = json.loads(full_path.read_text(encoding="utf-8"))
    provisions = get_provisions(root)

    n_norms_tagged = n_ambiguous_resolved = n_ambiguous_unresolved = 0

    for p in provisions:
        paragraphs = p.get("paragraphs") or []
        if not paragraphs:
            continue

        # number -> list of (index, paragraph) -- usually length 1, occasionally 2+
        by_number: dict = {}
        for idx, para in enumerate(paragraphs):
            by_number.setdefault(str(para.get("number")), []).append((idx, para))

        for num, entries in by_number.items():
            if len(entries) > 1:
                for idx, para in entries:
                    para["numbering_status"] = "inconsistent"

        for norm in p.get("norms", []):
            num = str(norm.get("number"))
            entries = by_number.get(num)
            if not entries:
                continue  # whole-article unit (paragraph_number None) -- nothing to index
            if len(entries) == 1:
                norm["paragraph_index"] = entries[0][0]
                n_norms_tagged += 1
                continue
            resolved = _disambiguate_by_verbatim(norm, entries)
            if resolved is not None:
                norm["paragraph_index"] = resolved
                n_ambiguous_resolved += 1
            else:
                norm["paragraph_index"] = None
                norm["paragraph_index_ambiguous"] = True
                n_ambiguous_unresolved += 1

    if not dry_run:
        full_path.write_text(json.dumps(root, ensure_ascii=False, indent=1), encoding="utf-8")

    return n_norms_tagged, n_ambiguous_resolved, n_ambiguous_unresolved


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    total = (0, 0, 0)
    for path, get_provisions in SOURCES:
        tagged, resolved, unresolved = migrate(path, get_provisions, args.dry_run)
        total = tuple(a + b for a, b in zip(total, (tagged, resolved, unresolved)))
        if tagged or resolved or unresolved:
            print(f"{path}: {tagged} unambiguous, {resolved} disambiguated by verbatim text, "
                  f"{unresolved} left unresolved", flush=True)

    print(f"\nTOTAL: {total[0]} unambiguous, {total[1]} disambiguated, {total[2]} unresolved"
          f"{' (dry-run, nothing written)' if args.dry_run else ' -- written to source files'}")
    if total[2]:
        print(f"WARNING: {total[2]} norm(s) could not be matched to a unique paragraph -- "
              f"these need a manual look, listed above by file.", file=sys.stderr)


if __name__ == "__main__":
    main()
