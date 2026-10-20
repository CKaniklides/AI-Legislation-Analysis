"""Frequencies of actor, action_lemma and object in a norms file.

Input : data/new_norms/original_norms_NL.json   (JSON array of norm records)
Output: data/conflict_NL/norm_field_freq.json

For each of ``norm.actor``, ``norm.action_lemma`` and ``norm.object``, every distinct
value is listed with its absolute frequency (number of norms carrying it) and relative
frequency (absolute / number of norms). Missing, null or empty values are counted as
"(null)"; list values are joined with " | ".

Output format
-------------
    {
      "input": "...", "n_norms": 3000,
      "fields": {
        "actor":        [{"value": "...", "absolute": 12, "relative": 0.004}, ...],
        "action_lemma": [...],
        "object":       [...]
      }
    }

Values are sorted by absolute frequency, highest first. The file is written to a temp
file and moved into place, so a killed run never leaves a half-written file.

Usage
-----
    python norm_field_freq.py
    python norm_field_freq.py --input data/new_norms/other_norms.json
    python norm_field_freq.py --out data/conflict_NL/freq.json --csv data/conflict_NL/freq.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

FIELDS = ("actor", "action_lemma", "object")
NULL_LABEL = "(null)"

DEFAULT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_INPUT = Path("data/new_norms/original_norms_NL.json")
DEFAULT_OUTPUT = Path("data/conflict_NL/norm_field_freq.json")


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_records(path: Path) -> list[dict]:
    records = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(records, list):
        raise ValueError(f"{path}: expected a JSON array of records, got {type(records).__name__}")
    for i, rec in enumerate(records):
        if not isinstance(rec, dict) or "norm_id" not in rec:
            raise ValueError(f"{path}: record {i} needs 'norm_id'")
    return records


def normalise(value) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return NULL_LABEL
    if isinstance(value, list):
        return " | ".join(str(v) for v in value) if value else NULL_LABEL
    return str(value).strip()


# ---------------------------------------------------------------------------
# Counting and writing
# ---------------------------------------------------------------------------


def count_fields(records: list[dict]) -> dict[str, Counter]:
    counters = {field: Counter() for field in FIELDS}
    for rec in records:
        norm = rec.get("norm") if isinstance(rec.get("norm"), dict) else {}
        for field in FIELDS:
            counters[field][normalise(norm.get(field))] += 1
    return counters


def frequency_table(counter: Counter, n: int) -> list[dict]:
    return [{"value": value, "absolute": count, "relative": round(count / n, 6)}
            for value, count in counter.most_common()]


def _atomic_write(path: Path, write) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            write(fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_json(path: Path, result: dict) -> None:
    _atomic_write(path, lambda fh: json.dump(result, fh, ensure_ascii=False, indent=2))


def write_csv(path: Path, fields: dict[str, list[dict]]) -> None:
    def write(fh):
        w = csv.writer(fh)
        w.writerow(["field", "value", "absolute_frequency", "relative_frequency"])
        for field, rows in fields.items():
            for row in rows:
                w.writerow([field, row["value"], row["absolute"], row["relative"]])
    _atomic_write(path, write)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="project root (default: %(default)s)")
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT,
                    help="norms file, relative to --root (default: %(default)s)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUTPUT,
                    help="frequency file, relative to --root (default: %(default)s)")
    ap.add_argument("--csv", type=Path, default=None,
                    help="optional CSV copy of the results, relative to --root")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    def resolve(p: Path) -> Path:
        return p if p.is_absolute() else args.root / p

    in_path, out_path = resolve(args.input), resolve(args.out)
    if not in_path.is_file():
        print(f"ERROR: input not found: {in_path}", file=sys.stderr)
        return 1
    try:
        records = load_records(in_path)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    n = len(records)
    if n == 0:
        print(f"ERROR: {in_path} contains no records", file=sys.stderr)
        return 1
    no_norm = sum(not isinstance(r.get("norm"), dict) for r in records)
    if no_norm:
        log.warning("%d record(s) have no 'norm' object; their fields count as %s.", no_norm, NULL_LABEL)

    counters = count_fields(records)
    fields = {field: frequency_table(counters[field], n) for field in FIELDS}
    write_json(out_path, {"input": str(in_path), "n_norms": n, "fields": fields})
    if args.csv:
        write_csv(resolve(args.csv), fields)

    print(f"Read {n} norms from {in_path}")
    for field in FIELDS:
        print(f"\n=== {field} ({len(fields[field])} distinct) ===")
        print(f"{'abs':>8}  {'rel':>7}  value")
        for row in fields[field]:
            print(f"{row['absolute']:>8,}  {row['relative']:>7.2%}  {row['value']}")
    print(f"\nWrote {out_path}")
    if args.csv:
        print(f"Wrote {resolve(args.csv)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())