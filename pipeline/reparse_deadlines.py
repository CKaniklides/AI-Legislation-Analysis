# -*- coding: utf-8 -*-
"""
Re-derive every stored norm's `deadline` from its own `deadline["raw"]` phrase using the
CURRENT _parse_deadline -- no model calls, no re-extraction.

Why this exists: extract_norms.py skips paragraphs that already have norms, so after the
deadline-parser changes (verbatim `from`, earliest-match-wins, ...) existing data would keep
the old parse while newly processed units get the new one. The parse is a deterministic
function of the stored phrase, so it can be refreshed in place.

What it touches: ONLY norms[].deadline (value / unit / from / raw). Nothing else in any file.
What it never touches: norms with human_verified == true (reported, left as they are).

Safety, in order:
  1. DRY-RUN BY DEFAULT: writes nothing until you pass --apply.
  2. Format guard: a file is only rewritten if re-serialising it with the pipeline's own
     json settings reproduces it byte-for-byte; otherwise the diff would be noise, so the file
     is skipped with a warning (override: --allow-format-change).
  3. Backup: the first time a file is changed, the ORIGINAL is copied to
     data/_backup_deadline_reparse/<date>/ and never overwritten by a later run.
  4. Atomic writes: temp file, then rename. A crash can't leave a truncated source file.
  5. Idempotent: running it again changes nothing.

Side effect worth knowing: parsing every stored phrase also fills
data/stage6_unrecognised_deadlines.json with a corpus-wide picture of what the parser
can't (fully) read -- the list that should drive the next lexicon extension.

Usage (from the folder containing extract_norms.py):
    python reparse_deadlines.py                       # dry-run, all sources
    python reparse_deadlines.py --only "Cbw"          # dry-run, one source
    python reparse_deadlines.py --report changes.json # also save every change as JSON
    python reparse_deadlines.py --apply               # write changes (after reviewing!)
"""
import argparse
import json
import shutil
import sys
from collections import Counter
from datetime import date

import extract_norms as en

# Must match how extract_norms.py writes these files (json.dumps(root, ensure_ascii=False, indent=1)).
JSON_FORMAT = dict(ensure_ascii=False, indent=1)


def _key(d: dict) -> tuple:
    return (d.get("value"), d.get("unit"), d.get("from"))


def _write_atomic(path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def main() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run, writes nothing)")
    ap.add_argument("--only", default=None, help="substring match against a source's label")
    ap.add_argument("--report", default=None, help="write every change to this JSON file")
    ap.add_argument("--allow-format-change", action="store_true",
                    help="rewrite files even if re-serialising them would reformat them")
    args = ap.parse_args()

    backup_dir = en.ROOT / "data" / "_backup_deadline_reparse" / date.today().isoformat()
    all_changes = []
    totals = Counter()

    for spec in en.SOURCES:
        if args.only and args.only.lower() not in spec.label.lower():
            continue
        path = en.ROOT / spec.path
        if not path.exists():
            print(f"\n=== {spec.label}: file not found ({spec.path}) -- skipped")
            continue

        text = path.read_text(encoding="utf-8")
        root = json.loads(text)
        format_ok = json.dumps(root, **JSON_FORMAT) == text

        stats = Counter()
        changes = []
        for provision in spec.get_provisions(root):
            for norm in provision.get("norms") or []:
                old = norm.get("deadline")
                if not old or not old.get("raw"):
                    stats["no_deadline"] += 1
                    continue
                new = en._parse_deadline(old["raw"])
                if _key(new) == _key(old):
                    stats["unchanged"] += 1
                    continue
                kind = ("from_only" if (new["value"], new["unit"]) == (old.get("value"), old.get("unit"))
                        else "value_or_unit")
                rec = {
                    "source": spec.label,
                    "instrument_id": provision.get("instrument_id") or provision.get("provision_id"),
                    "article": provision.get("article") or provision.get("number"),
                    "paragraph_number": norm.get("number"),
                    "norm_index": norm.get("norm_index"),
                    "raw": old["raw"], "kind": kind,
                    "old": {"value": old.get("value"), "unit": old.get("unit"), "from": old.get("from")},
                    "new": {"value": new["value"], "unit": new["unit"], "from": new["from"]},
                }
                if norm.get("human_verified"):
                    stats["skipped_human_verified"] += 1
                    rec["skipped"] = "human_verified"
                    changes.append(rec)
                    continue
                stats[kind] += 1
                changes.append(rec)
                if args.apply:
                    norm["deadline"] = new

        print(f"\n=== {spec.label} ({spec.path}) ===")
        print("  " + ", ".join(f"{k}={v}" for k, v in sorted(stats.items())) if stats else "  (no norms)")
        for rec in [c for c in changes if c["kind"] == "value_or_unit"]:
            tag = "  [SKIPPED: human_verified]" if rec.get("skipped") else ""
            print(f"  VALUE/UNIT  {rec['article']} para {rec['paragraph_number']}: {rec['raw']!r}\n"
                  f"              {rec['old']['value']} {rec['old']['unit']}  ->  "
                  f"{rec['new']['value']} {rec['new']['unit']}{tag}")
        from_only = [c for c in changes if c["kind"] == "from_only"]
        for rec in from_only[:5]:
            print(f"  from-only   {rec['old']['from']!r}  ->  {rec['new']['from']!r}")
        if len(from_only) > 5:
            print(f"  ... and {len(from_only) - 5} more from-only change(s)")

        n_to_write = stats["from_only"] + stats["value_or_unit"]
        if args.apply and n_to_write:
            if not format_ok and not args.allow_format_change:
                print(f"  !! NOT WRITTEN: re-serialising {path.name} would reformat it (diff would be "
                      f"noise). Check how it was last written, or pass --allow-format-change.")
                stats["files_skipped_format"] += 1
            else:
                backup_dir.mkdir(parents=True, exist_ok=True)
                dest = backup_dir / path.name
                if not dest.exists():          # keep the FIRST backup: the true original
                    shutil.copy2(path, dest)
                _write_atomic(path, json.dumps(root, **JSON_FORMAT))
                print(f"  wrote {n_to_write} change(s) -> {spec.path}  (original backed up in "
                      f"{backup_dir.relative_to(en.ROOT)})")
                stats["files_written"] += 1

        totals.update(stats)
        all_changes.extend(changes)

    print("\n" + "=" * 60)
    print("TOTAL: " + ", ".join(f"{k}={v}" for k, v in sorted(totals.items())))
    if not args.apply:
        print("DRY-RUN: nothing was written. Review the VALUE/UNIT lines above, then re-run with --apply.")
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(all_changes, f, ensure_ascii=False, indent=1)
        print(f"change report -> {args.report}")


if __name__ == "__main__":
    main()