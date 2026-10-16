"""Merge several ``load_norms.py`` outputs into one file.

Typical use: you exported sources one by one (``--only ai_act``, ``--only gdpr``, ...)
and now want a single file with every norm.

    python merge_norms.py                                  # data/norms_with_context_*.json(l) -> data/norms_with_context.json
    python merge_norms.py a.json b.json --out all.json     # explicit files
    python merge_norms.py "data/norms_with_context_*.json" --out all.jsonl

Record format (as written by load_norms.py)
-------------------------------------------
    {
      "norm_id":   "32024R1689:2:1:1",
      "norm":      { ...norm parameters as extracted... },
      "text":      {"paragraph": "<raw paragraphs[].text>", "resolution": "..."},
      "hierarchy": {"instrument_id": "32024R1689", "article": "2", ...}
    }

Rules
-----
* Inputs are ``.json`` (array of records) or ``.jsonl`` (one record per line). Patterns
  with wildcards are expanded; each pattern's matches are sorted by name so the result is
  deterministic. Input order is preserved in the output.
* ``load_norms.py`` can emit several distinct norms with the same ``norm_id`` inside one
  file (it only warns about this). Those are all kept. Records are therefore matched
  across files by (``norm_id``, occurrence within the file): the 2nd record with id X in
  file A is compared with the 2nd record with id X in file B.
* The same record appearing in two inputs (e.g. ``--only nis2`` and ``--only nis2_nl``
  both export the Dutch NIS2 law) is harmless: identical copies are dropped and counted.
  When they differ, that is a real conflict (typically an export made with an older
  load_norms.py): the default is to stop and list the ids, or choose with
  ``--on-conflict first|last``.
* The output file itself is never read as an input, and it is written atomically.
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Same root as load_norms.py, so the default input/output paths line up.
DEFAULT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PATTERNS = ["data/norms_with_context_*.json", "data/norms_with_context_*.jsonl"]
REQUIRED_KEYS = frozenset({"norm_id", "norm", "text", "hierarchy"})


class MergeConflict(Exception):
    """Two inputs hold different records for the same norm."""


def expand_inputs(patterns: list[str], root: Path, exclude: Optional[Path] = None,
                  require_match: bool = True) -> list[Path]:
    """Expand files / wildcard patterns (relative to ``root``) into a de-duplicated,
    ordered list of existing files. ``exclude`` (the output file) is skipped.
    With ``require_match=False`` a wildcard that matches nothing is skipped silently
    (used for the default patterns); explicit file names must always exist."""
    paths: list[Path] = []
    for pat in patterns:
        full = pat if Path(pat).is_absolute() else str(root / pat)
        if any(ch in pat for ch in "*?["):
            matches = sorted(Path(m) for m in glob.glob(full))
            if not matches and require_match:
                raise FileNotFoundError(f"Pattern matched no files: {pat}")
        else:
            matches = [Path(full)]
            if not matches[0].is_file():
                raise FileNotFoundError(f"Input file not found: {matches[0]}")
        paths.extend(matches)

    seen, out = set(), []
    for p in paths:
        rp = p.resolve()
        if exclude is not None and rp == exclude.resolve():
            log.info("Skipping output file among inputs: %s", p)
            continue
        if rp not in seen:
            seen.add(rp)
            out.append(p)
    if not out:
        raise FileNotFoundError(
            f"No input files to merge (looked for: {', '.join(patterns)} under {root}).")
    return out


def _check_record(path: Path, i: int, rec) -> None:
    """Fail early on anything that is not a current load_norms.py record."""
    if not isinstance(rec, dict) or not REQUIRED_KEYS <= rec.keys():
        raise ValueError(f"{path}: record {i} is not a load_norms record "
                         f"(needs keys {sorted(REQUIRED_KEYS)})")
    nid = rec["norm_id"]
    if not isinstance(nid, str) or not nid:
        raise ValueError(f"{path}: record {i} has an empty or non-string norm_id")
    text = rec["text"]
    if not isinstance(text, dict) or "paragraph" not in text:
        raise ValueError(f"{path}: record {i} ({nid}) has no text.paragraph; "
                         f"re-export this source with the current load_norms.py")
    hierarchy = rec["hierarchy"]
    if not isinstance(hierarchy, dict) or not hierarchy.get("instrument_id"):
        raise ValueError(f"{path}: record {i} ({nid}) has no hierarchy.instrument_id")


def read_records(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        records = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{lineno}: invalid JSON ({e.msg})") from e
    else:
        records = json.loads(text)
        if not isinstance(records, list):
            raise ValueError(f"{path}: expected a JSON array of records, got {type(records).__name__}")
    for i, rec in enumerate(records):
        _check_record(path, i, rec)
    return records


def _label(key: tuple[str, int]) -> str:
    nid, occurrence = key
    return nid if occurrence == 0 else f"{nid} (occurrence {occurrence + 1})"


def merge(paths: list[Path], on_conflict: str = "error") -> tuple[list[dict], Counter]:
    """Merge records from ``paths``. Returns (records, stats)."""
    merged: list[dict] = []
    index: dict[tuple[str, int], int] = {}     # (norm_id, occurrence) -> position in ``merged``
    origin: dict[tuple[str, int], Path] = {}   # same key -> file that supplied the kept record
    conflicts: list[str] = []
    stats: Counter = Counter()

    for path in paths:
        records = read_records(path)
        stats[f"file:{path.name}"] = len(records)
        occurrences: Counter = Counter()
        for rec in records:
            nid = rec["norm_id"]
            key = (nid, occurrences[nid])
            occurrences[nid] += 1

            if key not in index:
                index[key] = len(merged)
                origin[key] = path
                merged.append(rec)
            elif merged[index[key]] == rec:
                stats["duplicates_identical"] += 1
            else:
                stats["duplicates_conflicting"] += 1
                if on_conflict == "error":
                    conflicts.append(f"{_label(key)}  ({origin[key].name} vs {path.name})")
                elif on_conflict == "last":
                    merged[index[key]] = rec
                    origin[key] = path
                # "first": keep what we have
        stats["shared_ids_within_files"] += sum(1 for n in occurrences.values() if n > 1)

    if conflicts:
        shown = "\n  ".join(conflicts[:10])
        more = f"\n  ... and {len(conflicts) - 10} more" if len(conflicts) > 10 else ""
        raise MergeConflict(
            f"{len(conflicts)} norm(s) have different records in different inputs "
            f"(use --on-conflict first|last to resolve):\n  {shown}{more}")
    return merged, stats


def atomic_write(path: Path, records: list[dict]) -> None:
    """Temp file + fsync + os.replace: a killed run never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            if path.suffix == ".jsonl":
                for rec in records:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            else:
                json.dump(records, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("inputs", nargs="*",
                    help="files or wildcard patterns (default: data/norms_with_context_*.json and *.jsonl)")
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="project root (default: %(default)s)")
    ap.add_argument("--out", type=Path, default=None,
                    help="output file; .jsonl = one record per line "
                         "(default: <root>/data/norms_with_context.json)")
    ap.add_argument("--on-conflict", choices=["error", "first", "last"], default="error",
                    help="what to do when one norm has differing records (default: error)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    out = args.out or (args.root / "data" / "norms_with_context.json")
    patterns = args.inputs or DEFAULT_PATTERNS

    try:
        paths = expand_inputs(patterns, args.root, exclude=out,
                              require_match=bool(args.inputs))
        records, stats = merge(paths, args.on_conflict)
    except (FileNotFoundError, ValueError, MergeConflict) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    existed = out.exists()
    atomic_write(out, records)

    print(f"Merged {len(paths)} file(s) into {out}{' (replaced existing file)' if existed else ''}")
    for p in paths:
        print(f"  {p.name:<55} {stats[f'file:{p.name}']:>6} records")
    print(f"Total norms: {len(records)}   identical duplicates dropped: {stats['duplicates_identical']}"
          f"   conflicts resolved: {stats['duplicates_conflicting']}"
          f"   ids shared within a file: {stats['shared_ids_within_files']}")
    per_instrument = Counter(r["hierarchy"]["instrument_id"] for r in records)
    for inst, n in sorted(per_instrument.items()):
        print(f"  {inst:<14} {n:>6}")
    resolutions = Counter(r["text"].get("resolution") for r in records)
    print("Text resolution: " + ", ".join(f"{k}={v}" for k, v in sorted(resolutions.items(), key=lambda kv: str(kv[0]))))
    return 0


if __name__ == "__main__":
    sys.exit(main())