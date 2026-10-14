"""
Build the "object text" of a norm: what its action is about, with the verb, the
actor and the circumstances removed. Shared by object_coverage.py (measurement)
and, later, the action scorer (object similarity).

Stripping, in order:
  1. Verbs. Only real verb forms are removed. Action nouns the lexicon also matches
     (beoordeling, verwerking, documentatie, registratie ...) are kept: in
     "documenteert zijn beoordeling" the noun IS the object. Split verbs lose only
     the verb and particle, never the words in between.
  2. Addressee. Removed in active actions (unless that would remove every content word:
     then the "addressee" was really the object and is kept). In passive actions the extractor often
     quotes the grammatical subject as addressee ("de eisen van hoofdstuk III" in
     "worden de eisen ... in aanmerking genomen"), which is really the object; there it
     is only removed when addressee_type names a real actor (ACTOR_TYPES).
  3. Context fields, all verbatim quotes in the norm record:
       trigger_event, deadline.raw, thresholds  -> always removed
       conditions                               -> removed only when clause-like
                                                   ("indien ...", "na een verzoek",
                                                   "die in derde landen ..."); a bare noun
                                                   phrase ("onterecht afgegeven
                                                   certificaten") narrows the object and
                                                   is kept.
     Safety net: if removing the context fields would leave no content words at all,
     they are put back (the field probably quoted the object itself).

recipient_body is deliberately kept: who receives the documents is part of what
the action is about.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from action_verbs import SUBORDINATORS, clean_action

FLAGS = re.IGNORECASE

ACTOR_TYPES = {"REGULATED_ENTITY", "COMPETENT_AUTHORITY"}

STOPWORDS = set("""
de het een en of noch maar in op aan van voor met door bij tot naar uit over onder tegen
binnen buiten zonder na om te als dan dat die dit deze welke wie wat waar waarbij waarin
waarvan waarop waardoor hij zij ze zich zijn haar hun hem hen wij men er hier daar daarin
daarvan daarvoor daarbij daartoe hiervan hieraan hiertoe hierin ook niet geen wel nog al
alle elk elke ieder iedere enig enige ander andere zo zoals zodat indien wanneer tenzij
mits omdat terwijl nadat voordat zodra is was waren wordt worden werd werden zal zullen
kan kunnen mag mogen moet moeten heeft hebben had hadden hoeft hoeven dient dienen gaat
gaan doet doen ten minste tenminste onverminderd overeenkomstig krachtens betreffende
inzake namens alsmede respectievelijk eventueel eventuele passend passende redelijk
redelijke tijdig onverwijld onmiddellijk uiterlijk zoveel mogelijk relevant relevante
bedoeld bedoelde genoemd genoemde vermeld vermelde volgende volgend desbetreffend
desbetreffende betrokken eigen ervan erop eraan ertoe erin meer minder ten aanzien
gevallen geval verband mede tevens nader nadere
toepassing verzoek oog kader wijze manier basis gebied zin opzicht behoren
""".split())

# References carry no object meaning ("artikel 5, lid 2", "bijlage III").
REFERENCE_WORDS = set("""
artikel artikelen lid leden bijlage bijlagen punt punten alinea alinea's hoofdstuk
afdeling titel verordening richtlijn besluit sub onder
""".split())

TOKEN = re.compile(r"[a-zà-ÿ][a-zà-ÿ'\-]*", FLAGS)
ROMAN = re.compile(r"^[ivxlcdm]+$", FLAGS)
PASSIVE = re.compile(r"\b(?:wordt|worden|werd|werden)\b", FLAGS)
ARTICLE = re.compile(r"^(?:de|het|een)\s+", FLAGS)

# A lexicon hit that is a single noun (beoordeling, documentatie, inzage, doorgifte ...).
ACTION_NOUN = re.compile(r"^[^\s…]+(?:ing|ingen|atie|aties|age|gifte|slag)$", FLAGS)

# A condition is clause-like (circumstance, not object) when it opens with one of these.
CLAUSE_OPENERS = SUBORDINATORS | {
    "die", "dat", "welke", "wie", "waar", "waarvan", "waarbij", "waarin", "waarop",
    "voor", "na", "bij", "in", "op", "tijdens", "gedurende", "binnen", "zolang",
    "behalve", "behoudens", "uitgezonderd", "onverminderd", "overeenkomstig",
    "krachtens", "ingevolge", "zonder", "met", "als", "zo", "ingeval",
}

MIN_CONTENT_AFTER_CONTEXT = 1


@dataclass
class ObjectText:
    text: str                       # residual text after stripping
    content_words: list[str]
    passive: bool
    addressee_stripped: bool
    stripped_fields: list[str] = field(default_factory=list)
    kept_conditions: list[str] = field(default_factory=list)
    context_restored: bool = False  # safety net fired

    @property
    def n_content(self) -> int:
        return len(self.content_words)


def content_words(text: str) -> list[str]:
    words = [t.lower() for t in TOKEN.findall(text)]
    return [w for w in words
            if len(w) >= 3 and w not in STOPWORDS and w not in REFERENCE_WORDS
            and not ROMAN.match(w)]


def _phrase_pattern(s: str) -> re.Pattern | None:
    words = clean_action(s).split()
    if not words or len(" ".join(words)) < 3:
        return None
    return re.compile(r"\s+".join(re.escape(w) for w in words), FLAGS)


def _remove(text: str, phrase: str) -> tuple[str, bool]:
    pat = _phrase_pattern(phrase)
    if pat is None:
        return text, False
    new, n = pat.subn(" ", text)
    return new, n > 0


def _is_clause_like(condition: str) -> bool:
    first = re.match(r"\w+", clean_action(condition))
    return bool(first) and first.group().lower() in CLAUSE_OPENERS


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def build_object_text(norm: dict, extraction: dict) -> ObjectText:
    """norm: the inner norm dict (action, addressee, addressee_type, conditions, ...).
    extraction: the result of action_verbs.extract_action_verbs(norm['action'])."""
    text = extraction.get("action_clean") or clean_action(norm.get("action") or "")
    passive = bool(PASSIVE.search(text))

    # 1. verbs, keeping action nouns
    chars = list(text)
    for v in extraction.get("verbs", []):
        if v.get("source") == "lexicon" and ACTION_NOUN.match(v["matched"]):
            continue
        for s, e in v.get("parts") or [(v["start"], v["end"])]:
            for k in range(s, min(e, len(chars))):
                chars[k] = " "
    residual = "".join(chars)

    # 2. addressee
    addressee = norm.get("addressee")
    addressee_type = norm.get("addressee_type")
    strip_addressee = (not passive) or (addressee_type in ACTOR_TYPES)
    addressee_stripped = False
    if strip_addressee and isinstance(addressee, str) and addressee.strip():
        before_addressee = residual
        a = clean_action(addressee)
        for variant in {a, ARTICLE.sub("", a)}:
            residual, hit = _remove(residual, variant)
            addressee_stripped |= hit
        # Guard: an "addressee" that takes the whole object with it is the object
        # (e.g. passive "worden de eisen ... in aanmerking genomen" with the requirements
        # typed as an actor). Put it back.
        if addressee_stripped and not content_words(residual) and content_words(before_addressee):
            residual, addressee_stripped = before_addressee, False

    # 3. context fields
    before_context = residual
    stripped: list[str] = []
    kept_conditions: list[str] = []

    for t in _as_list(norm.get("trigger_event")):
        residual, hit = _remove(residual, t)
        if hit:
            stripped.append("trigger_event")
    deadline = norm.get("deadline")
    if isinstance(deadline, dict):
        for t in _as_list(deadline.get("raw")):
            residual, hit = _remove(residual, t)
            if hit:
                stripped.append("deadline")
    for t in _as_list(norm.get("thresholds")):
        residual, hit = _remove(residual, t)
        if hit:
            stripped.append("thresholds")
    for c in _as_list(norm.get("conditions")):
        if _is_clause_like(c):
            residual, hit = _remove(residual, c)
            if hit:
                stripped.append("conditions")
        else:
            kept_conditions.append(c)

    restored = False
    if stripped and len(content_words(residual)) < MIN_CONTENT_AFTER_CONTEXT \
            and len(content_words(before_context)) >= MIN_CONTENT_AFTER_CONTEXT:
        residual, stripped, restored = before_context, [], True

    residual = re.sub(r"\s+", " ", residual).strip(" ,;:")
    return ObjectText(
        text=residual,
        content_words=content_words(residual),
        passive=passive,
        addressee_stripped=addressee_stripped,
        stripped_fields=sorted(set(stripped)),
        kept_conditions=kept_conditions,
        context_restored=restored,
    )