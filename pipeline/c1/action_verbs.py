"""
Extract action verbs (lexicon families) from the 'action' field of each norm,
with negation detection. Does NOT score pairs.

For every verb found it reports the family, the axis/polarity literals from
verb_lexicon.LITERALS (flipped when the verb is negated), the clause it sits in,
and why it was (not) considered negated.

Negation is split into three kinds, because they mean different things downstream:

  action negation   "verstrekt de gegevens niet", "zonder ... te informeren",
                    "onthoudt zich van het verstrekken"
                    -> flips the verb's polarity (provide +1 becomes provide -1).
  modal negation    "mag ... niet bewaren", "hoeft ... niet in kennis te stellen",
                    "is niet verplicht"
                    -> negates the DEONTIC, not the action. Polarity is left alone;
                    the norm is flagged so deontic normalisation can deal with it.
  disapplication    "geldt niet", "is niet van toepassing"
                    -> the norm switches another norm off: an EXEMPTION, not an action.
                    Reported as verb_status "disapplication", not as a missing verb.
                    The deontic stage relabels these norms EXEMPTION (is_disapplication()).
  derogation        "in afwijking van" -> the norm replaces another norm with its own
                    action ("In afwijking van lid 1 mogen ..."). Flagged only; the norm
                    keeps its deontic and its verbs.

Scope: a negator only affects verbs in its own clause. Clauses are split on
punctuation and subordinators (indien, tenzij, dat/die as conjunction/relative...),
so "verstrekt informatie die niet openbaar is" does not negate "verstrekt".

Separable verbs: Dutch main clauses split verbs like "aanwijzen" into
"wijst ... aan". The lexicon only matches joined forms, so SEPARABLE below adds
split and "aan te wijzen" forms for verbs the lexicon already covers.

Usage:
    python action_verbs.py --norms merged_norms.json --out action_verbs.json
    python action_verbs.py --demo
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from verb_lexicon import FAMILIES, GENERIC_AXES, LITERALS

FLAGS = re.IGNORECASE

# ---------------------------------------------------------------------------
# Compiled lexicon
# ---------------------------------------------------------------------------

COMPILED: dict[str, list[re.Pattern]] = {
    fam: [re.compile(p, FLAGS) for p in pats] for fam, pats in FAMILIES.items()
}

# Words the lexicon would match but that are nouns naming an actor or object here.
# "gemachtigde" (authorised representative) matches _v("machtig") via ge- + -de.
NOT_VERBS = re.compile(r"\b(?:gemachtigden?|gemachtigdes)\b", FLAGS)

# ---------------------------------------------------------------------------
# Separable verbs
# ---------------------------------------------------------------------------
# (family, particle, finite forms, infinitive, anchored)
# anchored=True: single-word particle; it must close the clause or be followed by a
# preposition ("legt een kopie over aan het AI-bureau"), otherwise "stelt eisen aan de
# aanbieder" would read as "aanstellen". Multi-word particles are distinctive enough.

SEPARABLE: list[tuple[str, str, str, str, bool]] = [
    ("appoint", "aan", "wijst|wijzen", "wijzen", True),
    ("appoint", "aan", "stelt|stellen", "stellen", True),
    ("appoint", "in", "stelt|stellen", "stellen", True),
    ("appoint", "op", "richt|richten", "richten", True),
    ("provide", "in", "dient|dienen", "dienen", True),
    ("provide", "voor", "legt|leggen", "leggen", True),
    ("provide", "over", "legt|leggen", "leggen", True),   # overleggen: not in lexicon yet
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
    ("withhold", "vertrouwelijk", "houdt|houden|behandelt|behandelen", "houden", False),
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
    ("market", "in gebruik", "stelt|stellen|neemt|nemen", "stellen", False),
    ("market", "aan", "biedt|bieden", "bieden", True),
    ("withdraw", "uit de handel", "neemt|nemen", "nemen", False),
    ("withdraw", "van de markt", "haalt|halen|neemt|nemen", "halen", False),
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
    # legislative acts
    ("adopt", "vast", "stelt|stellen", "stellen", True),
    ("adopt", "uit", "vaardigt|vaardigen", "vaardigen", True),
    ("amend", "aan", "vult|vullen", "vullen", True),
    ("amend", "toe", "voegt|voegen", "voegen", True),
    ("amend", "aan", "past|passen", "passen", True),
    ("amend", "bij", "werkt|werken", "werken", True),
    # compliance and consideration
    ("comply", "na", "leeft|leven", "leven", True),
    ("comply", "na", "komt|komen", "komen", True),
    ("consider", "rekening", "houdt|houden", "houden", False),
    ("consider", "in aanmerking", "neemt|nemen", "nemen", False),
    ("consider", "in acht", "neemt|nemen", "nemen", False),
    ("consider", "in overweging", "neemt|nemen", "nemen", False),
    # producing, conditions, content
    ("produce", "op", "stelt|stellen", "stellen", True),
    ("produce", "uit", "werkt|werken", "werken", True),
    ("produce", "op", "zet|zetten", "zetten", True),
    ("produce", "op", "maakt|maken", "maken", True),
    ("condition", "afhankelijk", "stelt|stellen", "stellen", False),
    ("contain", "in", "voorziet|voorzien", "voorzien", True),
    ("contain", "in", "bouwt|bouwen", "bouwen", True),
]

_PREPOSITIONS = r"aan|bij|in|op|voor|met|naar|van|tot|binnen|door|over|uit|tegen|onder"

# Split forms that change meaning depending on what follows the particle.
# "stelt vast dat ..." establishes a fact; it does not adopt an act.
SEPARABLE_EXCLUDE_NEXT: dict[tuple[str, str], str] = {
    ("adopt", "vast"): r"\s*,?\s*(?:dat|of|welke|wie|hoe)\b",
}


def _phrase(words: str) -> str:
    return r"\s+".join(re.escape(w) for w in words.split())


@dataclass
class _SepEntry:
    family: str
    finite_re: re.Pattern
    particle_re: re.Pattern
    te_re: re.Pattern
    exclude_next: re.Pattern | None = None


_SEP_COMPILED: list[_SepEntry] = []
for _fam, _part, _finite, _inf, _anchored in SEPARABLE:
    if _fam not in FAMILIES:
        raise ValueError(f"SEPARABLE refers to unknown family {_fam!r}")
    _p = _phrase(_part)
    _particle = rf"\b{_p}\b(?=\s*$|\s+(?:{_PREPOSITIONS})\b)" if _anchored else rf"\b{_p}\b"
    _SEP_COMPILED.append(_SepEntry(
        family=_fam,
        finite_re=re.compile(rf"\b(?:{_finite})\b", FLAGS),
        particle_re=re.compile(_particle, FLAGS),
        te_re=re.compile(rf"\b{_p}\s+te\s+{re.escape(_inf)}\b", FLAGS),
        exclude_next=(re.compile(SEPARABLE_EXCLUDE_NEXT[(_fam, _part)], FLAGS)
                      if (_fam, _part) in SEPARABLE_EXCLUDE_NEXT else None),
    ))

# ---------------------------------------------------------------------------
# Clause segmentation
# ---------------------------------------------------------------------------

SUBORDINATORS = {
    "indien", "wanneer", "tenzij", "zodat", "waarbij", "waarin", "waarvan", "waarop",
    "waardoor", "waarmee", "welke", "omdat", "nadat", "voordat", "alvorens", "zodra",
    "mits", "opdat", "terwijl", "totdat", "teneinde",
}
# "dat"/"die" are also determiners ("dat document"). Only treat them as clause openers
# when a comma precedes them (handled by the comma split) or when followed by a word
# that cannot follow a determiner.
_DAT_DIE_FOLLOWERS = (
    r"de|het|een|deze|dit|die|dat|zij|hij|ze|men|er|zijn|haar|hun|"
    r"wordt|worden|werd|werden|is|zal|zullen|kan|kunnen|moet|moeten|mag|mogen|heeft|hebben|"
    r"niet|geen|nooit|ook|wel|alleen|uitsluitend|reeds|nog|"
    r"aan|bij|in|op|voor|met|naar|van|tot|binnen|door|over|uit|tegen|onder|krachtens"
)
_BOUNDARY = re.compile(
    r"(?P<punct>[,;:()])"
    rf"|\b(?:{'|'.join(sorted(SUBORDINATORS))})\b"
    r"|\bmaar\b"
    rf"|\b(?:dat|die)\b(?=\s+(?:{_DAT_DIE_FOLLOWERS})\b)",
    FLAGS,
)


@dataclass
class Clause:
    index: int
    start: int
    end: int
    subordinate: bool


_HARD_PUNCT = {";", ":"}
_COORDINATORS = {"en", "of", "alsmede", "dan wel"}
_AUX = re.compile(
    r"(?:is|zijn|wordt|worden|zal|zullen|mag|mogen|moet|moeten|kan|kunnen|heeft|hebben|"
    r"hoeft|hoeven|dient|dienen)\b", FLAGS)


def _starts_with_verb(seg: str) -> bool:
    """True if the segment opens with a verb (optionally after a coordinator).
    In Dutch, a main clause after a fronted subordinate clause opens with its finite verb
    ("Indien ..., meldt de aanbieder ..."); a continuation of the same clause does not."""
    seg = seg.lstrip()
    first = re.match(r"(\w+)\s+", seg)
    if first and first.group(1).lower() in _COORDINATORS:
        seg = seg[first.end():]
    if _AUX.match(seg):
        return True
    if any(rx.match(seg) for regs in COMPILED.values() for rx in regs):
        return True
    return any(e.finite_re.match(seg) for e in _SEP_COMPILED)


PARENTHETICAL_MAX_WORDS = 4


def segment_clauses(text: str) -> list[Clause]:
    """Split on subordinators and punctuation. Commas and brackets are soft: a segment
    after them is merged back into the previous clause unless it opens a new clause
    (subordinator, or a verb). Semicolons and colons are hard boundaries.
    Short comma-enclosed interjections (", indien nodig,", ", indien van toepassing,")
    stay inside the clause they interrupt, so "stelt, indien nodig, ... vast" keeps its verb."""
    raw: list[tuple[int, int, bool]] = []   # (start, end, preceded by a soft punctuation mark)
    pos, soft_before = 0, False
    for m in _BOUNDARY.finditer(text):
        cut_start = m.start()
        next_start = m.end() if m.group("punct") else m.start()
        if cut_start > pos and text[pos:cut_start].strip():
            raw.append((pos, cut_start, soft_before))
            soft_before = False
        if m.group("punct") in (",", "(", ")"):
            soft_before = True
        elif m.group("punct") in _HARD_PUNCT:
            soft_before = False
        pos = max(pos, next_start)
    raw.append((pos, len(text), soft_before))

    # trim whitespace, drop empties
    segs: list[tuple[int, int, bool]] = []
    for s, e, soft in raw:
        seg = text[s:e]
        if not seg.strip():
            continue
        s += len(seg) - len(seg.lstrip())
        e -= len(seg) - len(seg.rstrip())
        segs.append((s, e, soft))

    merged: list[list] = []   # [start, end, subordinate]
    for k, (s, e, soft) in enumerate(segs):
        seg = text[s:e]
        first = re.match(r"\w+", seg)
        first_word = first.group().lower() if first else ""
        opens_sub = first_word in SUBORDINATORS or first_word in ("dat", "die")
        next_soft = k + 1 < len(segs) and segs[k + 1][2]
        parenthetical = (opens_sub and soft and next_soft and merged
                         and len(seg.split()) <= PARENTHETICAL_MAX_WORDS)
        if merged and (parenthetical or (soft and not opens_sub and not _starts_with_verb(seg))):
            merged[-1][1] = e                 # continuation of (or aside within) the previous clause
            continue
        merged.append([s, e, opens_sub])

    return [Clause(index=i, start=s, end=e, subordinate=sub)
            for i, (s, e, sub) in enumerate(merged)]


# ---------------------------------------------------------------------------
# Negation markers
# ---------------------------------------------------------------------------

# "niet-naleving" is a noun compound, not a negation: hence (?!-).
NEGATOR = re.compile(r"\b(?:niet|geen|nooit|niets|nimmer|noch|generlei|geenszins)\b(?!-)", FLAGS)

# Modal verbs: a negator in the same clause negates the modality, not the action.
MODAL_VERB = re.compile(r"\b(?:mag|mogen|moet|moeten|hoeft|hoeven|kan|kunnen)\b", FLAGS)

# Fixed phrases where the negation belongs to the deontic.
MODAL_PHRASE = re.compile(
    r"\b(?:niet|geenszins)\s+(?:langer\s+)?(?:verplicht|vereist|toegestaan|toegelaten|gehouden|"
    r"bevoegd|gerechtigd|nodig)\b"
    r"|\bgeen\s+(?:verplichting|plicht|toestemming|bevoegdheid|recht)\b",
    FLAGS,
)

DISAPPLICATION = re.compile(
    r"\bgeld(?:t|en)\s+niet\b"
    r"|\b(?:is|zijn)\s+niet\s+van\s+toepassing\b|\bniet\s+van\s+toepassing\b",
    FLAGS,
)

DEROGATION = re.compile(r"\bin\s+afwijking\s+van\b", FLAGS)

PROHIBITION_MARKER = re.compile(r"\bverb(?:iedt|ieden|oden)\b|\bverbod\b", FLAGS)

# Refraining negates the verb that follows: "onthoudt zich van het verstrekken".
REFRAIN = re.compile(
    r"\bonthoud(?:t|en)?\s+zich\s+van\b|\bzich\s+(?:te\s+)?onthouden\s+van\b"
    r"|\bzie(?:t|n)\s+af\s+van\b|\bafzien\s+van\b",
    FLAGS,
)

ZONDER = re.compile(r"\bzonder\b", FLAGS)

# ---------------------------------------------------------------------------
# Hits
# ---------------------------------------------------------------------------


@dataclass
class Hit:
    family: str
    start: int
    end: int
    matched: str
    source: str                       # lexicon | separable | separable_te
    parts: list[tuple[int, int]]
    clause_index: int = -1
    subordinate: bool = False
    negated: bool = False
    negation_reasons: list[str] = field(default_factory=list)


def _overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def find_hits(text: str, clauses: list[Clause]) -> list[Hit]:
    contiguous: list[Hit] = []
    for fam, regs in COMPILED.items():
        for rx in regs:
            for m in rx.finditer(text):
                if m.end() > m.start() and not NOT_VERBS.fullmatch(m.group()):
                    contiguous.append(Hit(fam, m.start(), m.end(), m.group(), "lexicon",
                                          [(m.start(), m.end())]))

    separable: list[Hit] = []
    for entry in _SEP_COMPILED:
        for m in entry.te_re.finditer(text):
            contiguous.append(Hit(entry.family, m.start(), m.end(), m.group(), "separable_te",
                                  [(m.start(), m.end())]))
        for c in clauses:
            seg = text[c.start:c.end]
            for vm in entry.finite_re.finditer(seg):
                pm = entry.particle_re.search(seg, vm.end())
                if not pm:
                    continue
                if entry.exclude_next and entry.exclude_next.match(text, c.start + pm.end()):
                    continue
                v_abs = (c.start + vm.start(), c.start + vm.end())
                p_abs = (c.start + pm.start(), c.start + pm.end())
                separable.append(Hit(entry.family, v_abs[0], p_abs[1],
                                     f"{vm.group()} … {pm.group()}", "separable", [v_abs, p_abs]))

    # Separable splits are the most specific reading of their words: drop contiguous hits
    # that touch either part ("levert ... aan" is providing, not "levert" = market).
    sep_parts = [p for h in separable for p in h.parts]
    contiguous = [h for h in contiguous
                  if not any(_overlap((h.start, h.end), p) for p in sep_parts)]

    # Deduplicate identical (span, family), then keep the longest contiguous match.
    seen: set[tuple[int, int, str]] = set()
    unique: list[Hit] = []
    for h in sorted(contiguous, key=lambda h: (-(h.end - h.start), h.start)):
        key = (h.start, h.end, h.family)
        if key not in seen:
            seen.add(key)
            unique.append(h)

    kept: list[Hit] = []
    for h in unique:
        clash = False
        for k in kept:
            if _overlap((h.start, h.end), (k.start, k.end)):
                same_span = (h.start, h.end) == (k.start, k.end)
                if same_span and h.family != k.family:
                    continue        # genuinely ambiguous span: keep both readings
                clash = True
                break
        if not clash:
            kept.append(h)

    sep_seen: set[tuple[tuple[int, int], str]] = set()
    sep_unique: list[Hit] = []
    for h in separable:
        key = (h.parts[0], h.family)
        if key not in sep_seen:
            sep_seen.add(key)
            sep_unique.append(h)

    hits = sorted(kept + sep_unique, key=lambda h: h.start)
    for h in hits:
        for c in clauses:
            if c.start <= h.start < c.end:
                h.clause_index, h.subordinate = c.index, c.subordinate
                break
    return hits


# ---------------------------------------------------------------------------
# Negation analysis
# ---------------------------------------------------------------------------


@dataclass
class ClauseNegation:
    action_negators: list[str]
    modal_negation: list[str]
    modal_phrase_spans: list[tuple[int, int, str]]
    disapplication: list[str]
    refrain_spans: list[tuple[int, int, str]]
    zonder_positions: list[int]


def analyse_clause(text: str, clause: Clause) -> ClauseNegation:
    cs = clause.start
    seg = text[clause.start:clause.end]
    negators = [(cs + m.start(), cs + m.end(), m.group().lower()) for m in NEGATOR.finditer(seg)]
    consumed: set[tuple[int, int, str]] = set()

    disapplication: list[str] = []
    for m in DISAPPLICATION.finditer(seg):
        span = (cs + m.start(), cs + m.end())
        disapplication.append(m.group())
        consumed.update(n for n in negators if span[0] <= n[0] < span[1])

    modal_phrase_spans: list[tuple[int, int, str]] = []
    modal_negation: list[str] = []
    for m in MODAL_PHRASE.finditer(seg):
        span = (cs + m.start(), cs + m.end())
        modal_phrase_spans.append((span[0], span[1], m.group()))
        modal_negation.append(m.group())
        consumed.update(n for n in negators if span[0] <= n[0] < span[1])

    remaining = [n for n in negators if n not in consumed]
    modal = MODAL_VERB.search(seg)
    if modal and remaining:
        n = remaining.pop(0)
        modal_negation.append(f"{modal.group()} … {n[2]}")

    return ClauseNegation(
        action_negators=[n[2] for n in remaining],
        modal_negation=modal_negation,
        modal_phrase_spans=modal_phrase_spans,
        disapplication=disapplication,
        refrain_spans=[(cs + m.start(), cs + m.end(), m.group()) for m in REFRAIN.finditer(seg)],
        zonder_positions=[cs + m.start() for m in ZONDER.finditer(seg)],
    )


def apply_negation(hits: list[Hit], clauses: list[Clause], analyses: dict[int, ClauseNegation]) -> list[Hit]:
    out: list[Hit] = []
    for c in clauses:
        neg = analyses[c.index]
        in_clause = [h for h in hits if h.clause_index == c.index]

        # "onthoudt zich van" also matches the withhold family. If another verb follows,
        # refraining is a negator for that verb, not a verb of its own.
        dropped: set[int] = set()
        for rs, re_, _ in neg.refrain_spans:
            if any(h.start >= re_ for h in in_clause):
                dropped.update(id(h) for h in in_clause
                               if h.family == "withhold" and _overlap((h.start, h.end), (rs, re_)))
        in_clause = [h for h in in_clause if id(h) not in dropped]

        # "zonder" negates the first verb that follows it in the clause.
        zonder_targets: set[int] = set()
        for zp in neg.zonder_positions:
            following = [h for h in in_clause if h.start > zp]
            if following:
                zonder_targets.add(id(following[0]))

        for h in in_clause:
            reasons = list(neg.action_negators)
            reasons += [f"refrain:{txt}" for rs, re_, txt in neg.refrain_spans if h.start >= re_]
            if id(h) in zonder_targets:
                reasons.append("zonder")
            # A verb inside a modal phrase ("niet verplicht") carries that negation itself.
            reasons += [f"phrase:{txt}" for s, e, txt in neg.modal_phrase_spans
                        if _overlap((h.start, h.end), (s, e))]
            h.negation_reasons = reasons
            h.negated = len(reasons) % 2 == 1
            out.append(h)
    return out


# ---------------------------------------------------------------------------
# Per-norm extraction
# ---------------------------------------------------------------------------


def clean_action(text: str) -> str:
    text = text.replace("\u00a0", " ").replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"\s+", " ", text).strip()


def _hit_to_dict(h: Hit) -> dict:
    sign = -1 if h.negated else 1
    axes = [{"axis": axis, "polarity": pol * sign} for axis, pol in LITERALS[h.family]]
    return {
        "family": h.family,
        "axes": axes,
        "negated": h.negated,
        "negation_reasons": h.negation_reasons,
        "matched": h.matched,
        "start": h.start,
        "end": h.end,
        "source": h.source,
        "clause_index": h.clause_index,
        "subordinate": h.subordinate,
        "generic": any(a["axis"] in GENERIC_AXES for a in axes),
    }


def is_disapplication(action: Any) -> bool:
    """True if the action switches another norm off ("geldt niet", "is niet van toepassing").
    Used by the deontic stage to relabel such norms EXEMPTION."""
    return isinstance(action, str) and bool(DISAPPLICATION.search(clean_action(action)))


def _verb_status(action_present: bool, verbs: list, disapplication: list) -> str:
    """empty | disapplication | verb | none. Only 'none' is a lexicon or extraction gap."""
    if not action_present:
        return "empty"
    if disapplication:
        return "disapplication"
    return "verb" if verbs else "none"


def extract_action_verbs(action: Any) -> dict:
    """Extract verb hits and negation flags from one action string."""
    if not isinstance(action, str) or not action.strip():
        return {"action_clean": None, "verbs": [], "primary": None, "modal_negation": [],
                "disapplication": [], "derogation": [], "prohibition_marker": [],
                "verb_status": "empty", "no_verb_found": True, "clauses": []}

    text = clean_action(action)
    clauses = segment_clauses(text)
    hits = find_hits(text, clauses)
    analyses = {c.index: analyse_clause(text, c) for c in clauses}
    hits = apply_negation(hits, clauses, analyses)
    verbs = [_hit_to_dict(h) for h in hits]

    main = [v for v in verbs if not v["subordinate"]]
    primary = (main or verbs or [None])[0]
    disapplication = [d for a in analyses.values() for d in a.disapplication]
    status = _verb_status(True, verbs, disapplication)

    return {
        "action_clean": text,
        "verbs": verbs,
        "primary": primary,
        "modal_negation": [m for a in analyses.values() for m in a.modal_negation],
        "disapplication": disapplication,
        "derogation": [m.group() for m in DEROGATION.finditer(text)],
        "prohibition_marker": [m.group() for m in PROHIBITION_MARKER.finditer(text)],
        "verb_status": status,
        # True only for a real gap: disapplications and empty actions are counted separately.
        "no_verb_found": status == "none",
        "clauses": [{"index": c.index, "text": text[c.start:c.end], "subordinate": c.subordinate}
                    for c in clauses],
    }


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def load_norm_records(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict) and isinstance(data.get("norms"), list):
        return [r for r in data["norms"] if isinstance(r, dict)]
    if isinstance(data, dict):
        return [{"norm_id": k, **v} for k, v in data.items() if isinstance(v, dict)]
    raise ValueError(f"Unrecognised structure in {path}")


def _inner(rec: dict) -> dict:
    return rec["norm"] if isinstance(rec.get("norm"), dict) else rec


def run(norms_path: Path, out_path: Path) -> None:
    records = load_norm_records(norms_path)
    results: list[dict] = []
    fam_counts: Counter[str] = Counter()
    primary_counts: Counter[str] = Counter()
    deontic_vs_negation: Counter[str] = Counter()

    for rec in records:
        norm = _inner(rec)
        res = extract_action_verbs(norm.get("action"))
        deontic = norm.get("deontic")
        for v in res["verbs"]:
            fam_counts[v["family"]] += 1
        if res["primary"]:
            primary_counts[res["primary"]["family"]] += 1
            if res["primary"]["negated"]:
                deontic_vs_negation[f"{deontic} + negated primary verb"] += 1
        if res["modal_negation"]:
            deontic_vs_negation[f"{deontic} + modal negation"] += 1
        if res["disapplication"]:
            deontic_vs_negation[f"{deontic} + disapplication"] += 1
        if res["derogation"]:
            deontic_vs_negation[f"{deontic} + derogation"] += 1
        results.append({"norm_id": rec.get("norm_id"), "deontic": deontic,
                        "action": norm.get("action"), **res})

    n = len(results)
    no_verb = [r for r in results if r["no_verb_found"]]
    status_counts = Counter(r["verb_status"] for r in results)
    stats = {
        "n_norms": n,
        "verb_status": dict(status_counts.most_common()),
        "no_verb_found": len(no_verb),
        "norms_with_negated_verb": sum(1 for r in results if any(v["negated"] for v in r["verbs"])),
        "norms_with_modal_negation": sum(1 for r in results if r["modal_negation"]),
        "norms_with_disapplication": sum(1 for r in results if r["disapplication"]),
        "norms_with_derogation": sum(1 for r in results if r["derogation"]),
        "deontic_vs_negation": dict(deontic_vs_negation.most_common()),
        "primary_family_counts": dict(primary_counts.most_common()),
        "all_family_counts": dict(fam_counts.most_common()),
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump({"stage": "action-verbs", "input": str(norms_path), "stats": stats,
                   "norms": results}, f, ensure_ascii=False, indent=1)

    print(f"Processed {n} norms -> {out_path}")
    print("Verb status:", stats["verb_status"])
    print(f"No verb found (real gaps): {len(no_verb)} ({len(no_verb) / max(n, 1):.0%})")
    print(f"Negated verb: {stats['norms_with_negated_verb']}, modal negation: "
          f"{stats['norms_with_modal_negation']}, disapplication: {stats['norms_with_disapplication']}, "
          f"derogation: {stats['norms_with_derogation']}")
    print("Deontic vs negation:", stats["deontic_vs_negation"])
    if no_verb:
        print("\nFirst actions with no verb found (candidates for lexicon additions):")
        for r in no_verb[:15]:
            print(f"  {r['norm_id']}: {r['action']}")


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

DEMO_ACTIONS = [
    "beëindigt het mandaat",
    "middels een schriftelijke machtiging een gemachtigde aan die is gevestigd in de Unie",
    "legt op verzoek een kopie van het mandaat over aan het AI-bureau",
    "schrijft de aanmeldende autoriteit voor dat de aangemelde instantie binnen een redelijke, "
    "door de autoriteit bepaalde termijn, onterecht afgegeven certificaten schorst of intrekt",
    "stelt de aanmeldende autoriteit de Commissie en de lidstaten in kennis van certificaten "
    "die moeten worden geschorst of ingetrokken",
    "De in dit artikel genoemde verplichting geldt niet",
    "verstrekt de gegevens niet aan derden",
    "verstrekt informatie die niet openbaar is",
    "mag de persoonsgegevens niet langer bewaren dan noodzakelijk",
    "hoeft de betrokkene niet in kennis te stellen",
    "is niet verplicht de autoriteit te informeren",
    "onthoudt zich van het verstrekken van informatie",
    "verwerkt de gegevens zonder de betrokkene te informeren",
    "wijst geen gemachtigde aan",
    "voert de niet-nalevingsprocedure uit",
    "stelt eisen aan de aanbieder",
    "is niet van toepassing op gebieden die buiten het toepassingsgebied van het Unierecht vallen",
    "In afwijking van lid 1 mogen de lidstaten de vergunning intrekken",
]


def demo() -> None:
    for action in DEMO_ACTIONS:
        res = extract_action_verbs(action)
        print(f"\n{action}")
        if res["verb_status"] != "verb":
            print(f"   ({res['verb_status']})")
        for v in res["verbs"]:
            axes = ", ".join(f"{a['axis']}{'+' if a['polarity'] > 0 else '-'}" for a in v["axes"])
            neg = f" NEGATED {v['negation_reasons']}" if v["negated"] else ""
            sub = " [sub]" if v["subordinate"] else ""
            print(f"   {v['family']:<10} {axes:<40} '{v['matched']}'{sub}{neg}")
        for key in ("modal_negation", "disapplication", "derogation", "prohibition_marker"):
            if res[key]:
                print(f"   {key}: {res[key]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract action verbs and negation from norms.")
    parser.add_argument("--norms", type=Path, default=Path("merged_norms.json"))
    parser.add_argument("--out", type=Path, default=Path("action_verbs.json"))
    parser.add_argument("--demo", action="store_true", help="run on built-in examples and exit")
    args = parser.parse_args()
    if args.demo:
        demo()
        return
    run(args.norms, args.out)


if __name__ == "__main__":
    main()