"""
Stage 1: lexical action-verb mapping.

Maps the verb(s) in each norm's `action_lemma` to verb families in the lexicon.
No clause segmentation, negation detection, polarity flipping,
deontic interpretation, or pairwise scoring.

Source field: `action_lemma`. If it is empty the norm is reported as "empty"
(scored 0 downstream). With --span-fallback, an empty lemma falls back to the
action reconstructed from `action_span` (then `action`).

Dependency: verb_lexicon.py
  FAMILIES, GENERIC_AXES, LITERALS

Usage:
    python action_verbs.py --norms AI_ACT_norms.json --out action_verbs.json
    python action_verbs.py --norms AI_ACT_norms.json --out action_verbs.json --span-fallback
    python action_verbs.py --demo
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from pipeline.candidate_filter.action.verb_lexicon import FAMILIES, GENERIC_AXES, LITERALS

except ImportError:
    from pipeline.candidate_filter.action.verb_lexicon import FAMILIES, GENERIC_AXES, LITERALS
    

FLAGS = re.IGNORECASE

COMPILED = {
    family: [re.compile(pattern, FLAGS) for pattern in patterns]
    for family, patterns in FAMILIES.items()
}

# Prevent a known false positive: "gemachtigde" is a noun,
# not a form of the verb family "machtig".
NOT_VERBS = re.compile(
    r"\b(?:gemachtigden?|gemachtigdes)\b", FLAGS
)

# Each entry:
# (family, particle, finite forms, infinitive, anchored)
#
# Anchored particles must close the action string or be followed
# by a preposition. This is a lexical matching constraint, not
# clause segmentation or a claim about grammatical scope.

SEPARABLE = [
    ("appoint", "aan", "wijst|wijzen", "wijzen", True),
    ("appoint", "aan", "stelt|stellen", "stellen", True),
    ("appoint", "in", "stelt|stellen", "stellen", True),
    ("appoint", "op", "richt|richten", "richten", True),
    ("provide", "in", "dient|dienen", "dienen", True),
    ("provide", "voor", "legt|leggen", "leggen", True),
    ("provide", "over", "legt|leggen", "leggen", True),
    ("provide", "toe", "zendt|zenden", "zenden", True),
    ("provide", "aan", "levert|leveren", "leveren", True),
    ("provide", "ter beschikking", "stelt|stellen", "stellen", False),
    ("provide", "beschikbaar", "stelt|stellen", "stellen", False),
    ("notify", "in kennis", "stelt|stellen", "stellen", False),
    ("publish", "openbaar", "maakt|maken", "maken", False),
    ("publish", "bekend", "maakt|maken", "maken", False),
    ("transfer", "door", "geeft|geven", "geven", True),
    ("transfer", "door", "zendt|zenden", "zenden", True),
    ("access", "in", "ziet|zien", "zien", True),
    ("withhold", "geheim", "houdt|houden", "houden", False),
    ("withhold", "vertrouwelijk", "houdt|houden|behandelt|behandelen",
     "houden", False),
    ("collect", "in", "wint|winnen", "winnen", True),
    ("retain", "op", "slaat|slaan", "slaan", True),
    ("use", "in", "zet|zetten", "zetten", True),
    ("authorise", "toe", "staat|staan", "staan", True),
    ("authorise", "toe", "laat|laten", "laten", True),
    ("authorise", "toe", "kent|kennen", "kennen", True),
    ("authorise", "goed", "keurt|keuren", "keuren", True),
    ("deny", "af", "wijst|wijzen", "wijzen", True),
    ("deny", "in", "trekt|trekken", "trekken", True),
    ("deny", "uit", "sluit|sluiten", "sluiten", True),
    ("require", "voor", "schrijft|schrijven", "schrijven", True),
    ("require", "op", "legt|leggen", "leggen", True),
    ("exempt", "vrij", "stelt|stellen", "stellen", True),
    ("operate", "voort", "zet|zetten", "zetten", True),
    ("halt", "op", "schort|schorten", "schorten", True),
    ("halt", "stop", "zet|zetten", "zetten", True),
    ("halt", "stil", "legt|leggen", "leggen", True),
    ("halt", "uit", "schakelt|schakelen", "schakelen", True),
    ("market", "in de handel", "brengt|brengen", "brengen", False),
    ("market", "op de markt", "brengt|brengen", "brengen", False),
    ("market", "in gebruik", "stelt|stellen|neemt|nemen",
     "stellen", False),
    ("market", "aan", "biedt|bieden", "bieden", True),
    ("withdraw", "uit de handel", "neemt|nemen", "nemen", False),
    ("withdraw", "van de markt", "haalt|halen|neemt|nemen",
     "halen", False),
    ("withdraw", "buiten gebruik", "stelt|stellen", "stellen", False),
    ("withdraw", "terug", "roept|roepen", "roepen", True),
    ("withdraw", "terug", "neemt|nemen", "nemen", True),
    ("dismiss", "af", "zet|zetten", "zetten", True),
    ("dismiss", "op", "heft|heffen", "heffen", True),
    ("dismiss", "af", "schaft|schaffen", "schaffen", True),
    ("increase", "uit", "breidt|breiden", "breiden", True),
    ("decrease", "in", "perkt|perken", "perken", True),
    ("ensure", "zorg", "draagt|dragen", "dragen", False),
    ("ensure", "toe", "ziet|zien", "zien", True),
    ("measure", "toe", "past|passen", "passen", True),
    ("measure", "in", "voert|voeren", "voeren", True),
    ("assess", "toezicht", "houdt|houden", "houden", False),
    ("record", "vast", "legt|leggen", "leggen", True),
    ("record", "bij", "houdt|houden", "houden", True),
    ("adopt", "vast", "stelt|stellen", "stellen", True),
    ("adopt", "uit", "vaardigt|vaardigen", "vaardigen", True),
    ("amend", "aan", "vult|vullen", "vullen", True),
    ("amend", "toe", "voegt|voegen", "voegen", True),
    ("amend", "aan", "past|passen", "passen", True),
    ("amend", "bij", "werkt|werken", "werken", True),
    ("comply", "na", "leeft|leven", "leven", True),
    ("comply", "na", "komt|komen", "komen", True),
    ("consider", "rekening", "houdt|houden", "houden", False),
    ("consider", "in aanmerking", "neemt|nemen", "nemen", False),
    ("consider", "in acht", "neemt|nemen", "nemen", False),
    ("consider", "in overweging", "neemt|nemen", "nemen", False),
    ("produce", "op", "stelt|stellen", "stellen", True),
    ("produce", "uit", "werkt|werken", "werken", True),
    ("produce", "op", "zet|zetten", "zetten", True),
    ("produce", "op", "maakt|maken", "maken", True),
    ("condition", "afhankelijk", "stelt|stellen", "stellen", False),
    ("contain", "in", "voorziet|voorzien", "voorzien", True),
    ("contain", "in", "bouwt|bouwen", "bouwen", True),
    # v2: separable verbs of the new / extended families
    ("notify", "aan", "meldt|melden", "melden", True),
    ("contain", "aan", "brengt|brengen", "brengen", True),
    ("contain", "bij", "voegt|voegen", "voegen", True),
    ("require", "op", "draagt|dragen", "dragen", True),
    ("require", "af", "dwingt|dwingen", "dwingen", True),
    ("cooperate", "samen", "werkt|werken", "werken", True),
    ("cooperate", "deel", "neemt|nemen", "nemen", True),
    ("cooperate", "aan", "gaat|gaan", "gaan", True),
    ("request", "aan", "vraagt|vragen", "vragen", True),
    ("request", "uit", "nodigt|nodigen", "nodigen", True),
    ("perform", "uit", "voert|voeren", "voeren", True),
    ("perform", "uit", "oefent|oefenen", "oefenen", True),
    ("describe", "aan", "geeft|geven", "geven", True),
    ("describe", "aan", "toont|tonen", "tonen", True),
    ("describe", "uiteen", "zet|zetten", "zetten", True),
    ("describe", "toe", "licht|lichten", "lichten", True),
    ("support", "aan", "moedigt|moedigen", "moedigen", True),
]

PREPOSITIONS = (
    "aan|bij|in|op|voor|met|naar|van|tot|binnen|"
    "door|over|uit|tegen|onder"
)

# Explicit lexical exception: "stelt vast dat ..." is not necessarily
# an instance of the legislative-act family "adopt".
SEPARABLE_EXCLUDE_NEXT = {
    ("adopt", "vast"): re.compile(
        r"\s*,?\s*(?:dat|of|welke|wie|hoe)\b", FLAGS
    )
}


def clean_action(text: str) -> str:
    text = text.replace("\u00a0", " ")
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"\s+", " ", text).strip()


def phrase_pattern(words: str) -> str:
    return r"\s+".join(re.escape(w) for w in words.split())


@dataclass(frozen=True)
class Hit:
    family: str
    start: int
    end: int
    matched: str
    source: str
    parts: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class SepEntry:
    family: str
    finite_re: re.Pattern
    particle_re: re.Pattern
    te_re: re.Pattern
    exclude_next: re.Pattern | None


SEP_COMPILED = []

for family, particle, finite, infinitive, anchored in SEPARABLE:
    if family not in FAMILIES:
        raise ValueError(f"Unknown verb family: {family}")

    p = phrase_pattern(particle)

    particle_pattern = (
        rf"\b{p}\b(?=\s*$|\s+(?:{PREPOSITIONS})\b)"
        if anchored else rf"\b{p}\b"
    )

    SEP_COMPILED.append(
        SepEntry(
            family=family,
            finite_re=re.compile(rf"\b(?:{finite})\b", FLAGS),
            particle_re=re.compile(particle_pattern, FLAGS),
            te_re=re.compile(
                rf"\b{p}\s+te\s+{re.escape(infinitive)}\b",
                FLAGS,
            ),
            exclude_next=SEPARABLE_EXCLUDE_NEXT.get(
                (family, particle)
            ),
        )
    )


def overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def find_hits(text: str) -> list[Hit]:
    """Return lexical matches in source order, without interpretation."""
    contiguous: list[Hit] = []

    # Ordinary lexicon matches.
    for family, patterns in COMPILED.items():
        for pattern in patterns:
            for match in pattern.finditer(text):
                if (
                    match.end() > match.start()
                    and not NOT_VERBS.fullmatch(match.group())
                ):
                    contiguous.append(
                        Hit(
                            family=family,
                            start=match.start(),
                            end=match.end(),
                            matched=match.group(),
                            source="lexicon",
                            parts=((match.start(), match.end()),),
                        )
                    )

    separable: list[Hit] = []

    # Infinitive constructions such as "aan te wijzen".
    for entry in SEP_COMPILED:
        for match in entry.te_re.finditer(text):
            separable.append(
                Hit(
                    family=entry.family,
                    start=match.start(),
                    end=match.end(),
                    matched=match.group(),
                    source="separable_te",
                    parts=((match.start(), match.end()),),
                )
            )

    # Finite forms followed by a separable particle.
    # The search is lexical and bounded by punctuation, not clauses.
    for entry in SEP_COMPILED:
        for match in entry.finite_re.finditer(text):
            search_end = len(text)

            # Avoid matching particles across strong punctuation.
            boundary = re.search(r"[;:.!?]", text[match.end():])
            if boundary:
                search_end = match.end() + boundary.start()

            particle = entry.particle_re.search(
                text, match.end(), search_end
            )
            if particle is None:
                continue

            if (
                entry.exclude_next
                and entry.exclude_next.match(text, particle.end())
            ):
                continue

            verb_span = (match.start(), match.end())
            particle_span = (particle.start(), particle.end())

            separable.append(
                Hit(
                    family=entry.family,
                    start=match.start(),
                    end=particle.end(),
                    matched=(
                        f"{match.group()} … {particle.group()}"
                    ),
                    source="separable",
                    parts=(verb_span, particle_span),
                )
            )

    # A recognized separable construction takes precedence over
    # ordinary matches overlapping either component.
    separable_parts = [
        part for hit in separable for part in hit.parts
    ]

    contiguous = [
        hit for hit in contiguous
        if not any(
            overlaps((hit.start, hit.end), part)
            for part in separable_parts
        )
    ]

    # Deduplicate ordinary matches, preferring the longest match.
    seen: set[tuple[int, int, str]] = set()
    unique: list[Hit] = []

    for hit in sorted(
        contiguous,
        key=lambda h: (-(h.end - h.start), h.start, h.family),
    ):
        key = (hit.start, hit.end, hit.family)
        if key not in seen:
            seen.add(key)
            unique.append(hit)

    # Remove overlapping ordinary matches, but retain distinct
    # family readings over an identical span.
    kept: list[Hit] = []

    for hit in unique:
        clash = False

        for existing in kept:
            if overlaps(
                (hit.start, hit.end),
                (existing.start, existing.end),
            ):
                same_span = (
                    hit.start == existing.start
                    and hit.end == existing.end
                )
                if same_span and hit.family != existing.family:
                    continue
                clash = True
                break

        if not clash:
            kept.append(hit)

    # Deduplicate separable matches by verb position and family.
    sep_seen: set[tuple[tuple[int, int], str]] = set()
    sep_unique: list[Hit] = []

    for hit in separable:
        key = (hit.parts[0], hit.family)
        if key not in sep_seen:
            sep_seen.add(key)
            sep_unique.append(hit)

    return sorted(
        kept + sep_unique,
        key=lambda h: (h.start, h.end, h.family),
    )


def hit_to_dict(hit: Hit) -> dict:
    """Map a lexical family to the literal values in the lexicon."""
    axes = [
        {"axis": axis, "polarity": polarity}
        for axis, polarity in LITERALS[hit.family]
    ]

    return {
        "family": hit.family,
        "axes": axes,
        "matched": hit.matched,
        "start": hit.start,
        "end": hit.end,
        "source": hit.source,
        "parts": [
            {"start": start, "end": end}
            for start, end in hit.parts
        ],
        "generic": any(
            item["axis"] in GENERIC_AXES for item in axes
        ),
    }


def extract_action_verbs(action: Any) -> dict:
    """Extract lexical verb mappings only."""
    if not isinstance(action, str) or not action.strip():
        return {
            "action_clean": None,
            "verbs": [],
            "primary": None,
            "verb_status": "empty",
            "no_verb_found": False,
        }

    text = clean_action(action)
    verbs = [hit_to_dict(hit) for hit in find_hits(text)]

    status = "verb" if verbs else "none"

    return {
        "action_clean": text,
        "verbs": verbs,
        "primary": verbs[0] if verbs else None,
        "verb_status": status,
        "no_verb_found": status == "none",
    }


def load_norm_records(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as file:
        data = json.load(file)

    if isinstance(data, list):
        return [record for record in data if isinstance(record, dict)]

    if isinstance(data, dict) and isinstance(data.get("norms"), list):
        return [
            record for record in data["norms"]
            if isinstance(record, dict)
        ]

    if isinstance(data, dict):
        return [
            {"norm_id": key, **value}
            for key, value in data.items()
            if isinstance(value, dict)
        ]

    raise ValueError(f"Unrecognised input structure: {path}")


def inner_norm(record: dict) -> dict:
    norm = record.get("norm")
    return norm if isinstance(norm, dict) else record


def norm_action_context(
    record: dict, norm: dict, span_fallback: bool = False
) -> str | None:
    """Return the text to map: `action_lemma`, or (optionally) a fallback.

    The lemma is the primary source. Only when it is empty and span_fallback is
    set, the action is recovered from action_span (via the paragraph) or `action`.
    """
    lemma = norm.get("action_lemma")
    if isinstance(lemma, str) and lemma.strip():
        return clean_action(lemma)

    if not span_fallback:
        return None

    span = norm.get("action_span")
    text_obj = record.get("text")
    paragraph = (
        text_obj.get("paragraph")
        if isinstance(text_obj, dict) else None
    )

    if isinstance(span, list) and span:
        fragments = [
            clean_action(fragment)
            for fragment in span
            if isinstance(fragment, str) and fragment.strip()
        ]

        if fragments and isinstance(paragraph, str) and paragraph.strip():
            cursor = 0
            first_start = None
            last_end = None

            for fragment in fragments:
                match = re.search(
                    re.escape(fragment),
                    paragraph[cursor:],
                    FLAGS,
                )
                if not match:
                    break

                start = cursor + match.start()
                end = cursor + match.end()

                if first_start is None:
                    first_start = start

                last_end = end
                cursor = end
            else:
                if first_start is not None and last_end is not None:
                    return clean_action(paragraph[first_start:last_end])

        if fragments:
            return clean_action(" ".join(fragments))

    action = norm.get("action")
    if isinstance(action, str) and action.strip():
        return clean_action(action)

    return None


def run(norms_path: Path, out_path: Path, span_fallback: bool = False) -> None:
    records = load_norm_records(norms_path)
    results = []
    family_counts: Counter[str] = Counter()
    primary_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()

    for record in records:
        norm = inner_norm(record)
        action = norm_action_context(record, norm, span_fallback)
        mapping = extract_action_verbs(action)

        for verb in mapping["verbs"]:
            family_counts[verb["family"]] += 1

        if mapping["primary"]:
            primary_counts[mapping["primary"]["family"]] += 1

        status_counts[mapping["verb_status"]] += 1

        text_obj = record.get("text")
        paragraph = (
            text_obj.get("paragraph")
            if isinstance(text_obj, dict) else None
        )

        results.append({
            "norm_id": record.get(
                "norm_id", norm.get("norm_id")
            ),
            "action": action,
            "action_span": norm.get("action_span"),
            "action_lemma": norm.get("action_lemma"),
            "source_paragraph": paragraph,
            "extraction_uncertain": norm.get(
                "extraction_uncertain", False
            ),
            "uncertainty_note": norm.get("uncertainty_note"),
            **mapping,
        })

    total = len(results)
    missing = sum(
        result["verb_status"] == "none" for result in results
    )

    stats = {
        "n_norms": total,
        "verb_status": dict(status_counts.most_common()),
        "no_verb_found": missing,
        "primary_family_counts": dict(primary_counts.most_common()),
        "all_family_counts": dict(family_counts.most_common()),
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)

    with out_path.open("w", encoding="utf-8") as file:
        json.dump(
            {
                "stage": "action-verb-mapping",
                "schema_version": 3,
                "input": str(norms_path),
                "source_field": "action_lemma"
                + (" (span fallback)" if span_fallback else ""),
                "stats": stats,
                "norms": results,
            },
            file,
            ensure_ascii=False,
            indent=2,
        )

    print(f"Processed {total} norms -> {out_path}")
    print("Verb status:", stats["verb_status"])
    print(
        f"No verb found: {missing} "
        f"({missing / max(total, 1):.1%})"
    )
    print("Primary families:", stats["primary_family_counts"])
    print("All families:", stats["all_family_counts"])

    if missing:
        unmapped = Counter(
            result["action"].lower() for result in results
            if result["verb_status"] == "none"
        )
        print(f"\nMost frequent unmapped lemmas ({len(unmapped)} distinct):")
        for lemma, count in unmapped.most_common(25):
            print(f"  {count:4}  {lemma}")


DEMO_ACTIONS = [
    "maatregelen nemen",
    "op de hoogte brengen",
    "samenwerken",
    "verzenden, doorgeven of anderszins raadplegen",
    "schorsen of intrekken",
    "rekening houden",
    "beëindigt het mandaat",
    "verstrekt de gegevens niet aan derden",
    "verstrekt informatie die niet openbaar is",
    "mag de persoonsgegevens niet langer bewaren",
    "onthoudt zich van het verstrekken van informatie",
    "wijst geen gemachtigde aan",
    "voert de niet-nalevingsprocedure uit",
    "stelt eisen aan de aanbieder",
    "In afwijking van lid 1 mogen de lidstaten de vergunning intrekken",
]


def demo() -> None:
    for action in DEMO_ACTIONS:
        result = extract_action_verbs(action)
        print(f"\n{action}")

        if not result["verbs"]:
            print(f"  ({result['verb_status']})")

        for verb in result["verbs"]:
            axes = ", ".join(
                f"{item['axis']}:{item['polarity']:+d}"
                for item in verb["axes"]
            )
            print(
                f"  {verb['family']:<12} {axes:<35} "
                f"{verb['matched']!r} "
                f"[{verb['source']}]"
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stage 1: lexical action-verb mapping."
    )
    parser.add_argument(
        "--norms",
        type=Path,
        default=Path("AI_ACT_norms.json"),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("action_verbs.json"),
    )
    parser.add_argument(
        "--span-fallback",
        action="store_true",
        help="Use action_span/action when action_lemma is empty.",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run built-in examples.",
    )
    args = parser.parse_args()

    if args.demo:
        demo()
    else:
        run(args.norms, args.out, args.span_fallback)


if __name__ == "__main__":
    main()