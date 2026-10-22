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
                   sub-items (a), b) under 1) ... or 1°. under a.) are folded into their parent
  onder_verstaan   Onder X wordt (mede) verstaan Y.
  wordt_onder_verstaan   In deze wet wordt onder X verstaan Y.
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

ITEM = re.compile(
    r"^\s*(?:(?P<marker>[a-z]{1,3}|\d{1,3})[.°)]+\s+)?"
    r"(?P<term>"
    r"[" + OPEN_Q + r"][^" + CLOSE_Q + r"\n]{1,80}[" + CLOSE_Q + r"][^:;\n]{0,40}?"  # „quoted” (+ qualifier)
    r"|[^\W\d_][^:;\n]{1,80}?"                                                   # unquoted
    r")\s*:\s*(?P<definition>\S.*)?$"
)

STOP = re.compile(r"^\s*(?:artikel|hoofdstuk|afdeling|paragraaf|titel|§)\b", FLAGS)

# --- 2. Sentence-style definitions ---------------------------------------------

_END = r"(?P<definition>.+?)(?:\.(?=\s|$)|$)"
SENTENCE_PATTERNS = {
    "onder_verstaan": re.compile(
        r"\bonder\s+(?P<term>[^,:;.]{2,80}?)\s+(?:wordt|worden)\s+"
        r"(?:mede\s+|ook\s+)?(?:in\s+(?:dit|deze)\s+\w+\s+)?verstaan[:,]?\s+" + _END,
        FLAGS,
    ),
    "wordt_onder_verstaan": re.compile(
        r"\b(?:wordt|worden)\s+(?:in\s+(?:dit|deze)\s+\w+\s+)?onder\s+"
        r"(?P<term>[^,:;.]{2,80}?)\s+(?:mede\s+|ook\s+)?verstaan[:,]?\s+" + _END,
        FLAGS,
    ),
}

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
    return re.sub(r"\s+", " ", term).strip()


def clean_def(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().rstrip(";,").strip()


def marker_kind(marker):
    if not marker:
        return None
    return "num" if marker.isdigit() else "alpha"


def preceding_clause(text: str, pos: int, max_len: int = 500) -> str:
    """The clause right before `pos`, cut at the previous ; : newline or sentence end."""
    window = text[max(0, pos - max_len):pos]
    clause = re.split(r"[;:\n]|(?<=[a-z\)])\.\s", window)[-1]
    return re.sub(r"^\s*(?:[a-z]{1,3}|\d{1,3})[.°)]+\s+", "", clause)


def find_list_definitions(text: str):
    in_block, blanks, block_kind, current = False, 0, None, None

    def flush():
        nonlocal current
        if current and current["definition"].strip():
            yield current
        current = None

    for line in text.splitlines():
        if INTRO.search(line):
            yield from flush()
            in_block, blanks, block_kind = True, 0, None
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

        m = ITEM.match(line)
        is_new_item = False
        if m and len(clean_term(m.group("term")).split()) <= 8:
            kind = marker_kind(m.group("marker"))
            if block_kind is None and kind:
                block_kind = kind
            # a marker of the other kind (a) inside a 1) list) is a sub-item
            is_new_item = not (kind and block_kind and kind != block_kind)

        if is_new_item:
            yield from flush()
            current = {
                "term": clean_term(m.group("term")),
                "definition": m.group("definition") or "",
                "pattern": "lijst",
            }
        elif current:
            current["definition"] += " " + line.strip()
    yield from flush()


def find_sentence_definitions(text: str):
    flat = text.replace("\n", " ")
    for name, rx in SENTENCE_PATTERNS.items():
        for m in rx.finditer(flat):
            yield {"term": clean_term(m.group("term")), "definition": m.group("definition"),
                   "pattern": name}
    for name, rx in (("hierna", HIERNA), ("tussen_haakjes", INLINE_QUOTED)):
        for m in rx.finditer(text):
            yield {"term": clean_term(m.group("term")),
                   "definition": preceding_clause(text, m.start()), "pattern": name}


def extract(text: str, article, paragraph, document: str):
    seen = set()
    for d in [*find_list_definitions(text), *find_sentence_definitions(text)]:
        term, definition = d["term"], clean_def(d["definition"])
        if not term or not definition:
            continue
        key = (term.lower(), definition[:60].lower())
        if key in seen:
            continue
        seen.add(key)
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
    definitions, document = [], document_override
    for rec in records:
        doc = document_override or as_str(rec.get("celex") or rec.get("instrument_id")) or fallback_name
        document = document or doc
        article = as_str(rec.get("article"))
        paragraphs = [p for p in (rec.get("paragraphs") or []) if isinstance(p, dict) and p.get("text")]
        if paragraphs:  # search paragraph by paragraph so each hit knows its paragraph
            for p in paragraphs:
                definitions.extend(extract(p["text"], article, as_str(p.get("number")), doc))
        elif rec.get("text"):
            definitions.extend(extract(rec["text"], article, None, doc))
    return document or fallback_name, definitions


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

    document, defs = process_document(records, args.document, path.stem)
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


if __name__ == "__main__":
    main()