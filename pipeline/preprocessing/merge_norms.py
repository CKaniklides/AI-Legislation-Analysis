# -*- coding: utf-8 -*-
"""
Post-process a stage-4 norms file (data/stage_4/stage4_{name}_norms.json):

  1. MERGE duplicate norms within one unit. Two norms are duplicates when they share
     the same unit (paragraph), the same `deontic`, and the same `action_lemma`
     and `object` after normalization. The first one (lowest former norm_index) is kept
     as the stored reading. Every reading that differs from it -- the duplicate's own
     reading, and any alternative readings either norm already carried -- is kept in
     `alternative_reading`. Readings identical to the stored one are dropped, so the
     merge removes only exact repeats and never hides a reading (non-suppression).

  2. IDENTIFY each norm by the paragraph it refers to. `norm_id` is
     "{article}({paragraph number}).{k}", e.g. "83(2).1", "83(9).3", with k counting
     norms of that displayed paragraph number in document order. Whole-article units
     (no paragraphs) get "{article}.{k}". `norm_index` (position within the unit, with
     no paragraph in it) is removed. The norms list is re-sorted into document order.

Schema change for the fields C1 reads:
  * `alternative_reading` is now ALWAYS a list (empty when there is none). Each entry is
    a full reading plus `"source"`: "other_pass" (the second extraction pass disagreed)
    or "merged_duplicate" (a merged sibling norm read it differently).
  * `deontic_alternative` is now a list of the distinct deontics found among the
    alternative readings that differ from the stored `deontic`.
  * `merged_duplicates` (int) counts how many norms were folded into this one.

  3. SLIM each provision for downstream use (default). From each provision, remove
     `eli`, `celex`, `language`, `text`, `text_fidelity`, `source_ref`, `features`,
     and `max_condition_depth`. Each norm keeps its paragraph only as `paragraph_number`
     (renamed from `number`); `schema_version`, `paragraph_index`, and `norm_index`
     are removed. Each norm starts with `norm_id` and `paragraph_number`.

     Slimming is only for the merged copy that Stage A/B read. The stage-4 files must
     keep `schema_version` and `paragraph_index`: extract_norms.py uses them to decide
     which norms are current and which units are done, and without them it re-extracts
     everything and appends duplicates. Therefore --in-place requires
     --keep-bookkeeping, which merges and assigns norm_ids without slimming.

The script is idempotent: running it again on its own output gives the same file. On a
slimmed file, norms are grouped into units by `paragraph_number` (there is no
paragraph_index any more). It processes provisions carrying a `norms` list; in slim mode
it also removes the specified provision metadata fields. `_stale_norms` and empty-unit
markers are left as they are.

Usage:
    python merge_norms.py data/stage_4/stage4_GDPR_norms.json --dry-run
    python merge_norms.py data/stage_4/stage4_GDPR_norms.json -o merged.json
    python merge_norms.py data/stage_4/stage4_*_norms.json          # -> *_merged.json
    python merge_norms.py data/stage_4/stage4_*_norms.json --in-place --keep-bookkeeping
"""
import argparse
import json
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Iterator

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# The ExtractedNorm fields: what one "reading" of a norm consists of.
# Keep in sync with ExtractedNorm in extract_norms.py.
READING_FIELDS = [
    "deontic", "actor", "trigger_event", "action_span", "action_lemma", "object",
    "recipient", "deadline_raw", "thresholds", "conditions",
]

# Same normalization as extract_norms._normalize (keep in sync).
_CHAR_MAP = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'", "`": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u2033": '"',
    "\u00ab": '"', "\u00bb": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-",
    "\u2212": "-", "\u00ad": "",
})


def _normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).translate(_CHAR_MAP)
    return re.sub(r"\s+", " ", s).strip().lower()


def _canon(v):
    if isinstance(v, str):
        return _normalize(v)
    if isinstance(v, list):
        return [_canon(x) for x in v]
    return v


def _reading(d: dict) -> dict:
    return {f: d.get(f) for f in READING_FIELDS}


def _reading_key(d: dict) -> str:
    """Two readings are identical when every field matches after normalization."""
    return json.dumps({f: _canon(d.get(f)) for f in READING_FIELDS},
                      sort_keys=True, ensure_ascii=False)


BOOKKEEPING_FIELDS = ("schema_version", "paragraph_index", "number", "norm_index")

PROVISION_FIELDS_TO_REMOVE = {
    "eli", "celex", "language", "text", "text_fidelity", "source_ref",
    "features", "max_condition_depth",
}


def _paragraph_number(n: dict):
    """Displayed paragraph number: `paragraph_number` on slimmed norms, `number` on
    stage-4 norms. A paragraph without a displayed number gets its position + 1."""
    if "paragraph_number" in n:
        return n["paragraph_number"]
    if n.get("number") is not None:
        return n["number"]
    if n.get("paragraph_index") is not None:
        return str(n["paragraph_index"] + 1)
    return None


def _unit_key(n: dict) -> tuple:
    """The unit a norm was extracted from. Stage-4 norms: paragraph_index (exact, also
    for the few articles with two paragraphs sharing a number). Slimmed norms: the
    displayed paragraph_number."""
    if n.get("paragraph_index") is not None:
        return ("index", n["paragraph_index"])
    number = _paragraph_number(n)
    return ("number", str(number)) if number is not None else ("whole",)


def _position(n: dict) -> tuple:
    """Document order of a norm's paragraph; whole-article norms first."""
    if n.get("paragraph_index") is not None:
        return (0, n["paragraph_index"], "")
    number = _paragraph_number(n)
    if number is None:
        return (-1, 0, "")
    m = re.match(r"\s*(\d+)(.*)", str(number))
    return (0, int(m.group(1)), m.group(2)) if m else (0, 10 ** 9, str(number))


def _dedup_key(n: dict) -> tuple:
    return (_unit_key(n), n.get("deontic"),
            _normalize(n.get("action_lemma") or ""), _normalize(n.get("object") or ""))


def _existing_alternatives(n: dict) -> list[dict]:
    """Alternative readings in either the old shape (one dict or null) or the new
    shape (list of dicts with a "source")."""
    alt = n.get("alternative_reading")
    if not alt:
        return []
    if isinstance(alt, dict):
        return [{**_reading(alt), "source": "other_pass"}]
    return [{**_reading(a), "source": a.get("source") or "other_pass"} for a in alt]


MERGE_NOTE = ("{k} duplicate norm(s) in this unit with the same deontic, action_lemma "
              "and object were merged into this one; readings that differed are kept in "
              "alternative_reading with source \"merged_duplicate\".")


def _merge_group(group: list[dict]) -> dict:
    base = dict(group[0])
    seen = {_reading_key(base)}
    alternatives: list[dict] = []
    n_differing = 0

    def add(reading: dict, source: str) -> bool:
        key = _reading_key(reading)
        if key in seen:
            return False
        seen.add(key)
        alternatives.append({**_reading(reading), "source": source})
        return True

    for alt in _existing_alternatives(group[0]):
        add(alt, alt["source"])
    for other in group[1:]:
        if add(other, "merged_duplicate"):
            n_differing += 1
        for alt in _existing_alternatives(other):
            add(alt, alt["source"])

    notes = []
    for n in group:
        note = n.get("uncertainty_note")
        if note and note not in notes:
            notes.append(note)
    if n_differing:
        note = MERGE_NOTE.format(k=len(group) - 1)
        if note not in notes:
            notes.append(note)

    base.pop("norm_index", None)
    base["alternative_reading"] = alternatives
    base["deontic_alternative"] = sorted(
        {a["deontic"] for a in alternatives if a.get("deontic") and a["deontic"] != base.get("deontic")})
    base["extraction_uncertain"] = (any(bool(n.get("extraction_uncertain")) for n in group)
                                    or n_differing > 0)
    base["uncertainty_note"] = " | ".join(notes) if notes else None
    base["merged_duplicates"] = sum(int(n.get("merged_duplicates") or 0) for n in group) + len(group) - 1
    base["human_verified"] = all(bool(n.get("human_verified")) for n in group)
    return base


def _sort_key(n: dict) -> tuple:
    return (_position(n), n.get("norm_index") if n.get("norm_index") is not None else 0)


def _assign_norm_ids(provision: dict, norms: list[dict]) -> None:
    article = provision.get("article") or provision.get("number") or "?"
    counters: dict[str, int] = {}
    for n in norms:
        number = _paragraph_number(n)
        label = f"{article}({number})" if number is not None else str(article)
        counters[label] = counters.get(label, 0) + 1
        n["norm_id"] = f"{label}.{counters[label]}"


def _slim(n: dict) -> dict:
    """norm_id and paragraph_number first, bookkeeping fields removed."""
    rest = {k: v for k, v in n.items()
            if k not in BOOKKEEPING_FIELDS and k not in ("norm_id", "paragraph_number")}
    return {"norm_id": n["norm_id"], "paragraph_number": _paragraph_number(n), **rest}


def normalize_provision_norms(provision: dict, slim: bool = False) -> list[tuple[str, int]]:
    """Merges duplicates and assigns norm_ids in place; with slim=True also reduces the
    paragraph fields to paragraph_number. Returns (norm_id, n_folded) for every norm
    that absorbed at least one duplicate. Safe to call repeatedly. extract_norms.py can
    call it with slim=False after storing a unit's norms (never with slim=True)."""
    norms = provision.get("norms") or []
    if not norms:
        return []
    # Order by unit, then by the former within-unit position, so the kept reading is
    # the first one extracted and ids follow document order.
    ordered = sorted(norms, key=_sort_key)
    groups: dict[tuple, list[dict]] = {}
    for n in ordered:
        groups.setdefault(_dedup_key(n), []).append(n)

    merged, report = [], []
    for group in groups.values():
        m = _merge_group(group)
        merged.append(m)
        if len(group) > 1:
            report.append((m, len(group) - 1))
    merged.sort(key=_position)
    _assign_norm_ids(provision, merged)
    if slim:
        merged = [_slim(n) for n in merged]
    provision["norms"] = merged
    return [(m["norm_id"], k) for m, k in report]


def _iter_provisions(node) -> Iterator[dict]:
    """Every dict carrying a `norms` list, wherever it sits in the file (the stage-4
    files are a flat list, but e.g. Bijlage 35 nests its provisions)."""
    if isinstance(node, dict):
        if isinstance(node.get("norms"), list):
            yield node
        for k, v in node.items():
            if k not in ("norms", "_stale_norms"):
                yield from _iter_provisions(v)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_provisions(v)


def _atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def process_file(path: Path, out: Path | None, dry_run: bool, slim: bool) -> tuple[int, int]:
    root = json.loads(path.read_text(encoding="utf-8"))
    n_before = n_after = 0
    for p in _iter_provisions(root):
        # Preserve whole-article text in the paragraphs structure before slimming
        # removes the top-level `text` field. Do not invent a paragraph number:
        # articles that have no paragraphs remain whole-article provisions.
        if not p.get("paragraphs") and p.get("text") is not None:
            p["paragraphs"] = [{"text": p["text"]}]

        # Remove provision-level metadata only from the slimmed downstream copy.
        # --keep-bookkeeping preserves stage-4 data for extract_norms.py, but the
        # text-to-paragraphs migration above is still applied.
        if slim:
            for field in PROVISION_FIELDS_TO_REMOVE:
                p.pop(field, None)
        n_before += len(p["norms"])
        for norm_id, k in normalize_provision_norms(p, slim=slim):
            print(f"  {norm_id}: merged {k} duplicate(s)")
        n_after += len(p["norms"])
    print(f"{path}: {n_before} norm(s) -> {n_after} ({n_before - n_after} merged)")
    if not dry_run and out is not None:
        _atomic_write_json(out, root)
        print(f"  written -> {out}")
    return n_before, n_after


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="+", type=Path)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("-o", "--output", type=Path, help="output file (only with one input)")
    g.add_argument("--in-place", action="store_true",
                   help="overwrite the input file(s); requires --keep-bookkeeping")
    ap.add_argument("--keep-bookkeeping", action="store_true",
                    help="preserve stage-4 bookkeeping fields and provision metadata; "
                         "needed when writing back files used by extract_norms.py; default: slim")
    ap.add_argument("--dry-run", action="store_true", help="report merges, write nothing")
    args = ap.parse_args()

    if args.in_place and not args.keep_bookkeeping:
        ap.error("--in-place without --keep-bookkeeping would strip stage-4 bookkeeping "
                 "fields and provision metadata, which can make extract_norms.py re-extract "
                 "everything and append duplicates. Write a separate *_merged.json (the "
                 "default), or add --keep-bookkeeping to merge in place without slimming.")

    if args.output and len(args.paths) > 1:
        ap.error("-o/--output takes a single input file; use --in-place for several")

    for path in args.paths:
        if args.in_place:
            out = path
        elif args.output:
            out = args.output
        else:
            out = path.with_name(path.stem + "_merged" + path.suffix)
        process_file(path, out, args.dry_run, slim=not args.keep_bookkeeping)


if __name__ == "__main__":
    main()