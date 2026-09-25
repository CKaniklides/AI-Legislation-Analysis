# -*- coding: utf-8 -*-
"""
Part 5, C1 — Contradiction detection (architecture doc Part 5).

The common shape: cheap deterministic candidate generation -> deterministic pre-filters
-> deterministic sub-type routing (arithmetic or a graph lookup, wherever a fact is
computable) -> AI adjudication ONLY for the one sub-type that's a genuine judgement call
(duty conflict) -> a deterministic resolution filter that can suppress a finding entirely
-> a structured Finding record. Nothing here is a flag until a human confirms it
(`report_eligible` stays false on every record this script produces).

Two real data-consistency issues were found while building this, both handled here
rather than papered over:

1. Graph adjacency alone would MISS the project's own flagship example. Checked
   directly: Cbw art. 26 and GDPR art. 33 (24h vs. 72h incident-reporting deadlines)
   have NO edge between them in data/graph.gexf, in either direction -- they simply
   don't cite each other. A design that only looked for graph-connected pairs would
   silently never generate this candidate at all. Fixed by adding a second, independent
   discovery signal: a small documented keyword family for "this norm is triggered by a
   security/data incident requiring notification" (NOTIFICATION_TRIGGER_KEYWORDS below),
   checked against each norm's own trigger_event/action text. Verified against real
   trigger_event text from all four anchor instruments before relying on it -- see the
   keyword list's own comment.

2. The Uitvoeringswet dataverordening's extracted norms live in a DIFFERENT file
   (Datasets/Dutch Laws/uitvoeringswet_dataverordening_dataset.json) than the one the
   citation graph actually indexes for that instrument (data/BWBR0051796_2025-11-21_
   provisions.json, which still has norms=[] since Stage 6 was pointed at the other
   file). The two files describe the same articles but use different identifier
   schemes. `_graph_uid()` below reconstructs the graph's own uid convention for this
   instrument (and for Bijlage 35, which has the same split) so graph lookups still
   work despite the split -- but the split itself is a real inconsistency worth fixing
   properly later (merging Stage 6's output back into the pipeline-standard file),
   noted here rather than silently worked around forever.

Usage:
    python detect_c1_contradiction.py             # full run
    python detect_c1_contradiction.py --dry-run   # candidate generation only, no LLM calls
"""
import argparse
import hashlib
import json
import re
import sys
import threading
from dataclasses import dataclass
from datetime import date
from itertools import combinations
from pathlib import Path
from typing import Literal, Optional

import networkx as nx
from dotenv import load_dotenv
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
load_dotenv(ROOT / ".env")

DEFAULT_MODEL = "gpt-5.6-luna"  # single config value, same start-cheap-escalate-on-signal
                                 # decision as extract_norms.py (2026-09-20)

ELIGIBLE_DEONTICS = {"OBLIGATION", "PROHIBITION", "COMPETENCE"}

# Verified against real trigger_event text pulled from Cbw, GDPR, NIS2 and DORA's own
# extracted norms before relying on it -- plain keyword overlap on the raw phrases alone
# would NOT catch "significante incident" (Cbw/NIS2) against "inbreuk in verband met
# persoonsgegevens" (GDPR), since they share no distinctive words. This is a deliberately
# small, documented domain lexicon (same pattern as parse.py's CONDITIONAL_OPERATORS),
# not a general similarity measure -- it only knows this one anchor set's topic.
NOTIFICATION_TRIGGER_KEYWORDS = {
    "incident", "inbreuk", "dreiging", "kwetsbaarheid",
    "waarschuwing", "melding", "significant", "kennisgeving",
}

# Concept-pair families (2026-09-24, item 9 extension) -- modeled on a suggestion from
# an external review (the "opposing action pairs" idea: disclose/withhold, retain/erase,
# operate/suspend). Built to close a real, checked gap, not a hypothetical one: GDPR
# art. 17's erasure duty and AI Act art. 10's training-dataset governance duty score a
# genuinely high 0.65 cosine similarity, but only rank ~90th-135th out of 1474 norms for
# each other -- real signal, just below any affordable semantic top-k cutoff (reaching
# it would need k~150, multiplying candidate volume corpus-wide for a gain specific to
# this one pairing). A small, curated family-PAIR lexicon catches this kind of
# cross-domain "a duty on one side interacts with a duty on the other side of the same
# lifecycle" case cheaply, without touching the global semantic budget. Each tuple is
# (family_A, family_B) -- a hit requires ONE norm in family_A and the OTHER in family_B
# (either direction), not both norms using the same word.
CONCEPT_PAIR_FAMILIES = [
    # erase/destroy  <->  retain/train-on/govern (verified against GDPR art. 17 vs.
    # AI Act art. 10's real extracted action text before relying on this -- broadened
    # to standalone "training"/"trainen" after the first version missed real text that
    # says "datasets voor training" rather than the compound "trainingsgegevens")
    ({"wissen", "verwijderen", "vernietigen", "gewist", "verwijderd"},
     {"bewaren", "bewaring", "behoud", "opslaan", "retentie", "trainingsgegevens",
      "training", "trainen", "databeheer", "bewaartermijn", "archiveren"}),
    # demand access/audit/disclose  <->  restrict/keep confidential. Broadened to bare
    # verbs (2026-09-24 fix): real prohibition text reads "worden niet verzonden,
    # doorgegeven of anderszins geraadpleegd" -- "niet" negates all three verbs via the
    # sentence structure, not by sitting directly next to each one, so a fixed multi-
    # word phrase like "niet worden doorgegeven" never matches real variable Dutch
    # phrasing. Candidate generation only needs to be a plausible signal (AI
    # adjudication plus the independent challenge pass are what actually decide), so
    # the extra recall is worth the reduced precision here.
    ({"audit", "inspectie", "toegang", "verstrekken", "openbaar maken", "bekendmaken"},
     {"vertrouwelijk", "geheimhoud", "overgedragen", "doorgegeven", "verzonden",
      "geraadpleegd", "bekendgemaakt", "toegankelijk"}),
    # suspend/halt  <->  continuity/availability
    ({"opschorten", "stopzetten", "beëindigen", "stilleggen", "intrekken"},
     {"continuïteit", "in bedrijf houden", "ononderbroken", "beschikbaarheid"}),
]

UNIT_TO_HOURS = {"immediate": 0, "hour": 1, "day": 24, "week": 168, "month": 720}


# ---------------------------------------------------------------------------
# Loading every eligible norm, from the same six anchor-set sources extract_norms.py
# used, each carrying enough provenance to look it up in the citation graph and to
# quote its real source text as evidence.
# ---------------------------------------------------------------------------

# Full corpus (2026-09-23), matching extract_norms.py's SOURCES exactly -- including
# the same Telecommunicatiewet exclusion (Hoofdstuk 11 only was what got extracted;
# the rest of that file has no norms[] to find here regardless) and the same switch to
# the standard pipeline file for Uitvoeringswet dataverordening (see extract_norms.py's
# module docstring for why -- fixes the citation-graph uid mismatch, doesn't work
# around it).
SOURCES = [
    ("data/BWBR0052872_2026-08-15_provisions.json", lambda r: r),
    ("data/BWBR0040940_2026-09-01_provisions.json", lambda r: r),
    ("data/BWBR0048156_2025-11-11_provisions.json", lambda r: r),
    ("data/BWBR0009950_2026-08-15_provisions.json", lambda r: r),
    ("data/BWBR0051796_2025-11-21_provisions.json", lambda r: r),
    ("data/32016R0679_original_provisions.json", lambda r: r),
    ("data/32022L2555_original_provisions.json", lambda r: r),
    ("data/32022R2554_original_provisions.json", lambda r: r),
    ("data/32024R1689_original_provisions.json", lambda r: r),
    ("Datasets/Dutch Laws/bijlage35_dataset.json",
     lambda r: r["bijlage_35"]["substantive_text"]["provisions"]),
]


@dataclass
class NormRecord:
    norm: dict
    provision: dict
    instrument_id: str
    article: str
    heading: str
    text: str            # the paragraph's own text (or whole-provision text), for evidence
    graph_uid: Optional[str]


def _norm_id(r: "NormRecord") -> str:
    """Real, confirmed bug fixed here (2026-09-24, surfaced while building C2): this
    identity omitted norm_index -- the field added earlier this session specifically to
    distinguish multiple norms Stage 6 extracts from the SAME paragraph. Checked
    directly against the live corpus: 76 (instrument, article, paragraph_index, number)
    groups hold 2+ genuinely different norms (different deontic and/or action text), all
    sharing one identity string under the old formula. Worse, checked against the
    actual candidate set this pipeline generates: 847 groups of DIFFERENT candidate
    pairs collapsed to the same _pair_id, 124 of which already have a live adjudication
    cache entry -- meaning any sibling pair sharing that id would silently be treated as
    "already cached" and receive another pair's verdict without ever actually being
    checked. norm_index is None for the ~95% of norms that are the only one in their
    paragraph, so this is a strict refinement, not a behavior change for those."""
    return f"{r.instrument_id}:{r.article}:{r.norm.get('paragraph_index')}:{r.norm.get('number')}:{r.norm.get('norm_index')}"


def _pair_id(a: "NormRecord", b: "NormRecord") -> str:
    """Stable identity for a norm pair, independent of run order (2026-09-24 fix, item
    12) -- used both for the adjudication cache (a re-run never re-pays for a pair
    already checked) and for finding_id (IDs no longer shuffle every run just because
    ThreadPoolExecutor's as_completed() order is nondeterministic)."""
    ids = sorted([_norm_id(a), _norm_id(b)])
    return hashlib.sha256("::".join(ids).encode("utf-8")).hexdigest()[:12]


def _graph_uid(p: dict, instrument_id: str) -> Optional[str]:
    """See module docstring, issue 2. Provisions from the standard pipeline output
    already carry their own graph uid; the two hand-built Datasets/ files don't, so
    their graph node id is reconstructed here to match graph.py's own convention."""
    if "uid" in p:
        return p["uid"]
    if instrument_id == "BWBR0051796":
        return f"nl:BWBR0051796:art{p['number']}@2025-11-21"
    if instrument_id == "BWBR0049497":
        return f"nl:BWBR0049497:bijlage35-p{p['number']}@2026-08-15"
    return None


def load_all_norm_records() -> list[NormRecord]:
    records = []
    for path, get_provisions in SOURCES:
        root = json.loads((ROOT / path).read_text(encoding="utf-8"))
        for p in get_provisions(root):
            if "instrument_id" in p:
                instrument_id = p["instrument_id"]
            elif "uitvoeringswet" in path.lower():
                instrument_id = "BWBR0051796"
            elif "bijlage35" in path.lower():
                instrument_id = "BWBR0049497"

            graph_uid = _graph_uid(p, instrument_id)
            article = str(p.get("article") or p.get("number"))
            heading = p.get("heading") or f"Article {article}"
            para_list = p.get("paragraphs") or []
            # Keyed by list POSITION, not the displayed "number" (2026-09-24 fix): the
            # displayed number is not always unique within an article -- confirmed
            # directly (AI Act art. 73 has two lid-divs both displaying "11"; the
            # migrate_paragraph_index.py backfill resolved which norm belongs to which
            # by verbatim text match). A {number: text} dict here would let the second
            # paragraph silently overwrite the first, attaching the wrong paragraph's
            # text as a norm's adjudication evidence -- exactly what was happening
            # before this fix. Falls back to number-keyed lookup only for norms
            # extracted before the migration ran (paragraph_index absent).
            by_number = {str(para.get("number")): para["text"] for para in para_list}

            for norm in p.get("norms", []):
                if norm["deontic"] not in ELIGIBLE_DEONTICS:
                    continue
                if norm.get("addressee_type") is None:
                    continue
                pidx = norm.get("paragraph_index")
                if pidx is not None and 0 <= pidx < len(para_list):
                    text = para_list[pidx]["text"]
                else:
                    text = by_number.get(str(norm.get("number")), p.get("text", ""))
                records.append(NormRecord(norm, p, instrument_id, article, heading, text, graph_uid))
    return records


# ---------------------------------------------------------------------------
# Candidate generation: same addressee_type is a hard requirement; graph adjacency OR
# shared trigger-keyword family is the discovery signal (union of the two, per Part 5).
# ---------------------------------------------------------------------------

def _graph_connected(g: nx.MultiDiGraph, uid_a: Optional[str], uid_b: Optional[str]) -> bool:
    if uid_a is None or uid_b is None or not g.has_node(uid_a) or not g.has_node(uid_b):
        return False
    if g.has_edge(uid_a, uid_b) or g.has_edge(uid_b, uid_a):
        return True
    return bool(set(g.successors(uid_a)) & set(g.successors(uid_b)))  # cite a common third


def _trigger_keyword_hit(a: NormRecord, b: NormRecord) -> bool:
    """Checked against the project's own flagship pair before trusting it, and an
    earlier version of this function failed it: Cbw's trigger text uses "incident"
    vocabulary, GDPR's uses "inbreuk" (breach) vocabulary, and the two literally never
    share a word, so requiring the SAME keyword in both (a set intersection) returned
    empty for exactly the pair this signal exists to catch. The correct check is
    whether each norm INDEPENDENTLY belongs to the notification-keyword family --
    i.e. both are "this kind of thing", not "both use this exact word"."""
    def in_family(rec: NormRecord) -> bool:
        blob = f"{rec.norm.get('trigger_event') or ''} {rec.norm.get('action') or ''}".lower()
        return any(kw in blob for kw in NOTIFICATION_TRIGGER_KEYWORDS)
    return in_family(a) and in_family(b)


def _concept_pair_hit(a: NormRecord, b: NormRecord) -> bool:
    """One norm's family_A, the other's family_B, from the SAME pair -- either
    direction. Same "independently belongs to the family" logic as
    _trigger_keyword_hit, for the same reason: the two sides of a real cross-domain
    tension routinely share no literal vocabulary at all."""
    def blob(rec: NormRecord) -> str:
        return f"{rec.norm.get('trigger_event') or ''} {rec.norm.get('action') or ''}".lower()
    ba, bb = blob(a), blob(b)
    for left, right in CONCEPT_PAIR_FAMILIES:
        a_left, a_right = any(w in ba for w in left), any(w in ba for w in right)
        b_left, b_right = any(w in bb for w in left), any(w in bb for w in right)
        if (a_left and b_right) or (a_right and b_left):
            return True
    return False


def generate_candidates(records: list[NormRecord], g: nx.MultiDiGraph,
                         semantic_pairs: Optional[set] = None) -> list[dict]:
    """Checked against real output before trusting it: applying the trigger-keyword
    signal everywhere (not just across instruments) massively over-triggered here --
    nearly every paragraph in Cbw ch. 8 / NIS2 art. 23 / DORA 17-23 mentions "incident"
    somewhere, so it was pairing up unrelated articles within the SAME law just because
    they share that one common word. Within a single instrument, related articles
    normally already cite each other explicitly (e.g. "bedoeld in artikel 27"), so the
    citation graph alone is the right signal there. The keyword signal is only needed
    to bridge the gap BETWEEN instruments that don't cite each other at all.

    `semantic_pairs` (2026-09-23) is the general-purpose replacement for what the
    keyword signal alone couldn't do: {(i, j), ...} index pairs from
    similarity.semantic_candidate_pairs(), covering topic-similar provisions across
    ANY subject, not just incident/breach reporting. Kept as a third, independent
    signal alongside the other two (union, not replacement) -- a real citation link or
    a shared notification-keyword is still valid evidence on its own even where
    embedding similarity happens to be weak.

    semantic_hit is NOT restricted to cross-instrument (2026-09-24 fix, item 9), unlike
    trigger_hit: the trigger-keyword restriction above is about a specific, narrow
    lexicon over-triggering on one common word; embedding similarity is a fundamentally
    different, less brittle signal, and restricting it the same way silently excluded
    EVERY within-instrument semantic pair -- checked directly, a real gap, not just a
    hypothetical one. Safe to allow because similarity.semantic_candidate_pairs() now
    gives within-instrument neighbours their OWN, separate (typically smaller) top-k
    budget rather than sharing the cross-instrument one -- see that function's own note."""
    candidates = []
    n = len(records)
    for i, j in combinations(range(n), 2):
        a, b = records[i], records[j]
        if a.norm["addressee_type"] != b.norm["addressee_type"]:
            continue
        # No in-force gate here (2026-09-23 decision): a not-yet-in-force provision can
        # still genuinely contradict something already in force -- catching that before
        # the new provision takes effect is more useful than filtering it out, not less.
        graph_hit = _graph_connected(g, a.graph_uid, b.graph_uid)
        cross_instrument = a.instrument_id != b.instrument_id
        trigger_hit = cross_instrument and _trigger_keyword_hit(a, b)
        semantic_hit = semantic_pairs is not None and (i, j) in semantic_pairs
        # Cross-instrument only, same rationale as trigger_hit: within one instrument,
        # a real cross-domain tension between two duties would already be visible via
        # citation or shared drafting context; this signal exists to bridge instruments
        # that share neither vocabulary nor a citation link.
        concept_hit = cross_instrument and _concept_pair_hit(a, b)
        if not (graph_hit or trigger_hit or semantic_hit or concept_hit):
            continue
        candidates.append({"a": a, "b": b, "graph_hit": graph_hit, "trigger_hit": trigger_hit,
                            "semantic_hit": semantic_hit, "concept_hit": concept_hit})
    return candidates


# ---------------------------------------------------------------------------
# Recipient matching -- decides whether two norms' deadlines/competing-authority
# claims are even ABOUT the same recipient. This matters because the project's own
# definition of contradiction is "complying with one rule may make it difficult or
# impossible to comply with another" -- two different deadlines to two DIFFERENT
# recipients (Cbw's CSIRT/competent authority vs. GDPR's supervisory authority) is not
# actually a contradiction under that definition: nothing stops you satisfying both.
# Checked directly against the project's own flagship example before trusting this:
# Cbw's generic term is "bevoegde autoriteit" (competent authority, a NIS2-style
# sector-neutral term), GDPR's is "toezichthoudende autoriteit" (supervisory authority,
# GDPR's own term for the data-protection regulator specifically) -- genuinely
# different Dutch legal terms for different kinds of bodies, not interchangeable
# synonyms, so they correctly resolve to "different" below, not a match.
# ---------------------------------------------------------------------------

GENERIC_AUTHORITY_TERMS = {
    "bevoegde autoriteit", "toezichthoudende autoriteit", "bevoegde toezichthoudende autoriteit",
    "competent authority", "supervisory authority", "afwikkelingsautoriteit",
    "bevoegd orgaan", "bevoegde organen",
}


# Found by checking the full corpus's one and only pre-fix finding against the real
# text (2026-09-23): UAVG art. 6(4)'s recipient_body ("Autoriteit persoonsgegevens") is
# the body BEING GRANTED a power; UAVG art. 21a(4)'s recipient_body ("de overtreder",
# the offender) is the TARGET a power is exercised ON, not a competing claimant to it.
# Both share the field name recipient_body, but the roles are opposite, and comparing
# them as if both named "the authority" produced a false competence_competition. This
# lexicon excludes terms that are never an authority so they can never be misread as a
# competing one.
NON_AUTHORITY_RECIPIENT_TERMS = {
    "overtreder", "aanbieder", "entiteit", "onderneming", "verwerkingsverantwoordelijke",
    "betrokkene", "klant", "klanten", "cliënt", "cliënten", "gebruiker", "gebruikers",
    "consument", "consumenten", "natuurlijke persoon", "rechtspersoon", "orgaan",
    # Publication channels, not bodies (2026-09-24 fix): the real UAVG art. 19(1) High-
    # confidence competence_competition finding listed "de Staatscourant" (the
    # government gazette a cooperation protocol gets published in) as if it were a
    # competing authority alongside "andere toezichthouders" -- a category error from
    # Stage 6's own extraction (a gazette cannot hold or contest a regulatory power),
    # not something worth a full re-extraction to fix. Excluded the same way an
    # offender/entity/data-subject already is: a role that is never an authority.
    "staatscourant", "staatsblad", "publicatieblad", "tractatenblad",
}
_LEADING_ARTICLE_RE = re.compile(r"^(de|het|haar|zijn|een|hun)\s+", re.I)
# Qualifying adjectives that precede a generic-authority phrase without changing which
# generic role it names -- "nationale bevoegde autoriteiten" and "bevoegde autoriteit"
# are the same generic reference for matching purposes, just with different qualifiers.
_RECIPIENT_QUALIFIER_RE = re.compile(r"^(nationale|relevante|andere|betrokken)\s+", re.I)


def _is_authority_term(term: str) -> bool:
    """Substring, not exact-match (2026-09-23 fix): the first false positive this
    caught used "een essentiële entiteit", which doesn't equal "entiteit" but clearly
    names one. Word-for-word equality was never going to catch every phrasing.

    The HEAD of the phrase is checked first (2026-09-24 fix), before the exclusion
    list: checked directly against real data -- "de bevoegde autoriteit, bedoeld in
    artikel 8 van de Wet weerbaarheid KRITIEKE ENTITEITEN" and "Autoriteit CONSUMENT en
    Markt" are both genuine, unambiguous authorities, but a plain whole-string
    substring check wrongly excludes both, because a word LATER in the phrase (the
    cited law's own name; part of the authority's own proper name) happens to match a
    term meant to exclude a completely different role entirely. Dutch legal drafting
    reliably puts the head noun first, so a term recognized as STARTING WITH a known
    generic-authority phrase or a known named authority is trusted as an authority
    regardless of what a later citation clause or proper name happens to contain."""
    norm = _normalize_recipient(term)
    head_terms = GENERIC_AUTHORITY_TERMS | set(AUTHORITY_ALIASES.keys())
    if any(norm == head or norm.startswith(head + " ") or norm.startswith(head + ",")
           for head in head_terms):
        return True
    return not any(bad in norm for bad in NON_AUTHORITY_RECIPIENT_TERMS)


def _normalize_recipient(s: str) -> str:
    s = _LEADING_ARTICLE_RE.sub("", s.strip().lower())
    s = _LEADING_ARTICLE_RE.sub("", s).strip()
    for _ in range(2):  # handle a stacked qualifier, e.g. "andere relevante autoriteiten"
        s = _RECIPIENT_QUALIFIER_RE.sub("", s)
    # Narrow plural normalization for this one noun specifically, not a general
    # stemmer -- blind suffix-stripping on arbitrary Dutch nouns is unsafe, but
    # "autoriteiten"/"autoriteit" is common enough here to be worth handling directly.
    s = re.sub(r"\bautoriteiten\b", "autoriteit", s)
    # Known named-authority aliases -- none of these abbreviated forms happen to occur
    # in the current corpus's extracted recipient_body values (checked directly), but
    # kept here so a future extraction that DOES produce "AP" or "ACM" still resolves
    # to the same canonical name as the spelled-out version, rather than silently
    # failing to match on a technicality.
    for canonical, aliases in AUTHORITY_ALIASES.items():
        if s == canonical or s in aliases:
            return canonical
    return s.strip()


AUTHORITY_ALIASES = {
    "autoriteit persoonsgegevens": {"ap", "de ap"},
    "autoriteit consument en markt": {"acm", "de acm"},
    "autoriteit financiële markten": {"afm", "de afm"},
    "de nederlandsche bank": {"dnb"},
    "autoriteit financiële markten en de nederlandsche bank": {"afm en dnb"},
}


def _same_addressee_authority(a: "NormRecord", b: "NormRecord") -> bool:
    """Strict exact-match on the normalized addressee (2026-09-24, for
    competence_competition) -- deliberately NOT the word-overlap _addressee_matches()
    used elsewhere: checked directly that word overlap wrongly equates "Autoriteit
    Persoonsgegevens" and "Autoriteit Consument en Markt" (both share the single word
    "autoriteit", enough to clear a 50% threshold for short official names). This
    reuses _normalize_recipient()'s exact-identity logic instead -- the same apparatus
    already trusted for recipient_body comparison -- applied to the addressee field."""
    addr_a, addr_b = a.norm.get("addressee"), b.norm.get("addressee")
    if not addr_a or not addr_b:
        return False
    return _normalize_recipient(addr_a) == _normalize_recipient(addr_b)


def _recipient_terms(rec: NormRecord) -> tuple[set, set]:
    """Returns (named_terms, generic_terms) for one norm's recipient_body list. A
    recipient string counts as "generic" if it CONTAINS one of the fixed generic-role
    phrases anywhere (e.g. "... overeenkomstig artikel 55 bevoegde toezichthoudende
    autoriteit" still contains "toezichthoudende autoriteit"); otherwise it's treated
    as naming something specific (e.g. "haar CSIRT")."""
    named, generic = set(), set()
    for r in rec.norm.get("recipient_body") or []:
        norm = _normalize_recipient(r)
        hit = {term for term in GENERIC_AUTHORITY_TERMS if term in norm}
        if hit:
            generic |= hit
        elif norm:
            named.add(norm)
    return named, generic


def recipient_match_kind(a: NormRecord, b: NormRecord) -> str:
    """"same_named" (a specific body is named on both sides and matches) > "generic_unresolved"
    (both sides use the identical generic role phrase, but the real body behind it isn't
    confirmed to be the same -- see module note) > "different" (no basis for a match) >
    "unknown" (one or both sides have no recipient_body at all)."""
    named_a, generic_a = _recipient_terms(a)
    named_b, generic_b = _recipient_terms(b)
    if not (named_a or generic_a) or not (named_b or generic_b):
        return "unknown"
    if named_a & named_b:
        return "same_named"
    if generic_a & generic_b:
        return "generic_unresolved"
    return "different"


# ---------------------------------------------------------------------------
# Deterministic sub-type routing. Both deterministic sub-types require SOME basis for
# treating the two norms as pointing at the same recipient -- "different" or "unknown"
# falls through to duty_conflict instead, where the AI can still catch a genuine
# conflict for some other reason, but won't be handed a false "different deadlines"
# alarm for what are really just two separate, independently satisfiable duties.
# ---------------------------------------------------------------------------

def _deadline_hours(d: Optional[dict]) -> Optional[float]:
    if not d or d.get("value") is None or d.get("unit") not in UNIT_TO_HOURS:
        return None
    return d["value"] * UNIT_TO_HOURS[d["unit"]]


_DUTCH_STOPWORDS = {
    "de", "het", "een", "van", "voor", "aan", "met", "dat", "die", "deze", "dit", "als",
    "of", "en", "op", "in", "tot", "bij", "om", "kan", "kunnen", "wordt", "worden", "is",
    "zijn", "haar", "zijn", "hun", "niet", "ook", "dan", "over", "onder", "naar", "uit",
    # Ordinals and legal-reference structural words (2026-09-23 fix): a long
    # cross-reference list ("artikelen 3, 4, ... eerste, tweede, ... lid") shares tons
    # of these words with any OTHER long cross-reference list regardless of subject
    # matter -- caught via a real false positive (Uitvoeringswet dataverordening art. 8
    # vs. UAVG arts. 17/21a, three completely unrelated fine provisions that scored as
    # "same function" purely from shared ordinals and "lid"/"artikel", not genuine
    # topical overlap).
    "eerste", "tweede", "derde", "vierde", "vijfde", "zesde", "zevende", "achtste",
    "negende", "tiende", "elfde", "twaalfde", "dertiende", "veertiende", "lid", "leden",
    "artikel", "artikelen", "onderdeel", "onderdelen", "volzin", "bedoeld", "bepaalde",
    "hoofdstuk", "paragraaf", "bijlage",
    # Generic administrative-fine boilerplate (2026-09-23 fix, same root cause as the
    # ordinals above): "bestuurlijke boete... ten hoogste... jaaromzet... voorgaande
    # boekjaar" is the standard shape of nearly EVERY EU-style fine clause, so it
    # overlaps between any two fine provisions regardless of which violation they
    # actually punish -- caught via Uitvoeringswet dataverordening art. 8 vs. UAVG
    # art. 17/21a, three fine clauses for three unrelated violations that scored as
    # "same function" purely from shared penalty-mechanism vocabulary.
    "bestuurlijke", "boete", "bedrag", "jaaromzet", "boekjaar", "voorgaande", "hoogste",
    "indien", "wereldwijde", "totale", "opleggen", "oplegging", "overtreder", "overtreding",
    "geval", "meer",
}


def _content_words(text: str) -> set:
    return {w for w in re.findall(r"[a-zà-ÿ]+", text.lower())
            if w not in _DUTCH_STOPWORDS and len(w) > 3}


def _same_function(a: NormRecord, b: NormRecord, min_overlap: float = 0.2) -> bool:
    """Whether two COMPETENCE norms are plausibly about the same regulatory matter, not
    just both tagged COMPETENCE. Necessary in addition to the authority-term filter:
    checked directly against a real false positive (UAVG art. 19 vs Uitvoeringswet
    dataverordening art. 7) where BOTH sides passed the authority filter (their
    recipients -- "andere toezichthouders" / "andere bevoegde autoriteiten ..." -- are
    genuine authority references) but the two provisions are two DIFFERENT regulators
    (AP, ACM) each just exercising their own ordinary, unrelated cooperation power for
    two unrelated EU regimes (GDPR vs. the Data Governance Act). Known limitation,
    stated plainly rather than hidden: this specific case has near-identical generic
    drafting ("efficiënt en effectief toezicht", "samenwerkingsprotocollen") that this
    word-overlap check alone won't catch, since the overlapping words are genuinely
    shared -- they're just generic procedural vocabulary, not evidence of the same
    subject matter. Confirmed this check DOES correctly separate the other two known
    false positives (differing subject-matter vocabulary with no real overlap)."""
    words_a = _content_words(f"{a.norm.get('trigger_event') or ''} {a.norm.get('action') or ''}")
    words_b = _content_words(f"{b.norm.get('trigger_event') or ''} {b.norm.get('action') or ''}")
    if not words_a or not words_b:
        return False
    overlap = words_a & words_b
    return len(overlap) / min(len(words_a), len(words_b)) >= min_overlap


# ---------------------------------------------------------------------------
# Threshold mismatch (2026-09-23) -- the same arithmetic idea as standard_collision,
# applied to `thresholds` instead of `deadline`. Real threshold text is far messier
# than deadline text (checked directly: time durations that should have been
# deadline_raw, bare citations like "artikelen 80 en 87", vague self-references like
# "ten hoogste de in deze leden genoemde bedragen", qualitative levels like
# "betrouwbaarheidsniveau hoog") -- deliberately scoped to the one genuinely
# comparable, legally significant subset: euro fine amounts and turnover-percentage
# fine caps, the standard EU administrative-fine pattern. Everything else is left
# unparsed rather than guessed at.
# ---------------------------------------------------------------------------

# Amount chars include ordinary space and non-breaking space (\xa0) as thousands
# separators (2026-09-24 fix): checked directly against every threshold string actually
# in the corpus -- GDPR art. 83 and AI Act art. 99's fine amounts are ALL written as
# "20 000 000 EUR" / "35 000 000 EUR" (space-grouped, ISO currency code), never the
# Dutch word "euro" the old regex required. Only the Dutch-native laws (UAVG, Cbw) use
# "euro" the word or the "€" symbol. The old regex matched none of GDPR's or the AI
# Act's own fine amounts at all -- meaning threshold_mismatch, one of the three
# sub-types this system trusts as deterministic, had never once been able to compare
# an EU-regulation-level fine against anything. Checked and confirmed as a reproducible
# failure, not a one-off: _parse_threshold("20 000 000 EUR") returned None outright.
_AMOUNT = r"[\d][\d.,\xa0 ]*"
_EURO_SUFFIX_RE = re.compile(rf"({_AMOUNT})\s*(?:euro|eur)\b", re.I)
_EURO_PREFIX_RE = re.compile(rf"€\s*({_AMOUNT})")
# Not anchored to "van de ... jaaromzet" specifically (2026-09-24 fix): checked directly
# -- AI Act art. 99 phrases this as "van HAAR totale wereldwijde JAARLIJKSE OMZET" (a
# possessive, not "de", and two words instead of GDPR's one-word "jaaromzet"), which the
# original anchored pattern never matched. Loosened to "van ... omzet" within a bounded
# window -- still scoped narrowly enough given this only ever runs against an already-
# isolated thresholds[] entry, not a whole paragraph.
_PERCENT_TURNOVER_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*%\s*van\s+.{0,40}?omzet", re.I)


def _to_float_amount(raw: str) -> float:
    """Dutch/EU legal-drafting number formatting, normalized to a plain float. Handles
    space/nbsp thousands groups ("20 000 000"), dot thousands groups ("20.000.000"), the
    Dutch "no cents" notation ("1.000.000,-" / ",--" / ",–"), and a genuine decimal
    comma -- distinguished from a thousands comma by whether exactly 1-2 digits follow
    it. Every branch checked against a real string actually seen in the corpus."""
    s = re.sub(r"\s+", "", raw.strip().replace("\xa0", " "))
    s = re.sub(r",[-–]+$", "", s)
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".") if re.fullmatch(r"\d+,\d{1,2}", s) else s.replace(",", "")
    else:
        s = s.replace(".", "")
    return float(s)


def _parse_threshold(text: str) -> Optional[tuple[str, float]]:
    m = _EURO_SUFFIX_RE.search(text) or _EURO_PREFIX_RE.search(text)
    if m:
        return "euro", _to_float_amount(m.group(1))
    m = _PERCENT_TURNOVER_RE.search(text)
    if m:
        return "percent_turnover", float(m.group(1).replace(",", "."))
    return None


def _parsed_thresholds(norm: dict) -> list[tuple[str, float]]:
    parsed = (_parse_threshold(t) for t in (norm.get("thresholds") or []))
    return [p for p in parsed if p is not None]


# ---------------------------------------------------------------------------
# Deontic polarity conflict (2026-09-23) -- the textbook-clearest form of a real
# contradiction (one rule requires an act, another forbids the same act for the same
# actor), which previously had no distinct identity: it was just one more case falling
# into the generic duty_conflict bucket, indistinguishable from everything else sent to
# the AI. Made explicit here so it's both interpretable to a reviewer ("this is a MUST
# vs. MUST-NOT") and still AI-confirmed before becoming a finding, since deciding
# whether two action descriptions are really "the same act" is a judgement call a
# word-overlap heuristic can only narrow down, not settle on its own -- unlike
# threshold/standard collision, this is NOT trusted as deterministic.
# ---------------------------------------------------------------------------

def _addressee_matches(a: NormRecord, b: NormRecord) -> bool:
    """Same actor bears both duties -- the relevant identity check for a polarity
    conflict is WHO IS COMMANDED (addressee), not who receives a report
    (recipient_body, which is what standard_collision/competence_competition check)."""
    addr_a, addr_b = a.norm.get("addressee") or "", b.norm.get("addressee") or ""
    if not addr_a or not addr_b:
        return False
    if _normalize_recipient(addr_a) == _normalize_recipient(addr_b):
        return True
    words_a, words_b = _content_words(addr_a), _content_words(addr_b)
    if not words_a or not words_b:
        return False
    return len(words_a & words_b) / min(len(words_a), len(words_b)) >= 0.5


def route_subtype(a: NormRecord, b: NormRecord, rk: str) -> tuple[str, Optional[dict]]:
    ha, hb = _deadline_hours(a.norm.get("deadline")), _deadline_hours(b.norm.get("deadline"))
    if ha is not None and hb is not None and ha != hb and rk in ("same_named", "generic_unresolved"):
        # joint_compliance_possible (2026-09-24 fix, prompted by a real review finding):
        # EVERY deadline this parser can produce a hard hour figure for is a "no later
        # than N" ceiling -- checked directly, _DEADLINE_PATTERNS only ever matches
        # binnen/uiterlijk/onverwijld/onmiddellijk, never a floor phrasing like "ten
        # minste N". Two different ceilings to the same recipient are, by construction,
        # ALWAYS jointly satisfiable (comply with whichever is numerically stricter --
        # satisfying "within 24h" automatically satisfies "within 72h" too), so this is
        # a confirmed DIFFERENCE, not a confirmed INCOMPATIBILITY. Kept as its own
        # sub-type per the project's own decision to track this signal explicitly
        # rather than discard it (it's still useful -- e.g. as an eventual C2 overlap/
        # duplication candidate) -- but no longer presented as an established
        # contradiction; see incompatibility_established on the finding.
        return "standard_collision", {"deadline_a_hours": ha, "deadline_b_hours": hb,
                                       "delta_hours": abs(ha - hb),
                                       "joint_compliance_possible": True,
                                       "compliance_note": "both deadlines are 'no later than' "
                                       "ceilings; complying with the stricter one satisfies both"}

    # Recipient matching is the wrong gate for fines specifically -- checked directly:
    # most fine-threshold provisions have an EMPTY recipient_body (a fine's "recipient"
    # isn't a meaningful concept the way a report's recipient is), so the rk-based gate
    # silently excluded every real candidate. Two different gates instead: different
    # instruments (a law's OWN multi-tier fine structure, e.g. GDPR's own 2%/4% split
    # in one article, is deliberate design, not a bug -- comparing it against itself
    # would be a false positive) and _same_function, so a mismatch is only flagged when
    # the two fines plausibly apply to a similar kind of violation.
    if a.instrument_id != b.instrument_id and _same_function(a, b, min_overlap=0.15):
        for kind_a, val_a in _parsed_thresholds(a.norm):
            for kind_b, val_b in _parsed_thresholds(b.norm):
                if kind_a == kind_b and val_a != val_b:
                    return "threshold_mismatch", {"kind": kind_a, "value_a": val_a, "value_b": val_b,
                                                   "delta": abs(val_a - val_b)}

    if a.norm["deontic"] == "COMPETENCE" and b.norm["deontic"] == "COMPETENCE" and rk == "different" \
            and not _same_addressee_authority(a, b):
        # Here "different" (recipient) is the SIGNAL, not the disqualifier: two bodies
        # genuinely claiming the same power is the competition. "same_named"/
        # "generic_unresolved" would mean they actually agree on who holds it -- not a
        # real competition. Filtered to terms that could plausibly BE an authority, AND
        # required to be about the same underlying matter -- see
        # NON_AUTHORITY_RECIPIENT_TERMS and _same_function's notes on why both checks
        # are needed, and _same_function's note on the one known case neither catches.
        #
        # `not _same_addressee_authority(a, b)` (2026-09-24 fix): the power HOLDER
        # (addressee) must actually differ -- checked directly against a real High-
        # confidence false positive, Cbw art. 77 vs. art. 78, where BOTH norms have
        # addressee "de bevoegde autoriteit" (the same single competent authority) and
        # only recipient_body differs (a certifying body vs. a civil court -- i.e.
        # which THIRD PARTY the authority may ask for help, not a competing claimant
        # to the power). recipient_body answers "who does the authority act upon/
        # through", not "who else holds this power" -- comparing IT alone, as the old
        # code did, mistook "one authority, two available tools" for "two authorities,
        # one power". Uses _normalize_recipient()'s strict exact-match, NOT the
        # looser word-overlap _addressee_matches() (built for OBLIGATION/PROHIBITION
        # actor text, a different case) -- checked directly why: "Autoriteit
        # Persoonsgegevens" and "Autoriteit Consument en Markt" share the single word
        # "Autoriteit", which alone clears a 50% word-overlap bar for short official
        # names and would have wrongly called AP and ACM "the same addressee",
        # silently breaking the one accepted residual case this fix must NOT affect
        # (UAVG art. 19 vs. Uitvoeringswet dataverordening art. 7).
        rec_a = {r for r in (a.norm.get("recipient_body") or []) if _is_authority_term(r)}
        rec_b = {r for r in (b.norm.get("recipient_body") or []) if _is_authority_term(r)}
        if rec_a and rec_b and _same_function(a, b):
            return "competence_competition", {"recipient_a": sorted(rec_a), "recipient_b": sorted(rec_b)}

    if {a.norm["deontic"], b.norm["deontic"]} == {"OBLIGATION", "PROHIBITION"} \
            and _addressee_matches(a, b) and _same_function(a, b, min_overlap=0.25):
        return "deontic_polarity_conflict", {"obligation_action": (a if a.norm["deontic"] == "OBLIGATION" else b).norm.get("action"),
                                              "prohibition_action": (a if a.norm["deontic"] == "PROHIBITION" else b).norm.get("action")}

    return "duty_conflict", None


# ---------------------------------------------------------------------------
# Confidence tiering (2026-09-22 decision, relabeled 2026-09-23) -- one shared
# mechanism for ranking every finding's trustworthiness, reused across C1/C2/C3 rather
# than inventing bespoke handling for each new gray area (recipient matching today,
# something else in C2 or C3 later). Tier 1 is "High", never "Confirmed" -- the system
# never confirms anything; only a human expert reviewing report_eligible output does.
#   Tier 1 - High:   fully deterministic comparison, every input signal clean.
#   Tier 2 - Medium: likely real, but one input wasn't fully clean.
#   Tier 3 - Low:    worth surfacing, not worth trusting without a close look.
# ---------------------------------------------------------------------------

def compute_confidence_tier(a: NormRecord, b: NormRecord, subtype: str, rk: str,
                             det_result: Optional[dict],
                             llm_verdict: Optional["DutyConflictVerdict"],
                             needs_recheck_reason: Optional[str] = None,
                             challenge: Optional["ChallengeVerdict"] = None) -> tuple[int, list[str]]:
    reasons = []
    clean_extraction = not a.norm.get("extraction_uncertain") and not b.norm.get("extraction_uncertain")
    if not clean_extraction:
        reasons.append("one or both norms were finalized with the conservative default after "
                        "Stage 6's two extraction passes disagreed (extraction_uncertain)")

    if subtype == "threshold_mismatch":
        # Recipient identity isn't the relevant check here -- a fine doesn't have a
        # meaningful "recipient" the way a report deadline does. What actually grounds
        # confidence is that _same_function already gated entry (same violation type)
        # and the two numbers themselves are unambiguous once that's true.
        if clean_extraction:
            return 1, ["deterministic comparison (arithmetic, no AI)",
                       "same underlying violation type confirmed by content overlap",
                       "both norms cleanly extracted"]
        return 2, reasons or ["deterministic comparison, one weaker signal"]

    if subtype == "standard_collision":
        # This confirms a DIFFERENCE, not a confirmed INCOMPATIBILITY (2026-09-24 fix):
        # every deadline this parser can compare is a "no later than N" ceiling, so
        # complying with the stricter of the two always satisfies the looser one too --
        # nothing stops joint compliance. Still Tier 1 for how CONFIRMED the difference
        # itself is (pure arithmetic, no AI, recipient identity checked), but the reason
        # text says explicitly what is and isn't established, so a reviewer doesn't read
        # "High" as "confirmed contradiction".
        compatible = bool((det_result or {}).get("joint_compliance_possible"))
        if compatible:
            reasons.append("both deadlines are 'no later than' ceilings -- complying with the "
                            "stricter one satisfies both; a CONFIRMED DIFFERENCE, not a confirmed "
                            "incompatibility (see build_finding's incompatibility_established)")
        if rk == "generic_unresolved":
            reasons.append("recipients share only a generic role name (e.g. 'competent authority'), "
                            "not a specific named body -- the two may or may not be the same authority")
        if rk == "same_named" and clean_extraction:
            return 1, ["deterministic comparison (arithmetic, no AI)",
                       "recipient identity confirmed by name", "both norms cleanly extracted"] + reasons
        return (2 if clean_extraction else 3), reasons or ["deterministic comparison, one weaker signal"]

    if subtype == "competence_competition":
        if rk == "generic_unresolved":
            reasons.append("recipients share only a generic role name (e.g. 'competent authority'), "
                            "not a specific named body -- the two may or may not be the same authority")
        if rk == "different" and clean_extraction:
            return 1, ["deterministic comparison (a direct lookup, no AI)",
                       "recipient identity confirmed as two distinct named bodies",
                       "both norms cleanly extracted"]
        return (2 if clean_extraction else 3), reasons or ["deterministic comparison, one weaker signal"]

    reasons.append("resolved by AI judgement (duty conflict), not a deterministic computation -- "
                    "never eligible for Tier 1 regardless of how confident the model was")
    if needs_recheck_reason is not None:
        # Takes priority over everything else below: an internally inconsistent
        # response (verdict disagreeing with its own stated reasoning/evidence) is
        # never trusted at face value, no matter what confidence it claims.
        reasons.append(f"model's response was internally inconsistent even after a retry "
                        f"({needs_recheck_reason}) -- not accepted as a supported verdict")
        return 3, reasons
    if challenge is not None and not challenge.survives:
        # An independent adversarial second pass found a specific problem with a
        # self-consistent CONTRADICTION verdict -- caught a different failure mode than
        # needs_recheck (the first pass didn't contradict itself, it was just wrong).
        reasons.append(f"an independent challenge pass found this CONTRADICTION did not "
                        f"survive scrutiny: {challenge.corrections!r} -- not accepted as supported")
        return 3, reasons
    if llm_verdict is not None:
        if llm_verdict.verdict == "INSUFFICIENT_EVIDENCE":
            reasons.append("the model could not reach a firm verdict either way from the text alone "
                            "-- genuinely uncertain, not a confirmed non-conflict, so still worth a look")
            return 3, reasons
        reasons.append(f"model-reported confidence: {llm_verdict.confidence}")
        if llm_verdict.confidence >= 0.8 and clean_extraction:
            return 2, reasons
    return 3, reasons


# ---------------------------------------------------------------------------
# Resolution filter -- runs last, can suppress a candidate entirely. Deference is free
# text (Stage 6 never structured it into a target article id), so this is a documented
# heuristic, not an exact match: does the deferring norm's own text name the other
# norm's article number, and -- cross-instrument -- confirm it's actually THAT
# instrument's article, not just any law's article with the same number.
#
# Common-name aliases per instrument (2026-09-24 fix): deference/prose text never
# contains a raw instrument_id (a CELEX or BWBR code, e.g. "32016R0679") -- real Dutch
# legal text names an EU instrument by its formatted number ("Verordening (EU)
# 2016/679") or a common abbreviation ("AVG", "NIS2"), and a Dutch law by its short
# title. Checked directly: the previous instrument-name branch
# (`other.instrument_id.lower() in deference.lower()`) never fired for a single
# deference value anywhere in the corpus -- it was comparing the wrong vocabulary, a
# safety net that looked like it worked but never actually did.
# ---------------------------------------------------------------------------

INSTRUMENT_ALIASES = {
    "32016R0679": {"avg", "gdpr", "algemene verordening gegevensbescherming"},
    "32022L2555": {"nis2", "nis 2", "nis2-richtlijn"},
    "32022R2554": {"dora"},
    "32024R1689": {"ai-verordening", "ai act", "aiverordening"},
    "BWBR0052872": {"cyberbeveiligingswet", "cbw"},
    "BWBR0040940": {"uitvoeringswet algemene verordening gegevensbescherming", "uavg"},
    "BWBR0048156": {"wet digitale overheid", "wdo"},
    "BWBR0009950": {"telecommunicatiewet"},
    "BWBR0051796": {"uitvoeringswet dataverordening"},
    "BWBR0049497": {"bijlage 35"},
}


def _celex_number_form(instrument_id: str) -> Optional[str]:
    """"32016R0679" -> "2016/679" -- the formatted number an EU instrument is actually
    referred to by in Dutch prose ("Verordening (EU) 2016/679"), derived from the CELEX
    id's own structure (sector digit + 4-digit year + type letter + number) rather than
    hand-typed per instrument."""
    m = re.match(r"^\d(\d{4})[A-Z](\d+)$", instrument_id)
    if not m:
        return None
    year, seq = m.group(1), m.group(2).lstrip("0") or "0"
    return f"{year}/{seq}"


def _instrument_named_in(text: str, instrument_id: str) -> bool:
    text_l = text.lower()
    number_form = _celex_number_form(instrument_id)
    if number_form and number_form in text_l:
        return True
    return any(alias in text_l for alias in INSTRUMENT_ALIASES.get(instrument_id, ()))


# ---------------------------------------------------------------------------
# Conditional-exception registry (2026-09-24, item 7, scoped) -- a real gap that
# check_deference_suppresses() structurally cannot close: it only ever looks at the TWO
# norms being compared, but Cbw art. 31 is a THIRD provision that conditionally
# disapplies Cbw arts. 25-30 for an entity ALREADY subject to an equivalent sector-
# specific EU reporting duty (DORA being the running example). Its own norms[] entries
# are DEFINITION/DEFERENCE -- excluded from ELIGIBLE_DEONTICS entirely, so today it has
# ZERO influence on candidate suppression despite being exactly the kind of "this is
# already resolved by law" provision the resolution filter exists for.
#
# NOT auto-suppressing on purpose, per the same caution the reviewer raised: art. 31's
# condition ("if the sector-specific requirement is equivalent") is a genuine regulatory
# equivalence judgement this pipeline cannot make from text alone -- Cbw arts. 25-30 vs.
# DORA's incident-reporting arts. are NOT automatically resolved just because this
# clause exists, only flagged as having an unresolved, relevant exception a human should
# weigh. This is deliberately narrow: only "artikelen X tot en met Y" (the one pattern
# actually confirmed in this corpus), not a general resolution-rule parser -- a real
# start on item 7, not the full typed registry (citation/incorporation/preservation/
# displacement) the review calls for.
# ---------------------------------------------------------------------------

_ARTICLE_RANGE_RE = re.compile(r"artikelen?\s+(\d+)\s+tot\s+en\s+met\s+(\d+)", re.I)


def load_conditional_exceptions() -> list[dict]:
    exceptions = []
    for path, get_provisions in SOURCES:
        root = json.loads((ROOT / path).read_text(encoding="utf-8"))
        for p in get_provisions(root):
            instrument_id = p.get("instrument_id") or (
                "BWBR0051796" if "uitvoeringswet" in path.lower() else
                "BWBR0049497" if "bijlage35" in path.lower() else None)
            for norm in p.get("norms", []):
                if norm["deontic"] != "DEFERENCE":
                    continue
                text = p.get("text", "")
                m = _ARTICLE_RANGE_RE.search(text)
                if not m:
                    continue
                lo, hi = int(m.group(1)), int(m.group(2))
                exceptions.append({
                    "instrument_id": instrument_id,
                    "source_article": str(p.get("article") or p.get("number")),
                    "exempted_articles": {str(n) for n in range(lo, hi + 1)},
                    "condition_text": text,
                })
    return exceptions


def check_conditional_exception(a: NormRecord, b: NormRecord,
                                 exceptions: list[dict]) -> Optional[dict]:
    """Returns a dict describing an unresolved, potentially-relevant exception, or None.
    Cross-instrument only -- the whole point of this specific pattern (art. 31) is a
    sector-specific EU rule substituting for a domestic one; same-instrument pairs
    aren't what this clause is about."""
    if a.instrument_id == b.instrument_id:
        return None
    for norm in (a, b):
        for exc in exceptions:
            if exc["instrument_id"] == norm.instrument_id and norm.article in exc["exempted_articles"]:
                return {"exempted_norm": f"{norm.instrument_id} art. {norm.article}",
                        "source_article": f"{exc['instrument_id']} art. {exc['source_article']}",
                        "condition_text": exc["condition_text"]}
    return None


# Paragraph-level deference within the SAME article (2026-09-24 fix): checked directly
# against a real High-confidence false positive -- Cbw art. 27(2)'s own deference field
# says "In afwijking van het eerste lid" (by way of exception to paragraph 1), which
# Stage 6 correctly extracted, but the check above only ever recognized an "artikel N"
# reference, never "het eerste lid" (paragraph 1) of the SAME article. The two
# paragraphs' different deadlines (72h vs. 24h) were flagged as a standard_collision
# even though the text explicitly, unambiguously resolves it as a deliberate exception.
_DUTCH_ORDINALS = {
    "eerste": 1, "tweede": 2, "derde": 3, "vierde": 4, "vijfde": 5, "zesde": 6,
    "zevende": 7, "achtste": 8, "negende": 9, "tiende": 10, "elfde": 11, "twaalfde": 12,
}
_LID_REFERENCE_RE = re.compile(r"\b(" + "|".join(_DUTCH_ORDINALS) + r")\s+lid\b", re.I)


def _referenced_paragraph_numbers(text: str) -> set:
    return {_DUTCH_ORDINALS[m.group(1).lower()] for m in _LID_REFERENCE_RE.finditer(text)}


def _paragraph_reference_suppresses(a: NormRecord, b: NormRecord) -> Optional[str]:
    if a.instrument_id != b.instrument_id or a.article != b.article:
        return None  # this pattern only makes sense within one article
    for norm, other in ((a.norm, b), (b.norm, a)):
        deference = norm.get("deference")
        if not deference:
            continue
        try:
            other_num = int(other.norm.get("number"))
        except (TypeError, ValueError):
            continue
        if other_num in _referenced_paragraph_numbers(deference):
            return deference
    return None


def check_deference_suppresses(a: NormRecord, b: NormRecord) -> Optional[str]:
    """Same-instrument article-number match is trusted directly -- a law's own
    "artikel 33" unambiguously means article 33 of that same law. Cross-instrument is
    NOT trusted on article number alone: checked directly against a real case (NIS2
    art. ~33's deference, "op grond van artikel 33 van die verordening", which genuinely
    means GDPR art. 33) -- the old code would have suppressed a candidate against ANY
    OTHER instrument's unrelated article 33 (DORA, AI Act, Cbw, ...) just as readily,
    since nothing confirmed which instrument "die verordening" meant. Cross-instrument
    suppression now additionally requires the deference text to actually NAME the
    target instrument (see INSTRUMENT_ALIASES/_celex_number_form). A bare "artikel N"
    naming no instrument is left unsuppressed rather than guessed at, since it most
    plausibly self-refers to the deferring norm's OWN instrument, not the candidate
    partner's.

    The instrument name is looked for across the WHOLE norm, not the deference string
    alone -- checked directly against the real case above: `deference` itself is just
    "op grond van artikel 33 van die verordening" ("die verordening" = a pronoun, names
    nothing); "Verordening (EU) 2016/679" is only stated earlier, in `trigger_event`.
    Requiring the name to be IN the deference string specifically would make this real,
    correct suppression fail too, right alongside the false ones -- overcorrecting from
    "too eager" to "never fires on real prose", which just trades one wrong answer for
    another."""
    paragraph_ref = _paragraph_reference_suppresses(a, b)
    if paragraph_ref:
        return paragraph_ref
    same_instrument = a.instrument_id == b.instrument_id
    for norm, other in ((a.norm, b), (b.norm, a)):
        deference = norm.get("deference")
        if not deference:
            continue
        if not re.search(rf"\bartikel\s*{re.escape(other.article)}\b", deference, re.I):
            continue
        norm_blob = " ".join(str(norm.get(f) or "") for f in
                              ("trigger_event", "action", "deference", "conditions"))
        if same_instrument or _instrument_named_in(norm_blob, other.instrument_id):
            return deference
    return None


# ---------------------------------------------------------------------------
# Cross-reference/definition closure (2026-09-24, item from an external review's
# blueprint, §7.4): the adjudication prompt previously sent only the two norms' own
# provision text -- a term like "AI-systeem met een hoog risico" was never resolved
# back to its actual defining clause, so the model had to guess at scope from context
# alone. Checked directly against two real formats before trusting a single regex:
# EU-style numbered/quoted ('1) "term": definition;', AI Act/GDPR art. 3/4) and Dutch-
# native dash-bulleted ('– term: definition;', Cbw art. 1) -- both use the SAME
# "marker, term, colon, text" shape once the bullet style itself is treated as a
# delimiter, so one parser covers both rather than hand-writing one per instrument.
# ---------------------------------------------------------------------------

_DEF_MARKER_RE = re.compile(r"(?:^|\n)\s*(?:\d+\)|[a-z]\)|–|-)\s*")
_DEF_QUOTE_CHARS = "„“”‘’'\""


def _defs_from_chunks(chunks: list) -> dict:
    definitions = {}
    for chunk in chunks:
        if ":" not in chunk:
            continue
        term, _, rest = chunk.partition(":")
        term = term.strip().strip(_DEF_QUOTE_CHARS).strip()
        if not term or len(term) > 100 or "\n" in term:
            continue
        definitions[term.lower()] = _normalize_ws(rest)[:400]
    return definitions


def _parse_definitions(text: str) -> dict:
    """Splits a definitions article's raw text on its own bullet markers, then each
    chunk on its first colon -- "term" before, definition text after. Skips chunks
    that don't look like a real short defined term (too long, or no colon at all),
    since the intro sentence before the first bullet and any stray paragraph without
    a definition shape would otherwise pollute the map with garbage entries.

    Falls back to splitting on blank lines when no bullet markers are found at all
    (2026-09-24 fix): checked directly -- UAVG art. 1's definitions list uses NEITHER
    the EU numbered/quoted style nor Cbw's dash bullets, just bare "term: definition;"
    paragraphs separated by blank lines, which the marker-based split alone missed
    entirely (0 terms parsed until this fallback was added)."""
    definitions = _defs_from_chunks(_DEF_MARKER_RE.split(text)[1:])
    if definitions:
        return definitions
    return _defs_from_chunks(text.split("\n\n"))


_DEFINITIONS_CACHE: Optional[dict] = None


def load_definitions_by_instrument() -> dict:
    """{instrument_id: {term_lower: definition_text}} -- gathers every provision that
    has at least one DEFINITION-type norm (Stage 6 already tags these) and parses ITS
    OWN raw provision text, not the extracted norm object (Stage 6 extracts a
    definitions article as one generic DEFINITION norm with action="wordt verstaan
    onder" -- the term-by-term breakdown was never in the structured norm data,
    only in the source text). Cached at module level: this reads all ~10 source files,
    which is unnecessary to repeat for every one of the thousands of adjudication calls
    in a run."""
    global _DEFINITIONS_CACHE
    if _DEFINITIONS_CACHE is not None:
        return _DEFINITIONS_CACHE
    result: dict = {}
    for path, get_provisions in SOURCES:
        root = json.loads((ROOT / path).read_text(encoding="utf-8"))
        for p in get_provisions(root):
            if not any(n["deontic"] == "DEFINITION" for n in p.get("norms", [])):
                continue
            iid = p.get("instrument_id") or (
                "BWBR0051796" if "uitvoeringswet" in path.lower() else
                "BWBR0049497" if "bijlage35" in path.lower() else None)
            defs = _parse_definitions(p.get("text") or "")
            result.setdefault(iid, {}).update(defs)
    _DEFINITIONS_CACHE = result
    return result


def _relevant_definitions(rec: "NormRecord", definitions: dict) -> list:
    """Which of THIS norm's own instrument's defined terms actually appear (as a
    literal substring) in its action/trigger_event/conditions text -- real Dutch legal
    drafting reuses a defined term verbatim wherever it's invoked, so a substring check
    is a reasonable, cheap way to find the ones actually relevant to this specific
    norm rather than dumping the whole definitions article into every prompt. Capped
    and longest-first: a short generic term ("risico") matching inside a longer, more
    specific one ("aanzienlijk risico") is less useful context than the specific one,
    and only a couple of definitions are worth the prompt space anyway."""
    inst_defs = definitions.get(rec.instrument_id, {})
    if not inst_defs:
        return []
    blob = _normalize_ws(f"{rec.norm.get('trigger_event') or ''} {rec.norm.get('action') or ''} "
                          f"{' '.join(rec.norm.get('conditions') or [])}")
    hits = [(term, definition) for term, definition in inst_defs.items()
            if len(term) >= 4 and term in blob]
    hits.sort(key=lambda t: -len(t[0]))
    return hits[:2]


# ---------------------------------------------------------------------------
# LLM adjudication -- ONLY for duty_conflict/deontic_polarity_conflict, the sub-types
# that are a genuine judgement call. Same Structured Outputs discipline as
# extract_norms.py: strict schema, verbatim-checked evidence spans, no free prose.
#
# 2026-09-24 rework, prompted by a real observed failure: a saved finding had
# verdict=CONTRADICTION while its own criterion_fired text argued "there is no genuine
# contradiction" -- the driver accepted it anyway because it only ever looked at
# `verdict`, never checked it against the model's own stated reasoning. Fixed by asking
# for a short structured comparison BEFORE the verdict (same actor? same circumstance?
# what does each side require? is joint compliance possible? what's left unresolved?),
# then validating the verdict against those answers in code -- not trusting a single
# label field in isolation. An internally inconsistent response is retried once with
# the specific inconsistency named; if it's still inconsistent, it's kept as a finding
# (never silently dropped -- the model's uncertainty is real signal) but flagged
# needs_recheck and forced to Tier 3, never accepted as a supported CONTRADICTION.
# ---------------------------------------------------------------------------

class DutyConflictVerdict(BaseModel):
    shared_actor: Optional[str]          # who could be subject to both rules
    shared_circumstance: Optional[str]   # under which shared circumstances both apply
    requirement_a: str                   # what norm A requires or prohibits
    requirement_b: str                   # what norm B requires or prohibits
    joint_compliance_possible: Optional[bool]  # is there a plausible way to satisfy both?
    # Compliant alternative (2026-09-24, from an external review's reconciliation
    # checklist): a real reconciliation path is a compliant alternative that satisfies
    # both rules -- restricted disclosure, anonymisation, phased reporting, temporary
    # suspension -- not just "yes/no is joint compliance possible" in the abstract.
    compliant_alternative: Optional[str]
    unresolved_exception: Optional[str]  # relevant exception/resolution left unresolved
    # What fact, if known, would flip this verdict (2026-09-24) -- makes a Low/Medium
    # finding actionable for a reviewer instead of just "uncertain".
    outcome_changing_facts: Optional[str]
    verdict: Literal["CONTRADICTION", "NOT_A_CONFLICT", "INSUFFICIENT_EVIDENCE"]
    criterion_fired: str
    evidence_span_a: Optional[str]  # must be verbatim in norm A's provision text
    evidence_span_b: Optional[str]  # must be verbatim in norm B's provision text
    confidence: float


class ChallengeVerdict(BaseModel):
    """Independent adversarial second pass (2026-09-24), modeled on an external
    review's "independent challenge prompt": our own consistency check (_consistency_
    issue) can only catch a verdict that contradicts ITS OWN reasoning -- it cannot
    catch a verdict that is internally consistent but substantively wrong. This is a
    SEPARATE model call whose only job is to try to disprove a proposed CONTRADICTION,
    given the same source text but not the first pass's own words -- agreement between
    two passes of the same model is not independent legal validation, but a genuine
    attempt to break the finding is a real, different check than re-reading your own
    reasoning back to yourself."""
    survives: bool
    corrections: Optional[str]      # specific problems found, with source spans, if any
    checks_passed: list[str]        # which reconciliation checks were tried and passed
    remaining_uncertainty: Optional[str]


@dataclass
class AdjudicationResult:
    verdict: DutyConflictVerdict
    needs_recheck_reason: Optional[str]  # None if the response passed consistency checks
    challenge: Optional[ChallengeVerdict] = None  # only set for a proposed CONTRADICTION


_NEGATION_PHRASES = (
    "not a genuine contradiction", "no genuine contradiction", "not mutually exclusive",
    "geen contradictie", "geen echte tegenstrijdigheid", "is not a contradiction",
    "are not in conflict", "not in conflict",
)


def _normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _verbatim_in(span: Optional[str], source_text: str) -> bool:
    if span is None:
        return True  # nothing claimed, nothing to check
    return _normalize_ws(span) in _normalize_ws(source_text)


def _consistency_issue(v: DutyConflictVerdict, text_a: str, text_b: str) -> Optional[str]:
    """Returns a human-readable reason the response can't be trusted as-is, or None if
    it's internally consistent. Checked structurally where possible (the fields the
    reviewer proposed), plus a text-level safety net for the exact failure mode already
    observed in production (verdict disagreeing with its own criterion_fired prose)."""
    if not (0.0 <= v.confidence <= 1.0):
        return f"confidence {v.confidence} outside [0, 1]"
    if v.verdict == "CONTRADICTION":
        if v.joint_compliance_possible is True:
            return "verdict=CONTRADICTION but joint_compliance_possible=True"
        if any(p in v.criterion_fired.lower() for p in _NEGATION_PHRASES):
            return "verdict=CONTRADICTION but criterion_fired's own text denies a contradiction"
        if v.evidence_span_a is None and v.evidence_span_b is None:
            return "verdict=CONTRADICTION but no evidence span was quoted from either side"
    for span, text, label in ((v.evidence_span_a, text_a, "A"), (v.evidence_span_b, text_b, "B")):
        if span is not None and not _verbatim_in(span, text):
            return f"evidence_span_{label.lower()} is not a verbatim substring of norm {label}'s provision text"
    return None


def _definitions_context(rec: "NormRecord", definitions: dict, label: str) -> str:
    hits = _relevant_definitions(rec, definitions)
    if not hits:
        return ""
    lines = "\n".join(f'  "{term}": {definition}' for term, definition in hits)
    return (f"\nRelevant defined term(s) from {rec.instrument_id}'s own definitions article "
            f"(context for norm {label} -- not itself a norm being compared):\n{lines}\n")


def adjudicate_duty_conflict(client, model: str, a: NormRecord, b: NormRecord) -> AdjudicationResult:
    definitions = load_definitions_by_instrument()
    def_context = _definitions_context(a, definitions, "A") + _definitions_context(b, definitions, "B")

    def build_prompt(retry_note: Optional[str] = None) -> str:
        note = (f"\nYour previous response was rejected: {retry_note}. "
                f"Reconsider and answer again, making sure your verdict actually matches "
                f"your own stated reasoning and quoted evidence.\n" if retry_note else "")
        return (
            "Two legal norms, extracted from Dutch/EU digital-law legislation, both impose an "
            "obligation, prohibition or competence on the same category of actor and share a "
            "similar triggering situation (candidate generation found them SIMILAR -- it did "
            "NOT establish that they conflict; that is your job). Work through the comparison "
            "explicitly before giving a verdict: who could be subject to both rules, under what "
            "shared circumstances both apply, what each rule actually requires or prohibits, "
            "whether there is a plausible way to satisfy both at once (joint_compliance_possible), "
            "whether a COMPLIANT ALTERNATIVE would let both be satisfied (e.g. restricted "
            "disclosure, anonymisation, phased reporting, temporary suspension, a fallback "
            "process -- not just 'yes/no' in the abstract), and what relevant exception or "
            "resolution remains unresolved. Only call it CONTRADICTION if joint compliance is "
            "NOT possible even via such an alternative -- one rule requires what the other "
            "forbids, or they impose mutually exclusive duties on the same actor for the same "
            "trigger, not merely that they're both about a similar topic. If a provision's own "
            "text already reconciles them (e.g. one explicitly defers to the other), that is not "
            "a contradiction. State what specific fact, if it were known, would change your "
            "verdict (outcome_changing_facts). Quote the specific evidence_span_a/b verbatim from "
            "the provision text given below; if the text doesn't support a firm verdict either "
            "way, return INSUFFICIENT_EVIDENCE rather than guessing." + note + "\n\n"
            f"NORM A -- {a.instrument_id} art. {a.article} ({a.heading})\n"
            f"Provision text: {a.text}\n"
            f"Extracted: deontic={a.norm['deontic']}, action={a.norm.get('action')!r}, "
            f"conditions={a.norm.get('conditions')}\n\n"
            f"NORM B -- {b.instrument_id} art. {b.article} ({b.heading})\n"
            f"Provision text: {b.text}\n"
            f"Extracted: deontic={b.norm['deontic']}, action={b.norm.get('action')!r}, "
            f"conditions={b.norm.get('conditions')}\n"
            + def_context
        )

    verdict = None
    issue = None
    for _attempt in range(2):  # one retry, with the specific inconsistency named
        resp = client.responses.parse(
            model=model, input=build_prompt(issue), text_format=DutyConflictVerdict,
            temperature=0, reasoning={"effort": "none"},
        )
        verdict = resp.output_parsed
        issue = _consistency_issue(verdict, a.text, b.text)
        if issue is None:
            break
    else:
        return AdjudicationResult(verdict, issue)  # still inconsistent after retry -- kept, flagged

    if verdict.verdict != "CONTRADICTION":
        return AdjudicationResult(verdict, None)

    challenge = challenge_verdict(client, model, a, b, verdict)
    return AdjudicationResult(verdict, None, challenge)


def challenge_verdict(client, model: str, a: NormRecord, b: NormRecord,
                       verdict: DutyConflictVerdict) -> ChallengeVerdict:
    """Independent adversarial second pass -- see ChallengeVerdict's own note on why
    this catches a different failure mode than _consistency_issue. Deliberately does
    NOT show the first pass's own reasoning/criterion_fired text, only its bottom-line
    claim plus the same source material -- the point is to re-derive an assessment from
    the evidence, not to critique a transcript."""
    prompt = (
        "A prior analysis claims the following two legal norms genuinely CONTRADICT "
        "each other. Critically test that claim against the source text below. Try to "
        "DISPROVE it: look for mismatched actors or roles, non-overlapping versions or "
        "scope, an omitted exception or coordination clause, discretionary ('may') "
        "language read as mandatory, a compliant alternative that satisfies both, or an "
        "unsupported factual assumption. Do not approve the claim simply because it "
        "sounds confident -- your own agreement is not independent legal validation "
        "unless you can point to a check you actually performed against the text.\n\n"
        f"CLAIMED CONTRADICTION: {a.instrument_id} art. {a.article} requires/prohibits "
        f"{verdict.requirement_a!r}; {b.instrument_id} art. {b.article} requires/prohibits "
        f"{verdict.requirement_b!r}; claimed to be mutually exclusive for actor "
        f"{verdict.shared_actor!r} under circumstance {verdict.shared_circumstance!r}.\n\n"
        f"NORM A -- {a.instrument_id} art. {a.article} ({a.heading})\n"
        f"Provision text: {a.text}\n\n"
        f"NORM B -- {b.instrument_id} art. {b.article} ({b.heading})\n"
        f"Provision text: {b.text}\n\n"
        "Return whether the claim survives your challenge (survives), specific "
        "corrections with source spans if it does not, which reconciliation checks you "
        "tried and passed, and any remaining uncertainty."
    )
    resp = client.responses.parse(
        model=model, input=prompt, text_format=ChallengeVerdict,
        temperature=0, reasoning={"effort": "none"},
    )
    return resp.output_parsed


# ---------------------------------------------------------------------------
# Driver -- produces Finding records (a scoped-down version of Part 6's schema: exact
# character offsets are skipped for now, verbatim evidence text is kept instead, which
# is still fully checkable, just not pre-computed to a numeric range).
# ---------------------------------------------------------------------------

CONFIDENCE_LABELS = {1: "High", 2: "Medium", 3: "Low"}


def _incompatibility_established(subtype: str, deterministic_result: Optional[dict],
                                  llm: Optional[DutyConflictVerdict],
                                  needs_recheck_reason: Optional[str],
                                  challenge: Optional["ChallengeVerdict"] = None) -> Optional[bool]:
    """DIFFERENCE (a candidate exists at all, and a deterministic/AI comparison ran) is
    not the same claim as INCOMPATIBILITY (complying with one makes the other difficult
    or impossible) -- kept as an explicit, separate field (2026-09-24 fix) rather than
    letting confidence_tier alone imply it, after finding standard_collision's own
    "difference" was being read as a confirmed contradiction when it structurally can't
    be one (see route_subtype's note). True/False only when the comparison genuinely
    settles it; None when it's still an open question for a human."""
    if subtype == "standard_collision":
        return not bool((deterministic_result or {}).get("joint_compliance_possible"))
    if subtype in ("threshold_mismatch", "competence_competition"):
        return True  # genuine by construction once these sub-types fire at all -- see
        # route_subtype's own notes: a fine ceiling difference or a real competing
        # authority claim isn't something you can "satisfy the stricter one" out of.
    if needs_recheck_reason is not None:
        return None
    if challenge is not None and not challenge.survives:
        # A failed challenge doesn't PROVE compatibility -- it means the CONTRADICTION
        # claim wasn't solid enough to survive an independent adversarial re-check, so
        # this stays an open question, same treatment as needs_recheck.
        return None
    if llm is not None:
        if llm.verdict == "CONTRADICTION":
            return True
        if llm.verdict == "INSUFFICIENT_EVIDENCE":
            return None
    return None


def build_finding(a: NormRecord, b: NormRecord, candidate: dict, subtype: str,
                   deterministic_result: Optional[dict], llm: Optional[DutyConflictVerdict],
                   tier: int, tier_reasons: list[str],
                   needs_recheck_reason: Optional[str] = None,
                   unresolved_exception: Optional[dict] = None,
                   challenge: Optional[ChallengeVerdict] = None) -> dict:
    status = "candidate"
    if needs_recheck_reason:
        status = "needs_recheck"
    elif challenge is not None and not challenge.survives:
        status = "challenge_failed"
    return {
        # Stable, content-derived id (2026-09-24 fix, item 12): a sequential counter
        # tied to len(findings) -- or worse, completion order under ThreadPoolExecutor,
        # which is nondeterministic -- meant the SAME real-world finding could get a
        # different id on every run, breaking any reference to it (a human's review
        # note, a link from a report) the moment the pipeline was re-run.
        "finding_id": f"F-C1-{_pair_id(a, b)}",
        "category": "contradiction",
        "subtype": subtype,
        # needs_recheck: the model's own verdict was internally inconsistent even after
        # a retry. challenge_failed (2026-09-24): the verdict was self-consistent, but
        # an independent adversarial second pass found a specific problem with it.
        # Neither is presented as a supported CONTRADICTION or discarded as NOT_A_CONFLICT.
        "status": status,
        "confidence_tier": tier,
        "confidence_label": CONFIDENCE_LABELS[tier],
        "confidence_reasons": tier_reasons,
        # True: complying with one genuinely makes the other difficult/impossible.
        # False: a difference was found but it's compatible (see standard_collision).
        # None: genuinely unresolved -- worth a human's eyes, not yet settled either way.
        "incompatibility_established": _incompatibility_established(
            subtype, deterministic_result, llm, needs_recheck_reason, challenge),
        "challenge": challenge.model_dump() if challenge else None,
        "provisions": [
            # norm_index AND paragraph_index (2026-09-24, alongside the _norm_id fix
            # above): a paragraph can hold more than one extracted norm, so
            # paragraph_number alone doesn't tell a reviewer -- or C2's own cross-check
            # against this file -- WHICH norm at that paragraph is meant. norm_index
            # alone isn't quite enough either: the one confirmed real case in this
            # corpus (AI Act art. 73, two separate lid-divs both DISPLAYING "11") has
            # norm_index=None on both sides, and only paragraph_index (the actual list
            # position) tells them apart. Storing both is what lets a future exact
            # match reuse c1._norm_id()'s own full identity, not a partial one.
            {"uid": a.graph_uid, "instrument_id": a.instrument_id, "article": a.article,
             "paragraph_number": a.norm.get("number"), "norm_index": a.norm.get("norm_index"),
             "paragraph_index": a.norm.get("paragraph_index")},
            {"uid": b.graph_uid, "instrument_id": b.instrument_id, "article": b.article,
             "paragraph_number": b.norm.get("number"), "norm_index": b.norm.get("norm_index"),
             "paragraph_index": b.norm.get("paragraph_index")},
        ],
        "criteria_fired": [c for c, hit in [("graph_adjacency", candidate["graph_hit"]),
                                             ("trigger_keyword_match", candidate["trigger_hit"]),
                                             ("semantic_similarity", candidate.get("semantic_hit")),
                                             ("concept_pair_match", candidate.get("concept_hit"))] if hit],
        "deterministic_result": deterministic_result,
        "llm_adjudication": llm.model_dump() if llm else None,
        "needs_recheck_reason": needs_recheck_reason,
        "resolution_filter": {"checked": True, "deference_found": None, "result": "unresolved"},
        # A THIRD provision (e.g. Cbw art. 31) conditionally disapplies one side of this
        # pair for entities already meeting an equivalent sector-specific EU requirement
        # (2026-09-24, item 7) -- NOT auto-suppressed, since that equivalence is a
        # regulatory judgement this pipeline can't make from text alone; surfaced so a
        # reviewer sees it rather than treating this as an unqualified contradiction.
        "unresolved_exception": unresolved_exception,
        "extracted_on": date.today().isoformat(),
        "human_verified": False,
        "report_eligible": False,
    }


def _prepare_candidate(c: dict, exceptions: list[dict]) -> dict:
    """Phase 1 -- all the local, no-network work for one candidate pair: recipient
    matching, the resolution filter, and sub-type routing. Split out from the LLM call
    so that only the pairs that actually need AI (the duty_conflict ones) go anywhere
    near the network, and so those calls can be fired concurrently instead of one at a
    time."""
    a, b = c["a"], c["b"]
    rk = recipient_match_kind(a, b)
    # No vertical-pair exclusion (2026-09-23 decision, reversing the earlier design): a
    # Dutch transposition that actually CONTRADICTS its own EU parent, rather than just
    # implementing it with an acceptable stricter/local variation, is exactly the kind
    # of problem this project exists to catch -- every EU<->Dutch-implementation pair
    # goes through the full pipeline like any other pair, no special-casing.
    suppressing_text = check_deference_suppresses(a, b)
    unresolved_exception = check_conditional_exception(a, b, exceptions)
    subtype, det_result = route_subtype(a, b, rk)
    return {"a": a, "b": b, "c": c, "rk": rk, "subtype": subtype,
            "det_result": det_result, "suppressing_text": suppressing_text,
            "unresolved_exception": unresolved_exception}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--dry-run", action="store_true",
                     help="candidate generation only, no duty_conflict LLM calls -- "
                          "embeddings still run, since they're cheap (fractions of a "
                          "cent) and this is exactly how to check the new candidate "
                          "count before spending on adjudication")
    ap.add_argument("--concurrency", type=int, default=8,
                     help="parallel duty_conflict LLM adjudication calls -- these are "
                          "independent network requests, not local computation, so this "
                          "is the actual lever for run time, not local CPU/GPU")
    ap.add_argument("--semantic-k", type=int, default=8,
                     help="cross-instrument nearest-neighbour budget per norm for the "
                          "semantic candidate signal (see similarity.py)")
    ap.add_argument("--semantic-k-within", type=int, default=3,
                     help="within-instrument nearest-neighbour budget per norm -- "
                          "separate and smaller than --semantic-k (2026-09-24, item 9): "
                          "same-instrument text tends to sit closer in embedding space "
                          "(shared drafting boilerplate) than a genuinely useful cross-"
                          "instrument match, so it gets its own, tighter budget rather "
                          "than crowding cross-instrument neighbours out of a shared one")
    ap.add_argument("--no-semantic", action="store_true",
                     help="disable the embeddings-based candidate signal, e.g. to "
                          "reproduce the earlier graph+keyword-only behaviour")
    ap.add_argument("--sample-half", choices=["A", "B"], default=None,
                     help="adjudicate only a stratified ~50%% sample of the AI-judged "
                          "pairs (duty_conflict/deontic_polarity_conflict only -- the "
                          "free deterministic sub-types always run in full). Split is a "
                          "stable hash of each pair's own id, not list order, so it's "
                          "balanced across every instrument-pair combination rather than "
                          "systematically covering some and skipping others; 'A' and 'B' "
                          "are always complementary and non-overlapping across separate "
                          "runs, so running B later (via the adjudication cache) never "
                          "re-touches or re-pays for anything A already covered")
    ap.add_argument("--priority-only", action="store_true",
                     help="adjudicate only candidates driven by today's specific fixes: "
                          "a concept-pair signal hit, or a candidate involving a norm "
                          "extracted/recovered today (extracted_on == today) -- a small, "
                          "high-value subset to test whether today's fixes surface "
                          "anything real before spending on the full remaining batch")
    args = ap.parse_args()

    from openai import OpenAI
    client = OpenAI()  # needed even in --dry-run: embeddings are cheap, adjudication isn't

    print("Loading norms and the citation graph...", flush=True)
    records = load_all_norm_records()
    g = nx.read_gexf(DATA / "graph.gexf")
    print(f"  {len(records)} eligible norms (deontic in {ELIGIBLE_DEONTICS}, addressee_type known)", flush=True)

    semantic_pairs = None
    if not args.no_semantic:
        import similarity
        print(f"  embedding {len(records)} norms for semantic candidate matching "
              f"(model: {similarity.EMBEDDING_MODEL}, top-{args.semantic_k} cross-"
              f"instrument, top-{args.semantic_k_within} within-instrument)...", flush=True)
        semantic_pairs = similarity.semantic_candidate_pairs(
            client, [r.text for r in records], k=args.semantic_k,
            groups=[r.instrument_id for r in records], k_within=args.semantic_k_within)
        print(f"  {len(semantic_pairs)} semantic-similarity index pair(s) found", flush=True)

    exceptions = load_conditional_exceptions()
    print(f"  {len(exceptions)} conditional-exception rule(s) loaded (see "
          f"load_conditional_exceptions -- currently just the 'artikelen X tot en met "
          f"Y' pattern, item 7's scoped starting point)", flush=True)

    candidates = generate_candidates(records, g, semantic_pairs)
    print(f"  {len(candidates)} candidate pair(s) after addressee_type gate + "
          f"(graph adjacency OR trigger-keyword match OR semantic similarity)", flush=True)

    # threshold_mismatch joins standard_collision/competence_competition as
    # deterministic (arithmetic, or a direct lookup); deontic_polarity_conflict still
    # needs AI confirmation alongside duty_conflict -- word-overlap narrows down
    # "plausibly the same act", it doesn't settle it.
    AI_JUDGED_SUBTYPES = ("duty_conflict", "deontic_polarity_conflict")
    prepared = [p for p in (_prepare_candidate(c, exceptions) for c in candidates) if p["subtype"] is not None]
    needs_llm = [p for p in prepared if p["subtype"] in AI_JUDGED_SUBTYPES]
    no_llm = [p for p in prepared if p["subtype"] not in AI_JUDGED_SUBTYPES]
    print(f"  {len(no_llm)} resolved deterministically (no AI needed), "
          f"{len(needs_llm)} need AI adjudication ({'/'.join(AI_JUDGED_SUBTYPES)})", flush=True)

    if args.sample_half:
        target = 0 if args.sample_half == "A" else 1
        full_count = len(needs_llm)
        needs_llm = [p for p in needs_llm if int(_pair_id(p["a"], p["b"]), 16) % 2 == target]
        print(f"  --sample-half {args.sample_half}: {len(needs_llm)} of {full_count} "
              f"AI-judged pairs selected (stratified by pair-id hash, not list order)", flush=True)

    if args.priority_only:
        # concept_hit ONLY (2026-09-24 fix): "extracted_on == today" was tried first and
        # dropped -- checked directly, it matched nearly the WHOLE corpus, since this
        # entire session's extraction happened on the same calendar day by the system
        # clock, not just today's targeted recovery work. concept_hit is the one signal
        # that cleanly, uniquely identifies candidates that exist BECAUSE of today's new
        # discovery mechanism (the erase/retain, disclose/withhold, suspend/continuity
        # families) -- exactly the AG3/AD2-style cross-domain pairs this was built for.
        full_count = len(needs_llm)
        needs_llm = [p for p in needs_llm if p["c"].get("concept_hit")]
        print(f"  --priority-only: {len(needs_llm)} of {full_count} AI-judged pairs selected "
              f"(concept-pair signal hit)", flush=True)

    findings = []
    suppressed = []
    # Dry-run writes to its own file (2026-09-24 fix, item 12): the old code wrote
    # deterministic findings straight to the production findings_c1.json even under
    # --dry-run -- a dry run right after a real run could silently overwrite genuine
    # results with a partial (no-AI) preview, with nothing to tell them apart.
    out_path = DATA / ("findings_c1_dryrun_preview.json" if args.dry_run else "findings_c1.json")

    def _save():
        out_path.write_text(json.dumps({"findings": findings, "suppressed": suppressed},
                                        ensure_ascii=False, indent=1), encoding="utf-8")

    def finalize(p: dict, llm_verdict: Optional[DutyConflictVerdict] = None,
                 needs_recheck_reason: Optional[str] = None,
                 challenge: Optional[ChallengeVerdict] = None):
        tier, tier_reasons = compute_confidence_tier(p["a"], p["b"], p["subtype"], p["rk"],
                                                       p.get("det_result"), llm_verdict,
                                                       needs_recheck_reason, challenge)
        finding = build_finding(p["a"], p["b"], p["c"], p["subtype"], p.get("det_result"),
                                 llm_verdict, tier, tier_reasons, needs_recheck_reason,
                                 p.get("unresolved_exception"), challenge)
        if p["suppressing_text"]:
            finding["status"] = "managed"
            finding["resolution_filter"] = {"checked": True, "deference_found": p["suppressing_text"],
                                             "result": "suppressed"}
            suppressed.append(finding)
        else:
            findings.append(finding)
        _save()  # crash-safe: written after every single finding, not just at the end

    for p in no_llm:
        finalize(p)
        a, b = p["a"], p["b"]
        print(f"  {a.instrument_id} art.{a.article} <-> {b.instrument_id} art.{b.article} "
              f"-- {p['subtype']} [{CONFIDENCE_LABELS[compute_confidence_tier(a, b, p['subtype'], p['rk'], p.get('det_result'), None)[0]]}]",
              flush=True)
    _save()  # written even if no_llm was empty -- a genuine zero-finding run is recorded
             # explicitly rather than leaving a stale earlier run's output looking current.

    # Adjudication cache (2026-09-24 fix, item 12): EVERY verdict this model has ever
    # returned for this exact pair -- CONTRADICTION, NOT_A_CONFLICT and
    # INSUFFICIENT_EVIDENCE alike -- is persisted here, not just the ones that became a
    # finding. A re-run (after a code fix, or just resuming one that got interrupted)
    # never re-pays for a pair already checked, and NOT_A_CONFLICT verdicts -- previously
    # thrown away after being printed once -- are now real, reviewable data for exactly
    # the kind of negative-sampling recall check item 13 asks for.
    cache_path = DATA / "c1_adjudication_cache.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    cache_lock = threading.Lock()

    def _save_cache():
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")

    def _cache_key(p: dict) -> str:
        return f"{args.model}::{_pair_id(p['a'], p['b'])}"

    def _store_cache(p: dict, result: AdjudicationResult):
        with cache_lock:
            cache[_cache_key(p)] = {
                "verdict": result.verdict.model_dump(),
                "needs_recheck_reason": result.needs_recheck_reason,
                "challenge": result.challenge.model_dump() if result.challenge else None,
            }
            _save_cache()

    def _handle_result(p: dict, result: AdjudicationResult, label: str, tag: str):
        verdict = result.verdict
        if result.needs_recheck_reason:
            # Internally inconsistent even after a retry -- never silently trusted as
            # CONTRADICTION and never silently dropped as NOT_A_CONFLICT either; kept as
            # a visible, Tier-3, needs_recheck finding (see build_finding).
            finalize(p, verdict, result.needs_recheck_reason)
            print(f"  {tag}{label} ({p['subtype']}) -- "
                  f"NEEDS_RECHECK ({result.needs_recheck_reason}) [Low]", flush=True)
            return
        if verdict.verdict == "NOT_A_CONFLICT":
            # A confident negative -- the model looked closely and these are fine
            # together. Nothing to surface as a finding, but still cached above.
            print(f"  {tag}{label} -- NOT_A_CONFLICT", flush=True)
            return
        if result.challenge is not None and not result.challenge.survives:
            # Self-consistent CONTRADICTION, but an independent adversarial second pass
            # found a specific problem with it -- kept visible, never presented as
            # supported (see build_finding's "challenge_failed" status).
            finalize(p, verdict, None, result.challenge)
            print(f"  {tag}{label} ({p['subtype']}) -- CONTRADICTION but CHALLENGE_FAILED "
                  f"({result.challenge.corrections}) [Low]", flush=True)
            return
        # CONTRADICTION (survived challenge, or challenge not applicable) and
        # INSUFFICIENT_EVIDENCE both become findings -- the latter at Tier 3 (Low) via
        # compute_confidence_tier, since "I couldn't tell" is real uncertainty worth a
        # human's eyes, not the same as a confirmed non-conflict.
        finalize(p, verdict, None, result.challenge)
        tier = compute_confidence_tier(p["a"], p["b"], p["subtype"], p["rk"], p.get("det_result"),
                                        verdict, None, result.challenge)[0]
        print(f"  {tag}{label} ({p['subtype']}) -- {verdict.verdict} [{CONFIDENCE_LABELS[tier]}]", flush=True)

    cached_p, to_call_p = [], []
    for p in needs_llm:
        (cached_p if _cache_key(p) in cache else to_call_p).append(p)

    if args.dry_run:
        print(f"\n[dry-run] {len(needs_llm)} duty_conflict/deontic_polarity_conflict pair(s) "
              f"would be sent to the AI ({len(cached_p)} already cached from a prior run, "
              f"{len(to_call_p)} genuinely new) -- no calls made.", flush=True)
        return

    # Backward-compat defaults for cache entries written before compliant_alternative/
    # outcome_changing_facts existed (2026-09-24 fix): confirmed as a real crash, not
    # hypothetical -- batch A's cache entries predate this schema addition, and
    # reconstructing DutyConflictVerdict from one of them raised a Pydantic
    # ValidationError ("Field required") the moment a --priority-only run tried to
    # replay a cached result. Deliberately fixed HERE, not by giving the Pydantic
    # fields a Python-side default -- that could change how the SDK derives the JSON
    # schema sent to the live structured-outputs API, which is a different, riskier
    # change than just tolerating an old cache shape on the read path.
    _VERDICT_FIELD_DEFAULTS = {"compliant_alternative": None, "outcome_changing_facts": None}

    for p in cached_p:
        a, b = p["a"], p["b"]
        label = f"{a.instrument_id} art.{a.article} <-> {b.instrument_id} art.{b.article}"
        entry = cache[_cache_key(p)]
        challenge = ChallengeVerdict(**entry["challenge"]) if entry.get("challenge") else None
        verdict_dict = {**_VERDICT_FIELD_DEFAULTS, **entry["verdict"]}
        result = AdjudicationResult(DutyConflictVerdict(**verdict_dict), entry["needs_recheck_reason"], challenge)
        _handle_result(p, result, label, tag="[cached] ")

    print(f"  adjudicating {len(to_call_p)} duty-conflict pair(s) with "
          f"{args.concurrency} concurrent calls ({len(cached_p)} skipped -- already cached)...",
          flush=True)
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        future_to_p = {ex.submit(adjudicate_duty_conflict, client, args.model, p["a"], p["b"]): p
                       for p in to_call_p}
        for done, fut in enumerate(as_completed(future_to_p), 1):
            p = future_to_p[fut]
            a, b = p["a"], p["b"]
            label = f"{a.instrument_id} art.{a.article} <-> {b.instrument_id} art.{b.article}"
            try:
                result: AdjudicationResult = fut.result()
            except Exception as e:
                print(f"  [{done}/{len(to_call_p)}] {label} -- ERROR: {e}", flush=True)
                continue
            _store_cache(p, result)
            _handle_result(p, result, label, tag=f"[{done}/{len(to_call_p)}] ")

    print(f"\n{len(findings)} candidate finding(s), {len(suppressed)} suppressed by the "
          f"resolution filter -> {out_path.relative_to(ROOT)}", flush=True)


if __name__ == "__main__":
    main()
