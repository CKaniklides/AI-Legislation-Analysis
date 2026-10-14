"""Action-verb relations for norm pairs: flag DUPLICATION and CONTRADICTION from the verbs.

Standalone: it imports nothing from detect_c1_contradiction. You choose the input file; it
writes a score for every pair, stored so any score can be traced back to the two norms, the
verbs that were found, and the exact formula and lexicon version that produced it.

Usage:
    python action_verb_relations.py --input data/conflicts_data/merged_norms.json
    python action_verb_relations.py --input merged_norms.json --pairs stageA_pairs.json
    python action_verb_relations.py --input norms.json --field action=norm.action --field deontic=norm.deontic
    python action_verb_relations.py --selftest
    python action_verb_relations.py --explain "de inbreuk niet melden" --deontic PROHIBITION

Input : a JSON list of norm records (norm_id, action, deontic, and optionally addressee_type,
        hierarchy.instrument_id / hierarchy.article, text). Fields are found by name; override
        with --field. --pairs takes stageA_pairs.json (or a plain list of [i, j] / [id, id]).
Output: <out-dir>/pair_scores.jsonl          one row per pair, both scores + evidence
        <out-dir>/pair_scores.csv            same, flat, for sorting in a spreadsheet
        <out-dir>/norm_verb_profiles.jsonl   one row per norm: the verbs/signs behind its scores
        <out-dir>/manifest.json              input + output hashes, parameters, formula, counts

How it works
------------
1. VERB FAMILIES: each family is a set of near-synonyms (melden / kennisgeven / notificeren ...).
   Two norms whose verbs are in the same family are saying the same kind of thing.
2. AXES AND LITERALS: an axis is one yes/no question ("is this thing retained?"). A family is
   either a positive literal on its own axis, or the NEGATIVE literal of one or more axes:
       bewaren   -> (retain, +)          wissen/vernietigen -> (retain, -)
       melden    -> (notify, +)          achterhouden       -> (notify, -), (provide, -), ...
   So antonyms need no pairwise table: they are the same axis with the opposite sign.
3. EFFECTIVE SIGN = literal sign x deontic (PROHIBITION flips) x negation ("niet", "geen",
   "nooit", "nalaten", "afzien"; parity counted per sentence). "niet wissen" and "bewaren"
   therefore both land on (retain, +).
4. COMPARE: same axis, same sign -> duplicate; same axis, opposite sign -> contradiction.
   The score is scaled by how much the objects of the two actions overlap, because
   "notify the authority" vs "do not notify the data subject" is NOT a contradiction.

Known limits (deliberate, documented, not hidden):
 - Negation scope is the whole sentence segment (split on . ; :), not a parse tree.
 - Surface matching is regex over Dutch inflection; unknown verbs are silently ignored,
   which means misses, never false positives from them.
 - Object overlap is a 6-character-prefix Jaccard on content words. Dutch compounds
   ("beveiligingsmaatregelen" vs "maatregelen") will under-match; swap in embedding
   similarity of the object phrase if that matters.
 - A verb hit is a CANDIDATE signal. Adjudication still decides. Nothing here is a verdict.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import tempfile
import unicodedata
from collections import Counter, defaultdict
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Lexicon
# ---------------------------------------------------------------------------

_V_SUFFIX = r"(?:en|t|e|d|de|te)?"            # verb forms only (meld, melden, meldt, gemeld)
_N_SUFFIX = r"(?:en|t|e|d|de|te|ing|ingen)?"  # verb forms + the action noun (melding)


def _v(*stems: str) -> str:
    """Inflected verb forms of one or more stems, with an optional ge- participle prefix.
    Nouns are excluded on purpose where the noun is usually the OBJECT of another verb
    ("de vergunning intrekken" must not count as authorising)."""
    alt = "|".join(re.escape(s) for s in stems)
    return rf"\b(?:ge)?(?:{alt}){_V_SUFFIX}\b"


def _n(*stems: str) -> str:
    """Like _v, but also matches the -ing action noun (melding, bewaring, vernietiging)."""
    alt = "|".join(re.escape(s) for s in stems)
    return rf"\b(?:ge)?(?:{alt}){_N_SUFFIX}\b"


# family -> list of regexes (raw strings are for separable verbs and fixed phrases).
FAMILIES: dict[str, list[str]] = {
    # --- information flow ---------------------------------------------------------------
    "notify": [
        _n("meld"), _n("notificeer", "notificer"), _n("rapporteer", "rapporter"),
        _v("informeer", "informer"), r"\bge[iï]nformeerd\b", _n("waarschuw"),
        r"\bkennis(?:geving|geven|gegeven|geeft)\b",
        r"\bin\s+kennis\s+(?:te\s+)?(?:stel(?:len|t)|gesteld)\b",
        r"\bmede(?:delen|deelt|gedeeld|deling(?:en)?)\b",
    ],
    "provide": [
        _n("verstrekk", "verstrek"), _n("verschaff", "verschaf"),
        r"\bindienen\b", r"\bingediend\b", r"\bin\s+te\s+dienen\b", r"\bindiening\b",
        r"\btoe(?:zenden|zendt|gezonden|zending)\b", r"\b(?:doen|doet)\s+toekomen\b",
        _v("aanlever"), r"\baangeleverd\b",
        r"\b(?:ter\s+beschikking|beschikbaar)\s+(?:te\s+)?(?:stel(?:len|t)|gesteld)\b",
        r"\bvoorleg(?:gen|t)\b", r"\bvoorgelegd\b",
    ],
    "publish": [
        r"\bopenbaar\s+(?:te\s+)?(?:maken|maakt|gemaakt)\b",
        r"\bbekend\s*(?:te\s+)?(?:maken|maakt|gemaakt)\b", r"\bbekendmak(?:en|t|ing)\b",
        r"\bbekendgemaakt\b", _n("publiceer", "publicer"),
    ],
    "transfer": [
        _n("overdrag"), r"\bovergedragen\b", r"\bdoorgeven\b", r"\bdoorgegeven\b",
        r"\bdoorgifte\b", _n("verzend"), r"\bverzonden\b",
        r"\bdoorzend(?:en|t)\b", r"\bdoorgezonden\b",
    ],
    "access": [
        _v("raadpleeg", "raadpleg"), r"\binzien\b", r"\bingezien\b", r"\binzage\b", r"\binziet\b",
        r"\btoegang\s+(?:te\s+)?(?:hebben|heeft|krijgen|krijgt|verkrijgen|verkrijgt|verkregen)\b",
    ],
    "withhold": [   # NEGATIVE of notify / provide / publish / transfer
        _v("achterhoud"), r"\bachtergehouden\b", _v("verzwijg"), r"\bverzwegen\b",
        r"\bgeheimhoud\w*", r"\bgeheimgehouden\b", _v("onthoud"), r"\bonthouden\b",
        r"\bvertrouwelijk\s+(?:te\s+)?(?:houden|houdt|gehouden)\b",
        r"\bvertrouwelijk\s+(?:te\s+)?(?:behandel\w*|behandeld)\b",
    ],
    "deny_access": [   # NEGATIVE of access
        r"\btoegang\s+(?:te\s+)?(?:weiger(?:en|t)|geweigerd|ontzeg(?:gen|t)|ontzegd|beperk(?:en|t)|"
        r"beperkt|blokkeer(?:t)?|blokkeren|geblokkeerd)\b",
    ],
    # --- data lifecycle -----------------------------------------------------------------
    "collect": [
        _n("verzamel"), r"\binwinn?(?:en|t)\b", r"\bingewonnen\b", r"\binwinning\b",
        _v("vergaar", "vergar"), r"\bverkrijg(?:en|t|ing)\b", r"\bverkregen\b",
    ],
    "retain": [
        _n("bewaar", "bewar"), r"\bopslaan\b", r"\bopgeslagen\b", r"\bopslag\b",
        _n("archiveer", "archiver"), _n("behoud"), _v("conserveer", "conserver"),
    ],
    "erase": [   # NEGATIVE of retain
        _n("wiss", "wis"), _n("verwijder"), _n("vernietig"),
        _v("schrapp", "schrap"), _v("anonimiseer", "anonimiser"),
    ],
    "use": [
        _v("gebruik"), _n("verwerk"), _v("benut"), _v("inzett", "inzet"), r"\bingezet\b",
        _v("exploiteer", "exploiter"),
    ],
    # --- permission lifecycle -----------------------------------------------------------
    "authorise": [
        _v("goedkeur"), _v("machtig"), _v("vergunn"), _v("autoriseer", "autoriser"),
        _v("accepteer", "accepter"),
        r"\btoe(?:staan|staat|gestaan)\b", r"\btoegelaten\b", r"\btoelaten\b", r"\btoelaat\b",
        r"\btoelating\b", r"\btoeken(?:nen|t|ning)\b", r"\btoegekend\b",
        r"\b(?:vergunning|toestemming|toegang|machtiging|goedkeuring|licentie)\s+(?:te\s+)?"
        r"(?:verlen(?:en|ing)|verleent|verleend)\b",
        r"\btoestemming\s+(?:te\s+)?(?:geven|geeft|gegeven)\b",
    ],
    "deny": [   # NEGATIVE of authorise
        _v("weiger"), r"\bweigering\b", _v("afwijz"), r"\bafgewezen\b", r"\bintrek(?:ken|t|king)\b",
        r"\bingetrokken\b", _v("herroep"), _v("annuleer", "annuler"), _v("ontneem", "ontnem"),
        r"\bontnomen\b", _v("ontzegg", "ontzeg"), _v("blokkeer", "blokker"), _v("verhinder"),
        _v("uitsluit"), r"\buitgesloten\b",
    ],
    "require": [
        r"\bvereis(?:en|t)\b", r"\bvoorschrijv(?:en|t)\b", r"\bvoorgeschreven\b",
        r"\bopleg(?:gen|t)\b", r"\bopgelegd\b", r"\bverplicht(?:en|t)\b",
    ],
    "exempt": [   # NEGATIVE of require
        r"\bvrijstel(?:len|t|ling)\b", r"\bvrijgesteld\b", r"\bontheff(?:en|t|ing)\b",
        r"\bontheven\b", r"\bontslag(?:en|t)\s+van\s+(?:de\s+)?(?:verplichting|plicht)\b",
    ],
    # --- operation and supply -----------------------------------------------------------
    "operate": [
        _v("voortzett", "voortzet"), r"\bvoortgezet\b", _v("continueer", "continuer"),
        _v("activeer", "activer"), _v("start"),
        r"\bin\s+werking\s+(?:te\s+)?(?:stel(?:len|t)|gesteld)\b",
        r"\bin\s+bedrijf\s+(?:te\s+)?(?:hou(?:den|dt)|gehouden|stel(?:len|t)|gesteld)\b",
    ],
    "halt": [   # NEGATIVE of operate
        _n("opschort"), r"\bopgeschort\b", r"\bstopzet(?:ten|t|ting)\b", r"\bstopgezet\b",
        r"\bbe[eë]indig(?:en|t|d|de|ing)?\b", r"\bstil(?:legg?en|legt|legging|gelegd)\b",
        _v("stopp", "stop"), _n("staak", "stak"), r"\bonderbrek(?:en|t|ing)\b", r"\bonderbroken\b",
        _v("deactiveer", "deactiver"), r"\buitschakel(?:en|t|ing)\b", r"\buitgeschakeld\b",
    ],
    "market": [
        r"\bin\s+de\s+handel\s+(?:te\s+)?(?:brengen|brengt|gebracht)\b",
        r"\bop\s+de\s+markt\s+(?:te\s+)?(?:brengen|brengt|gebracht|aanbieden|aanbiedt|aangeboden)\b",
        r"\bin\s+gebruik\s+(?:te\s+)?(?:stel(?:len|t)|gesteld|nemen|neemt|genomen)\b",
        _n("lever"), r"\baanbied(?:en|t)\b", r"\baangeboden\b",
    ],
    "withdraw": [   # NEGATIVE of market
        r"\buit\s+de\s+handel\s+(?:te\s+)?(?:nemen|neemt|genomen)\b",
        r"\bvan\s+de\s+markt\s+(?:te\s+)?(?:halen|haalt|gehaald|nemen|neemt|genomen)\b",
        r"\buit\s+(?:de\s+)?(?:gebruik|omloop)\s+(?:te\s+)?(?:nemen|neemt|genomen|halen|haalt|gehaald)\b",
        r"\bbuiten\s+gebruik\s+(?:te\s+)?(?:stel(?:len|t)|gesteld)\b",
        r"\bterugroep\w*", r"\bteruggeroepen\b", r"\bterugnem(?:en|t)\b", r"\bteruggenomen\b",
    ],
    # --- institutions -------------------------------------------------------------------
    "appoint": [
        _n("aanwijz", "aanwijs"), r"\baangewezen\b", _n("benoem"), _v("aanstel"), r"\baangesteld\b",
        r"\binstellen\b", r"\bingesteld\b", r"\boprichten\b", r"\bopgericht\b", r"\boprichting\b",
    ],
    "dismiss": [   # NEGATIVE of appoint
        r"\bontsla(?:an|at)\b", r"\bontslagen\b", r"\bafzet(?:ten|t|ting)\b", r"\bafgezet\b",
        r"\broyeer\w*", r"\broyer\w*", _n("opheff"), r"\bopgeheven\b", _v("ontbind"),
        r"\bontbonden\b", r"\bafschaff?(?:en|t|ing)\b", r"\bafgeschaft\b",
    ],
    # --- magnitude ----------------------------------------------------------------------
    "increase": [
        _n("verhoog", "verhog"), _n("vergroot", "vergrot"), r"\buitbreid(?:en|t|ing)\b",
        _v("versterk"), _v("intensiveer", "intensiver"),
    ],
    "decrease": [   # NEGATIVE of increase
        _n("verlaag", "verlag"), _n("verminder"), _v("verklein"), _n("beperk"), _v("inperk"),
        _v("reduceer", "reducer"), _v("verzwakk", "verzwak"),
    ],
    "extend": [_n("verleng")],
    "shorten": [_n("verkort", "bekort")],   # NEGATIVE of extend
    # --- weak / generic duty verbs (duplicate signal only; no antonym) -------------------
    "ensure": [
        _v("waarborg"), _v("garandeer", "garander"), r"\bzorg(?:en|t|dragen|draagt)\b",
        r"\bzorg\s+(?:te\s+)?dragen\b", r"\bzorgdrag\w*", r"\bziet\s+erop\s+toe\b", r"\btoezien\b",
    ],
    "measure": [
        r"\btreff(?:en|t)\b", r"\bgetroffen\b", _n("implementeer", "implementer"),
        r"\bimplementatie\b", r"\binvoeren\b", r"\bingevoerd\b", r"\btoepassen\b", r"\btoegepast\b",
    ],
    "assess": [
        _n("controleer", "controler"), _n("toets"), _n("beoordeel", "beoordel"),
        _n("evalueer", "evaluer"), _v("inspecteer", "inspecter"), _v("monitor"),
        r"\btoezicht\s+(?:te\s+)?(?:houden|houdt|gehouden)\b", r"\bverifi[eë]r(?:en|t)\b",
        r"\bgeverifieerd\b", _v("test"), r"\bonderzoek(?:en|t)\b", r"\bonderzocht\b",
    ],
    "record": [
        _v("documenteer", "documenter"), _n("registreer", "registrer"), r"\bregistratie\b",
        r"\bvastleg(?:gen|t|ging)\b", r"\bvastgelegd\b", r"\bbijhoud(?:en|t)\b", r"\bbijgehouden\b",
        _v("noteer", "noter"), r"\bdocumentatie\b",
    ],
}

# Every family is a literal on some axis. Positive families are (own name, +1); the rest are
# the NEGATIVE literal of one or more positive axes. One negative family may negate several
# axes (withholding information negates notifying, providing, publishing and transferring).
NEGATIVE_LITERALS: dict[str, list[tuple[str, int]]] = {
    "withhold": [("notify", -1), ("provide", -1), ("publish", -1), ("transfer", -1)],
    "erase": [("retain", -1)],
    "deny": [("authorise", -1)],
    "deny_access": [("access", -1)],
    "halt": [("operate", -1)],
    "withdraw": [("market", -1)],
    "exempt": [("require", -1)],
    "dismiss": [("appoint", -1)],
    "decrease": [("increase", -1)],
    "shorten": [("extend", -1)],
}
LITERALS: dict[str, list[tuple[str, int]]] = {
    fam: NEGATIVE_LITERALS.get(fam, [(fam, +1)]) for fam in FAMILIES
}

# Axes where a bare same-sign verb match proves very little ("ensure", "assess" appear in
# half of all duties). Duplicates on these need real object overlap; contradictions still need it too.
GENERIC_AXES = {"ensure", "measure", "assess", "record", "use"}

# Different axes that describe neighbouring acts. Same-sign hits across them are a WEAK
# duplicate signal ("related_verbs"); they never produce a contradiction.
RELATED_AXES = {
    frozenset(p) for p in [
        ("notify", "provide"), ("provide", "publish"), ("provide", "transfer"),
        ("transfer", "publish"), ("retain", "record"), ("collect", "record"),
        ("access", "assess"), ("authorise", "require"), ("operate", "market"),
    ]
}

# ---------------------------------------------------------------------------
# Text handling
# ---------------------------------------------------------------------------

_FAMILY_RE = {fam: re.compile("|".join(pats), re.I) for fam, pats in FAMILIES.items()}

# "niet later dan 72 uur", "niet langer dan nodig", "niet-openbare" are not negations of the verb.
_NEG_NEUTRAL_RE = re.compile(
    r"\bniet\s+(?:later|langer|meer|minder|zwaarder|alleen|uitsluitend)\b|\bniet-\w+", re.I)
_TEMPORAL_LIMIT_RE = re.compile(r"\bniet\s+(?:langer|later)\b", re.I)
_NEGATION_RE = re.compile(r"\b(?:niet|geen|nooit|nalaten|nalaat|afzien|afziet)\b", re.I)
_TRUNCATE_RE = re.compile(r"\btenzij\b", re.I)      # trailing exception: its verbs are not the rule's
_OVERRIDE_RE = re.compile(
    r"\b(?:onverminderd|behoudens|met\s+uitzondering\s+van|in\s+afwijking\s+van|ongeacht)\b", re.I)
_SEGMENT_RE = re.compile(r"[.;:]")

_STOPWORDS = {
    "de", "het", "een", "van", "voor", "aan", "bij", "op", "in", "te", "en", "of", "dat", "die",
    "deze", "dit", "worden", "wordt", "zijn", "hebben", "heeft", "hun", "haar", "zich", "zij",
    "hij", "met", "door", "om", "tot", "als", "naar", "over", "onder", "ten", "ter", "tegen",
    "indien", "alle", "ieder", "elke", "binnen", "onverwijld", "onnodige", "vertraging", "dan",
    "uur", "dagen", "bedoeld", "artikel", "lid", "onderdeel", "bevoegde", "betrokken", "niet",
    "geen", "kan", "kunnen", "moet", "moeten", "dient", "dienen", "ook", "wel", "meer", "zoals",
}
_WORD_RE = re.compile(r"[a-zà-ÿ]{4,}")

MIN_OVERLAP = 0.15            # below this (when both sides have an object) -> not comparable
GENERIC_MIN_OVERLAP = 0.30    # same-sign matches on GENERIC_AXES need more
NEUTRAL_OVERLAP = 0.30        # used when one side has no object words at all
MIN_SCORE = {"duplicate": 0.45, "contradiction": 0.40}



@dataclass
class VerbHit:
    family: str
    surface: str
    negated: bool


@dataclass
class VerbProfile:
    deontic: str
    hits: list = field(default_factory=list)
    # axis -> [(effective_sign, family, surface)]
    literals: dict = field(default_factory=dict)
    has_exception: bool = False
    has_override: bool = False
    temporal_limit: bool = False
    object_stems: frozenset = frozenset()
    source: str = "action"

    @property
    def axes(self) -> set:
        return set(self.literals)


def _prepare(text: str) -> tuple[str, dict]:
    t = unicodedata.normalize("NFC", (text or "").lower())
    flags = {
        "temporal_limit": bool(_TEMPORAL_LIMIT_RE.search(t)),
        "has_override": bool(_OVERRIDE_RE.search(t)),
        "has_exception": False,
    }
    t = _NEG_NEUTRAL_RE.sub(" ", t)
    m = _TRUNCATE_RE.search(t)
    if m:
        flags["has_exception"] = True
        t = t[:m.start()]
    return t, flags


def extract_verb_hits(text: str) -> tuple[list[VerbHit], list[tuple[int, int]], dict]:
    """All family hits in `text`, with sentence-level negation parity, plus the matched spans."""
    prepared, flags = _prepare(text)
    hits: list[VerbHit] = []
    spans: list[tuple[int, int]] = []
    offset = 0
    for segment in _SEGMENT_RE.split(prepared):
        negated = len(_NEGATION_RE.findall(segment)) % 2 == 1
        seen_here: set[str] = set()
        for fam, rx in _FAMILY_RE.items():
            for m in rx.finditer(segment):
                spans.append((offset + m.start(), offset + m.end()))
                if fam not in seen_here:
                    seen_here.add(fam)
                    hits.append(VerbHit(fam, m.group(0).strip(), negated))
        offset += len(segment) + 1
    return hits, spans, {**flags, "prepared": prepared}


def _object_stems(prepared: str, spans: list[tuple[int, int]]) -> frozenset:
    chars = list(prepared)
    for s, e in spans:
        for k in range(s, min(e, len(chars))):
            chars[k] = " "
    words = _WORD_RE.findall("".join(chars))
    return frozenset(w[:6] for w in words if w not in _STOPWORDS)


def build_profile(action: Optional[str], deontic: str,
                  fallback_text: Optional[str] = None) -> VerbProfile:
    """Verb profile of one norm. `fallback_text` (e.g. the paragraph text) is used only when
    the extracted action field yields no verb at all -- the same "summarized out of the
    extracted fields" problem your other signals hit."""
    hits, spans, info = extract_verb_hits(action or "")
    source = "action"
    if not hits and fallback_text:
        hits, spans, info = extract_verb_hits(fallback_text)
        source = "text"

    base = -1 if deontic == "PROHIBITION" else +1
    literals: dict[str, list] = defaultdict(list)
    for h in hits:
        flip = -1 if h.negated else +1
        for axis, lit_sign in LITERALS[h.family]:
            literals[axis].append((lit_sign * base * flip, h.family, h.surface))
    return VerbProfile(
        deontic=deontic, hits=hits, literals=dict(literals),
        has_exception=info["has_exception"], has_override=info["has_override"],
        temporal_limit=info["temporal_limit"],
        object_stems=_object_stems(info["prepared"], spans), source=source,
    )


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

ELIGIBLE_DEONTICS = {"OBLIGATION", "PROHIBITION", "COMPETENCE"}

# Every number that turns verb evidence into a score lives here, so the formula is auditable and
# its fingerprint (LEXICON_VERSION) changes whenever any weight, family or stopword changes.
SCORE_WEIGHTS = {
    "same_verb": 1.0, "equivalent_via_negation": 0.85, "related_verbs": 0.5,
    "same_verb_opposite_polarity": 1.0, "antonym_verbs": 0.9,
    "overlap_floor": 0.35, "overlap_weight": 0.65,
    "exception_contradiction": 0.8,
    "both_powers_contradiction": 0.6, "power_vs_duty_contradiction": 0.7,
    "power_vs_duty_duplicate": 0.6,
}
SCORE_FORMULA = ("score = base[kind] x factor x (overlap_floor + overlap_weight x overlap_effective), "
                 "rounded to 3 decimals; every term is stored in the row's evidence block")

LEXICON_VERSION = hashlib.sha256(json.dumps({
    "families": FAMILIES, "negative_literals": NEGATIVE_LITERALS,
    "generic_axes": sorted(GENERIC_AXES), "related_axes": sorted(sorted(p) for p in RELATED_AXES),
    "stopwords": sorted(_STOPWORDS), "weights": SCORE_WEIGHTS,
    "params": [MIN_OVERLAP, GENERIC_MIN_OVERLAP, NEUTRAL_OVERLAP, MIN_SCORE],
}, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


def object_overlap(a: VerbProfile, b: VerbProfile) -> Optional[float]:
    """Jaccard of the objects' content-word stems; None when either side has no object words."""
    if not a.object_stems or not b.object_stems:
        return None
    return len(a.object_stems & b.object_stems) / len(a.object_stems | b.object_stems)


def _modality_factor(a: VerbProfile, b: VerbProfile, relation: str) -> tuple[float, Optional[str]]:
    """A power (COMPETENCE) and a duty are different things: they are neither duplicates nor
    clean contradictions, so the score is discounted and the reviewer is told why."""
    w = SCORE_WEIGHTS
    ca, cb = a.deontic == "COMPETENCE", b.deontic == "COMPETENCE"
    if ca and cb:
        return (w["both_powers_contradiction"], "both_powers") if relation == "contradiction" else (1.0, None)
    if ca or cb:
        return ((w["power_vs_duty_contradiction"], "power_vs_duty") if relation == "contradiction"
                else (w["power_vs_duty_duplicate"], "power_vs_duty"))
    return 1.0, None


@dataclass
class PairScore:
    """Both scores for one pair. 0.0 means 'no evidence', not 'checked and fine'."""
    duplicate_score: float = 0.0
    contradiction_score: float = 0.0
    object_overlap: Optional[float] = None
    shared_axes: list = field(default_factory=list)
    duplicate_evidence: Optional[dict] = None
    contradiction_evidence: Optional[dict] = None
    notes: list = field(default_factory=list)

    @property
    def flagged(self) -> dict:
        return {"duplicate": self.duplicate_score >= MIN_SCORE["duplicate"],
                "contradiction": self.contradiction_score >= MIN_SCORE["contradiction"]}

    @property
    def relation(self) -> Optional[str]:
        fl = self.flagged
        cands = [(self.duplicate_score, "duplicate")] * fl["duplicate"] + \
                [(self.contradiction_score, "contradiction")] * fl["contradiction"]
        return max(cands)[1] if cands else None


@dataclass
class VerbRelation:
    """Best flagged relation of a pair (compact view used by compare_norms / the self-test)."""
    relation: str
    kind: str
    axis: str
    score: float
    verbs_a: str
    verbs_b: str
    object_overlap: float
    notes: list = field(default_factory=list)


def score_pair(a: VerbProfile, b: VerbProfile, min_overlap: float = MIN_OVERLAP) -> PairScore:
    w = SCORE_WEIGHTS
    ps = PairScore(shared_axes=sorted(a.axes & b.axes))
    ps.object_overlap = object_overlap(a, b)
    notes: set[str] = set()
    if ps.object_overlap is None:
        eff = NEUTRAL_OVERLAP
        notes.add("no_object_words_on_one_side")
    else:
        eff = ps.object_overlap

    def consider(relation: str, kind: str, axis: str, sa: int, sb: int, va: str, vb: str) -> None:
        if relation == "duplicate" and axis in GENERIC_AXES and eff < GENERIC_MIN_OVERLAP:
            notes.add(f"generic_axis_needs_more_overlap:{axis}")
            return
        base = w[kind]
        factor, modal = _modality_factor(a, b, relation)
        ev_notes = [modal] if modal else []
        if relation == "contradiction" and (a.has_exception or b.has_exception
                                            or a.has_override or b.has_override):
            factor *= w["exception_contradiction"]
            ev_notes.append("exception_or_override_clause")
        if a.temporal_limit != b.temporal_limit:
            ev_notes.append("only_one_side_has_time_limit")
        score = round(base * factor * (w["overlap_floor"] + w["overlap_weight"] * eff), 3)
        if score > getattr(ps, f"{relation}_score"):
            setattr(ps, f"{relation}_score", score)
            setattr(ps, f"{relation}_evidence", {
                "kind": kind, "axis": axis, "base": base, "factor": round(factor, 3),
                "overlap_effective": round(eff, 3), "score": score,
                "verbs_a": va, "verbs_b": vb, "sign_a": sa, "sign_b": sb, "notes": ev_notes})

    for axis in ps.shared_axes:
        for sa, fa, va in a.literals[axis]:
            for sb, fb, vb in b.literals[axis]:
                if sa != sb:
                    consider("contradiction",
                             "same_verb_opposite_polarity" if fa == fb else "antonym_verbs",
                             axis, sa, sb, va, vb)
                else:
                    consider("duplicate", "same_verb" if fa == fb else "equivalent_via_negation",
                             axis, sa, sb, va, vb)

    if ps.duplicate_score == 0.0 and ps.contradiction_score == 0.0:   # weak cross-axis signal
        for ax_a in a.axes:
            for ax_b in b.axes:
                if ax_a != ax_b and frozenset((ax_a, ax_b)) in RELATED_AXES:
                    sa, _, va = a.literals[ax_a][0]
                    sb, _, vb = b.literals[ax_b][0]
                    if sa == sb:
                        consider("duplicate", "related_verbs", f"{ax_a}~{ax_b}", sa, sb, va, vb)
    ps.notes = sorted(notes)
    return ps


def compare_profiles(a: VerbProfile, b: VerbProfile,
                     min_overlap: float = MIN_OVERLAP) -> Optional[VerbRelation]:
    ps = score_pair(a, b, min_overlap)
    rel = ps.relation
    if rel is None:
        return None
    ev = getattr(ps, f"{rel}_evidence")
    return VerbRelation(rel, ev["kind"], ev["axis"], ev["score"], ev["verbs_a"], ev["verbs_b"],
                        round(ps.object_overlap, 3) if ps.object_overlap is not None else -1.0,
                        sorted(set(ps.notes) | set(ev["notes"])))


def compare_norms(norm_a: dict, norm_b: dict,
                  text_a: Optional[str] = None, text_b: Optional[str] = None) -> Optional[VerbRelation]:
    """Convenience wrapper over two plain norm dicts (needs 'action' and 'deontic')."""
    return compare_profiles(
        build_profile(norm_a.get("action"), norm_a["deontic"], text_a),
        build_profile(norm_b.get("action"), norm_b["deontic"], text_b))


# ---------------------------------------------------------------------------
# Input: any JSON list of norm records, fields located by name (override with --field)
# ---------------------------------------------------------------------------

FIELD_CANDIDATES: dict[str, list[tuple]] = {
    "norm_id": [("norm_id",), ("id",)],
    "action": [("action",), ("norm", "action")],
    "deontic": [("deontic",), ("norm", "deontic")],
    "addressee": [("addressee",), ("norm", "addressee"), ("hierarchy", "addressee")],
    "instrument": [("hierarchy", "instrument_id"), ("instrument_id",)],
    "article": [("hierarchy", "article"), ("article",)],
    "text": [("text",), ("paragraph_text",), ("norm", "text"), ("source_text",)],
}


def _dig(rec, path: tuple):
    cur = rec
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def resolve_fields(records: list, overrides: dict) -> dict:
    """For each logical field, the candidate path that is populated in the most records."""
    chosen: dict[str, Optional[tuple]] = {}
    for name, cands in FIELD_CANDIDATES.items():
        if name in overrides:
            chosen[name] = overrides[name]
            continue
        best, best_n = None, 0
        for path in cands:
            n = sum(1 for r in records if _dig(r, path) not in (None, ""))
            if n > best_n:
                best, best_n = path, n
        chosen[name] = best
    return chosen


@dataclass
class NormView:
    idx: int
    norm_id: str
    action: Optional[str]
    deontic: Optional[str]
    addressee: Optional[str]
    instrument: Optional[str]
    article: Optional[str]
    text: Optional[str]
    profile: Optional[VerbProfile]
    status: str          # ok | no_verb | ineligible_deontic | missing_deontic


def build_views(records: list, fields: dict) -> tuple[list[NormView], int]:
    def get(rec, name):
        path = fields.get(name)
        val = _dig(rec, path) if path else None
        return None if val in (None, "") else val

    views, synthetic = [], 0
    for i, rec in enumerate(records):
        nid = get(rec, "norm_id")
        if nid is None:
            nid, synthetic = f"idx{i}", synthetic + 1
        deontic = get(rec, "deontic")
        deontic = str(deontic).upper() if deontic is not None else None
        action, text = get(rec, "action"), get(rec, "text")
        instr, art = get(rec, "instrument"), get(rec, "article")
        profile, status = None, "ok"
        if deontic is None:
            status = "missing_deontic"
        elif deontic not in ELIGIBLE_DEONTICS:
            status = "ineligible_deontic"
        else:
            profile = build_profile(action if isinstance(action, str) else None, deontic,
                                    text if isinstance(text, str) else None)
            if not profile.literals:
                status = "no_verb"
        views.append(NormView(
            i, str(nid), action if isinstance(action, str) else None, deontic,
            None if get(rec, "addressee") is None else str(get(rec, "addressee")),
            None if instr is None else str(instr), None if art is None else str(art),
            text if isinstance(text, str) else None, profile, status))
    return views, synthetic


def load_pairs(path: Path, views: list[NormView]) -> tuple[list[tuple[int, int]], dict]:
    """Pairs to score. Accepts stageA_pairs.json ({"norm_ids": [...], "pairs": [[i, j], ...]},
    indices into the input file's order), a plain [[i, j], ...] list, or [[id_a, id_b], ...]."""
    data = json.loads(path.read_text(encoding="utf-8"))
    ids = [v.norm_id for v in views]
    if isinstance(data, dict):
        header_ids = data.get("norm_ids")
        if header_ids is not None and list(header_ids) != ids:
            raise ValueError(f"{path}: its norm_ids do not match the --input file (different file or "
                             f"order); pair indices would point at the wrong norms.")
        raw = data.get("pairs")
        if raw is None:
            raise ValueError(f"{path}: dict without a 'pairs' list")
    else:
        raw = data
    by_id: dict[str, list[int]] = defaultdict(list)
    for v in views:
        by_id[v.norm_id].append(v.idx)

    def to_idx(x):
        if isinstance(x, int):
            return x
        hits = by_id.get(str(x), [])
        if len(hits) != 1:
            raise ValueError(f"{path}: norm id {x!r} matches {len(hits)} records in --input")
        return hits[0]

    out, seen, dupes, self_pairs = [], set(), 0, 0
    for p in raw:
        a, b = (p["a"], p["b"]) if isinstance(p, dict) else (p[0], p[1])
        i, j = to_idx(a), to_idx(b)
        if not (0 <= i < len(views) and 0 <= j < len(views)):
            raise ValueError(f"{path}: pair {p!r} is out of range for {len(views)} records")
        if i == j:
            self_pairs += 1
            continue
        key = (min(i, j), max(i, j))
        if key in seen:
            dupes += 1
            continue
        seen.add(key)
        out.append(key)
    return out, {"duplicate_pairs_dropped": dupes, "self_pairs_dropped": self_pairs}


def iter_shared_axis_pairs(views: list[NormView]):
    """Every pair of verb-bearing norms that share at least one axis, each exactly once (under
    the alphabetically first axis the two share)."""
    by_axis: dict[str, list[int]] = defaultdict(list)
    for v in views:
        if v.status == "ok":
            for axis in v.profile.axes:
                by_axis[axis].append(v.idx)
    for axis, idxs in sorted(by_axis.items()):
        for i, j in combinations(sorted(idxs), 2):
            if min(views[i].profile.axes & views[j].profile.axes) == axis:
                yield i, j


# ---------------------------------------------------------------------------
# Output rows: every score is keyed to both norm ids, and every norm id has a profile row
# ---------------------------------------------------------------------------

def make_pair_id(id_a: str, id_b: str) -> str:
    """Stable, order-independent identity of a norm pair (same pair -> same id on every run)."""
    return hashlib.sha256("::".join(sorted([id_a, id_b])).encode("utf-8")).hexdigest()[:12]


def profile_row(v: NormView) -> dict:
    p = v.profile
    row = {"idx": v.idx, "norm_id": v.norm_id, "status": v.status, "deontic": v.deontic,
           "action": v.action, "addressee": v.addressee, "instrument": v.instrument, "article": v.article}
    if p is not None:
        row.update({
            "profile_source": p.source,
            "verbs": [{"family": h.family, "surface": h.surface, "negated": h.negated} for h in p.hits],
            "literals": {ax: [{"sign": s, "family": f, "surface": sv} for s, f, sv in lits]
                         for ax, lits in p.literals.items()},
            "has_exception": p.has_exception, "has_override": p.has_override,
            "temporal_limit": p.temporal_limit, "object_stems": sorted(p.object_stems)})
    return row


def pair_row(a: NormView, b: NormView, gate_addressee: bool, min_overlap: float) -> dict:
    if a.idx > b.idx:
        a, b = b, a
    row = {"pair_id": make_pair_id(a.norm_id, b.norm_id), "idx_a": a.idx, "idx_b": b.idx,
           "norm_id_a": a.norm_id, "norm_id_b": b.norm_id,
           "status": None, "duplicate_score": 0.0, "contradiction_score": 0.0, "relation": None,
           "flagged": {"duplicate": False, "contradiction": False},
           "object_overlap": None, "shared_axes": [],
           "duplicate_evidence": None, "contradiction_evidence": None, "notes": []}
    if a.status != "ok":
        row["status"] = f"a_{a.status}"
    elif b.status != "ok":
        row["status"] = f"b_{b.status}"
    elif a.instrument is not None and a.instrument == b.instrument and a.article == b.article:
        row["status"] = "same_article"
    elif gate_addressee and a.addressee and b.addressee and a.addressee != b.addressee:
        row["status"] = "different_addressee"
    else:
        ps = score_pair(a.profile, b.profile, min_overlap)
        row.update({
            "status": "scored" if ps.shared_axes or ps.duplicate_score or ps.contradiction_score
                      else "no_shared_verb_axis",
            "duplicate_score": ps.duplicate_score, "contradiction_score": ps.contradiction_score,
            "relation": ps.relation, "flagged": ps.flagged,
            "object_overlap": None if ps.object_overlap is None else round(ps.object_overlap, 3),
            "shared_axes": ps.shared_axes, "duplicate_evidence": ps.duplicate_evidence,
            "contradiction_evidence": ps.contradiction_evidence, "notes": ps.notes})
    return row


CSV_COLUMNS = ["pair_id", "idx_a", "idx_b", "norm_id_a", "norm_id_b", "status", "duplicate_score",
               "contradiction_score", "relation", "flagged_duplicate", "flagged_contradiction",
               "object_overlap", "shared_axes", "notes"]


def _csv_row(r: dict) -> list:
    return [r["pair_id"], r["idx_a"], r["idx_b"], r["norm_id_a"], r["norm_id_b"], r["status"],
            r["duplicate_score"], r["contradiction_score"], r["relation"] or "",
            r["flagged"]["duplicate"], r["flagged"]["contradiction"],
            "" if r["object_overlap"] is None else r["object_overlap"],
            "|".join(r["shared_axes"]), "|".join(r["notes"])]


@contextmanager
def atomic_open(path: Path, newline: Optional[str] = None):
    """Write to a sibling temp file, fsync, then os.replace: a killed run never leaves a
    half-written or zeroed output file, only the old file or the complete new one."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline=newline) as fh:
            yield fh
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run(args) -> int:
    in_path: Path = args.input
    if not in_path.is_file():
        print(f"ERROR: input not found: {in_path}", file=sys.stderr)
        return 1
    raw = json.loads(in_path.read_text(encoding="utf-8"))
    records = raw if isinstance(raw, list) else next(
        (raw[k] for k in ("norms", "records") if isinstance(raw, dict) and isinstance(raw.get(k), list)), None)
    if records is None:
        print(f"ERROR: {in_path}: expected a JSON list of norm records (or a dict with 'norms'/'records')",
              file=sys.stderr)
        return 1

    overrides = {}
    for item in args.field:
        name, _, dotted = item.partition("=")
        if name not in FIELD_CANDIDATES or not dotted:
            print(f"ERROR: --field expects NAME=a.b.c with NAME in {sorted(FIELD_CANDIDATES)}; got {item!r}",
                  file=sys.stderr)
            return 2
        overrides[name] = tuple(dotted.split("."))
    fields = resolve_fields(records, overrides)
    missing = [n for n in ("deontic", "action") if fields[n] is None]
    if missing:
        sample = sorted(records[0].keys()) if records and isinstance(records[0], dict) else "n/a"
        print(f"ERROR: could not find field(s) {missing} in {in_path}. Top-level keys of record 0: "
              f"{sample}. Point to them with --field action=path.to.action --field deontic=path.to.deontic.",
              file=sys.stderr)
        return 1

    views, synthetic_ids = build_views(records, fields)
    status_counts = Counter(v.status for v in views)
    ids = [v.norm_id for v in views]
    dup_ids = len(ids) - len(set(ids))
    print(f"{len(views)} norms from {in_path}")
    print("  fields: " + ", ".join(f"{k}={'.'.join(p) if p else '-'}" for k, p in fields.items()))
    print("  norm status: " + ", ".join(f"{k}={n}" for k, n in sorted(status_counts.items())))
    if synthetic_ids:
        print(f"  WARNING: {synthetic_ids} record(s) have no norm_id; synthetic 'idx<N>' ids are only "
              f"stable for this input order.")
    if dup_ids:
        print(f"  WARNING: {dup_ids} duplicate norm_id value(s); pair_id will collide for those. "
              f"idx_a/idx_b in each row still identify the exact records.")

    gate = (not args.no_addressee_gate) and fields["addressee"] is not None
    pairs_meta: dict = {}
    if args.pairs:
        try:
            pair_iter, pairs_meta = load_pairs(args.pairs, views)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        mode = "pairs_file"
    else:
        pair_iter, mode = iter_shared_axis_pairs(views), "all_shared_axis_pairs"

    out_dir = args.out_dir or in_path.with_name(in_path.stem + "_verb_scores")
    out_dir.mkdir(parents=True, exist_ok=True)
    p_pairs, p_csv = out_dir / "pair_scores.jsonl", out_dir / "pair_scores.csv"
    p_prof, p_manifest = out_dir / "norm_verb_profiles.jsonl", out_dir / "manifest.json"

    emitted, skipped = Counter(), Counter()
    flagged = Counter()
    n_rows = 0
    with ExitStack() as stack:
        f_pairs = stack.enter_context(atomic_open(p_pairs))
        f_csv = stack.enter_context(atomic_open(p_csv, newline=""))
        f_prof = stack.enter_context(atomic_open(p_prof))
        writer = csv.writer(f_csv)
        writer.writerow(CSV_COLUMNS)
        for v in views:
            f_prof.write(json.dumps(profile_row(v), ensure_ascii=False) + "\n")
        for i, j in pair_iter:
            row = pair_row(views[i], views[j], gate, args.min_overlap)
            if mode == "all_shared_axis_pairs" and not (
                    row["status"] == "scored" and (args.emit_zero
                                                    or row["duplicate_score"] or row["contradiction_score"])):
                skipped[row["status"] if row["status"] != "scored" else "scored_zero"] += 1
                continue
            f_pairs.write(json.dumps(row, ensure_ascii=False) + "\n")
            writer.writerow(_csv_row(row))
            n_rows += 1
            emitted[row["status"]] += 1
            for kind, hit in row["flagged"].items():
                flagged[kind] += hit

    manifest = {
        "tool": "action_verb_relations.py", "lexicon_version": LEXICON_VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": mode,
        "input": {"path": str(in_path), "sha256": sha256_file(in_path), "n_records": len(views),
                  "fields_used": {k: ".".join(p) if p else None for k, p in fields.items()},
                  "norm_status": dict(status_counts), "duplicate_norm_ids": dup_ids,
                  "synthetic_norm_ids": synthetic_ids},
        "pairs_input": ({"path": str(args.pairs), "sha256": sha256_file(args.pairs), **pairs_meta}
                        if args.pairs else None),
        "parameters": {
            "min_overlap": args.min_overlap, "generic_min_overlap": GENERIC_MIN_OVERLAP,
            "neutral_overlap": NEUTRAL_OVERLAP, "min_score_to_flag": MIN_SCORE,
            "addressee_gate": gate, "emit_zero": args.emit_zero, "weights": SCORE_WEIGHTS},
        "score_formula": SCORE_FORMULA,
        "limits": ["addressee gate compares extracted addressee_type literally: directive phrasing "
                   "('Member States shall ensure that entities ...') is NOT resolved here; pass "
                   "--no-addressee-gate to compare across addressee types",
                   "negation scope is the whole sentence segment",
                   "object overlap is a 6-character-prefix Jaccard"],
        "counts": {"rows_written": n_rows, "by_status": dict(emitted),
                   "flagged": dict(flagged), "not_emitted_by_reason": dict(skipped)},
        "files": {p.name: {"sha256": sha256_file(p),
                           "rows": (sum(1 for _ in open(p, "rb")) - (1 if p.suffix == ".csv" else 0))}
                  for p in (p_pairs, p_csv, p_prof)},
        "row_schema": {
            "pair_scores.jsonl": ["pair_id", "idx_a", "idx_b", "norm_id_a", "norm_id_b", "status",
                                  "duplicate_score", "contradiction_score", "relation", "flagged",
                                  "object_overlap", "shared_axes", "duplicate_evidence",
                                  "contradiction_evidence", "notes"],
            "norm_verb_profiles.jsonl": "one row per input record, joined to pair rows on norm_id / idx"},
    }
    with atomic_open(p_manifest) as fh:    # written last: its presence means the run completed
        json.dump(manifest, fh, ensure_ascii=False, indent=1)

    print(f"\nWrote {n_rows:,} scored pair(s) to {out_dir}")
    print(f"  flagged: duplicate={flagged['duplicate']:,}  contradiction={flagged['contradiction']:,}")
    if skipped:
        print("  not emitted: " + ", ".join(f"{k}={n:,}" for k, n in sorted(skipped.items())))
    for p in (p_pairs, p_csv, p_prof, p_manifest):
        print(f"  {p.name}")
    return 0


# ---------------------------------------------------------------------------
# Self-test and explain
# ---------------------------------------------------------------------------

def _selftest() -> int:
    OBL, PRO = "OBLIGATION", "PROHIBITION"
    cases = [
        ("same verb, opposite deontic",
         (OBL, "de inbreuk melden aan de toezichthouder"),
         (PRO, "de inbreuk melden aan de toezichthouder"),
         ("contradiction", "same_verb_opposite_polarity")),
        ("antonym verbs, both obligations",
         (OBL, "persoonsgegevens bewaren"), (OBL, "persoonsgegevens wissen"),
         ("contradiction", "antonym_verbs")),
        ("synonyms in one family",
         (OBL, "persoonsgegevens wissen"), (OBL, "persoonsgegevens vernietigen"),
         ("duplicate", "same_verb")),
        ("prohibition of the antonym = same thing",
         (OBL, "persoonsgegevens bewaren"), (PRO, "persoonsgegevens wissen"),
         ("duplicate", "equivalent_via_negation")),
        ("publish vs keep secret",
         (OBL, "gegevens openbaar maken"), (OBL, "gegevens geheimhouden"),
         ("contradiction", "antonym_verbs")),
        ("explicit negation flips sign",
         (OBL, "gegevens doorgeven"), (OBL, "gegevens niet doorgeven"),
         ("contradiction", "same_verb_opposite_polarity")),
        ("'niet later dan' is a deadline, not a negation",
         (OBL, "binnen 24 uur melden aan het csirt"),
         (OBL, "niet later dan 72 uur melden aan het csirt"),
         ("duplicate", "same_verb")),
        ("require vs exempt",
         (OBL, "entiteiten verplichten tot incidentrapportage"),
         (OBL, "entiteiten vrijstellen van incidentrapportage"),
         ("contradiction", "antonym_verbs")),
        ("different objects -> no match",
         (OBL, "de toezichthouder melden"), (PRO, "de betrokkene melden"), None),
        ("unrelated verbs",
         (OBL, "incidenten melden"), (OBL, "risico's beoordelen"), None),
        ("generic verb with real object overlap",
         (OBL, "passende beveiligingsmaatregelen treffen"),
         (OBL, "beveiligingsmaatregelen treffen"),
         ("duplicate", "same_verb")),
        ("generic verb, unrelated object",
         (OBL, "passende beveiligingsmaatregelen treffen"),
         (OBL, "documentatie over trainingsdata treffen"), None),
    ]
    failed = 0
    for name, (da, aa), (db, ab), expected in cases:
        rel = compare_norms({"action": aa, "deontic": da}, {"action": ab, "deontic": db})
        got = (rel.relation, rel.kind) if rel else None
        ok = got == expected
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name}: expected {expected}, got {got}"
              + (f" score={rel.score}" if rel else ""))

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failed
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name}{(': ' + detail) if detail else ''}")

    p = build_profile("de vergunning intrekken", OBL)
    fams = {h.family for h in p.hits}
    check("object noun 'vergunning' is not an authorise verb", "deny" in fams and "authorise" not in fams)
    p = build_profile("persoonsgegevens niet langer bewaren dan noodzakelijk", OBL)
    check("'niet langer ... dan' = time limit, retain stays positive",
          p.temporal_limit and p.literals.get("retain", [(0,)])[0][0] == +1)
    p = build_profile("persoonsgegevens niet verzenden, doorgeven of raadplegen", PRO)
    signs = {ax: s for ax, lits in p.literals.items() for s, _, _ in lits}
    check("prohibition + explicit 'niet' cancel (parity)", signs.get("transfer") == +1
          and signs.get("access") == +1, str(signs))
    p = build_profile("melden aan de toezichthouder tenzij de inbreuk geen risico inhoudt", OBL)
    check("'tenzij' clause is cut before negation scan", p.has_exception and not any(h.negated for h in p.hits))
    p = build_profile(None, OBL, fallback_text="De verwerkingsverantwoordelijke moet de inbreuk melden.")
    check("falls back to paragraph text when action has no verb", p.source == "text" and "notify" in p.axes)

    # Score arithmetic is reproducible from the stored evidence block.
    ps = score_pair(build_profile("persoonsgegevens bewaren", OBL), build_profile("persoonsgegevens wissen", OBL))
    ev = ps.contradiction_evidence
    recomputed = round(ev["base"] * ev["factor"] * (SCORE_WEIGHTS["overlap_floor"]
                       + SCORE_WEIGHTS["overlap_weight"] * ev["overlap_effective"]), 3)
    check("evidence block reproduces the stored score", recomputed == ps.contradiction_score == ev["score"])

    # End-to-end: files, traceability, integrity.
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        recs = [
            {"norm_id": "N0", "hierarchy": {"instrument_id": "X", "article": "1"}, "deontic": OBL,
             "action": "persoonsgegevens bewaren", "addressee_type": "REGULATED_ENTITY"},
            {"norm_id": "N1", "hierarchy": {"instrument_id": "Y", "article": "2"}, "deontic": OBL,
             "action": "persoonsgegevens wissen", "addressee_type": "REGULATED_ENTITY"},
            {"norm_id": "N2", "hierarchy": {"instrument_id": "Z", "article": "3"}, "deontic": OBL,
             "action": "persoonsgegevens vernietigen", "addressee_type": "REGULATED_ENTITY"},
            {"norm_id": "N3", "hierarchy": {"instrument_id": "Z", "article": "4"}, "deontic": OBL,
             "action": "incidenten melden", "addressee_type": "REGULATED_ENTITY"},
            {"norm_id": "N4", "hierarchy": {"instrument_id": "Z", "article": "5"}, "deontic": "PERMISSION",
             "action": "iets doen", "addressee_type": "REGULATED_ENTITY"},
        ]
        (tmp / "in.json").write_text(json.dumps(recs), encoding="utf-8")
        (tmp / "pairs.json").write_text(json.dumps(
            {"norm_ids": ["N0", "N1", "N2", "N3", "N4"], "pairs": [[0, 1], [1, 2], [0, 3], [0, 4]]}),
            encoding="utf-8")
        rc = main(["--input", str(tmp / "in.json"), "--pairs", str(tmp / "pairs.json"),
                   "--out-dir", str(tmp / "out_pairs")])
        out = tmp / "out_pairs"
        rows = [json.loads(line) for line in (out / "pair_scores.jsonl").read_text(encoding="utf-8").splitlines()]
        by_ids = {(r["norm_id_a"], r["norm_id_b"]): r for r in rows}
        profs = {json.loads(line)["norm_id"] for line in (out / "norm_verb_profiles.jsonl").read_text(
            encoding="utf-8").splitlines()}
        man = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        check("pairs file: one row per input pair (4)", rc == 0 and len(rows) == 4, str(len(rows)))
        check("N0 x N1 -> contradiction flagged", by_ids[("N0", "N1")]["relation"] == "contradiction")
        check("N1 x N2 -> duplicate flagged", by_ids[("N1", "N2")]["relation"] == "duplicate")
        check("N0 x N3 -> no_shared_verb_axis, scores 0",
              by_ids[("N0", "N3")]["status"] == "no_shared_verb_axis"
              and by_ids[("N0", "N3")]["duplicate_score"] == 0.0)
        check("N0 x N4 -> b_ineligible_deontic, row still present",
              by_ids[("N0", "N4")]["status"] == "b_ineligible_deontic")
        check("every pair row joins to two profile rows",
              all(r["norm_id_a"] in profs and r["norm_id_b"] in profs for r in rows))
        check("manifest hashes match files",
              all(sha256_file(out / n) == meta["sha256"] for n, meta in man["files"].items()))
        check("manifest records input hash and lexicon version",
              man["input"]["sha256"] == sha256_file(tmp / "in.json") and man["lexicon_version"] == LEXICON_VERSION)
        check("pair_id is stable and order independent",
              make_pair_id("N0", "N1") == make_pair_id("N1", "N0") == by_ids[("N0", "N1")]["pair_id"])

        rc = main(["--input", str(tmp / "in.json"), "--out-dir", str(tmp / "out_all")])
        rows = [json.loads(line) for line in (tmp / "out_all" / "pair_scores.jsonl").read_text(
            encoding="utf-8").splitlines()]
        got = {(r["norm_id_a"], r["norm_id_b"]) for r in rows}
        check("all-pairs mode emits exactly the three retain/erase pairs",
              rc == 0 and got == {("N0", "N1"), ("N0", "N2"), ("N1", "N2")}, str(sorted(got)))

        (tmp / "bad_pairs.json").write_text(json.dumps({"norm_ids": ["other"], "pairs": [[0, 1]]}),
                                            encoding="utf-8")
        rc = main(["--input", str(tmp / "in.json"), "--pairs", str(tmp / "bad_pairs.json"),
                   "--out-dir", str(tmp / "out_bad")])
        check("pairs file from a different input is rejected", rc == 1)

    print(f"\n{'ALL PASSED' if not failed else f'{failed} FAILED'}")
    return 1 if failed else 0


def _explain(text: str, deontic: str) -> int:
    p = build_profile(text, deontic)
    print(f"deontic={deontic} source={p.source} exception={p.has_exception} "
          f"override={p.has_override} time_limit={p.temporal_limit}  lexicon={LEXICON_VERSION}")
    for h in p.hits:
        print(f"  verb {h.surface!r:<28} family={h.family:<12} negated={h.negated}")
    for axis, lits in p.literals.items():
        print(f"  axis {axis:<10} " + ", ".join(f"{'+' if s > 0 else '-'}({f})" for s, f, _ in lits))
    print(f"  object stems: {sorted(p.object_stems)}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--input", type=Path, help="JSON list of norm records (e.g. merged_norms.json)")
    ap.add_argument("--pairs", type=Path, default=None,
                    help="score exactly these pairs (e.g. stageA_pairs.json); default: every pair "
                         "sharing a verb axis")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="output directory (default: <input stem>_verb_scores next to the input)")
    ap.add_argument("--field", action="append", default=[], metavar="NAME=a.b.c",
                    help=f"where a field lives in each record; NAME in {sorted(FIELD_CANDIDATES)}")
    ap.add_argument("--no-addressee-gate", action="store_true",
                    help="do not require equal addressee_type")
    ap.add_argument("--emit-zero", action="store_true",
                    help="all-pairs mode: also write shared-axis pairs whose scores are 0")
    ap.add_argument("--min-overlap", type=float, default=MIN_OVERLAP,
                    help="object overlap below this -> scores stay 0 (default: %(default)s)")
    ap.add_argument("--selftest", action="store_true", help="run the built-in checks")
    ap.add_argument("--explain", metavar="TEXT", help="show how one action text is parsed")
    ap.add_argument("--deontic", default="OBLIGATION", choices=sorted(ELIGIBLE_DEONTICS))
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()
    if args.explain:
        return _explain(args.explain, args.deontic)
    if args.input:
        return run(args)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())