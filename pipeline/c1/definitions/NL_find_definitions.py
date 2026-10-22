#!/usr/bin/env python3
"""Extract definitions from ONE Dutch legal document (run the script once per document).

Input (a single file)
  * .json / .jsonl : the document as a list of article records, each with a "text" field
                     and optionally "article", "paragraphs" [{"number", "text"}], "celex",
                     "instrument_id". A single record (one JSON object) also works.
  * .txt           : plain text, treated as one article.

Output (JSON)
  {
    "document": "32016R0679",
    "terms": ["persoonsgegevens", ...],
    "definitions": [
      {"term": ..., "definition": ..., "paragraph": ..., "article": ..., "document": ...,
       "pattern": ...},
      ...
    ]
  }
  "paragraph" is the paragraph number when the article record has paragraphs, otherwise null
  (e.g. a definitions article whose text is one list without numbered paragraphs).

Usage
  python find_definitions.py gdpr.json -o gdpr_definitions.json
  python find_definitions.py wet.txt --document "Wet X" > wet_definitions.json

Recognised patterns ("pattern" field)
  lijst            "... wordt verstaan onder:" followed by items such as
                       1) „persoonsgegevens”: alle informatie over ...;
                       a. aanbieder: degene die ...;
                   sub-items (a), b) under 1) ... or 1°. under a.) are folded into their parent;
                   "... verstaan onder X:" + list defines the single term X
  onder_verstaan   Onder X(, bedoeld in ...,) wordt (mede/tevens) verstaan/begrepen Y.
                   In dit artikel wordt onder «X» verstaan: Y.
  verstaan_onder   In dit artikel wordt verstaan onder X: Y.
  is_sprake        Van X is sprake indien Y.
  hieronder_verstaan   Voor zover wordt verwezen naar X, wordt hieronder verstaan / gedoeld op: Y.
                   (a definition that ends in ":" takes the following list as its text)
  hierna           de Algemene wet bestuursrecht (hierna: de Awb)
  tussen_haakjes   ... verwerkt („doelbinding”)   (label attached to the preceding clause;
                   lower confidence)
"""
import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

FLAGS = re.IGNORECASE
OPEN_Q = "„“\"‘«"
CLOSE_Q = "”“\"’»"
QUOTE_CHARS = "„”“\"‘’‚«»'"

# --- 1. List-style definitions -------------------------------------------------

INTRO = re.compile(
    r"\b(?:wordt|worden)\b[^.:\n]{0,150}?\bverstaan\s+onder\s*:?\s*$", FLAGS
)
# "... wordt verstaan onder publieke infrastructuur:" -> the whole list defines ONE term
INTRO_TERM = re.compile(r"\bverstaan\s+onder\s+(?P<term>[^:\n]{2,80}):\s*$", FLAGS)

MARKER = r"(?:–|[a-z]{1,3}[.)]|\d{1,3}[.°)]+)"
# BWB text often puts the marker on its own line ("–", "a.", "1°.") and the content below
MARKER_ONLY = re.compile(rf"^\s*(?P<marker>{MARKER})\s*$")

ITEM = re.compile(
    rf"^\s*(?:(?P<marker>{MARKER})\s+)?"
    r"(?P<term>"
    r"[" + OPEN_Q + r"][^" + CLOSE_Q + r"\n]{1,80}[" + CLOSE_Q + r"][^:;\n]{0,40}?"  # „quoted” (+ qualifier)
    r"|[^\W\d_][^:;\n]{1,80}?"                                                   # unquoted
    r")\s*:\s*(?P<definition>\S.*)?$"
)

# only a bare heading line ("Hoofdstuk 3", "Artikel 5a"), not "hoofdstuk VII van het Handvest ..."
STOP = re.compile(
    r"^\s*(?:(?:artikel|hoofdstuk|afdeling|paragraaf|titel)\s+[\dIVXLC]+[a-z.]*|§.*)\s*$", FLAGS
)

# --- 2. Sentence-style definitions ---------------------------------------------
# The definition is the rest of the line; if that is empty or ends in ":" (a list
# follows), the rest of the paragraph is taken as well -- see definition_after().

SENTENCE_PATTERNS = {
    # In dit artikel wordt verstaan onder openbare onderneming: onderneming waarop ...
    "verstaan_onder": re.compile(
        r"\bverstaan\s+onder\s+(?P<term>[^:\n]{2,80}?):[ \t]*(?P<definition>\S[^\n]*)", FLAGS
    ),
    # Van overwegende zeggenschap is sprake indien ...
    "is_sprake": re.compile(
        r"^Van\s+(?P<term>(?!dergelijke|deze|die|dit)[^,.;:\n]{2,100}?),?\s+is\s+sprake\s+"
        r"(?:indien|als|wanneer)\b[ \t]*(?P<definition>[^\n]*)",
        re.MULTILINE,
    ),
    # Voor zover in deze wet wordt verwezen naar X, wordt hieronder verstaan / gedoeld op: ...
    "hieronder_verstaan": re.compile(
        r"\bverwezen\s+naar\s+(?P<term>[^,\n]{2,80}),\s+wordt\s+(?:hieronder\s+verstaan|gedoeld\s+op)"
        r":?[ \t]*(?P<definition>[^\n]*)",
        FLAGS,
    ),
}

# Onder X(, bedoeld in ...,) wordt (mede|tevens) verstaan Y  /  wordt onder «X» verstaan: Y
# The span may contain commas, «», and "." or ":" that are not followed by a space
# (6a.12, 1:1). The term is cut out of the span afterwards by onder_term().
ONDER = re.compile(
    r"\bonder\s+(?P<span>(?:[^.;:\n]|[.:](?=\S)){2,400}?)\s+(?:verstaan|begrepen)\b"
    r"[:,]?[ \t]*(?P<definition>[^\n]*)",
    FLAGS,
)
WORDT = re.compile(r"\b(?:wordt|worden)\b", FLAGS)
TERM_CUT = re.compile(
    r",\s*bedoeld|\s+als\s+bedoeld|,|\s+(?:wordt|worden|kan|tevens|mede|ook|in\s+(?:dit|deze)\s+\w+)\b",
    FLAGS,
)
TRIGGER = re.compile(r"\b(?:verstaan|begrepen|is\s+sprake|gedoeld\s+op)\b", FLAGS)

# --- 3. Inline labels ------------------------------------------------------------

HIERNA = re.compile(
    r"\(\s*hierna\s*(?:te\s+noemen|aan\s+te\s+duiden\s+als)?\s*:?\s*"
    r"(?P<term>[^)]{2,80}?)\s*\)",
    FLAGS,
)
INLINE_QUOTED = re.compile(
    r"\(\s*[„“\"]\s*(?P<term>[^”“\"\n)]{2,80}?)\s*[”“\"]\s*\)"
)


@dataclass
class Definition:
    term: str
    definition: str
    paragraph: object  # str | None
    article: object    # str | None
    document: str
    pattern: str


def clean_term(term: str) -> str:
    term = re.sub("[" + re.escape(QUOTE_CHARS) + "]", "", term)
    term = re.sub(r"\s+", " ", term).strip()
    return re.sub(r"^(?:een|de|het)\s+", "", term)


def clean_def(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().rstrip(";,").strip(QUOTE_CHARS + " ")


def marker_kind(marker):
    if not marker:
        return "none"
    if marker == "–":
        return "dash"
    return "num" if marker[0].isdigit() else "alpha"


def preceding_clause(text: str, pos: int, max_len: int = 500) -> str:
    """The clause right before `pos`, cut at the previous ; : newline or sentence end."""
    window = text[max(0, pos - max_len):pos]
    clause = re.split(r"[;:\n]|(?<=[a-z\)])\.\s", window)[-1]
    return re.sub(r"^\s*(?:[a-z]{1,3}|\d{1,3})[.°)]+\s+", "", clause)


def definition_after(text: str, m) -> str:
    """Rest of the line; plus the rest of the paragraph when a list follows."""
    definition = m.group("definition").strip()
    if not definition or definition.endswith(":"):
        definition += " " + text[m.end():]
    return definition


def onder_term(span: str) -> str:
    span = span.strip()
    if span[:1] in OPEN_Q:  # «quoted term» -- take it whole
        close = re.search("[" + CLOSE_Q + "]", span[1:])
        return span[1:close.start() + 1] if close else span
    return TERM_CUT.split(span, 1)[0]


def find_list_definitions(text: str):
    in_block, blanks, block_kind, current, pending = False, 0, None, None, None

    def flush():
        nonlocal current
        if current and current["definition"].strip():
            yield current
        current = None

    for line in text.splitlines():
        m = INTRO_TERM.search(line)
        if m or INTRO.search(line):
            yield from flush()
            in_block, blanks, block_kind, pending = True, 0, None, None
            if m:  # every following line belongs to this one term
                block_kind = "single"
                current = {"term": clean_term(m.group("term")), "definition": "", "pattern": "lijst"}
            continue
        if not in_block:
            continue
        if not line.strip():
            blanks += 1
            if blanks >= 3:
                yield from flush()
                in_block = False
            continue
        blanks = 0
        if STOP.match(line):
            yield from flush()
            in_block = False
            continue

        mo = MARKER_ONLY.match(line)
        if mo:  # hold the marker for the content line below it
            pending = mo.group("marker")
            continue

        m = ITEM.match(line)
        marker = (m.group("marker") if m else None) or pending
        is_new_item = False
        if m and len(clean_term(m.group("term")).split()) <= 10:
            kind = marker_kind(marker)
            if block_kind is None:
                block_kind = kind
            # a marker of another kind (a. inside a – list) is a sub-item
            is_new_item = kind == block_kind

        if is_new_item:
            yield from flush()
            current = {
                "term": clean_term(m.group("term")),
                "definition": m.group("definition") or "",
                "pattern": "lijst",
            }
        elif current:
            prefix = pending + " " if pending and pending != "–" else ""
            current["definition"] += " " + prefix + line.strip()
        pending = None
    yield from flush()


def find_sentence_definitions(text: str):
    for name, rx in SENTENCE_PATTERNS.items():
        for m in rx.finditer(text):
            yield {"term": clean_term(m.group("term")), "definition": definition_after(text, m),
                   "pattern": name}
    for m in ONDER.finditer(text):
        before = text[max(0, m.start() - 40):m.start()]
        if not (WORDT.search(m.group("span")) or re.search(r"\b(?:wordt|worden)\b[^.\n]*$", before, FLAGS)):
            continue
        yield {"term": clean_term(onder_term(m.group("span"))), "definition": definition_after(text, m),
               "pattern": "onder_verstaan"}
    for name, rx in (("hierna", HIERNA), ("tussen_haakjes", INLINE_QUOTED)):
        for m in rx.finditer(text):
            yield {"term": clean_term(m.group("term")),
                   "definition": preceding_clause(text, m.start()), "pattern": name}


def extract(text: str, article, paragraph, document: str):
    seen = set()
    for d in [*find_list_definitions(text), *find_sentence_definitions(text)]:
        term, definition = d["term"], clean_def(d["definition"])
        if not term or not definition or term.lower() in seen:
            continue
        seen.add(term.lower())
        yield Definition(term, definition, paragraph, article, document, d["pattern"])


# --- input handling ------------------------------------------------------------

def parse_json_loose(raw: str):
    """A JSON array, a single object, comma-separated objects, or JSONL."""
    raw = raw.strip()
    for attempt in (raw, "[" + raw.rstrip().rstrip(",") + "]"):
        try:
            data = json.loads(attempt)
            return data if isinstance(data, list) else [data]
        except json.JSONDecodeError:
            pass
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def load_records(path: Path):
    raw = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() in (".json", ".jsonl"):
        return [r for r in parse_json_loose(raw) if isinstance(r, dict)]
    return [{"text": raw}]


def as_str(value):
    return None if value in (None, "") else str(value)


def process_document(records, document_override: str, fallback_name: str):
    definitions, unmatched, document = [], [], document_override
    for rec in records:
        doc = document_override or as_str(rec.get("celex") or rec.get("instrument_id")) or fallback_name
        document = document or doc
        article = as_str(rec.get("article"))
        paragraphs = [p for p in (rec.get("paragraphs") or []) if isinstance(p, dict) and p.get("text")]
        # search paragraph by paragraph so each hit knows its paragraph
        units = [(p["text"], as_str(p.get("number"))) for p in paragraphs] or \
                ([(rec["text"], None)] if rec.get("text") else [])
        for text, paragraph in units:
            found = list(extract(text, article, paragraph, doc))
            definitions.extend(found)
            if not found and TRIGGER.search(text):  # recall check: trigger word but no hit
                unmatched.append(f"art. {article} lid {paragraph}: {TRIGGER.search(text).group(0)}")
    return document or fallback_name, definitions, unmatched


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("input", help="one document: .json / .jsonl / .txt")
    ap.add_argument("-o", "--output", help="output .json file (default: stdout)")
    ap.add_argument("--document", help="document name/id to use (default: celex / instrument_id / file name)")
    args = ap.parse_args()

    path = Path(args.input)
    try:
        records = load_records(path)
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"Error reading {path}: {e}")

    document, defs, unmatched = process_document(records, args.document, path.stem)
    result = {
        "document": document,
        "terms": list(dict.fromkeys(d.term for d in defs)),
        "definitions": [asdict(d) for d in defs],
    }

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    print(f"{document}: {len(defs)} definities gevonden", file=sys.stderr)
    for u in unmatched:
        print(f"  controleren (triggerwoord, geen definitie): {u}", file=sys.stderr)


if __name__ == "__main__":
    main()