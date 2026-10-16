"""
EuroVoc Step 6: clean norm objects so they map better to EuroVoc labels.

Each raw object becomes zero or more short noun phrases ("units"):
  - enumeration markers, quotes and trailing punctuation are removed
  - cross-references are removed:  'de in lid 6 bepaalde informatie' -> 'informatie',
    'als bedoeld in artikel 3', 'van artikel 5, lid 2', 'van Verordening (EU) 2016/679'
  - pronominal adverbs are removed: 'het gebruik ervan' -> 'gebruik'
  - leading articles, quantifiers, possessives and anaphoric determiners are
    removed: 'alle', 'ten minste', 'dergelijke', 'desbetreffende' ...
  - light nouns give way to their complement: 'het gebruik van X' -> X,
    'het verstrekken van X' -> X, 'om te voorzien in X' -> X
  - coordinated nouns are split: 'kennis, ervaring en opleiding' -> 3 units
  - units without a real noun are dropped, so placeholders such as
    'het volgende' and bare references stay unmapped

Shorter phrases match labels more often at the phrase level, and the head noun
of a cleaned phrase is the content noun rather than 'gebruik' or 'informatie
in lid 6'. The process is deterministic. Word lists are constants below.

Output (in --dir)
  object_units.jsonl       per unique raw object: its units
  norm_object_units.jsonl  per norm: norm_id, object, units
                           (input to eurovoc_embed.py --units and eurovoc_map.py --units)

Usage
  python eurovoc_clean.py --records <norm records>
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from collections import Counter
from pathlib import Path

log = logging.getLogger("eurovoc.clean")

# --------------------------------------------------------------------------
# Word lists
# --------------------------------------------------------------------------

LEADING_WORDS = [
    # articles, quantifiers, possessives
    "de", "het", "een", "'t", "alle", "al", "al dan niet", "elk", "elke", "ieder", "iedere",
    "enig", "enige", "enkele", "sommige", "bepaalde", "geen", "zijn", "haar",
    "hun", "diens", "ons", "onze", "uw", "ten minste", "tenminste", "minstens",
    "ten hoogste", "uitsluitend", "alleen", "slechts", "ook", "nog", "andere",
    "volgende", "navolgende", "onderstaande", "bovenstaande",
    # anaphoric determiners
    "dergelijke", "dergelijk", "zulke", "zulk", "zodanige", "zodanig", "deze",
    "die", "dit", "dat", "voornoemde", "bovengenoemde", "bovenbedoelde",
    "eerdergenoemde", "laatstgenoemde", "desbetreffende", "betreffende", "betrokken",
]
PRONOMINAL_ADVERBS = [
    "daarvan", "hiervan", "ervan", "daarin", "hierin", "erin", "daarop", "hierop",
    "erop", "daartoe", "hiertoe", "ertoe", "daarvoor", "hiervoor",
]
LIGHT_NOUNS = [
    "gebruik", "toepassing", "inzet", "uitvoering", "verstrekking", "naleving",
    "invoering", "vaststelling", "toekenning", "verlening", "behandeling",
    "aanwending", "terbeschikkingstelling", "indiening",
]
PREPOSITIONS = ["aan", "op", "in", "over", "voor", "met", "tot", "van", "naar", "bij", "uit",
                "tegen", "als"]
# nouns that only occur in references or placeholders
NON_CONTENT = {
    "artikel", "artikelen", "lid", "leden", "punt", "punten", "bijlage", "bijlagen",
    "hoofdstuk", "afdeling", "alinea", "onderdeel", "paragraaf", "titel", "sub",
    "volgende", "bepaalde",
}

# --------------------------------------------------------------------------
# Regexes
# --------------------------------------------------------------------------

def _alt(words) -> str:
    return "|".join(re.escape(w).replace(r"\ ", r"\s+")
                    for w in sorted(words, key=len, reverse=True))


F = re.IGNORECASE
REF_NOUN = (r"(?:artikel(?:en)?|lid|leden|punt(?:en)?|bijlagen?|hoofdstuk(?:ken)?|"
            r"afdeling(?:en)?|alinea'?s?|onderdel(?:en)?|onderdeel|paragraaf|titel|sub)")
ORDINAL = r"(?:eerste|tweede|derde|vierde|vijfde|zesde|zevende|achtste|negende|tiende)"
PARTICIPLE = (r"(?:bedoelde|bepaalde|genoemde|vermelde|vastgestelde|voorziene|omschreven|"
              r"beschreven|opgenomen|opgesomde|gedefinieerde|aangewezen|neergelegde)")

NUM = r"(?:[0-9]+[a-z]?|(?-i:[IVXLC]+)\b|[a-z]\)|[ivx]+\))"
NUMS = NUM + r"(?:\s*(?:,|en|of|tot\s+en\s+met|tot)\s*" + NUM + r")*"
REF_PART = (r"(?:" + REF_NOUN + r"(?:\s+" + NUMS + r")?|" + ORDINAL + r"(?:\s+" + REF_NOUN + r")?"
            r"|(?:deze|dit|die)\s+(?:verordening|richtlijn|wet|besluit|" + REF_NOUN + r"))")
# 'artikel 12, lid 8, en artikel 43', 'de artikelen 13 en 14', 'bijlage III'
REF_TARGET = (r"(?:(?:het|de|dit|dat|deze)\s+)?" + REF_PART +
              r"(?:\s*,?\s*(?:en\s+|alsmede\s+(?:in\s+)?)?(?:van\s+(?:het\s+|de\s+)?)?" + REF_PART + r")*,?")
REF_PREP = (r"(?:(?:als|zoals)\s+)?(?:bedoeld|genoemd|vermeld|vervat|omschreven|gedefinieerd|"
            r"vastgesteld)\s+(?:in|bij|onder)|van|uit|in|bij|onder|krachtens|overeenkomstig|"
            r"op\s+grond\s+van|uit\s+hoofde\s+van|in\s+de\s+zin\s+van|ingevolge")

RE_ENUM = re.compile(r"^\(?(?:[a-z]|[0-9]{1,2}|[ivx]{1,4})[\.\)°]\s+", F)
RE_QUOTES = re.compile(r"[\"“”„`]")   # not ’: it is an apostrophe in kmo’s
RE_EN_OF = re.compile(r"\ben\s*/\s*of\b", F)

REFS = [
    # 'de in lid 1, punt h), bedoelde doelstellingen' -> 'de doelstellingen'
    (re.compile(r"\b(de|het|een|die|dat|alle|elke|ieder|iedere)\s+"
                r"(?:in|bij|krachtens|overeenkomstig|ingevolge|volgens|op\s+grond\s+van)\s+"
                r"[^;]{0,60}?\b(?:" + REF_NOUN + r"|verordening|richtlijn)\b[^;]{0,60}?\s"
                + PARTICIPLE + r"\b", F), r"\1"),
    # 'van Verordening (EU) 2016/679'
    (re.compile(r"\s+(?:" + REF_PREP + r")\s+(?:de\s+)?(?:verordening|richtlijn)\s*"
                r"(?:\((?:eu|eg|eeg|euratom)\)\s*)?(?:nr\.\s*)?[\d/]+", F), " "),
    # 'als bedoeld in de artikelen 12 tot en met 22', 'van artikel 5, lid 1, punt h)',
    # 'uit hoofde van deze verordening'
    (re.compile(r",?\s+(?:" + REF_PREP + r")\s+" + REF_TARGET, F), " "),
]
RE_PRONADV = re.compile(r"\s*\b(?:" + _alt(PRONOMINAL_ADVERBS) + r")\b", F)
RE_LEADING = re.compile(r"^(?:" + _alt(LEADING_WORDS) + r")\s+", F)
RE_LEADING_PREP = re.compile(r"^(?:" + _alt(PREPOSITIONS) + r")\s+", F)
RE_TRAILING_PREP = re.compile(r"\s+(?:" + _alt(PREPOSITIONS) + r")$", F)
RE_INF_FRAME = re.compile(r"^(?:om\s+)?te\s+\w+(?:\s+(?:in|op|voor|van|aan|tot|met|over))?\s+(.+)$", F)
RE_LIGHT = re.compile(r"^(?:[\w\-]+e\s+)?(?:" + _alt(LIGHT_NOUNS) + r")\s+(?:van|voor|aan)\s+(.+)$", F)
# 'het verstrekken van X': needs 'het', otherwise plurals match ('de verplichtingen van X')
RE_NOMINF = re.compile(r"^het\s+[a-z]{4,}en\s+van\s+(.+)$", F)


def _tidy(s: str) -> str:
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s+([,.;:)])", r"\1", s)
    s = s.strip(" ,;:.-–")
    return RE_TRAILING_PREP.sub("", s)


def _strip_leading(s: str) -> str:
    while m := RE_LEADING.match(s):
        s = s[m.end():]
    return s


def clean_string(text: str) -> str:
    """All string-level rules; returns the cleaned phrase (possibly empty)."""
    s = RE_ENUM.sub("", RE_QUOTES.sub("", text).strip())
    s = RE_EN_OF.sub("of", s)
    for rx, repl in REFS:
        s = rx.sub(repl, s)
    s = _tidy(RE_PRONADV.sub("", s))
    # A preposition is only removed at the very start of the object (governed
    # by the verb: 'voldoen aan de eisen'); later on it belongs to a modifier
    # ('alle voor de uitvoering vereiste informatie').
    s = RE_LEADING_PREP.sub("", s, count=1)
    # leading words and light nouns, until nothing changes
    for _ in range(10):
        before = s
        m = RE_NOMINF.match(s)
        if m:
            s = m.group(1)
        s = _strip_leading(s)
        m = RE_INF_FRAME.match(s) or RE_LIGHT.match(s)
        if m:
            s = m.group(1)
        s = _tidy(s)
        if s == before:
            break
    return s


# --------------------------------------------------------------------------
# Parse-level: coordination and noun check
# --------------------------------------------------------------------------

def _join(tokens) -> str:
    return _tidy("".join(t.text_with_ws for t in sorted(tokens, key=lambda t: t.i)))


def split_coordination(doc, max_parts: int) -> list[str]:
    """'kennis, ervaring en opleiding' -> ['kennis', 'ervaring', 'opleiding'].
    Only nouns coordinated with the head are split; coordinations inside a
    modifier ('gegevens over naam en adres') are left alone."""
    roots = [t for t in doc if t.dep_ == "ROOT"]
    if not roots or roots[0].pos_ not in ("NOUN", "PROPN"):
        return [doc.text]
    head = roots[0]
    # objects with a clause ('informatie die ... wordt verkregen') are not split:
    # the parser rarely gets the coordination right there
    if any(t.pos_ == "AUX" or t.dep_.startswith(("acl", "advcl", "ccomp", "xcomp", "csubj"))
           for t in doc):
        return [doc.text]
    conjs = [c for c in head.children if c.dep_ == "conj" and c.pos_ in ("NOUN", "PROPN")]
    if not conjs or len(conjs) + 1 > max_parts:
        return [doc.text]
    in_conj = {t.i for c in conjs for t in c.subtree}
    owners = {head.i} | {c.i for c in conjs}

    def keep(t):
        return not (t.dep_ in ("cc", "punct") and t.head.i in owners)
    parts = [[t for t in head.subtree if t.i not in in_conj and keep(t)]]
    parts += [[t for t in c.subtree if keep(t)] for c in conjs]
    return [_tidy(_strip_leading(_join(p))) for p in parts]


def has_content_noun(doc) -> bool:
    """A noun that is not a reference word. A single word is accepted unless it
    is a function word: the small model often tags lone plurals as verbs."""
    def ok(t):
        return t.text.lower() not in NON_CONTENT and t.lemma_.lower() not in NON_CONTENT
    if len(doc) == 1:
        t = doc[0]
        return ok(t) and t.text.replace("-", "").isalpha() and len(t) >= 3 and \
            t.pos_ not in ("DET", "PRON", "ADP", "CCONJ", "SCONJ", "NUM", "AUX")
    return any(t.pos_ in ("NOUN", "PROPN") and ok(t) for t in doc)


def clean_objects(objects: list[str], nlp, max_parts: int = 6) -> dict[str, list[str]]:
    """Raw object -> list of units (empty for placeholders)."""
    first = {o: clean_string(o) for o in objects}
    texts = sorted({s for s in first.values() if s})
    parts = {t: split_coordination(d, max_parts) for t, d in zip(texts, nlp.pipe(texts))}
    all_parts = sorted({p for ps in parts.values() for p in ps if p})
    ok = {p: has_content_noun(d) for p, d in zip(all_parts, nlp.pipe(all_parts))}
    return {o: [p for p in dict.fromkeys(parts.get(s, [])) if p and ok[p]]
            for o, s in first.items()}


# --------------------------------------------------------------------------

def _get_path(obj, path: list[str]):
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dir", type=Path, default=Path("data/conflict/eurovoc"))
    p.add_argument("--records", type=Path, required=True)
    p.add_argument("--object-field", default="norm.object")
    p.add_argument("--id-field", default="norm_id")
    p.add_argument("--spacy-model", default="nl_core_news_sm")
    p.add_argument("--max-parts", type=int, default=6,
                   help="coordinations with more parts are not split")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    import spacy
    nlp = spacy.load(args.spacy_model, disable=["ner"])

    raw = args.records.read_text(encoding="utf-8").strip()
    records = json.loads(raw) if raw.startswith("[") else \
        [json.loads(l) for l in raw.splitlines() if l.strip()]
    norms = []
    for r in records:
        v = _get_path(r, args.object_field.split("."))
        norms.append({"norm_id": _get_path(r, args.id_field.split(".")),
                      "object": " ".join(v.split()) if isinstance(v, str) else ""})
    objects = sorted({n["object"] for n in norms if n["object"]})
    units = clean_objects(objects, nlp, args.max_parts)

    args.dir.mkdir(parents=True, exist_ok=True)
    with open(args.dir / "object_units.jsonl", "w", encoding="utf-8") as f:
        for o in objects:
            f.write(json.dumps({"object": o, "units": units[o]}, ensure_ascii=False) + "\n")
    with open(args.dir / "norm_object_units.jsonl", "w", encoding="utf-8") as f:
        for n in norms:
            f.write(json.dumps({**n, "units": units.get(n["object"], [])},
                               ensure_ascii=False) + "\n")

    c = Counter(len(units[o]) for o in objects)
    log.info("Unique objects: %d -> no units: %d, one unit: %d, several: %d; unique units: %d",
             len(objects), c[0], c[1], sum(v for k, v in c.items() if k > 1),
             len({u for us in units.values() for u in us}))


if __name__ == "__main__":
    main()