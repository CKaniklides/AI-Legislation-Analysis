#!/usr/bin/env python3
"""norm_cards.py - build one LLM-readable card per norm, and join two cards into a pair.

Design choices
--------------
* One card per NORM, not per pair. A norm takes part in up to PER_NORM_CAP pairs
  (select_candidates.py), so building per pair would repeat the same work and, if
  stored, the same text up to that many times.
* Cards are BUILT ONCE AND STORED (norm_cards.jsonl), pairs are ASSEMBLED AT RUN TIME.
  Building a card means running every defined term of the instrument over the text;
  cheap, but pointless to repeat on every LLM call or re-run. Assembling a pair is a
  string join, so storing pairs would only duplicate cards on disk.
  For a few thousand norms the card file is a few MB and fits in memory.
* Cards are stored as parts (body + definitions), not as one finished string, so that
  render_pair() can print definitions shared by A and B only once.
* Each card carries card_hash. Include both hashes in the LLM cache key, so a card that
  changes (new definitions, re-extraction) is reviewed again instead of hitting an
  outdated cache entry.

What a card contains
--------------------
  source       instrument id, jurisdiction, and the instrument type read from the CELEX
               id (R = regulation, L = directive, D = decision); "act" for Dutch BWB ids
  location     chapter, article, heading, paragraph
  text         the verbatim provision text; scope="article" when the extraction covered
               the whole article. Texts longer than MAX_TEXT_CHARS keep the first
               sentence and the sentence(s) containing the action, joined by " […] ".
  extracted    the extracted fields (actor renamed addressee), empty fields left out;
               reliability="uncertain" with the disagreeing fields and the other reading
               when the extractor flagged the norm
  definitions  only definitions of terms that occur in the text shown on the card,
               from the norm's own instrument, at most MAX_DEFINITIONS. Terms that also
               occur in the extracted fields come first and are tagged with that field;
               the rest follow in order of appearance. Terms defined in the norm's own
               article (e.g. a definitions article) are left out.

Term matching
-------------
  Case-insensitive, whole words; a leading "de/het/een" in the term is ignored. Each
  word may be inflected (-en, -s, -n, -'s, and the long-vowel plural systeem ->
  systemen). The longest term wins: "AI-systeem voor algemene doeleinden" is reported
  as that term, not also as "AI-systeem". If one document defines a term twice with
  different text, the first definition is kept and a warning is printed.

Usage
-----
  python norm_cards.py                                   # all norms
  python norm_cards.py --queue data/conflict/review_queue.jsonl   # only queued norms
  python norm_cards.py --show 32024R1689:4:whole:1      # print one card

  from norm_cards import load_cards, render_pair
  cards = load_cards(Path("data/conflict/norm_cards.jsonl"))
  text = render_pair(cards[id_a], cards[id_b])
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

DEFAULT_NORMS = Path("data/new_norms/original_norms.json")
DEFAULT_OUT = Path("data/new_norms/LLM_norm_cards.jsonl")
DEFAULT_DEFINITIONS_GLOB = "data/definitions/*_definitions.json"

MAX_TEXT_CHARS = 6000       # longer texts are reduced to the relevant sentences
MAX_DEFINITIONS = 8         # per card
MAX_DEFINITION_CHARS = 600  # longer definitions are cut and marked […]
CARD_VERSION = "1"          # bump when the card layout changes

# Extracted fields shown on the card, in this order: (card label, field in the record).
FIELDS = [
    ("deontic", "deontic"),
    ("addressee", "actor"),
    ("trigger", "trigger_event"),
    ("action", "action_lemma"),
    ("object", "object"),
    ("conditions", "conditions"),
    ("recipient", "recipient"),
    ("deadline", "deadline_raw"),
    ("thresholds", "thresholds"),
]
# Fields searched for defined terms (the ones a comparison depends on).
TERM_FIELDS = ("actor", "trigger_event", "object", "conditions", "recipient")

CELEX_TYPES = {"R": "regulation", "L": "directive", "D": "decision"}


# ---------------------------------------------------------------------------
# Definitions: loading and term matching
# ---------------------------------------------------------------------------

def parse_json_loose(raw: str):
    raw = raw.strip()
    for attempt in (raw, "[" + raw.rstrip().rstrip(",") + "]"):
        try:
            data = json.loads(attempt)
            return data if isinstance(data, list) else [data]
        except json.JSONDecodeError:
            pass
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def load_json(path: Path):
    try:
        return parse_json_loose(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"Error reading {path}: {e}")


def resolve_definition_paths(paths):
    """Explicit -d paths, or every file matching the default glob."""
    if paths:
        return [Path(p) for p in paths]
    found = sorted(Path(".").glob(DEFAULT_DEFINITIONS_GLOB))
    if not found:
        sys.exit(f"No definitions files found ({DEFAULT_DEFINITIONS_GLOB}); pass -d.")
    return found


def load_definitions(paths, allowed_patterns):
    """{document: [definition, ...]}, one entry per term (first definition kept)."""
    by_doc = defaultdict(dict)          # document -> {term_lower: definition dict}
    conflicts = []
    for path in paths:
        for item in load_json(path):
            if not isinstance(item, dict):
                continue
            if "definitions" in item:
                defs, fallback_doc = item["definitions"], item.get("document")
            elif "term" in item:
                defs, fallback_doc = [item], None
            else:
                continue
            for d in defs:
                doc = d.get("document") or fallback_doc
                term, definition = (d.get("term") or "").strip(), (d.get("definition") or "").strip()
                if not (doc and term and definition):
                    continue
                if allowed_patterns and d.get("pattern") not in allowed_patterns:
                    continue
                key = term.lower()
                existing = by_doc[str(doc)].get(key)
                if existing is None:
                    by_doc[str(doc)][key] = {**d, "term": term, "definition": definition}
                elif existing["definition"] != definition:
                    conflicts.append((str(doc), term, path.name))
    for doc, term, fname in conflicts:
        print(f"Warning: {doc}: term {term!r} defined more than once with different text "
              f"(first kept; duplicate in {fname})", file=sys.stderr)
    return {doc: list(terms.values()) for doc, terms in by_doc.items()}



_VOWELS = "aeiou"


def word_variants(word: str, inflect: bool) -> list[str]:
    """Spellings of one word of a term that should still match it."""
    variants = [re.escape(word) + (r"(?:en|s|n|['’]s)?" if inflect else "")]
    if inflect:
        # Dutch long-vowel plural: a doubled vowel before the final consonant is
        # written single before -en (systeem -> systemen, incident is unaffected).
        m = re.match(rf"^(.*)([{_VOWELS}])\2([^{_VOWELS}\W\d])$", word, re.IGNORECASE)
        if m:
            variants.append(re.escape(m.group(1) + m.group(2) + m.group(3)) + "en")
    return variants


def term_regex(term: str, inflect: bool):
    core = re.sub(r"^(?:de|het|een)\s+", "", term.strip(), flags=re.IGNORECASE)
    words = core.split()
    if not words:
        return None
    # Every word may be inflected, not just the last: Dutch multi-word terms often put
    # the head noun first ("AI-systemen voor algemene doeleinden"). Inflecting the
    # other words cannot cause false matches, because the whole phrase must match.
    pattern = r"\s+".join("(?:" + "|".join(word_variants(w, inflect)) + ")" for w in words)
    return re.compile(r"(?<!\w)" + pattern + r"(?!\w)", re.IGNORECASE)


class Matcher:
    def __init__(self, defs, inflect: bool):
        entries = [(d, term_regex(d["term"], inflect)) for d in defs]
        # Longest terms first, so they claim their span before shorter terms inside them.
        self.entries = sorted(((d, rx) for d, rx in entries if rx),
                              key=lambda e: -len(e[0]["term"]))

    def find(self, text: str, skip_article=None):
        """Defined terms in `text`, in order of first occurrence, longest match winning.
        Terms defined in `skip_article` are left out (the defining article itself)."""
        taken: list[tuple[int, int]] = []
        hits = []
        for d, rx in self.entries:
            if skip_article is not None and str(d.get("article")) == skip_article:
                continue
            for m in rx.finditer(text):
                s, e = m.span()
                if any(s < te and ts < e for ts, te in taken):
                    continue            # inside (or overlapping) a longer term already found
                taken.append((s, e))
                hits.append((s, d))
                break                   # one occurrence per term is enough
        hits.sort(key=lambda h: h[0])
        return [d for _, d in hits]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def instrument_type(instrument_id: str, jurisdiction: str | None) -> str:
    m = re.match(r"^\d{5}([A-Z])\d+$", instrument_id or "")
    if m:
        return CELEX_TYPES.get(m.group(1), "EU act")
    if (instrument_id or "").startswith("BWBR"):
        return "act"
    return "unknown"


def as_text(value) -> str | None:
    """Field value -> one line, or None if empty."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        parts = [as_text(v) for v in value]
        parts = [p for p in parts if p]
        return "; ".join(parts) or None
    if isinstance(value, dict):
        parts = [f"{k}={v}" for k, v in value.items() if v not in (None, "", [], {})]
        return ", ".join(parts) or None
    s = str(value).strip()
    return s or None


def one_line(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def reduce_text(text: str, action_span) -> tuple[str, bool]:
    """Keep long texts within MAX_TEXT_CHARS: first sentence + the sentence(s) that
    contain the most action-span words. Returns (text, was_reduced)."""
    if len(text) <= MAX_TEXT_CHARS:
        return text, False
    sentences = [s for s in re.split(r"(?<=[.;:])\s+", text) if s.strip()]
    spans = [w.lower() for w in (action_span or []) if isinstance(w, str) and w.strip()]
    scores = [sum(w in s.lower() for w in spans) for s in sentences]
    best = max(scores, default=0)
    keep = {0} | ({i for i, sc in enumerate(scores) if sc == best} if best else set())
    out, used = [], 0
    for i in sorted(keep):
        if used + len(sentences[i]) > MAX_TEXT_CHARS and out:
            break
        out.append(sentences[i])
        used += len(sentences[i])
    if not best:                    # action not found: plain head of the text
        out, used = [], 0
        for s in sentences:
            if used + len(s) > MAX_TEXT_CHARS:
                break
            out.append(s)
            used += len(s)
    reduced = " […] ".join(out) if best else " ".join(out) + " […]"
    return reduced, True


def disagreed_fields(note: str | None) -> list[str]:
    """['action_lemma', 'object'] from "... disagreed on ['action_lemma', 'object'] ..."."""
    m = re.search(r"\[([^\]]*)\]", note or "")
    return re.findall(r"[A-Za-z_]+", m.group(1)) if m else []


def alternative_lines(norm: dict) -> list[str]:
    """The other extraction reading(s), only the fields that differ from the stored one."""
    out = []
    for alt in norm.get("alternative_reading") or []:
        diffs = []
        for label, field in FIELDS:
            if field in alt and as_text(alt.get(field)) != as_text(norm.get(field)):
                diffs.append(f"{label}: {as_text(alt.get(field)) or 'none'}")
        if diffs:
            out.append("; ".join(diffs))
    for d in norm.get("deontic_alternative") or []:
        if as_text(d) and as_text(d) != norm.get("deontic"):
            out.append(f"deontic: {as_text(d)}")
    return out


# ---------------------------------------------------------------------------
# Card
# ---------------------------------------------------------------------------

def build_card(record: dict, matchers: dict[str, Matcher]) -> dict:
    """One norm record (norm / text / hierarchy) -> stored card."""
    norm = record.get("norm") or {}
    text_info = record.get("text") or {}
    h = record.get("hierarchy") or {}
    norm_id = str(record["norm_id"])
    instrument = str(h.get("instrument_id") or norm_id.split(":")[0])
    article = str(h.get("article") or "")

    # --- text ---
    raw_text = one_line(text_info.get("paragraph") or "")
    text, reduced = reduce_text(raw_text, norm.get("action_span"))
    scope = ("article" if text_info.get("resolution") == "all_paragraphs"
             or h.get("paragraph_number") is None else "paragraph")
    scope_attr = scope + (" reduced" if reduced else "")

    # --- header lines ---
    source = " | ".join(x for x in (instrument, h.get("jurisdiction"),
                                    instrument_type(instrument, h.get("jurisdiction"))) if x)
    location = " | ".join(x for x in (
        f"ch. {h['chapter']}" if h.get("chapter") else None,
        f"art. {article}" if article else None,
        one_line(h["article_heading"]) if h.get("article_heading") else None,
        f"par. {h.get('paragraph_number') or 'whole'}",
    ) if x)

    # --- extracted fields ---
    uncertain = bool(norm.get("extraction_uncertain"))
    ext_lines = [f"{label}: {v}" for label, field in FIELDS
                 if (v := as_text(norm.get(field)))]
    alt = alternative_lines(norm) if uncertain else []
    disagreed = [lbl for lbl, fld in FIELDS if fld in disagreed_fields(norm.get("uncertainty_note"))]
    attr = 'reliability="uncertain"' + (f' disagreed="{", ".join(disagreed)}"' if disagreed else "") \
        if uncertain else 'reliability="normal"'
    extracted = f"<extracted {attr}>\n" + "\n".join(ext_lines) \
        + "".join(f"\n<alt>{a}</alt>" for a in alt) + "\n</extracted>"

    # --- definitions: only terms in the text shown, field terms first ---
    matcher = matchers.get(instrument)
    definitions = []
    if matcher is not None and text:
        in_text = matcher.find(text, skip_article=article)
        field_of: dict[str, str] = {}
        for field in TERM_FIELDS:
            label = next(lbl for lbl, f in FIELDS if f == field)
            value = as_text(norm.get(field))
            if value:
                for d in matcher.find(value, skip_article=article):
                    field_of.setdefault(d["term"].lower(), label)
        ordered = ([d for d in in_text if d["term"].lower() in field_of]
                   + [d for d in in_text if d["term"].lower() not in field_of])
        for d in ordered[:MAX_DEFINITIONS]:
            definition = one_line(d["definition"])
            if len(definition) > MAX_DEFINITION_CHARS:
                definition = definition[:MAX_DEFINITION_CHARS].rsplit(" ", 1)[0] + " […]"
            definitions.append({
                "term": d["term"],
                "definition": definition,
                "source": f"art. {d.get('article')}" if d.get("article") else instrument,
                "tag": f"field:{field_of[d['term'].lower()]}" if d["term"].lower() in field_of else "text",
            })

    body = (f"source: {source}\nlocation: {location}\n"
            f'<text scope="{scope_attr}">{text}</text>\n{extracted}')
    card = {"norm_id": norm_id, "instrument_id": instrument, "card_version": CARD_VERSION,
            "body": body, "definitions": definitions}
    card["card_hash"] = hashlib.sha256(
        json.dumps(card, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return card


def _definition_line(d: dict) -> str:
    return f"{d['term']} [{d['source']}, {d['tag']}]: {d['definition']}"


def render_card(card: dict, side: str, skip_terms: frozenset = frozenset()) -> str:
    defs = [d for d in card["definitions"] if d["term"].lower() not in skip_terms]
    block = ("\n<definitions>\n" + "\n".join(_definition_line(d) for d in defs)
             + "\n</definitions>") if defs else ""
    return f'<norm id="{card["norm_id"]}" side="{side}">\n{card["body"]}{block}\n</norm>'


def render_pair(card_a: dict, card_b: dict, facts: str | None = None) -> str:
    """Two cards -> one pair block. Definitions with the same term and text on both
    sides (same instrument) are printed once, after both cards. `facts` is an optional
    pre-rendered facts block, placed first."""
    by_a = {(d["term"].lower(), d["definition"]): d for d in card_a["definitions"]}
    shared = [d for d in card_b["definitions"] if (d["term"].lower(), d["definition"]) in by_a]
    skip = frozenset(d["term"].lower() for d in shared)
    parts = [f"<facts>\n{facts}\n</facts>"] if facts else []
    parts += [render_card(card_a, "A", skip), render_card(card_b, "B", skip)]
    if shared:
        lines = []
        for d_b in shared:
            d_a = by_a[(d_b["term"].lower(), d_b["definition"])]
            tag = d_a["tag"] if d_a["tag"] == d_b["tag"] else f"A {d_a['tag']}; B {d_b['tag']}"
            lines.append(_definition_line({**d_a, "tag": tag}))
        parts.append('<definitions for="A B">\n' + "\n".join(lines) + "\n</definitions>")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Build / load
# ---------------------------------------------------------------------------

def load_norm_records(path: Path) -> list[dict]:
    data = load_json(path)
    if len(data) == 1 and isinstance(data[0], dict) and isinstance(data[0].get("norms"), list):
        data = data[0]["norms"]
    return [r for r in data if isinstance(r, dict) and "norm_id" in r]


def queued_norm_ids(path: Path) -> set[str]:
    ids = set()
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                ids.update((str(r["norm_a"]), str(r["norm_b"])))
    return ids


def load_cards(path: Path) -> dict[str, dict]:
    with path.open(encoding="utf-8") as f:
        return {c["norm_id"]: c for c in (json.loads(l) for l in f if l.strip())}


def main() -> None:
    p = argparse.ArgumentParser(description="Build one LLM card per norm.")
    p.add_argument("--norms", type=Path, default=DEFAULT_NORMS)
    p.add_argument("-d", "--definitions", nargs="+",
                   help="definitions files (default: data/definitions/*_definitions.json)")
    p.add_argument("--queue", type=Path, default=None,
                   help="review_queue.jsonl; build cards only for norms in it")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--show", metavar="NORM_ID", help="print the card of one norm and exit")
    args = p.parse_args()

    by_doc = load_definitions(resolve_definition_paths(args.definitions), set())
    matchers = {doc: Matcher(defs, True) for doc, defs in by_doc.items()}
    records = load_norm_records(args.norms)

    if args.show:
        rec = next((r for r in records if str(r["norm_id"]) == args.show), None)
        if rec is None:
            sys.exit(f"norm_id {args.show!r} not found in {args.norms}")
        print(render_card(build_card(rec, matchers), "A"))
        return

    wanted = queued_norm_ids(args.queue) if args.queue else None
    if wanted is not None:
        records = [r for r in records if str(r["norm_id"]) in wanted]

    cards, no_defs_for = [], set()
    for rec in records:
        card = build_card(rec, matchers)
        if card["instrument_id"] not in matchers:
            no_defs_for.add(card["instrument_id"])
        cards.append(card)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_name(args.out.name + ".tmp")
    tmp.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cards), encoding="utf-8")
    os.replace(tmp, args.out)

    n_defs = sum(len(c["definitions"]) for c in cards)
    chars = sum(len(render_card(c, "A")) for c in cards)
    print(f"{len(cards)} cards -> {args.out}  ({n_defs} definitions, "
          f"~{chars // max(len(cards), 1) // 4} tokens per card on average)")
    if wanted is not None and len(cards) < len(wanted):
        missing = wanted - {c["norm_id"] for c in cards}
        print(f"Warning: {len(missing)} queued norm_ids not in {args.norms}, "
              f"e.g. {sorted(missing)[:3]}", file=sys.stderr)
    for doc in sorted(no_defs_for):
        print(f"Note: no definitions loaded for instrument {doc!r}", file=sys.stderr)


if __name__ == "__main__":
    main()