# -*- coding: utf-8 -*-
"""
Part 5, C2 -- Deduplication detection (architecture doc Part 5, "### C2 -- Deduplication").

Same overall shape as C1: cheap deterministic candidate generation -> deterministic
filters -> one LLM call PER CANDIDATE PAIR to confirm "substantially the same
compliance action" -> a structured Finding. Nothing here is a flag until a human
confirms it (`report_eligible` stays false on every record this script produces).

Pipeline: find candidate pairs of OBLIGATION norms by trigger_event similarity +
addressee_type match (embeddings, reusing similarity.py) -> deterministic filters (drop
a vertical EU<->NL-transposition pair, drop a pair whose deference already resolves it,
drop a pair C1 already produced a finding for) -> deterministic burden scoring (distinct
recipient/deadline/instrument counts) -> one LLM call per surviving PAIR to confirm
"substantially the same compliance action" -> a structured Finding.

2026-09-24, reworked from an earlier cluster-based design (one call per connected
component of 2+ similar norms) to pairwise (one call per candidate pair), on explicit
request: a single cluster-level verdict could paper over one member of a larger group
being genuinely different from the others, and pairwise gives each specific relationship
its own checked verdict instead. This does mean C2 no longer has the cost advantage the
cluster design had over C1 (a handful of group calls vs. thousands of pairwise ones) --
on the current corpus the candidate-pair count is still small (checked directly, well
under a hundred), so this remains cheap in practice, just not cheap BY DESIGN the way
the cluster version was.

Decisions made directly with the project owner (2026-09-24), rather than guessed at:

1. The architecture doc's burden score has 4 axes (recipient_body, deadline, format
   requirements, instrument count), but NO `format_requirement` field has ever been
   extracted onto any of the corpus's norms (checked directly: absent from the norm
   schema Stage 6 produces). Rather than guess at a proxy, this ships 3-of-4 axes now
   and reports `"format_requirements": null` explicitly in every burden score, so the
   gap is visible in the data instead of silently absent.
2. Runs over the WHOLE corpus in one pass, not a pilot on the incident-reporting anchor
   cluster first.
3. Only `deontic == "OBLIGATION"` norms are candidates. A duplicated COMPLIANCE BURDEN
   is what this category flags (per the architecture doc's own framing -- "confirm
   'substantially the same nature' for the COMPLIANCE ACTION"), and
   PROHIBITION/COMPETENCE norms don't carry a "do this" action to compare the same way.
4. Candidates are restricted to `addressee_type == REGULATED_ENTITY` (revised twice,
   2026-09-24, each time on checked evidence rather than a priori theory). First cut
   excluded only `EU_INSTITUTION`, after the first real run's only findings turned out
   to be Commission-internal legislative-drafting boilerplate. That still left
   `COMPETENT_AUTHORITY`/`MEMBER_STATE` in scope on the theory that the Ministry's own
   authorities could bear a genuine duplicate burden too -- but checked directly against
   the next full run's 19 findings, EVERY finding where both sides were
   `COMPETENT_AUTHORITY` was an inter-authority RELAY chain (one authority telling
   another about an incident it already received), not a duplicate burden, while every
   `REGULATED_ENTITY`-vs-`REGULATED_ENTITY` finding was substantively about the real
   compliance burden -- a clean, exceptionless split. See `ALLOWED_ADDRESSEE_TYPES`'s
   own note for the full reasoning.

Usage:
    python detect_c2_deduplication.py             # full run
    python detect_c2_deduplication.py --dry-run   # candidate generation + burden
                                                    # scoring only, no LLM calls
"""
import argparse
import json
import re
import sys
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, Optional

import networkx as nx
from dotenv import load_dotenv
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent))
import detect_c1_contradiction as c1
import similarity

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
load_dotenv(ROOT / ".env")

CONFIDENCE_LABELS = c1.CONFIDENCE_LABELS  # {1: "High", 2: "Medium", 3: "Low"} -- shared
                                           # tiering philosophy across C1/C2, see C1's
                                           # own note on why this is one mechanism, not
                                           # bespoke handling per category.

# Cache versioning (2026-09-24, per external review): the adjudication cache key was
# model + pair_id only, with no way to tell a verdict computed under an OLD prompt from
# one computed under the CURRENT prompt. Every C2 iteration tonight in fact changed the
# prompt (candidate scope, reference context) and relied on manually deleting the cache
# file before each run -- correct so far, but fragile and easy to forget. Bump this
# string whenever the prompt, the DuplicationVerdict schema, or what context gets
# injected changes; a version bump makes every old entry simply miss the cache (cheap: a
# fresh, correctly-computed call) rather than silently reusing a stale verdict.
C2_ADJUDICATION_VERSION = "v5_structured_duplicate_burden_schema"


def _norm_key(r: c1.NormRecord) -> str:
    """Delegates to c1._norm_id() directly (2026-09-24 fix, per external review) rather
    than maintaining a second, separate identity: a first version of this function used
    (instrument_id, article, paragraph_number, norm_index), which fixed the "multiple
    norms per paragraph" gap but NOT the one confirmed real edge case still in the
    corpus -- AI Act art. 73 has two separate paragraph-list positions both DISPLAYING
    "11", and norm_index is None on both, so norm_index alone doesn't disambiguate them
    either. c1._norm_id() already carries the full identity (instrument, article,
    paragraph_index, number, norm_index) that this needs."""
    return c1._norm_id(r)


def load_c1_labelled_pairs() -> set:
    """Pairs C1 already produced a FINDING for (i.e. actually surfaced to a reviewer) --
    per the spec, "C2 runs only on pairs C1 did not label." Deliberately reads only
    findings_c1.json's findings[] list, not its suppressed[] one: a pair C1 suppressed
    via its own deference resolution was never shown to a reviewer as anything, so it's
    not "labelled" in the sense this exclusion is protecting against.

    Rebuilds the SAME string _norm_id() would produce, from the stored provisions dict's
    fields -- all read via .get(), since findings written before today's fixes predate
    one or more of these fields and will come back None, matching a single-norm
    paragraph's own None values. That's the conservative side of the gap for any OLDER
    finding that predates a given field: it can only fail to EXCLUDE a candidate C2
    would otherwise have surfaced anyway, never wrongly exclude one."""
    path = DATA / "findings_c1.json"
    if not path.exists():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    labelled = set()
    for f in data.get("findings", []):
        provs = f.get("provisions", [])
        if len(provs) == 2:
            key = frozenset(
                f"{p['instrument_id']}:{p['article']}:{p.get('paragraph_index')}:"
                f"{p['paragraph_number']}:{p.get('norm_index')}"
                for p in provs
            )
            labelled.add(key)
    return labelled


# ---------------------------------------------------------------------------
# Vertical-pair filter -- an EU instrument and its own Dutch transposition being "the
# same requirement" is expected, not a finding. Note the asymmetry with C1: C1
# deliberately DROPPED its own vertical-pair filter on 2026-09-23, because a
# transposition that CONTRADICTS its parent is exactly what C1 hunts for. C2 wants the
# opposite filter, and that's correct: two instruments merely AGREEING because one
# implements the other isn't duplication -- it's the system working as designed.
#
# Exception added (2026-09-24, per external review, confirmed with real evidence before
# implementing): checked Bijlage 35 art. 5's actual extracted text directly -- it
# requires banks/trading venues/CSDs/CCPs to make the SAME DORA art. 19 notification
# "tevens" (ALSO) to the Dutch sectoral CSIRT designated under Cbw art. 16. That is not a
# restatement of the parent EU duty; it is a genuine additional reporting channel layered
# on top of it, and blanket-dropping every vertical pair was hiding exactly the kind of
# real added burden this category exists to surface. _adds_additional_channel() is a
# small, curated marker-word check (same spirit as NOTIFICATION_TRIGGER_KEYWORDS) rather
# than a general "how much did the transposition add" classifier -- narrow and directly
# evidenced by one confirmed real case, not built ahead of a need.
# ---------------------------------------------------------------------------

_ADDITIONAL_CHANNEL_MARKERS = {"tevens", "ook", "daarnaast", "bovendien"}


def _adds_additional_channel(a: c1.NormRecord, b: c1.NormRecord) -> bool:
    for r in (a, b):
        action = f" {(r.norm.get('action') or '').lower()} "
        if any(f" {m} " in action for m in _ADDITIONAL_CHANNEL_MARKERS):
            return True
    return False


def _is_vertical_pair(a: c1.NormRecord, b: c1.NormRecord, g: nx.MultiDiGraph) -> bool:
    if a.instrument_id == b.instrument_id:
        return False
    node_a, node_b = f"instrument:{a.instrument_id}", f"instrument:{b.instrument_id}"
    for u, v in ((node_a, node_b), (node_b, node_a)):
        if not g.has_edge(u, v):
            continue
        for attrs in g.get_edge_data(u, v).values():
            if str(attrs.get("kind", "")).startswith("implements"):
                return True
    return False


# ---------------------------------------------------------------------------
# Candidate generation: pairs, not clusters (2026-09-24 rework). Partition by
# addressee_type (hard requirement, same rule as C1), embed each OBLIGATION norm's
# trigger_event text and take semantic kNN neighbours (reusing similarity.py exactly as
# C1 does), then keep the ones scoring above a real similarity floor as candidate PAIRS
# -- no transitive grouping. Two norms with IDENTICAL trigger_event text (the literal
# "flag it if it's repeated twice" case) get cosine similarity 1.0 and are always each
# other's nearest neighbour, so this still directly catches exact repeats.
#
# MIN_TRIGGER_CONTENT_WORDS and CLUSTER_SIMILARITY_THRESHOLD guard against the same
# problem found in the earlier cluster-based version, still relevant here even without
# clustering itself: short, generic procedural trigger_event fragments Stage 6
# sometimes extracts ("Indien nodig"/"if necessary", "Daartoe"/"to that end") score
# cosine similarity 1.0 against every OTHER occurrence of the same generic fragment
# anywhere in the corpus, which would otherwise flood the candidate list with meaningless
# pairs. CLUSTER_SIMILARITY_THRESHOLD (0.85) was chosen by checking the real similarity
# distribution directly: p95 of the raw kNN edge scores was ~0.87, and manual inspection
# of edges at 0.60-0.65 (the median band) showed clearly unrelated provisions, while
# edges at >=0.85 were consistently genuine near-duplicate phrasing of the same real
# trigger event.
#
# MIN_TRIGGER_CONTENT_WORDS gates on trigger_event+action COMBINED, not trigger_event
# alone (2026-09-24 fix, per external review -- a real, confirmed bug, not a hypothetical
# one): AI Act art. 73(1) -- the single most important norm in the AI Act's entire
# incident-reporting article, the one every other paragraph of art. 73 refers back to --
# has trigger_event "ernstige incidenten" (2 content words), which the OLD trigger-only
# gate dropped before it ever had a chance to be compared against DORA/Cbw/GDPR/
# Telecommunicatiewet. Its action field, "melden ernstige incidenten", pushes the
# combined count to 3, clearing the gate -- and this is not a special case: it is
# EXACTLY the blob _trigger_keyword_hit() already uses internally (see its own
# docstring), so the eligibility gate was silently narrower than the signal it's gating.
# ---------------------------------------------------------------------------

MIN_TRIGGER_CONTENT_WORDS = 3
CLUSTER_SIMILARITY_THRESHOLD = 0.85


def _gate_text(r: c1.NormRecord) -> str:
    return f"{r.norm.get('trigger_event') or ''} {r.norm.get('action') or ''}"

# EU_INSTITUTION excluded from candidate generation entirely (2026-09-24, per external
# review): checked directly -- both findings the first real run actually produced had
# addressee_type EU_INSTITUTION (the Commission notifying Parliament/Council; the
# Commission consulting Member-State experts before a delegated act). Both are
# boilerplate legislative-drafting convention repeated because every EU regulation using
# delegated powers carries the same interinstitutional clause, not a real-world
# COMPLIANCE BURDEN anyone would want simplified -- this category is about duplicated
# burden, not duplicated legal text for its own sake.
#
# Narrowed to REGULATED_ENTITY only (2026-09-24, reversing an earlier decision on new
# evidence, per an external review): originally kept COMPETENT_AUTHORITY/MEMBER_STATE in
# scope on the theory that the Ministry's OWN authorities could bear a genuine duplicate
# burden too, not just companies. Checked directly against the first full run's 19
# findings, though: EVERY SINGLE ONE of the 8 findings where both sides were
# COMPETENT_AUTHORITY was an inter-authority RELAY chain (Cbw's CSIRT/central contact
# point telling another authority about an incident it already received) -- structurally
# different from duplication (the same actor doing the same thing twice under two laws),
# and arguably the kind of routing machinery the Digital Omnibus's proposed "single
# entry point" is meant to formalize, not the problem it exists to fix. Meanwhile ALL 9
# REGULATED_ENTITY-vs-REGULATED_ENTITY findings, including both Medium-confidence ones,
# were substantively about the real compliance burden. A clean, exceptionless split in
# the actual data -- simpler and better-evidenced than building new "duty role family"
# or "recipient role compatibility" classifiers to filter the relay chains out
# after the fact.
ALLOWED_ADDRESSEE_TYPES = {"REGULATED_ENTITY"}


def _cosine_scores(client, texts: list[str], pairs: set) -> list[tuple[int, int, float]]:
    import numpy as np
    embeddings = similarity.compute_embeddings(client, texts)
    vecs = {t: np.array(v) for t, v in embeddings.items()}

    def cos(i: int, j: int) -> float:
        va, vb = vecs[texts[i]], vecs[texts[j]]
        return float(va.dot(vb) / (np.linalg.norm(va) * np.linalg.norm(vb)))

    return [(i, j, cos(i, j)) for i, j in pairs]


def generate_pairs(client, records: list[c1.NormRecord], g: nx.MultiDiGraph,
                    c1_labelled: set, semantic_k: int = 6,
                    semantic_k_within: int = 3
                    ) -> tuple[list[tuple[c1.NormRecord, c1.NormRecord, dict]], dict, int]:
    # No longer requires a non-empty trigger_event up front (2026-09-24 fix, per external
    # review -- a real, confirmed bug): _gate_text() already covers trigger_event+action
    # combined, but this hard filter ran BEFORE _gate_text() ever got a chance, silently
    # dropping any norm whose trigger_event is empty regardless of how substantive its
    # action text is. Checked directly: 22 REGULATED_ENTITY incident/notification norms
    # were lost this way, including two from Telecommunicatiewet art. 11.3a itself (the
    # content of the very notification duty this module already treats as a real
    # finding). The word-count gate below is now the ONLY eligibility filter.
    all_obligations = [r for r in records if r.norm["deontic"] == "OBLIGATION"
                       and r.norm.get("addressee_type") in ALLOWED_ADDRESSEE_TYPES]
    obligations = [r for r in all_obligations
                   if len(c1._content_words(_gate_text(r))) >= MIN_TRIGGER_CONTENT_WORDS]
    # Embedding text falls back to action when trigger_event is empty (2026-09-24, a
    # direct consequence of the fix above, NOT the fuller composite-embedding change a
    # reviewer also proposed separately -- that stays out of scope for tonight). Purely
    # to avoid ever embedding a blank string: several of the 22 recovered norms above
    # have trigger_event=None, and an empty string has no meaningful embedding -- worse,
    # multiple blank strings would embed identically and register spurious perfect
    # similarity with each other, reintroducing exactly the chaining problem
    # MIN_TRIGGER_CONTENT_WORDS exists to prevent. Every norm surviving the gate above
    # has >=3 combined content words, so this fallback is never itself empty.
    texts = [r.norm.get("trigger_event") or r.norm.get("action") or "" for r in obligations]
    groups = [r.instrument_id for r in obligations]
    knn_pairs = similarity.semantic_candidate_pairs(client, texts, k=semantic_k,
                                                     groups=groups, k_within=semantic_k_within)
    scored_pairs = _cosine_scores(client, texts, knn_pairs)
    semantic_hit_idx = {(i, j) for i, j, score in scored_pairs if score >= CLUSTER_SIMILARITY_THRESHOLD}

    # Trigger-keyword-family signal, reused directly from C1 (2026-09-24 fix, found by
    # checking a real, official ground truth: the EU's own Digital Omnibus proposal
    # exists specifically because NIS2/GDPR/DORA/eIDAS/CER incident-notification duties
    # overlap. Checked directly why embedding similarity alone missed that anchor
    # cluster entirely: real cosine similarity between "nadat zij kennis heeft gekregen
    # van het significante incident" (Cbw/NIS2) and "een inbreuk in verband met
    # persoonsgegevens heeft plaatsgevonden" (GDPR) -- the textbook duplicative-burden
    # pair the Omnibus itself cites -- measured 0.30-0.63, nowhere near this module's
    # 0.85 similarity floor. C1 hit and solved the EXACT same problem for the EXACT same
    # anchor cluster (see detect_c1_contradiction.py's own module docstring, issue 1) by
    # adding NOTIFICATION_TRIGGER_KEYWORDS -- a small, curated "does this norm belong to
    # the incident/breach-notification family, independently of the other side's
    # vocabulary" check. C2 never reused it and relied on embedding similarity alone,
    # which structurally cannot bridge two different vocabularies for the same concept.
    # Union of both signals now, not a replacement -- a real embedding match is still
    # valid evidence on its own where it fires; the keyword signal exists to bridge
    # vocabulary gaps embeddings miss, exactly as it does in C1.
    from itertools import combinations
    keyword_hit_idx = set()
    for i, j in combinations(range(len(obligations)), 2):
        a, b = obligations[i], obligations[j]
        if a.instrument_id != b.instrument_id and c1._trigger_keyword_hit(a, b):
            keyword_hit_idx.add((i, j))

    candidate_idx = semantic_hit_idx | keyword_hit_idx

    drop_counts = {"too_generic_trigger": len(all_obligations) - len(obligations),
                   "addressee_mismatch": 0, "same_instrument": 0, "vertical": 0,
                   "deference": 0, "already_c1": 0}
    pairs = []
    for i, j in candidate_idx:
        a, b = obligations[i], obligations[j]
        if a.norm["addressee_type"] != b.norm["addressee_type"]:
            drop_counts["addressee_mismatch"] += 1
            continue
        if a.instrument_id == b.instrument_id:
            drop_counts["same_instrument"] += 1  # a same-law repeat isn't cross-law
            continue                              # duplication -- explicit per pair now
        if _is_vertical_pair(a, b, g) and not _adds_additional_channel(a, b):
            drop_counts["vertical"] += 1
            continue
        if c1.check_deference_suppresses(a, b):
            drop_counts["deference"] += 1
            continue
        if frozenset({_norm_key(a), _norm_key(b)}) in c1_labelled:
            drop_counts["already_c1"] += 1
            continue
        signals = {"semantic_hit": (i, j) in semantic_hit_idx, "keyword_hit": (i, j) in keyword_hit_idx}
        pairs.append((a, b, signals))

    return pairs, drop_counts, len(candidate_idx)


# ---------------------------------------------------------------------------
# Burden scoring -- deterministic, three of the spec's four axes (see module docstring,
# decision 1). Reported as explicit counts, never blended into one score, per the spec:
# "Report the counts, never a blended index."
# ---------------------------------------------------------------------------

def _burden_score(a: c1.NormRecord, b: c1.NormRecord) -> dict:
    recipients = set()
    for m in (a, b):
        for r in (m.norm.get("recipient_body") or []):
            norm = c1._normalize_recipient(r)
            if norm:
                recipients.add(norm)
    deadlines = {m.norm["deadline"].get("raw") for m in (a, b)
                 if m.norm.get("deadline") and m.norm["deadline"].get("raw")}
    return {
        "distinct_recipient_body": len(recipients),
        "distinct_deadline": len(deadlines),
        "distinct_instrument": len({a.instrument_id, b.instrument_id}),
        # Not extracted anywhere in the corpus (see module docstring, decision 1) --
        # reported explicitly as a known gap, not silently omitted or guessed at.
        "format_requirements": None,
    }


# ---------------------------------------------------------------------------
# Reference expansion (2026-09-24, per external review, confirmed necessary by direct
# evidence): several of AI Act art. 73's own paragraphs are deadline/procedure rules
# ABOUT the substantive duty in paragraph 1, not standalone duties -- para 2's action is
# literally "De in lid 1 bedoelde melding wordt gedaan" (the notification referred to in
# paragraph 1 is made...), para 3's is "de in lid 1 van dit artikel bedoelde melding
# onmiddellijk gedaan". Checked directly: ALL 5 of the first full run's
# INSUFFICIENT_EVIDENCE verdicts explicitly said so in their own reasoning -- e.g. "Norm
# B refers to a notification defined elsewhere and does not provide enough information."
# The model was right given what it was shown; it just wasn't shown paragraph 1.
#
# C1 already has a paragraph-reference detector (_referenced_paragraph_numbers,
# _DUTCH_ORDINALS) -- but built for Cbw's native Dutch drafting style ("eerste lid",
# ordinal WORD before "lid"). Checked directly: the AI Act's translated text instead uses
# a numeric style ("lid 1", digit after "lid"), which C1's regex does not match at all.
# _referenced_paragraph_numbers_c2 below unions both styles rather than replacing C1's
# (Cbw genuinely needs the ordinal-word form; this only adds the numeric form C1 never
# needed for its own purpose).
# ---------------------------------------------------------------------------

_NUMERIC_LID_RE = re.compile(r"\blid\s+(\d+)\b", re.I)


def _referenced_paragraph_numbers_c2(text: str) -> set:
    return {int(m.group(1)) for m in _NUMERIC_LID_RE.finditer(text)} | c1._referenced_paragraph_numbers(text)


def _build_paragraph_text_index(records: list[c1.NormRecord]) -> dict:
    """(instrument_id, article, paragraph_number) -> LIST of that paragraph's own
    text(s), not a single string (2026-09-24 fix, per external review, confirmed real
    but currently latent): AI Act art. 73 has two separate paragraph-list positions both
    DISPLAYING "11" with genuinely different text (the same corpus quirk
    detect_c1_contradiction.py's own module docstring already documents). A reference in
    Dutch legal text cites the DISPLAYED number ("lid 11"), which cannot distinguish
    between them -- there is no way to tell from the reference alone which one is meant.
    Silently keeping only the first one seen would be wrong exactly half the time for a
    colliding key; every distinct text is kept instead and shown to the LLM, clearly
    labelled as ambiguous when there's more than one (see _reference_context)."""
    index: dict = {}
    for r in records:
        key = (r.instrument_id, r.article, str(r.norm.get("number")))
        texts = index.setdefault(key, [])
        if r.text not in texts:
            texts.append(r.text)
    return index


def _build_reverse_reference_index(records: list[c1.NormRecord]) -> dict:
    """(instrument_id, article, referenced_paragraph_number) -> {paragraph numbers of
    OTHER norms in that article that reference it}. The other half of reference
    expansion (2026-09-24, per external review): the forward direction alone left a real
    asymmetry -- AI Act art. 73(1) (the substantive duty) never learns that 73(2)/(3)
    exist and specify ITS deadline, only 73(2)/(3) learn about 73(1). Checked directly:
    this is exactly why 73(1) vs. Cbw art. 27(1) came back DIFFERENT_ENOUGH ("Norm B
    lacks the deadline/content package Norm A states directly") even though the full
    AI Act 73 duty (across all three paragraphs) states one too -- 73(1) alone just
    isn't where it's written. Built once from every norm's own reference blob, the
    mirror image of _reference_context's forward lookup."""
    from collections import defaultdict
    reverse = defaultdict(set)
    for r in records:
        blob = " ".join(str(r.norm.get(f) or "") for f in
                         ("trigger_event", "action", "deference", "conditions"))
        own_number = str(r.norm.get("number"))
        for num in _referenced_paragraph_numbers_c2(blob):
            if str(num) == own_number:
                continue
            reverse[(r.instrument_id, r.article, str(num))].add(own_number)
    return reverse


def _paragraph_context_line(key: tuple, num, paragraph_text_index: dict, tag: str) -> Optional[str]:
    texts = paragraph_text_index.get(key) or []
    if not texts:
        return None
    if len(texts) == 1:
        return f"  lid {num} ({tag}): {texts[0]}"
    # More than one paragraph in the source displays this same number (2026-09-24 fix,
    # see _build_paragraph_text_index's own note) -- shown as an explicit ambiguity
    # rather than silently guessing which one the reference means.
    joined = " -- OR (ambiguous, source has multiple paragraphs displaying this number) -- ".join(texts)
    return f"  lid {num} ({tag}, ambiguous in the source): {joined}"


def _reference_context(r: c1.NormRecord, paragraph_text_index: dict,
                        reverse_reference_index: dict, label: str) -> str:
    blob = " ".join(str(r.norm.get(f) or "") for f in
                     ("trigger_event", "action", "deference", "conditions"))
    own_number = str(r.norm.get("number"))
    lines = []
    for num in sorted(_referenced_paragraph_numbers_c2(blob)):
        if str(num) == own_number:
            continue  # a norm referencing its own paragraph number isn't a cross-reference
        line = _paragraph_context_line((r.instrument_id, r.article, str(num)), num,
                                        paragraph_text_index, "referenced by this norm")
        if line:
            lines.append(line)
    referencing_nums = reverse_reference_index.get((r.instrument_id, r.article, own_number), set())
    for num in sorted(referencing_nums, key=lambda x: (len(x), x)):
        line = _paragraph_context_line((r.instrument_id, r.article, num), num, paragraph_text_index,
                                        "elaborates on THIS norm's deadline/procedure")
        if line:
            lines.append(line)
    if not lines:
        return ""
    return (f"\nRelated paragraph(s) from {r.instrument_id} art. {r.article} -- context "
            f"for norm {label}, not itself part of the norm being compared:\n"
            + "\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# LLM adjudication -- ONE call per candidate PAIR, to confirm "substantially the same
# nature" for the compliance action. Everything else in this category is computable
# (candidate generation, the filters, the burden score), per the spec.
#
# Structured verdict (2026-09-24 rework, replacing a binary SUBSTANTIALLY_SAME/
# DIFFERENT_ENOUGH schema): found necessary by checking two real, concrete cases, not
# guessed at. DORA art. 19 vs. Bijlage 35 art. 5 -- the exact case the vertical-pair
# "tevens" exception was built to recover -- came back DIFFERENT_ENOUGH at 0.98
# confidence, and reading the model's own reasoning showed it wasn't wrong about the
# facts: it correctly identified that Bijlage 35 requires the SAME DORA notification
# ALSO sent to a Dutch CSIRT, then called that "different" purely because the two sides'
# ANSWER to "is this the same duty" is no once you require an identical recipient. But
# that's exactly backwards for deduplication -- an entity now doing the SAME reporting
# work twice, once per recipient, IS the burden this category exists to surface, not a
# reason to discard the pair. A binary schema cannot express "same real action,
# genuinely duplicated effort, despite a difference in recipient" as anything other than
# a false "different." DORA art. 28(3) vs. AI Act art. 34(3) (both "make documentation
# available to an authority on request") showed the opposite failure mode: a real but
# generic pattern-level match got the same confident SUBSTANTIALLY_SAME as a specific,
# concrete duplicate, with nothing in the schema to tell the two apart.
#
# The new fields ask the model to reason about the SHAPE of the overlap explicitly
# (same actor, same event, same recipient role, same content) before collapsing that
# into one verdict, and the verdict itself now has a real middle category
# (RELATED_BURDEN) for exactly the DORA/Bijlage-35 shape: same actor, same event, same
# report content, but sent again to an additional recipient -- a genuine duplicated
# effort, not "the same duty" and not "unrelated" either.
# ---------------------------------------------------------------------------

class DuplicationVerdict(BaseModel):
    shared_compliance_action: str          # what both sides substantively require, in
                                            # the model's own words
    distinguishing_factor: Optional[str]   # any real difference in scope/substance
                                            # found, if any
    same_core_action: bool                 # is the underlying verb-level action the
                                            # same (report / notify / retain / provide...)
    same_event_family: bool                # is the triggering real-world event the same
                                            # kind of thing (an incident, a breach, a
                                            # request), not necessarily worded the same
    same_actor_role: bool                  # does the same kind of actor bear the duty
    recipient_relation: Literal["same_recipient", "different_authority_same_role",
                                 "customer_or_data_subject_vs_authority", "unrelated"]
    deadline_relation: Literal["same", "different", "only_one_specified", "not_applicable"]
    content_overlap: Literal["high", "medium", "low"]
    duplicate_burden_verdict: Literal["DUPLICATE_BURDEN", "RELATED_BURDEN",
                                       "DIFFERENT", "INSUFFICIENT_EVIDENCE"]
    criterion_fired: str
    evidence_span_a: Optional[str]         # verbatim in norm A's provision text
    evidence_span_b: Optional[str]         # verbatim in norm B's provision text
    confidence: float


@dataclass
class PairAdjudicationResult:
    verdict: DuplicationVerdict
    needs_recheck_reason: Optional[str]  # None if the response passed consistency checks


def _consistency_issue(v: DuplicationVerdict, text_a: str, text_b: str) -> Optional[str]:
    if not (0.0 <= v.confidence <= 1.0):
        return f"confidence {v.confidence} outside [0, 1]"
    for span, text, label in ((v.evidence_span_a, text_a, "a"), (v.evidence_span_b, text_b, "b")):
        if span is not None and not c1._verbatim_in(span, text):
            return f"evidence_span_{label} is not a verbatim substring of that norm's provision text"
    if (v.duplicate_burden_verdict in ("DUPLICATE_BURDEN", "RELATED_BURDEN")
            and v.evidence_span_a is None and v.evidence_span_b is None):
        return f"verdict={v.duplicate_burden_verdict} but no evidence span was quoted from either norm"
    if v.duplicate_burden_verdict == "DIFFERENT" and v.same_core_action and v.same_event_family \
            and v.same_actor_role and v.content_overlap == "high":
        return ("verdict=DIFFERENT but same_core_action, same_event_family, same_actor_role are "
                "all true and content_overlap is high -- that combination describes a real "
                "overlap, not DIFFERENT")
    return None


def adjudicate_pair(client, model: str, a: c1.NormRecord, b: c1.NormRecord,
                     paragraph_text_index: dict, reverse_reference_index: dict
                     ) -> PairAdjudicationResult:
    ref_context = (_reference_context(a, paragraph_text_index, reverse_reference_index, "A")
                   + _reference_context(b, paragraph_text_index, reverse_reference_index, "B"))

    def build_prompt(retry_note: Optional[str] = None) -> str:
        note = (f"\nYour previous response was rejected: {retry_note}. Reconsider and "
                f"answer again.\n" if retry_note else "")
        return (
            "The following two legal norms, extracted from different Dutch/EU digital-law "
            "instruments, were flagged as candidates because they share a similar "
            "triggering event and the same category of addressee (candidate generation "
            "found them SIMILAR -- it did NOT establish that they impose the same "
            "compliance obligation; that is your job).\n\n"
            "First answer four structural questions about the two norms, independently "
            "of your final verdict: same_core_action (is the underlying verb-level action "
            "the same -- report, notify, retain, provide information, implement a "
            "measure...), same_event_family (is the real-world triggering event the same "
            "KIND of thing -- e.g. a security/data incident -- even if worded very "
            "differently), same_actor_role (does the same kind of actor bear the duty), "
            "recipient_relation (same_recipient / different_authority_same_role / "
            "customer_or_data_subject_vs_authority / unrelated), deadline_relation (same "
            "/ different / only_one_specified / not_applicable), and content_overlap "
            "(high/medium/low -- how much of what must actually be reported or done is "
            "the same, not just the trigger).\n\n"
            "Then give duplicate_burden_verdict, one of:\n"
            "- DUPLICATE_BURDEN: the same real compliance work must be done again -- "
            "same actor, same event, same or near-same content -- even if it must be "
            "sent to an ADDITIONAL recipient/channel because of a second legal basis. A "
            "different recipient does NOT by itself make this DIFFERENT: an entity doing "
            "the identical reporting work twice, once per recipient, is exactly the "
            "duplicated burden this category looks for -- do not call that DIFFERENT "
            "just because recipient_relation isn't same_recipient.\n"
            "- RELATED_BURDEN: the same general event family and actor role, genuinely "
            "worth a reviewer's attention, but the actual content/purpose differs enough "
            "that it isn't simply the same work repeated (e.g. a generic 'provide "
            "information/documentation on request' pattern where the specific documents "
            "or purpose differ) -- content_overlap will usually be medium or low here.\n"
            "- DIFFERENT: not meaningfully the same duty -- different event family or "
            "different actor role, not merely a different recipient or channel.\n"
            "- INSUFFICIENT_EVIDENCE: you genuinely can't tell from the text.\n\n"
            "Some norms are part of a single duty spread across several paragraphs of the "
            "same article -- one paragraph states WHO must do WHAT, another states BY "
            "WHEN or WHAT ELSE it must include. Related paragraph(s) are provided below "
            "for exactly this reason: a norm whose own text mainly refers back to another "
            "paragraph should be understood together with what that paragraph says, and a "
            "norm whose deadline/procedure is instead specified BY a later paragraph "
            "(shown below as 'elaborates on this norm') should be judged as including "
            "that fuller picture too, not as if it were the whole duty in isolation. "
            "Don't return INSUFFICIENT_EVIDENCE just because the immediate paragraph "
            "alone looks partial when related paragraphs fill in the rest. Quote a "
            "verbatim evidence_span_a/b from each norm's OWN provision text below (not "
            "the related paragraph) -- null only if you truly can't quote anything useful."
            + note + "\n\n"
            f"NORM A -- {a.instrument_id} art. {a.article} ({a.heading})\n"
            f"Provision text: {a.text}\n"
            f"Extracted: action={a.norm.get('action')!r}, "
            f"recipient_body={a.norm.get('recipient_body')}, deadline={a.norm.get('deadline')}\n\n"
            f"NORM B -- {b.instrument_id} art. {b.article} ({b.heading})\n"
            f"Provision text: {b.text}\n"
            f"Extracted: action={b.norm.get('action')!r}, "
            f"recipient_body={b.norm.get('recipient_body')}, deadline={b.norm.get('deadline')}"
            + ref_context
        )

    verdict = None
    issue = None
    for _attempt in range(2):  # one retry, with the specific inconsistency named
        resp = client.responses.parse(
            model=model, input=build_prompt(issue), text_format=DuplicationVerdict,
            temperature=0, reasoning={"effort": "none"},
        )
        verdict = resp.output_parsed
        issue = _consistency_issue(verdict, a.text, b.text)
        if issue is None:
            break
    else:
        return PairAdjudicationResult(verdict, issue)
    return PairAdjudicationResult(verdict, None)


# ---------------------------------------------------------------------------
# Confidence tiering -- AI-judged, so it can never reach Tier 1 (High), same philosophy
# as C1's duty_conflict/deontic_polarity_conflict findings: every real C2 finding needs
# the LLM's own "substantially the same" confirmation, so nothing here is a purely
# deterministic comparison the way C1's standard_collision/threshold_mismatch are.
# ---------------------------------------------------------------------------

def compute_confidence_tier(a: c1.NormRecord, b: c1.NormRecord, verdict: DuplicationVerdict,
                             needs_recheck_reason: Optional[str] = None) -> tuple[int, list[str]]:
    reasons = []
    clean_extraction = not a.norm.get("extraction_uncertain") and not b.norm.get("extraction_uncertain")
    if not clean_extraction:
        reasons.append("one or both norms were finalized with the conservative default "
                        "after Stage 6's two extraction passes disagreed (extraction_uncertain)")
    reasons.append("resolved by AI judgement (pairwise confirmation), not a deterministic "
                    "computation -- never eligible for Tier 1 regardless of confidence")

    if needs_recheck_reason is not None:
        reasons.append(f"model's response was internally inconsistent even after a retry "
                        f"({needs_recheck_reason}) -- not accepted as a supported verdict")
        return 3, reasons
    if verdict.duplicate_burden_verdict == "INSUFFICIENT_EVIDENCE":
        reasons.append("the model could not confirm the two norms share one real "
                        "compliance action -- genuinely uncertain, not a confirmed "
                        "non-duplicate, so still worth a look")
        return 3, reasons
    reasons.append(f"model-reported confidence: {verdict.confidence}")
    if verdict.confidence >= 0.8 and clean_extraction:
        return 2, reasons
    return 3, reasons


# ---------------------------------------------------------------------------
# Overlap strength -- deliberately SEPARATE from confidence_tier. confidence_tier
# answers "how much does the SYSTEM trust this verdict" (an epistemic question --
# AI-judged findings are capped at Medium regardless of how sure the model sounds, same
# rule C1 uses for duty_conflict). overlap_strength answers a different, practical
# question a reviewer actually wants triaged first: "if this verdict is right, how
# substantial is the real-world overlap."
#
# Rebuilt (2026-09-24, per external review) to read the STRUCTURED fields instead of the
# model's raw confidence number -- confidence alone let a generic, pattern-level match
# (DORA art. 28(3) vs. AI Act art. 34(3), both "make documentation available to an
# authority on request", confidence 0.94) score identically to a specific, concrete
# duplicate (Cbw art. 27 vs. DORA art. 19, the same incident report to the same kind of
# authority, confidence 0.94) -- the SAME strength for two findings that clearly aren't
# equally substantial. content_overlap now does that discriminating work directly: a
# generic "provide documentation" match naturally gets content_overlap=medium/low (the
# specific documents differ) even when the model is highly confident the pattern itself
# matches, so it can no longer read as "High" just because the model sounded sure.
# ---------------------------------------------------------------------------

def compute_overlap_strength(verdict: DuplicationVerdict, needs_recheck_reason: Optional[str]) -> str:
    if needs_recheck_reason is not None:
        return "low"
    if verdict.duplicate_burden_verdict in ("DIFFERENT", "INSUFFICIENT_EVIDENCE"):
        return "low"
    if (verdict.duplicate_burden_verdict == "DUPLICATE_BURDEN" and verdict.same_core_action
            and verdict.same_event_family and verdict.same_actor_role
            and verdict.content_overlap == "high"):
        return "high"
    if verdict.same_event_family and verdict.content_overlap in ("high", "medium"):
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Finding record -- same reviewer-facing shape as C1's Part 6 schema (a pair, exactly
# like C1's own findings) plus the burden_score this category adds. `report_eligible`
# always false.
# ---------------------------------------------------------------------------

def build_finding(a: c1.NormRecord, b: c1.NormRecord, burden: dict, verdict: DuplicationVerdict,
                   tier: int, tier_reasons: list[str], signals: dict,
                   needs_recheck_reason: Optional[str] = None) -> dict:
    criteria = ["same_addressee_type", "cross_instrument"]
    if signals.get("semantic_hit"):
        criteria.append("trigger_event_embedding_similarity")
    if signals.get("keyword_hit"):
        criteria.append("trigger_event_keyword_family_match")
    return {
        "finding_id": f"F-C2-{c1._pair_id(a, b)}",
        "category": "deduplication",
        "subtype": "duplicate_obligation",
        "status": "needs_recheck" if needs_recheck_reason else "candidate",
        "confidence_tier": tier,
        "confidence_label": CONFIDENCE_LABELS[tier],
        "confidence_reasons": tier_reasons,
        "overlap_strength": compute_overlap_strength(verdict, needs_recheck_reason),
        "provisions": [
            # norm_index AND paragraph_index included (2026-09-24, same fix as C1's
            # build_finding): without both, a reviewer -- or a future cross-check
            # against this file -- can't always tell WHICH norm is meant when a
            # paragraph holds more than one (norm_index alone doesn't disambiguate the
            # AI Act art. 73 case, see _norm_key()'s own note).
            {"uid": m.graph_uid, "instrument_id": m.instrument_id, "article": m.article,
             "paragraph_number": m.norm.get("number"), "norm_index": m.norm.get("norm_index"),
             "paragraph_index": m.norm.get("paragraph_index")}
            for m in (a, b)
        ],
        "criteria_fired": criteria,
        "burden_score": burden,
        "llm_adjudication": verdict.model_dump(),
        "needs_recheck_reason": needs_recheck_reason,
        "resolution_filter": {"checked": True, "vertical_pair_checked": True,
                               "deference_checked": True, "already_labelled_by_c1_checked": True},
        "extracted_on": date.today().isoformat(),
        "human_verified": False,
        "report_eligible": False,
    }


# ---------------------------------------------------------------------------
# Article-pair grouping (2026-09-24, explicit request): a purely presentational layer,
# NOT a change to detection -- norm-level adjudication stays exactly as precise as
# before (each specific clause-pair is still independently checked by the LLM, so
# nothing about WHICH duty overlaps gets lost or blurred together). This just groups
# the resulting findings by which two ARTICLES they're between before showing them to a
# reviewer, since a reviewer thinks "does Cbw art. 39 duplicate AI Act art. 73"
# article-by-article, not clause-fragment-by-clause-fragment -- and a real check on the
# first full run's 19 findings showed 2 article-pairs each producing 2 separate,
# unconsolidated findings (Cbw art. 39 vs. AI Act art. 73; Cbw art. 27 vs. AI Act art.
# 73), which is exactly the confusing repetition this avoids.
# ---------------------------------------------------------------------------

def group_by_article(findings: list[dict]) -> list[dict]:
    from collections import defaultdict
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for f in findings:
        p = f["provisions"]
        key = tuple(sorted([f"{p[0]['instrument_id']}:{p[0]['article']}",
                             f"{p[1]['instrument_id']}:{p[1]['article']}"]))
        groups[key].append(f)

    grouped = []
    for (ref_a, ref_b), members in groups.items():
        best_tier = min(m["confidence_tier"] for m in members)
        grouped.append({
            "article_pair": [ref_a, ref_b],
            "overlapping_duty_count": len(members),
            "best_confidence_tier": best_tier,
            "best_confidence_label": CONFIDENCE_LABELS[best_tier],
            "findings": members,
        })
    grouped.sort(key=lambda g: (g["best_confidence_tier"], -g["overlapping_duty_count"]))
    return grouped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=c1.DEFAULT_MODEL)
    ap.add_argument("--dry-run", action="store_true",
                     help="candidate generation + burden scoring only, no LLM calls -- "
                          "prints the candidate count and drop-count breakdown so the "
                          "method is auditable before it costs anything")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--semantic-k", type=int, default=6,
                     help="cross-instrument nearest-neighbour budget per norm for "
                          "trigger_event similarity")
    ap.add_argument("--semantic-k-within", type=int, default=3,
                     help="within-instrument nearest-neighbour budget -- separate and "
                          "smaller, same rationale as C1's own --semantic-k-within")
    args = ap.parse_args()

    from openai import OpenAI
    client = OpenAI()

    print("Loading norms and the citation graph...", flush=True)
    records = c1.load_all_norm_records()
    g = nx.read_gexf(DATA / "graph.gexf")
    c1_labelled = load_c1_labelled_pairs()
    paragraph_text_index = _build_paragraph_text_index(records)
    reverse_reference_index = _build_reverse_reference_index(records)
    print(f"  {len(records)} eligible norms loaded, {len(c1_labelled)} pair(s) already "
          f"labelled by C1 (excluded from C2 entirely)", flush=True)

    print(f"  finding candidate pairs among OBLIGATION norms by trigger_event embedding "
          f"similarity (top-{args.semantic_k} cross-instrument, top-"
          f"{args.semantic_k_within} within-instrument) OR trigger-keyword-family match "
          f"(reused from C1, bridges vocabulary gaps embeddings miss -- see module "
          f"docstring), both gated on addressee_type match...", flush=True)
    pairs, drop_counts, n_candidates = generate_pairs(
        client, records, g, c1_labelled, args.semantic_k, args.semantic_k_within)
    print(f"  {drop_counts['too_generic_trigger']} OBLIGATION norm(s) excluded before "
          f"matching (trigger_event+action too short/generic, <{MIN_TRIGGER_CONTENT_WORDS} "
          f"combined content words)", flush=True)
    print(f"  {n_candidates} candidate index-pair(s) (embedding OR keyword signal); "
          f"dropped {drop_counts['addressee_mismatch']} (addressee_type mismatch), "
          f"{drop_counts['same_instrument']} (same instrument), "
          f"{drop_counts['vertical']} (vertical EU<->NL transposition pair), "
          f"{drop_counts['deference']} (deference resolves), "
          f"{drop_counts['already_c1']} (already labelled by C1)", flush=True)
    print(f"  {len(pairs)} candidate pair(s) survive", flush=True)

    out_path = DATA / ("findings_c2_dryrun_preview.json" if args.dry_run else "findings_c2.json")

    if args.dry_run:
        preview = [{"a": {"instrument_id": a.instrument_id, "article": a.article,
                           "number": a.norm.get("number"), "trigger_event": a.norm.get("trigger_event")},
                    "b": {"instrument_id": b.instrument_id, "article": b.article,
                           "number": b.norm.get("number"), "trigger_event": b.norm.get("trigger_event")},
                    "signals": signals}
                   for a, b, signals in pairs]
        out_path.write_text(json.dumps({"pairs_preview": preview}, ensure_ascii=False, indent=1),
                             encoding="utf-8")
        print(f"  preview written -> {out_path.relative_to(ROOT)} (no LLM calls made)", flush=True)
        return

    cache_path = DATA / "c2_adjudication_cache.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    cache_lock = threading.Lock()

    def _cache_key(a: c1.NormRecord, b: c1.NormRecord) -> str:
        return f"{C2_ADJUDICATION_VERSION}::{args.model}::{c1._pair_id(a, b)}"

    findings = []

    def _save():
        out_path.write_text(json.dumps({"findings": findings}, ensure_ascii=False, indent=1),
                             encoding="utf-8")

    def _label(a: c1.NormRecord, b: c1.NormRecord) -> str:
        return f"{a.instrument_id} art.{a.article} <-> {b.instrument_id} art.{b.article}"

    def _handle(a: c1.NormRecord, b: c1.NormRecord, signals: dict,
                result: PairAdjudicationResult, tag: str):
        verdict = result.verdict
        label = _label(a, b)
        if result.needs_recheck_reason:
            tier, reasons = compute_confidence_tier(a, b, verdict, result.needs_recheck_reason)
            findings.append(build_finding(a, b, _burden_score(a, b), verdict, tier,
                                           reasons, signals, result.needs_recheck_reason))
            _save()
            print(f"  {tag}{label} -- NEEDS_RECHECK ({result.needs_recheck_reason}) [Low]", flush=True)
            return
        if verdict.duplicate_burden_verdict == "DIFFERENT":
            print(f"  {tag}{label} -- DIFFERENT, no finding", flush=True)
            return
        # RELATED_BURDEN + content_overlap=low treated as not-a-finding (2026-09-24, a
        # real problem found in the first run of this schema, not assumed ahead of
        # evidence): checked directly -- 160 of 233 RELATED_BURDEN verdicts had
        # content_overlap=low, meaning the model was using RELATED_BURDEN as an easy
        # safe-middle default instead of committing to DIFFERENT for a pair that barely
        # overlaps at all. content_overlap is the model's own structured judgment of
        # HOW MUCH actually overlaps -- using it here directly is more reliable than
        # re-prompting to ask the model to be stricter about a category it's already
        # shown it treats too loosely.
        if verdict.duplicate_burden_verdict == "RELATED_BURDEN" and verdict.content_overlap == "low":
            print(f"  {tag}{label} -- RELATED_BURDEN but content_overlap=low, no finding", flush=True)
            return
        tier, reasons = compute_confidence_tier(a, b, verdict, None)
        findings.append(build_finding(a, b, _burden_score(a, b), verdict, tier, reasons, signals))
        _save()
        print(f"  {tag}{label} -- {verdict.duplicate_burden_verdict} [{CONFIDENCE_LABELS[tier]}]", flush=True)

    cached_p, to_call_p = [], []
    for a, b, signals in pairs:
        (cached_p if _cache_key(a, b) in cache else to_call_p).append((a, b, signals))

    for a, b, signals in cached_p:
        entry = cache[_cache_key(a, b)]
        result = PairAdjudicationResult(DuplicationVerdict(**entry["verdict"]),
                                         entry["needs_recheck_reason"])
        _handle(a, b, signals, result, tag="[cached] ")

    print(f"  adjudicating {len(to_call_p)} pair(s) with {args.concurrency} concurrent "
          f"calls ({len(cached_p)} skipped -- already cached)...", flush=True)
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        future_to_pair = {ex.submit(adjudicate_pair, client, args.model, a, b,
                                     paragraph_text_index, reverse_reference_index): (a, b, signals)
                           for a, b, signals in to_call_p}
        for done, fut in enumerate(as_completed(future_to_pair), 1):
            a, b, signals = future_to_pair[fut]
            label = _label(a, b)
            try:
                result: PairAdjudicationResult = fut.result()
            except Exception as e:
                print(f"  [{done}/{len(to_call_p)}] {label} -- ERROR: {e}", flush=True)
                continue
            with cache_lock:
                cache[_cache_key(a, b)] = {"verdict": result.verdict.model_dump(),
                                            "needs_recheck_reason": result.needs_recheck_reason}
                cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
            _handle(a, b, signals, result, tag=f"[{done}/{len(to_call_p)}] ")

    grouped = group_by_article(findings)
    grouped_path = DATA / "findings_c2_by_article.json"
    grouped_path.write_text(json.dumps({"article_pairs": grouped}, ensure_ascii=False, indent=1),
                             encoding="utf-8")
    print(f"\n{len(findings)} candidate finding(s) across {len(grouped)} distinct "
          f"article-pair(s) -> {out_path.relative_to(ROOT)} (per-finding) and "
          f"{grouped_path.relative_to(ROOT)} (grouped by article-pair)", flush=True)


if __name__ == "__main__":
    main()
