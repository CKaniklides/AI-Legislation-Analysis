"""Load every extracted norm together with its raw text and hierarchy.

What this produces
------------------
One flat list of records, one per norm. Each record has three parts::

    {
      "norm_id":   "32024R1689:10:1:2:None",    # same format as candidates' _norm_id()
      "norm":      { ...all norm parameters exactly as extracted... },
      "text": {
          "paragraph":  "<raw text of the paragraph the norm was extracted from>",
                    "resolution": "paragraph_index" | "paragraph_number" | "all_paragraphs" | "provision_text"
      },
      "hierarchy": {
          "instrument_id", "jurisdiction", "graph_uid",
          "chapter", "chapter_number", "section",
          "article", "article_heading",
          "paragraph_number"          # as displayed in the law ("lid 6"), 1-based
      }
    }

Design choices
--------------
* Nothing is filtered, inferred or rewritten. Norms with any deontic type, or with a
  missing addressee_type, are kept as extracted (use ``--eligible-only`` to apply the
  deontic filter of the candidate generator). The source JSON is never mutated.
* Paragraph text is resolved position-first (``paragraph_index``), then by displayed
  paragraph number, then falls back to the provision text. ``text.resolution`` records
  which route was taken, so weak links are auditable. Displayed numbers are not unique
  within an article (AI Act art. 73 has two lids shown as "11"), hence position first.
* ``norm_id`` uses the same recipe as the candidate generator, so ids and any caches
  keyed on them stay compatible.

Usage
-----
    python load_norms.py                          # writes data/norms_with_context.json
    python load_norms.py --out norms.jsonl        # one record per line
    python load_norms.py --eligible-only
    python load_norms.py --list-sources                # show what --only can match
    python load_norms.py --only ai_act                 # one source (path substring)
    python load_norms.py --file data/laws/gdpr/32016R0679_original_provisions.json
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

log = logging.getLogger(__name__)

# Same layout assumption as the candidate generator: <root>/src/<pkg>/<this file>.
# Override with --root if this script lives elsewhere.
DEFAULT_ROOT = Path(__file__).resolve().parent.parent.parent

ELIGIBLE_DEONTICS = frozenset({"OBLIGATION", "PROHIBITION", "COMPETENCE"})


# ---------------------------------------------------------------------------
# Sources (mirrors the candidate generator and extract_norms.py)
# ---------------------------------------------------------------------------



def _all_provisions(root):
    if isinstance(root, dict):
        if "norms" in root:
            return [root]
        if "bijlage_35" in root:
            # The Bijlage 35 dataset shape, also kept by extract_norms.py and merge_norms.py
            # outputs (data/new_norms/...). Without this, --file read 0 norms from it (fixed
            # 22 Oct 2026). Its provisions carry no instrument_id; copies get it, the file is
            # not changed.
            return [{**p, "instrument_id": p.get("instrument_id") or "BWBR0049497"}
                    for p in _bijlage35_provisions(root)]
        return []
    if isinstance(root, list):
        return root
    raise ValueError("Expected a provision object or a list of provisions")


def _bijlage35_provisions(root):
    return root["bijlage_35"]["substantive_text"]["provisions"]


@dataclass(frozen=True)
class Source:
    """One input file. ``instrument_id`` is a fallback used only when a provision
    carries none; there is deliberately no silent default."""
    path: str
    instrument_id: Optional[str] = None
    get_provisions: Callable[[dict], list] = _all_provisions


SOURCES = [
    Source("data/new_norms/new_Cbw_norms_merged.json"),
    Source("data/new_norms/new_UAVG_norms_merged.json"),
    Source("data/new_norms/new_Wdo_norms_merged.json"),
    Source("data/new_norms/new_Telecommunicatiewet_norms_merged.json"),
    Source("data/new_norms/new_Uitvoeringswet_norms_merged.json"),
    Source("data/laws/gdpr/32016R0679_original_provisions.json"),
    Source("data/laws/nis2/32022L2555_original_provisions.json"),
    Source("data/laws/dora/32022R2554_original_provisions.json"),
    Source("data/laws/ai_act/32024R1689_original_provisions.json"),
    Source("Datasets/Dutch Laws/bijlage35_dataset.json", "BWBR0049497", _bijlage35_provisions),
]

# Hand-built datasets carry no graph uid; rebuild it using graph.py's naming convention.
_GRAPH_UID_TEMPLATES = {
    "BWBR0051796": "nl:BWBR0051796:art{number}@2025-11-21",
    "BWBR0049497": "nl:BWBR0049497:bijlage35-p{number}@2026-08-15",
}


def _graph_uid(p: dict, instrument_id: str) -> Optional[str]:
    if "uid" in p:
        return p["uid"]
    template = _GRAPH_UID_TEMPLATES.get(instrument_id)
    return template.format(number=p["number"]) if template else None


# ---------------------------------------------------------------------------
# Record building
# ---------------------------------------------------------------------------



def make_norm_id(
    instrument_id: str,
    article: str,
    norm: dict,
    ordinal: int,
) -> str:
    paragraph_number = norm.get(
        "paragraph_number",
        norm.get("number")
    )
    return (
        f"{instrument_id}:{article}:"
        f"{paragraph_number if paragraph_number is not None else 'whole'}:"
        f"{ordinal}"
    )



def _paragraph_text(paragraph: dict) -> str:
    """Raw text of one entry in ``provision["paragraphs"]``."""
    return paragraph.get("text") or ""


def _resolve_paragraph(
    norm: dict,
    paragraphs: list[dict],
) -> tuple[Optional[dict], str]:
    """Resolve by paragraph index when available, otherwise by paragraph number."""
    idx = norm.get("paragraph_index")
    if isinstance(idx, int) and 0 <= idx < len(paragraphs):
        return paragraphs[idx], "paragraph_index"

    number = norm.get("paragraph_number", norm.get("number"))
    if number is not None:
        for paragraph in paragraphs:
            if str(paragraph.get("number")) == str(number):
                return paragraph, "paragraph_number"

    return None, "all_paragraphs"


# "HOOFDSTUK I. ALGEMENE BEPALINGEN" -> "I"; "Hoofdstuk 3a. ..." -> "3a". The number is
# returned as written (Roman numerals stay Roman). Keyword is case-insensitive, the
# numeral is not, so a title word such as "Mix" cannot be read as a numeral.
_CHAPTER_NUMBER_RE = re.compile(
    r"^\s*(?i:hoofdstuk|chapter)\s+([IVXLCDM]+|\d+[A-Za-z]?)(?=[\s.:\-\u2013\u2014]|$)")


def _chapter_number(chapter: Optional[str]) -> Optional[str]:
    m = _CHAPTER_NUMBER_RE.match(chapter) if chapter else None
    return m.group(1) if m else None


def _hierarchy(provision: dict, instrument_id: str, article: str,
               heading: str, norm: dict, paragraph: Optional[dict]) -> dict:
    chapter = provision.get("chapter")
    return {
        "instrument_id": instrument_id,
        "jurisdiction": provision.get("jurisdiction"),
        "graph_uid": _graph_uid(provision, instrument_id),
        "chapter": chapter,
        "chapter_number": _chapter_number(chapter),
        "section": provision.get("section"),
        "article": article,
        "article_heading": heading,
        "paragraph_number": (paragraph.get("number") if paragraph else norm.get("paragraph_number", norm.get("number"))),
    }


def _records_from_provision(provision: dict, source: Source,
                            eligible_only: bool, stats: Counter) -> list[dict]:
    instrument_id = provision.get("instrument_id") or source.instrument_id
    if not instrument_id:
        raise ValueError(
            f"No instrument_id for provision "
            f"{provision.get('article') or provision.get('number')!r} in {source.path}; "
            f"set Source.instrument_id for this file.")

    article = str(provision.get("article") or provision.get("number"))
    heading = provision.get("heading") or f"Article {article}"
    paragraphs = provision.get("paragraphs") or []

    out = []
    for ordinal, norm in enumerate(provision.get("norms", []), start=1):
        deontic = norm.get("deontic")
        if eligible_only and deontic not in ELIGIBLE_DEONTICS:
            stats[f"dropped_deontic:{deontic}"] += 1
            continue

        paragraph, how = _resolve_paragraph(norm, paragraphs)
        if paragraph is not None:
            paragraph_text = _paragraph_text(paragraph)
        else:
            # No single paragraph matched: use the text of every paragraph in the article.
            paragraph_text = "\n\n".join(
                t for t in (_paragraph_text(p) for p in paragraphs) if t)
            if not paragraph_text:  # legacy files that only carry provision-level text
                paragraph_text = provision.get("text", "")
                how = "provision_text"
        stats[f"text_resolution:{how}"] += 1
        
        text = {"paragraph": paragraph_text, "resolution": how}

        out.append({
            "norm_id": make_norm_id(instrument_id, article, norm, ordinal),
            "norm": copy.deepcopy(norm),  # never hand out a view into the source data
            "text": text,
            "hierarchy": _hierarchy(provision, instrument_id, article,
                                    heading, norm, paragraph),
        })
    return out


def load_norms(root: Path = DEFAULT_ROOT,
               sources: Iterable[Source] = SOURCES,
               eligible_only: bool = True,
               skip_missing: bool = False) -> list[dict]:
    """Load every norm from every source as a record (see module docstring)."""
    records: list[dict] = []
    stats: Counter = Counter()

    for source in sources:
        path = root / source.path
        if not path.exists():
            if skip_missing:
                log.warning("Missing source, skipped: %s", path)
                stats["missing_sources"] += 1
                continue
            raise FileNotFoundError(f"Source file not found: {path}")

        data = json.loads(path.read_text(encoding="utf-8"))
        before = len(records)
        for provision in source.get_provisions(data):
            records.extend(_records_from_provision(
                provision, source, eligible_only, stats))
        added = len(records) - before
        stats[f"instrument_norms:{source.path}"] = added
        log.info("%-70s %5d norms", source.path, added)

    # Duplicate ids would make pair ids collide downstream; surface them loudly.
    id_counts = Counter(r["norm_id"] for r in records)
    dupes = {k: v for k, v in id_counts.items() if v > 1}
    if dupes:
        log.warning("%d duplicate norm_id values (e.g. %s). Siblings in one paragraph "
                    "need a distinct norm_index.", len(dupes), next(iter(dupes)))
    stats["duplicate_norm_ids"] = len(dupes)

    log.info("Loaded %d norms; %s", len(records),
             {k: v for k, v in sorted(stats.items()) if not k.startswith("instrument_norms:")})
    return records


def select_sources(patterns: Iterable[str], sources: Iterable[Source] = SOURCES) -> list[Source]:
    """Pick sources whose path contains ANY of the patterns (case-insensitive substring),
    e.g. ``["ai_act"]``, ``["32016R0679"]`` or ``["gdpr", "nis2"]`` (note: "nis2" matches
    both the EU directive and the Dutch implementation). Fails loudly when nothing matches,
    so a typo cannot silently produce an empty export."""
    sources = list(sources)
    pats = [p.lower() for p in patterns]
    chosen = [s for s in sources if any(p in s.path.lower() for p in pats)]
    if not chosen:
        available = "\n  ".join(s.path for s in sources)
        raise ValueError(f"No source matches {list(patterns)}. Available sources:\n  {available}")
    return chosen


def load_single(path: str, instrument_id: Optional[str] = None, root: Path = DEFAULT_ROOT,
                **kwargs) -> list[dict]:
    """Load one file. If ``path`` is one of the known SOURCES it reuses that definition
    (including its provision extractor); otherwise it is treated as a plain provisions
    list and needs ``instrument_id`` only when its provisions carry none."""
    known = [s for s in SOURCES if s.path == path]
    source = known[0] if known else Source(path, instrument_id)
    return load_norms(root, [source], **kwargs)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def atomic_write(path: Path | str, records: list[dict]) -> None:
    """Write via temp file + fsync + os.replace, so a killed run cannot leave a
    half-written file. ``.jsonl`` gets one record per line, anything else a JSON array."""
    path = Path(path)
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
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="project root (default: %(default)s)")
    ap.add_argument("--out", type=Path, default=None,
                    help="output file; .jsonl = one record per line (default: <root>/data/norms_with_context.json)")
    ap.add_argument("--eligible-only", action="store_true",
                    help="keep only OBLIGATION / PROHIBITION / COMPETENCE norms")
    ap.add_argument("--skip-missing", action="store_true",
                    help="warn instead of failing when a source file is missing")
    ap.add_argument("--only", nargs="+", metavar="PATTERN",
                    help="load only sources whose path contains any PATTERN, e.g. --only ai_act")
    ap.add_argument("--file", metavar="PATH",
                    help="load one specific file (relative to --root, or absolute)")
    ap.add_argument("--instrument-id", metavar="ID",
                    help="fallback instrument_id for --file when its provisions carry none")
    ap.add_argument("--list-sources", action="store_true", help="print the known sources and exit")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.list_sources:
        for s in SOURCES:
            print(s.path)
        return 0
    if args.only and args.file:
        ap.error("use either --only or --file, not both")

    if args.file:
        known = [s for s in SOURCES if s.path == args.file]
        sources = known or [Source(args.file, args.instrument_id)]
        label = Path(args.file).stem
    elif args.only:
        sources = select_sources(args.only)
        label = "_".join(args.only)
    else:
        sources, label = SOURCES, None

    records = load_norms(args.root, sources, args.eligible_only, args.skip_missing)

    # A partial export gets its own default name so it can never overwrite the full one.
    default_name = "norms_with_context.json" if label is None else f"norms_with_context_{label}.json"
    out = args.out or (args.root / "data" / default_name)
    atomic_write(out, records)

    per_instrument = Counter(r["hierarchy"]["instrument_id"] for r in records)
    print(f"Wrote {len(records)} norms to {out}")
    for inst, n in sorted(per_instrument.items()):
        print(f"  {inst:<14} {n:>6}")
    return 0


if __name__ == "__main__":
    sys.exit(main())