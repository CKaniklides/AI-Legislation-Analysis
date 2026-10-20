"""
EuroVoc Step 2: clean norm objects so they map better to EuroVoc labels.

Each raw object becomes zero or more short noun phrases ("units"):
  - inline lists are split into their items: 'a. de ernst ...; b. de duur ...'
  - enumeration markers, quotes and trailing punctuation are removed
  - cross-references are removed, in EU and in Dutch style:
    'de in lid 6 bepaalde informatie' -> 'informatie', 'als bedoeld in artikel 3',
    'van artikel 5, lid 2', 'van Verordening (EU) 2016/679',
    'als bedoeld in artikel 1.1, onder g, van de Wet op het hoger onderwijs ...',
    'bedoeld in artikel 3:2 van de Algemene wet bestuursrecht',
    'als bedoeld in het tweede lid, onderdeel b', 'bij of krachtens deze wet',
    'van de verordening', 'van de NIS2-richtlijn'
  - pronominal adverbs are removed: 'het gebruik ervan' -> 'gebruik'
  - leading articles, quantifiers, possessives and anaphoric determiners are
    removed: 'alle', 'ten minste', 'dergelijke', 'desbetreffende' ...
    A leading 'geen' is removed too, but recorded: the norm gets "negated": true
  - fixed modifiers are removed: 'de van toepassing zijnde rechten' -> 'rechten'
  - light nouns give way to their complement: 'het gebruik van X' -> X,
    'het verstrekken van X' -> X, 'om te voorzien in X' -> X
  - relative clauses after a noun are cut: 'informatie die door de entiteit
    wordt verstrekt' -> 'informatie'; a coordination after the clause is kept,
    and the cut clause is kept in "clauses"
  - coordinated nouns are split: 'kennis, ervaring en opleiding' -> 3 units,
    'voorzitter, de andere leden' -> 2 units
  - units without a real noun are dropped, so placeholders such as
    'het volgende' and bare references stay unmapped

Every unit is checked afterwards: its words must occur, in order, in the raw
object (no glued or invented words), it should not contain reference
leftovers, and it should be short. Failed checks are logged and written to
object_clean_flags.jsonl, so problems are visible without reading every unit.

Shorter phrases match labels more often at the phrase level, and the head noun
of a cleaned phrase is the content noun rather than 'gebruik' or 'informatie
in lid 6'. The process is deterministic. Word lists are constants below.

Output (in --dir)
  norm_object_units.jsonl  per norm: norm_id, object, units, negated, clauses
                           (input to eurovoc_embed.py --units and eurovoc_map.py --units;
                           "clauses" holds the relative clauses cut from the object,
                           so later steps can still use their nouns as context)
  object_clean_flags.jsonl per flagged unit: object, unit, flags

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
    "welke",
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
    "volgende", "bepaalde", "volzin", "volzinnen",
}
# Words that start a relative clause after a noun. 'die', 'dat' and 'welke' only
# count when the parser tags them PRON, not DET ('gegevens over die personen').
REL_ADVERBS = {
    "waarin", "waaruit", "waarbij", "waarmee", "waarop", "waarvan", "waartoe",
    "waarover", "waarvoor", "waaraan", "waardoor", "waarnaar", "wiens", "wier",
}
REL_PRONOUNS = {"die", "dat", "welke"}
# Dutch law abbreviations that can follow a reference preposition
LAW_ABBREVIATIONS = ["AVG", "UAVG", "Awb", "Wbni", "Wbp", "BW"]

# --------------------------------------------------------------------------
# Regexes
# --------------------------------------------------------------------------

def _alt(words) -> str:
    return "|".join(re.escape(w).replace(r"\ ", r"\s+")
                    for w in sorted(words, key=len, reverse=True))


F = re.IGNORECASE
REF_NOUN = (r"(?:artikel(?:en)?|lid|leden|punt(?:en)?|bijlagen?|hoofdstuk(?:ken)?|"
            r"afdeling(?:en)?|alinea'?s?|onderdel(?:en)?|onderdeel|paragraaf|titel|sub|"
            r"volzin(?:nen)?|(?-i:Boek))")
ORDINAL = r"(?:eerste|tweede|derde|vierde|vijfde|zesde|zevende|achtste|negende|tiende)"
PARTICIPLE = (r"(?:bedoelde|bepaalde|genoemde|vermelde|vastgestelde|voorziene|omschreven|"
              r"beschreven|opgenomen|opgesomde|gedefinieerde|aangewezen|neergelegde|"
              r"gestelde|gedane|gegeven|verleende|opgelegde|ingediende|verstrekte|"
              r"uitgevoerde|genomen|vereiste|geldende)")

# Numbers: '5', '5a', Dutch '1.1' and '3:2', '2°', Roman 'III', 'h)', 'iv)'
NUM = r"(?:[0-9]+(?:[.:][0-9]+)*[a-z]?°?|(?-i:[IVXLC]+)\b|[a-z]\)|[ivx]+\))"
LIST_SEP = r"(?:\s*(?:,|en|of|tot\s+en\s+met|tot)\s*)"
NUMS = NUM + r"(?:" + LIST_SEP + NUM + r")*"
# Dutch letter references without brackets: 'onder a', 'onderdeel b', 'sub 1°'
LETTER = r"(?:[a-z]°?|[0-9]{1,2}°)(?!\w)"
LETTER_REF = r"(?:onder(?:deel|delen)?|sub)\s+" + LETTER + r"(?:" + LIST_SEP + LETTER + r")*"
REF_PART = (r"(?:" + LETTER_REF + r"|" + REF_NOUN + r"(?:\s+" + NUMS + r")?|"
            + ORDINAL + r"(?:\s+" + REF_NOUN + r")?"
            r"|(?:deze|dit|die)\s+(?:verordening|richtlijn|wet|besluit|regeling|" + REF_NOUN + r"))")
# 'artikel 12, lid 8, en artikel 43', 'de artikelen 13 en 14', 'bijlage III',
# 'het eerste tot en met zesde lid', 'artikel 1.1, onder g'
REF_TARGET = (r"(?:(?:het|de|dit|dat|deze)\s+)?" + REF_PART +
              r"(?:\s*,?\s*(?:en\s+|of\s+|tot\s+en\s+met\s+|alsmede\s+(?:in\s+)?)?"
              r"(?:van\s+(?:het\s+|de\s+)?)?" + REF_PART + r")*,?")
REF_PREP = (r"(?:(?:als|zoals)\s+)?(?:bedoeld|genoemd|vermeld|vervat|omschreven|gedefinieerd|"
            r"vastgesteld)\s+(?:in|bij|onder)|bij\s+of\s+krachtens|van|uit|in|bij|onder|krachtens|"
            r"overeenkomstig|op\s+grond\s+van|uit\s+hoofde\s+van|in\s+de\s+zin\s+van|ingevolge")
# Named Dutch acts: 'van de Wet op het hoger onderwijs en wetenschappelijk onderzoek',
# 'van de Algemene wet bestuursrecht', 'van het Burgerlijk Wetboek'. Case-sensitive:
# a named act starts with a capital, so a plain 'de wet' is not removed here.
# The name runs to a comma or the end, but stops before a participle: in 'de bij
# artikel 5 van de Wet X gestelde vereisten' the object continues after the name.
LAW_NAME = (r"(?-i:(?:Algemene\s+wet|Uitvoeringswet|Burgerlijk\s+Wetboek|Wetboek|Wet|Besluit|"
            r"Regeling|Kaderwet|Rijkswet)\b)(?:(?!\s" + PARTICIPLE + r"\b)[^,;:])*")
LAW_REF = (r"(?:(?:de|het)\s+)?(?:" + LAW_NAME + r"|(?-i:(?:" + _alt(LAW_ABBREVIATIONS) + r"))\b"
           r"|[\w\-]+-(?:richtlijn|verordening)\b)")

RE_ENUM = re.compile(r"^\(?(?:[a-z]|[0-9]{1,2}|[ivx]{1,4})[\.\)°]\s+", F)
RE_QUOTES = re.compile(r"[\"“”„`]")   # not ’: it is an apostrophe in kmo’s
RE_EN_OF = re.compile(r"\ben\s*/\s*of\b", F)
RE_FIXED_MOD = re.compile(r"\b(?:van\s+toepassing|in\s+het\s+geding|in\s+werking)\s+zijnde\s+", F)
RE_NEGATED = re.compile(r"^\W*(?:(?:aan|op|in|over|voor|met|tot|van|naar|bij|uit|tegen)\s+)?"
                        r"geen\b", F)

REFS = [
    # 'de in lid 1, punt h), bedoelde doelstellingen' -> 'de doelstellingen',
    # 'de op grond van de Wet X gedane melding' -> 'de melding'
    (re.compile(r"\b(de|het|een|die|dat|alle|elke|ieder|iedere)\s+"
                r"(?:in|bij\s+of\s+krachtens|bij|krachtens|overeenkomstig|ingevolge|volgens|"
                r"op\s+grond\s+van)\s+"
                r"[^;]{0,80}?\b(?:" + REF_NOUN + r"|verordening|richtlijn|wet|wetboek|besluit)\b"
                r"[^;]{0,80}?\s"
                + PARTICIPLE + r"\b", F), r"\1"),
    # 'van Verordening (EU) 2016/679'
    (re.compile(r"\s+(?:" + REF_PREP + r")\s+(?:de\s+)?(?:verordening|richtlijn)\s*"
                r"(?:\((?:eu|eg|eeg|euratom)\)\s*)?(?:nr\.\s*)?[\d/]+", F), " "),
    # 'als bedoeld in de artikelen 12 tot en met 22', 'van artikel 5, lid 1, punt h)',
    # 'uit hoofde van deze verordening', 'als bedoeld in artikel 1.1, onder g',
    # 'bij of krachtens deze wet'
    (re.compile(r",?\s+(?:" + REF_PREP + r")\s+" + REF_TARGET, F), " "),
    # what is left of a reference: 'van de Wet ...', 'van de Algemene wet bestuursrecht',
    # 'van de AVG', 'van de NIS2-richtlijn'
    (re.compile(r",?\s+(?:" + REF_PREP + r")\s+" + LAW_REF, F), " "),
    # a bare 'van de verordening' at the end of the object or before a comma
    (re.compile(r",?\s+(?:" + REF_PREP + r")\s+(?:de|het|deze|die|dit)\s+"
                r"(?:verordening|richtlijn|wet)\b(?=\s*(?:[,;]|$))", F), " "),
]
RE_PRONADV = re.compile(r"\s*\b(?:" + _alt(PRONOMINAL_ADVERBS) + r")\b", F)
RE_LEADING = re.compile(r"^(?:" + _alt(LEADING_WORDS) + r")\s+", F)
RE_LEADING_PREP = re.compile(r"^(?:" + _alt(PREPOSITIONS) + r")\s+", F)
RE_TRAILING_PREP = re.compile(r"\s+(?:" + _alt(PREPOSITIONS) + r")$", F)
RE_INF_FRAME = re.compile(r"^(?:om\s+)?te\s+\w+(?:\s+(?:in|op|voor|van|aan|tot|met|over))?\s+(.+)$", F)
RE_LIGHT = re.compile(r"^(?:[\w\-]+e\s+)?(?:" + _alt(LIGHT_NOUNS) + r")\s+(?:van|voor|aan)\s+(.+)$", F)
# 'het verstrekken van X': needs 'het', otherwise plurals match ('de verplichtingen van X')
RE_NOMINF = re.compile(r"^het\s+[a-z]{4,}en\s+van\s+(.+)$", F)

# Inline list markers: 'a. ', 'b. ', '1°. ', '2°. ' at the start or after a space
RE_LIST_MARK = re.compile(r"(?:^|(?<=\s))(?:[a-z]|[0-9]{1,2}°)\.\s+", F)
RE_LIST_TRIM = re.compile(r"(?:\s*[,;:])*(?:\s+(?:en|of|alsmede))?(?:\s*[,;:])*\s*$", F)

# Post-clean checks
# 'lid'/'leden' only count as a reference next to a number ('lid 2', not 'de andere leden')
RE_RESIDUAL_REF = re.compile(r"\b(?:" + REF_NOUN.replace("lid|leden|", "") + r")\b|"
                             r"\b(?:lid|leden)\s+[0-9]|(?<!\w)[.:]\d|\d+°|\s[a-z]$|"
                             r"\b(?-i:Wet|Algemene wet)\b|\bbij of\b", F)
MAX_UNIT_WORDS = 8


def _tidy(s: str) -> str:
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s+([,.;:)])", r"\1", s)
    s = s.strip(" ,;:.-–")
    return RE_TRAILING_PREP.sub("", s)


def _strip_leading(s: str) -> str:
    while m := RE_LEADING.match(s):
        s = s[m.end():]
    return s


def split_list(text: str) -> list[str]:
    """'omstandigheden, alsmede: a. de ernst ...; b. de duur ...' -> the intro and
    each item. Only splits when a list is clearly present ('a.' and 'b.', or
    '1°.' and '2°.'); otherwise returns the text unchanged."""
    marks = list(RE_LIST_MARK.finditer(text))
    labels = {m.group(0).strip().lower().rstrip(".") for m in marks}
    if not ({"a", "b"} <= labels or {"1°", "2°"} <= labels):
        return [text]
    parts, start = [], 0
    for m in marks:
        parts.append(text[start:m.start()])
        start = m.end()
    parts.append(text[start:])
    return [p for p in (RE_LIST_TRIM.sub("", p.strip()) for p in parts) if p]


def clean_string(text: str, strip_prep: bool = True) -> str:
    """All string-level rules; returns the cleaned phrase (possibly empty).
    strip_prep=False on the second pass (after cutting clauses): by then a
    leading preposition belongs to a modifier ('op de zaak betrekking hebbende')."""
    s = RE_ENUM.sub("", RE_QUOTES.sub("", text).strip())
    s = RE_EN_OF.sub("of", s)
    for rx, repl in REFS:
        s = rx.sub(repl, s)
    s = _tidy(RE_PRONADV.sub("", s))
    s = _tidy(RE_FIXED_MOD.sub("", s))
    # A preposition is only removed at the very start of the object (governed
    # by the verb: 'voldoen aan de eisen'); later on it belongs to a modifier
    # ('alle voor de uitvoering vereiste informatie').
    if strip_prep:
        s = RE_LEADING_PREP.sub("", s, count=1)
    # leading words, fixed modifiers and light nouns, until nothing changes
    for _ in range(10):
        before = s
        m = RE_NOMINF.match(s)
        if m:
            s = m.group(1)
        s = _strip_leading(s)
        s = _tidy(RE_FIXED_MOD.sub("", s))
        m = RE_INF_FRAME.match(s) or RE_LIGHT.match(s)
        if m:
            s = m.group(1)
        s = _tidy(s)
        if s == before:
            break
    return s


# --------------------------------------------------------------------------
# Parse-level: relative clauses, coordination and noun check
# --------------------------------------------------------------------------

def _join(tokens) -> str:
    """Rebuild text from tokens. Adjacent tokens keep their original spacing; a
    gap (removed tokens in between) always becomes one space. spaCy stores the
    space after a comma on the comma, so dropping the comma must not glue the
    neighbouring words together ('blijken, met' -> 'blijken met', not 'blijkenmet')."""
    toks = sorted(tokens, key=lambda t: t.i)
    out = []
    for k, t in enumerate(toks):
        out.append(t.text)
        if k + 1 < len(toks):
            out.append(t.whitespace_ if toks[k + 1].i == t.i + 1 else " ")
    return _tidy("".join(out))


def _clause_end(doc, start: int) -> int:
    """Last token of a relative clause starting at `start`. A Dutch relative
    clause is verb-final, so it ends at the first verb followed by the end, a
    comma or semicolon, or a coordinator. Without such a verb it runs to the end."""
    for k in range(start + 1, len(doc)):
        if doc[k].pos_ not in ("VERB", "AUX"):
            continue
        nxt = doc[k + 1] if k + 1 < len(doc) else None
        if nxt is None or nxt.text in (",", ";") or nxt.lower_ in ("en", "of", "alsmede"):
            return k
    return len(doc) - 1


def cut_relative_clauses(doc) -> tuple[str, list[str]]:
    """'informatie die door de entiteit wordt verstrekt' -> 'informatie';
    'verwerking van persoonsgegevens waaruit ... blijken, of gegevens over gezondheid'
    -> 'verwerking van persoonsgegevens, of gegevens over gezondheid'.
    Only clauses after a noun (or a comma) are cut. Returns the shortened text
    and the clauses that were removed (kept as context, see "clauses" in the output)."""
    drop: set[int] = set()
    clauses: list[str] = []
    k = 1
    while k < len(doc):
        t = doc[k]
        is_rel = t.lower_ in REL_ADVERBS or (t.lower_ in REL_PRONOUNS and t.pos_ == "PRON")
        if is_rel and doc[k - 1].pos_ in ("NOUN", "PROPN", "PUNCT", "ADJ"):
            end = _clause_end(doc, k)
            first = k - 1 if doc[k - 1].text == "," else k
            drop.update(range(first, end + 1))
            clauses.append(_tidy(doc[k:end + 1].text))
            k = end + 1
            continue
        k += 1
    if not drop:
        return doc.text, []
    return _join([t for t in doc if t.i not in drop]), clauses


def split_coordination(doc, max_parts: int) -> list[str]:
    """'kennis, ervaring en opleiding' -> ['kennis', 'ervaring', 'opleiding'].
    Only nouns coordinated with the head are split; coordinations inside a
    modifier ('gegevens over naam en adres') are left alone. A conjunct counts
    only if a coordinator or comma marks it: the small parser sometimes links
    two nouns as 'conj' without one. An apposition after a comma
    ('voorzitter, de andere leden') is split too."""
    roots = [t for t in doc if t.dep_ == "ROOT"]
    if not roots or roots[0].pos_ not in ("NOUN", "PROPN"):
        return [doc.text]
    head = roots[0]
    # objects with a clause ('informatie die ... wordt verkregen') are not split:
    # the parser rarely gets the coordination right there
    if any(t.pos_ == "AUX" or t.dep_.startswith(("acl", "advcl", "ccomp", "xcomp", "csubj"))
           for t in doc):
        return [doc.text]

    def marked(c):
        lo, hi = sorted((head.i, c.i))
        return any(t.text in (",", ";", "en", "of", "alsmede") for t in doc[lo + 1:hi])
    conjs = [c for c in head.children
             if c.pos_ in ("NOUN", "PROPN") and c.i > head.i and marked(c)
             and (c.dep_ == "conj" or (c.dep_ == "appos" and
                                       any(t.text == "," for t in doc[head.i:c.i])))]
    if not conjs or len(conjs) + 1 > max_parts:
        return [doc.text]
    in_conj = {t.i for c in conjs for t in c.subtree}
    owners = {head.i} | {c.i for c in conjs}

    def keep(t):
        return not (t.dep_ in ("cc", "punct") and t.head.i in owners)
    parts = [[t for t in head.subtree if t.i not in in_conj and keep(t)]]
    parts += [[t for t in c.subtree if keep(t)] for c in conjs]
    return [_tidy(_strip_leading(_join(p))) for p in parts]


MEMBER_WORDS = {"lid", "leden"}   # 'de andere leden' (members) vs 'het eerste lid' (paragraph)
RE_NUMLIKE = re.compile(r"^(?:[0-9]+[a-z]?|" + ORDINAL + r")$", F)


def has_content_noun(doc) -> bool:
    """A noun that is not a reference word. A single word is accepted unless it
    is a function word: the small model often tags lone plurals as verbs.
    'lid'/'leden' count as content (members) unless a number or ordinal is next to them."""
    def ok(t):
        w = t.text.lower()
        if w in MEMBER_WORDS:
            near = [doc[j].text for j in (t.i - 1, t.i + 1) if 0 <= j < len(doc)]
            return not any(RE_NUMLIKE.match(x) for x in near)
        return w not in NON_CONTENT and t.lemma_.lower() not in NON_CONTENT
    if len(doc) == 1:
        t = doc[0]
        return ok(t) and t.text.replace("-", "").isalpha() and len(t) >= 3 and \
            t.pos_ not in ("DET", "PRON", "ADP", "CCONJ", "SCONJ", "NUM", "AUX")
    return any(t.pos_ in ("NOUN", "PROPN") and ok(t) for t in doc)


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

_WORD = re.compile(r"\w+")


def _in_order(unit: str, source: str) -> bool:
    """True if the unit's words occur in the source, in the same order."""
    src = _WORD.findall(source.lower())
    pos = 0
    for w in _WORD.findall(unit.lower()):
        try:
            pos = src.index(w, pos) + 1
        except ValueError:
            return False
    return True


def unit_flags(unit: str, raw: str) -> list[str]:
    flags = []
    if not _in_order(unit, raw):
        flags.append("not_in_source")
    if RE_RESIDUAL_REF.search(unit):
        flags.append("residual_reference")
    if len(unit.split()) > MAX_UNIT_WORDS:
        flags.append("long")
    return flags


# --------------------------------------------------------------------------

def clean_objects(objects: list[str], nlp, max_parts: int = 6, return_clauses: bool = False):
    """Raw object -> list of units (empty for placeholders). With
    return_clauses=True, also returns raw object -> relative clauses that were cut."""
    segments = {o: split_list(o) for o in objects}
    first = {seg: clean_string(seg) for segs in segments.values() for seg in segs}
    texts = sorted({s for s in first.values() if s})
    # relative clauses; cutting one can expose leading words again
    second, cut = {}, {}
    for t, d in zip(texts, nlp.pipe(texts)):
        short, cut[t] = cut_relative_clauses(d)
        second[t] = clean_string(short, strip_prep=False)
    texts2 = sorted({s for s in second.values() if s})
    parts = {t: split_coordination(d, max_parts) for t, d in zip(texts2, nlp.pipe(texts2))}
    # every part must come from its source text; otherwise keep the text whole
    for t, ps in parts.items():
        if len(ps) > 1 and not all(p and _in_order(p, t) for p in ps):
            parts[t] = [t]
    all_parts = sorted({p for ps in parts.values() for p in ps if p})
    ok = {p: has_content_noun(d) for p, d in zip(all_parts, nlp.pipe(all_parts))}
    out, clauses = {}, {}
    for o, segs in segments.items():
        units, cl = [], []
        for seg in segs:
            s = second.get(first[seg], "")
            units += [p for p in parts.get(s, []) if p and ok[p]]
            cl += cut.get(first[seg], [])
        out[o] = list(dict.fromkeys(units))
        clauses[o] = list(dict.fromkeys(cl))
    return (out, clauses) if return_clauses else out


def _get_path(obj, path: list[str]):
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dir", type=Path, default=Path("data/conflict_nl/object"))
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
    units, clauses = clean_objects(objects, nlp, args.max_parts, return_clauses=True)

    args.dir.mkdir(parents=True, exist_ok=True)
    with open(args.dir / "norm_object_units.jsonl", "w", encoding="utf-8") as f:
        for n in norms:
            f.write(json.dumps({**n, "units": units.get(n["object"], []),
                                "negated": bool(RE_NEGATED.match(n["object"])),
                                "clauses": clauses.get(n["object"], [])},
                               ensure_ascii=False) + "\n")

    flagged, flag_count = [], Counter()
    for o in objects:
        for u in units[o]:
            fl = unit_flags(u, o)
            if fl:
                flagged.append({"object": o, "unit": u, "flags": fl})
                flag_count.update(fl)
    with open(args.dir / "object_clean_flags.jsonl", "w", encoding="utf-8") as f:
        for r in flagged:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    c = Counter(len(units[o]) for o in objects)
    log.info("Unique objects: %d -> no units: %d, one unit: %d, several: %d; unique units: %d",
             len(objects), c[0], c[1], sum(v for k, v in c.items() if k > 1),
             len({u for us in units.values() for u in us}))
    log.info("Negated objects (leading 'geen'): %d",
             sum(bool(RE_NEGATED.match(o)) for o in objects))
    if flagged:
        log.warning("Flagged units: %d (%s); see object_clean_flags.jsonl",
                    len(flagged), ", ".join(f"{k} {v}" for k, v in flag_count.most_common()))
        for r in flagged[:5]:
            log.warning("  %s: %r", "/".join(r["flags"]), r["unit"][:100])


if __name__ == "__main__":
    main()