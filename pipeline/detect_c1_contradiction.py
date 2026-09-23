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
import json
import re
import sys
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
            paragraphs = {str(para["number"]): para["text"] for para in (p.get("paragraphs") or [])}

            for norm in p.get("norms", []):
                if norm["deontic"] not in ELIGIBLE_DEONTICS:
                    continue
                if norm.get("addressee_type") is None:
                    continue
                text = paragraphs.get(str(norm.get("number")), p.get("text", ""))
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
    embedding similarity happens to be weak."""
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
        semantic_hit = cross_instrument and semantic_pairs is not None and (i, j) in semantic_pairs
        if not (graph_hit or trigger_hit or semantic_hit):
            continue
        candidates.append({"a": a, "b": b, "graph_hit": graph_hit,
                            "trigger_hit": trigger_hit, "semantic_hit": semantic_hit})
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
}
_LEADING_ARTICLE_RE = re.compile(r"^(de|het|haar|zijn|een|hun)\s+", re.I)
# Qualifying adjectives that precede a generic-authority phrase without changing which
# generic role it names -- "nationale bevoegde autoriteiten" and "bevoegde autoriteit"
# are the same generic reference for matching purposes, just with different qualifiers.
_RECIPIENT_QUALIFIER_RE = re.compile(r"^(nationale|relevante|andere|betrokken)\s+", re.I)


def _is_authority_term(term: str) -> bool:
    """Substring, not exact-match (2026-09-23 fix): the first false positive this
    caught used "een essentiële entiteit", which doesn't equal "entiteit" but clearly
    names one. Word-for-word equality was never going to catch every phrasing."""
    norm = _normalize_recipient(term)
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

_EURO_RE = re.compile(r"([\d][\d.,]*)\s*euro", re.I)
_PERCENT_TURNOVER_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*%\s*van\s+de.*?jaaromzet", re.I)


def _parse_threshold(text: str) -> Optional[tuple[str, float]]:
    m = _EURO_RE.search(text)
    if m:
        return "euro", float(m.group(1).replace(".", "").replace(",", "."))
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
        return "standard_collision", {"deadline_a_hours": ha, "deadline_b_hours": hb, "delta_hours": abs(ha - hb)}

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

    if a.norm["deontic"] == "COMPETENCE" and b.norm["deontic"] == "COMPETENCE" and rk == "different":
        # Here "different" is the SIGNAL, not the disqualifier: two bodies genuinely
        # claiming the same power is the competition. "same_named"/"generic_unresolved"
        # would mean they actually agree on who holds it -- not a real competition.
        # Filtered to terms that could plausibly BE an authority, AND required to be
        # about the same underlying matter -- see NON_AUTHORITY_RECIPIENT_TERMS and
        # _same_function's notes on why both checks are needed, and _same_function's
        # note on the one known case neither check catches.
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
                             llm_verdict: Optional["DutyConflictVerdict"]) -> tuple[int, list[str]]:
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

    if subtype in ("standard_collision", "competence_competition"):
        if rk == "generic_unresolved":
            reasons.append("recipients share only a generic role name (e.g. 'competent authority'), "
                            "not a specific named body -- the two may or may not be the same authority")
        if rk in ("same_named", "different") and clean_extraction:
            # same_named -> standard_collision's confirmed-same-recipient case;
            # different -> competence_competition's confirmed-distinct-bodies case.
            return 1, ["deterministic comparison (arithmetic or a direct lookup, no AI)",
                       "recipient identity confirmed by name", "both norms cleanly extracted"]
        return (2 if clean_extraction else 3), reasons or ["deterministic comparison, one weaker signal"]

    reasons.append("resolved by AI judgement (duty conflict), not a deterministic computation -- "
                    "never eligible for Tier 1 regardless of how confident the model was")
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
# LLM adjudication -- ONLY for duty_conflict, the one sub-type that's a genuine
# judgement call. Same Structured Outputs discipline as extract_norms.py: strict
# schema, verbatim-checked evidence spans, no free prose.
# ---------------------------------------------------------------------------

class DutyConflictVerdict(BaseModel):
    verdict: Literal["CONTRADICTION", "NOT_A_CONFLICT", "INSUFFICIENT_EVIDENCE"]
    criterion_fired: str
    evidence_span_a: Optional[str]  # must be verbatim in norm A's provision text
    evidence_span_b: Optional[str]  # must be verbatim in norm B's provision text
    confidence: float


def adjudicate_duty_conflict(client, model: str, a: NormRecord, b: NormRecord) -> DutyConflictVerdict:
    prompt = (
        "Two legal norms, extracted from Dutch/EU digital-law legislation, both impose an "
        "obligation, prohibition or competence on the same category of actor and share a "
        "similar triggering situation. Decide whether they genuinely CONTRADICT each other "
        "(one requires what the other forbids, or they impose mutually exclusive duties on "
        "the same actor for the same trigger) -- not merely whether they are both about a "
        "similar topic. If a provision's own text already reconciles them (e.g. one explicitly "
        "defers to the other), that is not a contradiction. Quote the specific evidence_span_a/b "
        "verbatim from the provision text given below; if the text doesn't support a firm verdict "
        "either way, return INSUFFICIENT_EVIDENCE rather than guessing.\n\n"
        f"NORM A -- {a.instrument_id} art. {a.article} ({a.heading})\n"
        f"Provision text: {a.text}\n"
        f"Extracted: deontic={a.norm['deontic']}, action={a.norm.get('action')!r}, "
        f"conditions={a.norm.get('conditions')}\n\n"
        f"NORM B -- {b.instrument_id} art. {b.article} ({b.heading})\n"
        f"Provision text: {b.text}\n"
        f"Extracted: deontic={b.norm['deontic']}, action={b.norm.get('action')!r}, "
        f"conditions={b.norm.get('conditions')}\n"
    )
    resp = client.responses.parse(
        model=model, input=prompt, text_format=DutyConflictVerdict,
        temperature=0, reasoning={"effort": "none"},
    )
    return resp.output_parsed


# ---------------------------------------------------------------------------
# Driver -- produces Finding records (a scoped-down version of Part 6's schema: exact
# character offsets are skipped for now, verbatim evidence text is kept instead, which
# is still fully checkable, just not pre-computed to a numeric range).
# ---------------------------------------------------------------------------

CONFIDENCE_LABELS = {1: "High", 2: "Medium", 3: "Low"}


def build_finding(a: NormRecord, b: NormRecord, candidate: dict, subtype: str,
                   deterministic_result: Optional[dict], llm: Optional[DutyConflictVerdict],
                   tier: int, tier_reasons: list[str], idx: int) -> dict:
    return {
        "finding_id": f"F-C1-{idx:04d}",
        "category": "contradiction",
        "subtype": subtype,
        "status": "candidate",
        "confidence_tier": tier,
        "confidence_label": CONFIDENCE_LABELS[tier],
        "confidence_reasons": tier_reasons,
        "provisions": [
            {"uid": a.graph_uid, "instrument_id": a.instrument_id, "article": a.article,
             "paragraph_number": a.norm.get("number")},
            {"uid": b.graph_uid, "instrument_id": b.instrument_id, "article": b.article,
             "paragraph_number": b.norm.get("number")},
        ],
        "criteria_fired": [c for c, hit in [("graph_adjacency", candidate["graph_hit"]),
                                             ("trigger_keyword_match", candidate["trigger_hit"]),
                                             ("semantic_similarity", candidate.get("semantic_hit"))] if hit],
        "deterministic_result": deterministic_result,
        "llm_adjudication": llm.model_dump() if llm else None,
        "resolution_filter": {"checked": True, "deference_found": None, "result": "unresolved"},
        "extracted_on": date.today().isoformat(),
        "human_verified": False,
        "report_eligible": False,
    }


def _prepare_candidate(c: dict) -> dict:
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
    subtype, det_result = route_subtype(a, b, rk)
    return {"a": a, "b": b, "c": c, "rk": rk, "subtype": subtype,
            "det_result": det_result, "suppressing_text": suppressing_text}


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
                     help="how many nearest neighbours per norm count as a semantic "
                          "candidate signal (see similarity.py)")
    ap.add_argument("--no-semantic", action="store_true",
                     help="disable the embeddings-based candidate signal, e.g. to "
                          "reproduce the earlier graph+keyword-only behaviour")
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
              f"(model: {similarity.EMBEDDING_MODEL}, top-{args.semantic_k})...", flush=True)
        semantic_pairs = similarity.semantic_candidate_pairs(
            client, [r.text for r in records], k=args.semantic_k)
        print(f"  {len(semantic_pairs)} semantic-similarity index pair(s) found", flush=True)

    candidates = generate_candidates(records, g, semantic_pairs)
    print(f"  {len(candidates)} candidate pair(s) after addressee_type gate + "
          f"(graph adjacency OR trigger-keyword match OR semantic similarity)", flush=True)

    # threshold_mismatch joins standard_collision/competence_competition as
    # deterministic (arithmetic, or a direct lookup); deontic_polarity_conflict still
    # needs AI confirmation alongside duty_conflict -- word-overlap narrows down
    # "plausibly the same act", it doesn't settle it.
    AI_JUDGED_SUBTYPES = ("duty_conflict", "deontic_polarity_conflict")
    prepared = [p for p in (_prepare_candidate(c) for c in candidates) if p["subtype"] is not None]
    needs_llm = [p for p in prepared if p["subtype"] in AI_JUDGED_SUBTYPES]
    no_llm = [p for p in prepared if p["subtype"] not in AI_JUDGED_SUBTYPES]
    print(f"  {len(no_llm)} resolved deterministically (no AI needed), "
          f"{len(needs_llm)} need AI adjudication ({'/'.join(AI_JUDGED_SUBTYPES)})", flush=True)

    findings = []
    suppressed = []
    out_path = DATA / "findings_c1.json"

    def _save():
        out_path.write_text(json.dumps({"findings": findings, "suppressed": suppressed},
                                        ensure_ascii=False, indent=1), encoding="utf-8")

    def finalize(p: dict, llm_verdict: Optional[DutyConflictVerdict] = None):
        tier, tier_reasons = compute_confidence_tier(p["a"], p["b"], p["subtype"], p["rk"], llm_verdict)
        finding = build_finding(p["a"], p["b"], p["c"], p["subtype"], p.get("det_result"),
                                 llm_verdict, tier, tier_reasons, len(findings) + len(suppressed) + 1)
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
              f"-- {p['subtype']} [{CONFIDENCE_LABELS[compute_confidence_tier(a, b, p['subtype'], p['rk'], None)[0]]}]",
              flush=True)

    if args.dry_run:
        print(f"\n[dry-run] {len(needs_llm)} duty_conflict pair(s) would be sent to the AI "
              f"-- no calls made.", flush=True)
        return

    print(f"  adjudicating {len(needs_llm)} duty-conflict pair(s) with "
          f"{args.concurrency} concurrent calls...", flush=True)
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        future_to_p = {ex.submit(adjudicate_duty_conflict, client, args.model, p["a"], p["b"]): p
                       for p in needs_llm}
        for done, fut in enumerate(as_completed(future_to_p), 1):
            p = future_to_p[fut]
            a, b = p["a"], p["b"]
            label = f"{a.instrument_id} art.{a.article} <-> {b.instrument_id} art.{b.article}"
            try:
                verdict = fut.result()
            except Exception as e:
                print(f"  [{done}/{len(needs_llm)}] {label} -- ERROR: {e}", flush=True)
                continue
            if verdict.verdict == "NOT_A_CONFLICT":
                # A confident negative -- the model looked closely and these are fine
                # together. Genuinely nothing to surface, unlike INSUFFICIENT_EVIDENCE below.
                print(f"  [{done}/{len(needs_llm)}] {label} -- NOT_A_CONFLICT", flush=True)
                continue
            # CONTRADICTION and INSUFFICIENT_EVIDENCE both become findings -- the latter at
            # Tier 3 (Low) via compute_confidence_tier, since "I couldn't tell" is real
            # uncertainty worth a human's eyes, not the same as a confirmed non-conflict.
            finalize(p, verdict)
            tier = compute_confidence_tier(a, b, p["subtype"], p["rk"], verdict)[0]
            print(f"  [{done}/{len(needs_llm)}] {label} ({p['subtype']}) -- "
                  f"{verdict.verdict} [{CONFIDENCE_LABELS[tier]}]", flush=True)

    print(f"\n{len(findings)} candidate finding(s), {len(suppressed)} suppressed by the "
          f"resolution filter -> {out_path.relative_to(ROOT)}", flush=True)


if __name__ == "__main__":
    main()
