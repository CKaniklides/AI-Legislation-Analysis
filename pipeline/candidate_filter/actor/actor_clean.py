"""
Actor filter, Step 1: clean norm actors (adapted from eurovoc_clean.py).

Each raw actor becomes zero or more short noun phrases ("units"):
  - enumeration markers, quotes and trailing punctuation are removed
  - cross-references are removed:  'de in lid 1 bedoelde overheidsinstanties'
    -> 'overheidsinstanties', 'van Verordening (EU) 2016/679'
  - relative clauses are cut off: 'de toezichthoudende autoriteit waarbij de
    klacht is ingediend' -> 'toezichthoudende autoriteit'
  - inserted phrases are removed: 'of, in voorkomend geval, de ...'
  - leading articles, quantifiers, possessives and anaphoric determiners are
    removed: 'elke', 'alle', 'dergelijke', 'die', 'hun' ...
  - coordinated actors are split: 'de Commissie en de AI-board' -> 2 units,
    and the coordinator ('en', 'of') is kept per norm
  - units without a real noun are dropped, so pronouns ('zij', 'hij', 'het')
    and bare references give no units

Differences with the object version, because actors are different phrases:
  - no light-noun or infinitive rules ('het gebruik van X' -> X): an actor
    phrase that needs them is not an actor, and rewriting it would hide that
  - 'betrokken' is kept ('betrokken toezichthoudende autoriteit' is a defined
    term) and 'lid'/'leden' count as content ('Elk lid' is an actor)
  - relative clauses are cut instead of blocking the coordination split
  - a substantivized adjective counts as a noun ('de verwerkingsverantwoordelijke'
    is often tagged ADJ), as do appositions in lists ('A, B, C en D')

Output (in --dir)
  norm_actor_units.jsonl  per norm: norm_id, actor, kind, coord, units
                          kind: units | pronoun | no_units | missing

Usage
  python actor_clean.py
  python actor_clean.py --records <norm records>
  python actor_clean.py --records <norm records> --actor-field norm.actor
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from collections import Counter
from pathlib import Path

log = logging.getLogger("actor.clean")

# --------------------------------------------------------------------------
# Word lists
# --------------------------------------------------------------------------

LEADING_WORDS = [
    # articles, quantifiers, possessives
    "de", "het", "een", "één", "'t", "alle", "al", "elk", "elke", "ieder", "iedere",
    "enig", "enige", "enkele", "sommige", "geen", "zijn", "haar", "hun", "diens",
    "ten minste", "uitsluitend", "alleen", "slechts", "ook", "nog", "andere",
    "eventuele", "eventueel",
    # anaphoric determiners
    "dergelijke", "dergelijk", "zulke", "zulk", "zodanige", "zodanig", "deze",
    "die", "dit", "dat", "voornoemde", "bovengenoemde", "bovenbedoelde",
    "eerdergenoemde", "laatstgenoemde", "desbetreffende", "betreffende",
]
PRONOUNS = {"zij", "ze", "hij", "het", "die", "dit", "dat", "deze", "men", "zich", "wij"}
PRONOMINAL_ADVERBS = [
    "daarvan", "hiervan", "ervan", "daarin", "hierin", "erin", "daarop", "hierop",
    "erop", "daartoe", "hiertoe", "ertoe", "daarvoor", "hiervoor",
]
# phrases inserted between commas that are not part of the actor
INSERTS = [
    "in voorkomend geval", "indien van toepassing", "waar van toepassing",
    "naar gelang van het geval", "al naargelang het geval", "in het bijzonder",
    "met name", "onverminderd artikel \\d+",
]
# words that start a relative clause; the actor ends before them
RELATIVE = ["die", "dat", "welke", "wier", "wiens", "waarbij", "waaraan", "waarvan",
            "waarin", "waarop", "waarmee", "waardoor", "waartoe", "waarvoor", "tot wie",
            "aan wie", "bij wie", "van wie", "met inbegrip van", "zoals", "dan"]
PREPOSITIONS = ["door", "aan", "op", "in", "voor", "met", "tot", "van", "bij"]
# nouns that only occur in references or placeholders ('lid' is an actor here)
NON_CONTENT = {
    "artikel", "artikelen", "punt", "punten", "bijlage", "bijlagen",
    "hoofdstuk", "afdeling", "alinea", "onderdeel", "paragraaf", "titel", "sub",
    "volgende", "bepaalde",
}

# --------------------------------------------------------------------------
# Regexes (reference patterns as in eurovoc_clean.py)
# --------------------------------------------------------------------------

def _alt(words) -> str:
    return "|".join(re.escape(w).replace(r"\ ", r"\s+")
                    for w in sorted(words, key=len, reverse=True))


F = re.IGNORECASE
REF_NOUN = (r"(?:artikel(?:en)?|lid|leden|punt(?:en)?|bijlagen?|hoofdstuk(?:ken)?|"
            r"afdeling(?:en)?|alinea'?s?|onderdel(?:en)?|onderdeel|paragraaf|titel|sub)")
# \b: 'derde' is also an actor ('bij derden')
ORDINAL = r"(?:eerste|tweede|derde|vierde|vijfde|zesde|zevende|achtste|negende|tiende)\b"
# with or without -e: 'een in lid 1 bedoeld orgaan'
PARTICIPLE = (r"(?:bedoelde?|bepaalde?|genoemde?|vermelde?|vastgestelde?|voorziene?|omschreven|"
              r"beschreven|opgenomen|opgesomde|gedefinieerde|aangewezen|neergelegde)")

NUM = r"(?:[0-9]+[a-z]?|(?-i:[IVXLC]+)\b|[a-z]\)|[ivx]+\))"
NUMS = NUM + r"(?:\s*(?:,|en|of|tot\s+en\s+met|tot)\s*" + NUM + r")*"
# a reference noun needs a number here, otherwise 'van de leden' would go
REF_PART = (r"(?:" + REF_NOUN + r"\s+" + NUMS + r"|" + ORDINAL + r"\s+" + REF_NOUN +
            r"|(?:deze|dit|die)\s+(?:verordening|richtlijn|wet|besluit|" + REF_NOUN + r"))")
REF_TARGET = (r"(?:(?:het|de|dit|dat|deze)\s+)?" + REF_PART +
              r"(?:\s*,?\s*(?:en\s+|alsmede\s+(?:in\s+)?)?(?:van\s+(?:het\s+|de\s+)?)?" + REF_PART + r")*,?")
REF_PREP = (r"(?:(?:als|zoals)\s+)?(?:bedoeld|genoemd|vermeld|vervat|omschreven|gedefinieerd|"
            r"vastgesteld|aangewezen)\s+(?:in|bij|onder|overeenkomstig)|van|uit|in|bij|onder|"
            r"krachtens|overeenkomstig|op\s+grond\s+van|uit\s+hoofde\s+van|in\s+de\s+zin\s+van|"
            r"ingevolge")

RE_ENUM = re.compile(r"^\(?(?:[a-z]|[0-9]{1,2}|[ivx]{1,4})[\.\)°]\s+", F)
RE_QUOTES = re.compile(r"[\"“”„`]")   # not ’: it is an apostrophe in kmo’s
RE_EN_OF = re.compile(r"\ben\s*/\s*of\b", F)

REFS = [
    # 'de in lid 1, punt h), bedoelde instanties' -> 'de instanties'
    (re.compile(r"\b(de|het|een|die|dat|alle|elke|ieder|iedere)\s+"
                r"(?:in|bij|krachtens|overeenkomstig|ingevolge|volgens|op\s+grond\s+van)\s+"
                r"[^;]{0,60}?\b(?:" + REF_NOUN + r"|verordening|richtlijn)\b[^;]{0,60}?\s"
                + PARTICIPLE + r"\b", F), r"\1"),
    # 'van Verordening (EU) 2016/679'
    (re.compile(r"\s+(?:" + REF_PREP + r")\s+(?:de\s+)?(?:verordening|richtlijn)\s*"
                r"(?:\((?:eu|eg|eeg|euratom)\)\s*)?(?:nr\.\s*)?[\d/]+", F), " "),
    # 'als bedoeld in artikel 43', 'uit hoofde van deze verordening'
    (re.compile(r",?\s+(?:" + REF_PREP + r")\s+" + REF_TARGET, F), " "),
]
RE_INSERT = re.compile(r"(?:,\s*|\s+)(?:" + _alt(INSERTS) + r")\s*,?(?=\s)", F)
RE_RELATIVE = re.compile(r",?\s+\b(?:" + _alt(RELATIVE) + r")\b.*$", F)
RE_PRONADV = re.compile(r"\s*\b(?:" + _alt(PRONOMINAL_ADVERBS) + r")\b", F)
RE_LEADING = re.compile(r"^(?:" + _alt(LEADING_WORDS) + r")\s+", F)
RE_LEADING_PREP = re.compile(r"^(?:" + _alt(PREPOSITIONS) + r")\s+", F)
RE_TRAILING_PREP = re.compile(r"\s+(?:" + _alt(PREPOSITIONS) + r")$", F)


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
    if s.lower() in PRONOUNS:
        return ""
    s = RE_EN_OF.sub("of", s)
    s = RE_INSERT.sub(" ", s)
    for rx, repl in REFS:
        s = rx.sub(repl, s)
    # the relative clause is cut after the first word, so a leading
    # demonstrative ('Die lidstaten') is not mistaken for it
    first, _, rest = s.partition(" ")
    s = first + (" " + RE_RELATIVE.sub("", " " + rest).strip() if rest else "")
    s = _tidy(RE_PRONADV.sub("", s))
    s = RE_LEADING_PREP.sub("", s, count=1)        # 'door de Commissie'
    return _tidy(_strip_leading(s))


# --------------------------------------------------------------------------
# Parse-level: coordination and noun check
# --------------------------------------------------------------------------

def _join(tokens) -> str:
    """Tokens back to text, with a space where a dropped token was."""
    toks = sorted(tokens, key=lambda t: t.i)
    return _tidy("".join(t.text + (t.whitespace_ if i + 1 < len(toks) and toks[i + 1].i == t.i + 1
                                   else " ") for i, t in enumerate(toks)))


def is_nominal(t) -> bool:
    """A noun, or an adjective used as one: 'de verwerkingsverantwoordelijke'.
    Such an adjective is the root or hangs on a word to its left; an
    adjective that modifies a noun hangs on the noun to its right."""
    if t.pos_ in ("NOUN", "PROPN"):
        return True
    return t.pos_ == "ADJ" and (t.dep_ == "ROOT" or t.head.i < t.i)


def split_coordination(doc, max_parts: int) -> tuple[list[str], str | None]:
    """'de Commissie en de AI-board' -> (['Commissie', 'AI-board'], 'en').
    Only actors coordinated with the head are split; coordinations inside a
    modifier ('vertegenwoordiger van de verwerkingsverantwoordelijke of de
    verwerker') are left alone. Lists are often parsed as appositions, so
    those count as well when the phrase has a coordinator."""
    roots = [t for t in doc if t.dep_ == "ROOT"]
    if not roots or not is_nominal(roots[0]):
        return [doc.text], None
    head = roots[0]
    ccs = [t for t in doc if t.dep_ == "cc"]
    deps = ("conj", "appos") if ccs else ("conj",)
    # 'A of B of C' is often parsed as A <- B <- C: follow conjuncts of conjuncts
    conjs, todo = [], [head]
    while todo:
        t = todo.pop()
        new = [c for c in t.children if c.dep_ in deps and is_nominal(c) and c not in conjs]
        conjs += new
        todo += new
    conjs.sort(key=lambda t: t.i)
    if not conjs or len(conjs) + 1 > max_parts:
        return [doc.text], None
    owners = {head.i} | {c.i for c in conjs}

    def own(c):
        """Tokens of a conjunct without the conjuncts that hang on it."""
        inner = {t.i for o in conjs if o is not c and o.i in {x.i for x in c.subtree}
                 for t in o.subtree}
        return [t for t in c.subtree if t.i not in inner
                and not (t.dep_ in ("cc", "punct") and t.head.i in owners)]
    parts = [own(c) for c in [head] + conjs]
    words = {t.lower_ for t in ccs if t.head.i in owners}
    coord = "of" if "of" in words else "en"
    # 'of door de bevoegde autoriteit' -> 'bevoegde autoriteit'
    return [_tidy(_strip_leading(RE_LEADING_PREP.sub("", _join(p), count=1))) for p in parts], coord


def has_content_noun(doc) -> bool:
    """A noun that is not a reference word. A single word is accepted unless it
    is a function word: the model often tags lone actors as ADJ or VERB."""
    def ok(t):
        return t.text.lower() not in NON_CONTENT and t.lemma_.lower() not in NON_CONTENT
    if len(doc) == 1:
        t = doc[0]
        return ok(t) and t.text.replace("-", "").isalpha() and len(t) >= 3 and \
            t.text.lower() not in PRONOUNS and \
            t.pos_ not in ("DET", "PRON", "ADP", "CCONJ", "SCONJ", "NUM", "AUX")
    return any(is_nominal(t) and ok(t) for t in doc)


def clean_actors(actors: list[str], nlp, max_parts: int = 6) -> dict[str, tuple[list[str], str | None]]:
    """Raw actor -> (units, coordinator). Units are empty for pronouns and placeholders."""
    first = {a: clean_string(a) for a in actors}
    texts = sorted({s for s in first.values() if s})
    split = {t: split_coordination(d, max_parts) for t, d in zip(texts, nlp.pipe(texts))}
    all_parts = sorted({p for ps, _ in split.values() for p in ps if p})
    ok = {p: has_content_noun(d) for p, d in zip(all_parts, nlp.pipe(all_parts))}
    out = {}
    for a, s in first.items():
        parts, coord = split.get(s, ([], None))
        units = [p for p in dict.fromkeys(parts) if p and ok[p]]
        out[a] = (units, coord if len(units) > 1 else None)
    return out


def kind_of(actor: str, units: list[str]) -> str:
    if not actor:
        return "missing"
    if units:
        return "units"
    return "pronoun" if actor.lower() in PRONOUNS else "no_units"


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
    p.add_argument("--dir", type=Path, default=Path("data/conflict/actor"))
    p.add_argument("--records", type=Path, default = Path("data/new_norms/original_norms.json"))
    p.add_argument("--actor-field", default="norm.actor")
    p.add_argument("--id-field", default="norm_id")
    p.add_argument("--spacy-model", default="nl_core_news_md",
                   help="_md splits coordinations clearly better than _sm on actors")
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
        v = _get_path(r, args.actor_field.split("."))
        norms.append({"norm_id": _get_path(r, args.id_field.split(".")),
                      "actor": " ".join(v.split()) if isinstance(v, str) else ""})
    actors = sorted({n["actor"] for n in norms if n["actor"]})
    cleaned = clean_actors(actors, nlp, args.max_parts)

    args.dir.mkdir(parents=True, exist_ok=True)
    kinds = Counter()
    with open(args.dir / "norm_actor_units.jsonl", "w", encoding="utf-8") as f:
        for n in norms:
            units, coord = cleaned.get(n["actor"], ([], None))
            kind = kind_of(n["actor"], units)
            kinds[kind] += 1
            f.write(json.dumps({**n, "kind": kind, "coord": coord, "units": units},
                               ensure_ascii=False) + "\n")

    c = Counter(len(cleaned[a][0]) for a in actors)
    log.info("Norms: %d (%s)", len(norms), ", ".join(f"{k} {v}" for k, v in kinds.most_common()))
    log.info("Unique actors: %d -> no units: %d, one unit: %d, several: %d; unique units: %d",
             len(actors), c[0], c[1], sum(v for k, v in c.items() if k > 1),
             len({u for us, _ in cleaned.values() for u in us}))


if __name__ == "__main__":
    main()