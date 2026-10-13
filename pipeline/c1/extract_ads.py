#!/usr/bin/env python3
"""
Extract addressees from norm-extraction JSON files.

Accepts files or directories (searched recursively for *.json / *.jsonl).
Each file may contain a single article record, a list of records, a dict that
wraps a list of records, or JSON Lines (one record per line).

Outputs (in --out-dir):
  addressees.csv        one row per norm (with or without addressee, see --include-null)
  addressee_counts.csv  one row per distinct addressee string, with frequencies

Usage:
  python extract_addressees.py data/ --out-dir inventory/
  python extract_addressees.py a.json b.json --include-null
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterator

ROW_FIELDS = [
    "file", "uid", "celex", "instrument_id", "jurisdiction", "language",
    "article", "heading", "paragraph_index", "norm_number", "deontic",
    "addressee", "addressee_key", "addressee_type", "recipient_body",
    "extraction_model", "human_verified",
]


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def iter_files(paths: list[str]) -> Iterator[Path]:
    for p in map(Path, paths):
        if p.is_dir():
            yield from sorted(f for f in p.rglob("*") if f.suffix in (".json", ".jsonl"))
        elif p.is_file():
            yield p
        else:
            print(f"warning: not found: {p}", file=sys.stderr)


def iter_records(obj) -> Iterator[dict]:
    """Yield every dict that has a 'norms' list, wherever it sits in the structure."""
    if isinstance(obj, dict):
        if isinstance(obj.get("norms"), list):
            yield obj
        else:
            for v in obj.values():
                if isinstance(v, (list, dict)):
                    yield from iter_records(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from iter_records(item)


def load_file(path: Path) -> Iterator[dict]:
    text = path.read_text(encoding="utf-8")
    try:
        yield from iter_records(json.loads(text))
        return
    except json.JSONDecodeError:
        pass
    # JSON Lines fallback
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip().rstrip(",")
        if not line:
            continue
        try:
            yield from iter_records(json.loads(line))
        except json.JSONDecodeError as e:
            print(f"warning: {path}:{n}: could not parse ({e.msg})", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #
def addressee_key(s: str | None) -> str:
    """Light grouping key only: lowercase + collapsed whitespace. No linguistic
    normalisation here, so the inventory shows the real variation."""
    return re.sub(r"\s+", " ", s).strip().lower() if s else ""


def rows_from_record(rec: dict, file: Path, include_null: bool) -> Iterator[dict]:
    for norm in rec.get("norms", []):
        addr = norm.get("addressee")
        if not addr and not include_null:
            continue
        recip = norm.get("recipient_body") or []
        yield {
            "file": str(file),
            "uid": rec.get("uid"),
            "celex": rec.get("celex"),
            "instrument_id": rec.get("instrument_id"),
            "jurisdiction": rec.get("jurisdiction"),
            "language": rec.get("language"),
            "article": rec.get("article"),
            "heading": rec.get("heading"),
            "paragraph_index": norm.get("paragraph_index"),
            "norm_number": norm.get("number"),
            "deontic": norm.get("deontic"),
            "addressee": addr,
            "addressee_key": addressee_key(addr),
            "addressee_type": norm.get("addressee_type"),
            "recipient_body": " | ".join(map(str, recip)) if isinstance(recip, list) else recip,
            "extraction_model": norm.get("extraction_model"),
            "human_verified": norm.get("human_verified"),
        }


def summarise(rows: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["addressee_key"]:
            groups[r["addressee_key"]].append(r)

    out = []
    for key, rs in groups.items():
        variants = Counter(r["addressee"] for r in rs)
        instruments = Counter(r["instrument_id"] or r["celex"] for r in rs)
        out.append({
            "addressee_key": key,
            "n_norms": len(rs),
            "n_instruments": len(instruments),
            "n_variants": len(variants),
            "most_common_variant": variants.most_common(1)[0][0],
            "instruments": "; ".join(f"{i} ({c})" for i, c in instruments.most_common()),
            "addressee_types": "; ".join(f"{t} ({c})" for t, c in
                                         Counter(str(r["addressee_type"]) for r in rs).most_common()),
            "deontic": "; ".join(f"{d} ({c})" for d, c in
                                 Counter(str(r["deontic"]) for r in rs).most_common()),
            "jurisdictions": "; ".join(sorted({str(r["jurisdiction"]) for r in rs})),
            "example_uid": rs[0]["uid"],
        })
    out.sort(key=lambda d: (-d["n_norms"], d["addressee_key"]))
    return out


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    # utf-8-sig so Excel shows ë, é etc. correctly
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def row_key(r: dict) -> tuple:
    """Identity of a norm row, used to skip rows already in the output file."""
    return tuple(str(r.get(k) if r.get(k) is not None else "")
                 for k in ("uid", "paragraph_index", "norm_number", "addressee"))


def read_existing(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != ROW_FIELDS:
            sys.exit(f"error: {path} has different columns than this script writes; "
                     f"use --overwrite or another --out-dir")
        return list(reader)


def append_rows(path: Path, new_rows: list[dict], existing: list[dict]) -> list[dict]:
    """Append rows not yet present; return the rows actually added."""
    seen = {row_key(r) for r in existing}
    added = []
    for r in new_rows:
        k = row_key(r)
        if k not in seen:
            seen.add(k)
            added.append(r)
    new_file = not path.exists()
    # BOM only when the file is created, never in the middle of the file
    with path.open("a", newline="", encoding="utf-8-sig" if new_file else "utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ROW_FIELDS)
        if new_file:
            w.writeheader()
        w.writerows(added)
    return added


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", help="JSON/JSONL files or directories")
    ap.add_argument("--out-dir", default="addressee_inventory")
    ap.add_argument("--include-null", action="store_true",
                    help="also write norms without an addressee to addressees.csv")
    ap.add_argument("--overwrite", action="store_true",
                    help="start a fresh output instead of appending to the existing one")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows_path = out_dir / "addressees.csv"
    counts_path = out_dir / "addressee_counts.csv"
    if args.overwrite:
        rows_path.unlink(missing_ok=True)
        counts_path.unlink(missing_ok=True)

    new_rows, n_files, n_records = [], 0, 0
    for f in iter_files(args.inputs):
        n_files += 1
        for rec in load_file(f):
            n_records += 1
            new_rows.extend(rows_from_record(rec, f, args.include_null))

    existing = read_existing(rows_path)
    added = append_rows(rows_path, new_rows, existing)

    # counts are always rebuilt from the full cumulative file
    all_rows = existing + added
    counts = summarise(all_rows)
    if counts:
        write_csv(counts_path, counts, list(counts[0].keys()))

    with_addr = sum(1 for r in all_rows if r["addressee_key"])
    print(f"this run: files {n_files}, article records {n_records}, "
          f"rows added {len(added)}, duplicates skipped {len(new_rows) - len(added)}")
    print(f"cumulative: rows {len(all_rows)}, norms with addressee {with_addr}, "
          f"distinct addressees {len(counts)}")
    if counts:
        top = sum(c["n_norms"] for c in counts[:20])
        print(f"top 20 addressees cover {top / with_addr:.0%} of norms with an addressee")
    print(f"output: {out_dir.resolve()}")


if __name__ == "__main__":
    main()