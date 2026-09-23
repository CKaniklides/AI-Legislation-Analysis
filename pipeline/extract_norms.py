# -*- coding: utf-8 -*-
"""
Stage 6 — Extract the norm layer (architecture doc Part 4, Stage 6 / Part 3.1).

For every paragraph (or whole article, where a provision has no `paragraphs`) that
carries an independent rule, extract one `norms[]` entry: who must do what, by when,
under what conditions, with what deference to another provision. This is the first
stage that calls a model — everything before it (parse.py, parse_eu.py, parse_wti.py,
graph.py, features.py) is deterministic code with no model calls, and everything after
Stage 6's own deterministic guardrails (Part 5) stays deterministic wherever the fact is
computable at all.

Design constraints, all from the architecture doc, none optional:
  - Structured Outputs, strict mode. The API returns JSON conforming to `ExtractedNorm`
    or the call fails -- no free prose to parse, no prompted-JSON convention.
  - Verbatim spans. `addressee`, `action`, `trigger_event` and `deadline_raw` must each
    be a literal substring of the paragraph text (checked in code below, not trusted from
    the prompt). A non-matching span is a failed pass: retried once, then treated as
    failed. This is what stops the model from inventing details that aren't there.
  - Deadlines are parsed, not read. The model returns only `deadline_raw` (the phrase);
    `_parse_deadline()` below -- plain regex, no model -- turns "binnen 24 uur",
    "onverwijld", "uiterlijk 72 uur nadat ..." into {value, unit, from}. The model never
    produces the number a later comparison depends on.
  - Two-pass, temperature 0, different framings. Run extraction twice per unit with
    different instruction phrasing; only write a `norms[]` entry when both passes
    validate AND agree on deontic/deadline/deference. Anything else (a validation
    failure that survives its retry, or a two-pass disagreement) is written to the
    review queue instead of silently guessed at -- `human_verified` stays false either
    way, but an unresolved item isn't allowed to masquerade as a resolved one.
  - Model is a single config value (MODEL below), not hardcoded per call site, so
    switching from gpt-5.6-luna to gpt-5.6-terra is a one-line change. Per the
    2026-09-20 decision: start on gpt-5.6-luna for everything; escalate a stage only
    when this script's own review-queue rate says so, not on a guess.
  - Prioritise. Full-corpus extraction is not attempted here. SOURCES below is exactly
    the doc's incident-reporting anchor set: Cbw ch. 8, GDPR arts. 33-34, NIS2 art. 23,
    DORA arts. 17-23, Bijlage 35, and the Uitvoeringswet dataverordening's competence
    provisions (arts. 2-8 -- the designation/cooperation/sanctioning articles; 9-12 are
    amendments to other laws and 13-14 are final provisions, neither in scope for norm
    extraction).

Usage:
    python extract_norms.py --dry-run          # verify scope/counts, no API key needed
    python extract_norms.py --only "Cbw"       # run one source
    python extract_norms.py --limit 5          # smoke-test on the first 5 units total
    python extract_norms.py                    # run the full anchor set
"""
import argparse
import json
import re
import sys
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Literal, Optional

from dotenv import load_dotenv

# Windows consoles routinely default to a legacy codepage (e.g. cp1253) that can't
# encode the Dutch legal text this script prints (curly quotes, non-breaking spaces,
# accented characters) -- confirmed directly: a real run crashed mid-way through
# resolve_queue_conservatively() on an ordinary print() over real deadline text,
# losing no data (writes had already happened) but stopping before completion for a
# reason that had nothing to do with the extraction logic itself.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")  # picks up OPENAI_API_KEY from a .env file in the project root

DEFAULT_MODEL = "gpt-5.6-luna"  # single config value -- see module docstring


# ---------------------------------------------------------------------------
# The norms[] entry schema (architecture doc Part 3.1), split into what the
# model produces (ExtractedNorm) and the deterministic post-processing that
# turns it into the documented entry shape (see _finalize_norm below).
# ---------------------------------------------------------------------------

Deontic = Literal[
    "OBLIGATION", "PROHIBITION", "PERMISSION", "DEFINITION",
    "COMPETENCE", "DEFERENCE", "PROCEDURAL", "NONE",
]

# Deliberately a small, fixed vocabulary, not free text: C1's candidate-generation
# pre-filter (Part 5) drops pairs whose addressee_type differs, which only means
# something if the same handful of categories is used consistently across all ten
# instruments rather than each extraction inventing its own label.
AddresseeType = Literal[
    "REGULATED_ENTITY", "COMPETENT_AUTHORITY", "MEMBER_STATE",
    "EU_INSTITUTION", "DATA_SUBJECT", "OTHER",
]


class ExtractedNorm(BaseModel):
    """What the model returns for ONE independent norm. Verbatim-checked fields
    (addressee, action, trigger_event, deadline_raw) are validated against the source
    text by _verbatim_ok() below, never trusted outright.

    A single unit (paragraph, or whole article where there are no paragraphs) can
    contain zero, one, or several of these (2026-09-24, item 3) -- see
    ExtractedNormsResponse. Splitting a paragraph into independent norms is itself a
    judgement call the model makes, same discipline as every other field here: not
    verified line-by-line against a human annotation (that would need Stage 9's
    labelled sample), but grounded the same way everything else in Stage 6 is --
    verbatim spans, two independently-framed passes, disagreement queued rather than
    silently guessed at."""
    deontic: Deontic
    addressee: Optional[str]
    addressee_type: Optional[AddresseeType]
    trigger_event: Optional[str]
    action: Optional[str]
    recipient_body: list[str]
    deadline_raw: Optional[str]  # e.g. "binnen 24 uur" -- parsed deterministically, not by the model
    thresholds: list[str]
    conditions: list[str]
    deference: Optional[str]
    extraction_confidence: float


class ExtractedNormsResponse(BaseModel):
    """The model's actual per-call response (2026-09-24, item 3): a paragraph
    routinely bundles more than one independent duty in one sentence ("shall X, and
    shall not disclose Y to any third party") -- the earlier one-ExtractedNorm-per-call
    schema had no way to represent a second bundled duty at all; it was silently
    dropped, not flagged, not queued, just never asked for. An empty list is a valid,
    useful answer (a paragraph that states no independent rule at all -- pure
    definitions, cross-references, procedural text)."""
    norms: list[ExtractedNorm]


VERBATIM_FIELDS = ("addressee", "action", "trigger_event", "deadline_raw")


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _verbatim_ok(norm: ExtractedNorm, source_text: str) -> bool:
    haystack = _normalize(source_text)
    for field_name in VERBATIM_FIELDS:
        span = getattr(norm, field_name)
        if span is None:
            continue
        if _normalize(span) not in haystack:
            return False
    return True


# ---------------------------------------------------------------------------
# Deterministic deadline parser. First-pass lexicon, same spirit as features.py's
# RARITY_CUTOFF -- covers the phrasing actually seen in the anchor set (Cbw ch. 8,
# GDPR 33/34, NIS2 23, DORA 17-23) and should be extended, not trusted as exhaustive,
# once the review queue surfaces phrasing it doesn't recognise.
# ---------------------------------------------------------------------------

# Numeric patterns are checked BEFORE the vague qualifiers below. Real drafting
# routinely states both in one phrase -- "onverwijld of, indien dat niet mogelijk is,
# binnen 24 uur ..." (Cbw art. 26), "zonder onredelijke vertraging en, indien mogelijk,
# uiterlijk 72 uur ..." (GDPR art. 33) -- and the specific figure is the operative,
# comparable deadline; "onverwijld"/"zonder onredelijke vertraging" is a qualifier on
# it, not a competing deadline. Checking "onverwijld" first (the original ordering)
# silently discarded the 24-hour figure whenever it appeared earlier in the string --
# caught only by checking this parser's output against the project's own flagship
# Cbw-vs-GDPR example, where it made the comparison impossible.
_DEADLINE_PATTERNS: list[tuple[re.Pattern, Callable[[re.Match], dict]]] = [
    (re.compile(r"\b(binnen|uiterlijk)\s+(\d+)\s+uur\b", re.I), lambda m: {"value": int(m.group(2)), "unit": "hour"}),
    (re.compile(r"\b(binnen|uiterlijk)\s+(\d+)\s+(werk)?dag(en)?\b", re.I), lambda m: {"value": int(m.group(2)), "unit": "day"}),
    (re.compile(r"\b(binnen|uiterlijk)\s+(\d+)\s+we(e)?k(en)?\b", re.I), lambda m: {"value": int(m.group(2)), "unit": "week"}),
    (re.compile(r"\b(binnen|uiterlijk)\s+(\d+)\s+maand(en)?\b", re.I), lambda m: {"value": int(m.group(2)), "unit": "month"}),
    # "onverwijld"/"onmiddellijk" are QUALITATIVE urgency standards ("as soon as
    # reasonably possible"), not a literal commitment to act at time zero (2026-09-24
    # fix) -- the old value=0 treated a judgment-call duty as if it were exactly as
    # precise and comparable as a real numeric deadline like "binnen 24 uur". Given the
    # same non-numeric treatment as "zo spoedig mogelijk" below (value=None, excluded
    # from _deadline_hours() arithmetic in detect_c1_contradiction.py, same as "asap"
    # already was) rather than a hardcoded fake zero.
    (re.compile(r"\bonverwijld\b", re.I), lambda m: {"value": None, "unit": "qualitative_urgent"}),
    (re.compile(r"\bonmiddellijk\b", re.I), lambda m: {"value": None, "unit": "qualitative_urgent"}),
    (re.compile(r"\bzo\s+spoedig\s+mogelijk\b", re.I), lambda m: {"value": None, "unit": "asap"}),
]

# What the deadline is measured from, e.g. "nadat hij er kennis van heeft genomen" ->
# "kennis van heeft genomen". Best-effort text capture, not a normalized taxonomy of
# trigger types -- flagged as a heuristic like everything else in this section.
_FROM_RE = re.compile(r"\bna(dat)?\s+(?:hij|zij|het|de\s+\w+)?\s*(.+?)(?:[,.;]|$)", re.I)


def _parse_deadline(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    parsed = {"value": None, "unit": None, "from": None, "raw": raw}
    for pattern, extractor in _DEADLINE_PATTERNS:
        m = pattern.search(raw)
        if m:
            parsed.update(extractor(m))
            break
    else:
        print(f"    [deadline parser] unrecognised phrasing, value/unit left null: {raw!r}")
    from_m = _FROM_RE.search(raw)
    if from_m:
        parsed["from"] = from_m.group(2).strip()
    return parsed


# ---------------------------------------------------------------------------
# Prompting. Two independent framings of the same schema -- the two-pass check's
# whole point is that agreement between differently-worded instructions is stronger
# evidence than one pass at high confidence.
# ---------------------------------------------------------------------------

_SCHEMA_FIELDS_NOTE = (
    "Every span you return for addressee, action, trigger_event and deadline_raw must "
    "be a verbatim, word-for-word substring of the paragraph text -- copy it exactly, "
    "do not paraphrase or summarise it. If a field genuinely does not apply to this "
    "norm, return null (or an empty list for the list fields), never invent a value. "
    "For deadline_raw specifically: a sentence often states both a vague qualifier "
    "('onverwijld', 'zonder onredelijke vertraging') AND a specific figure ('binnen 24 "
    "uur', 'uiterlijk 72 uur') as a fallback or outer bound on the same duty -- when both "
    "appear, extract the phrase containing the specific number, not the vague qualifier "
    "alone, since the number is what a later comparison against another law's deadline "
    "actually needs. Extract the vague qualifier only when no specific figure is present "
    "anywhere in the paragraph."
)

# 2026-09-24, item 3: a paragraph may state more than one INDEPENDENT norm in a single
# sentence -- the classic shape is "shall X, and shall not disclose Y to any third
# party", one obligation and one prohibition in one breath. Made explicit and given a
# worked boundary case (both directions: don't split, don't merge) because "independent
# norm" is a genuine judgement call, not something the model will get right from the
# field list alone.
_MULTI_NORM_NOTE = (
    "\n\nA paragraph may state ZERO, ONE, or SEVERAL independent norms -- return one "
    "entry in `norms` for each. Two norms are INDEPENDENT when they have a different "
    "deontic (e.g. one OBLIGATION and one PROHIBITION) or clearly different actions, "
    "even if they share the same sentence and the same addressee. Example: 'de "
    "aanbieder meldt het incident binnen 24 uur, en verstrekt geen persoonsgegevens aan "
    "derden zonder toestemming' is TWO norms (an OBLIGATION to report, a PROHIBITION on "
    "disclosure) -- do not merge them into one. Conversely, do NOT split a single duty "
    "into multiple entries just because it has several qualifying clauses, conditions, "
    "or a list of required contents (e.g. a report that 'must contain a, b, and c' is "
    "still ONE norm; a, b, c belong in that one norm's own fields, not three norms). If "
    "the paragraph states no independent rule at all (pure definitions, a cross-"
    "reference, procedural text with no actor/action of its own), return an empty list."
)

_FRAMING_DIRECT = (
    "You are extracting the compliance obligation(s) from one paragraph of Dutch or EU "
    "digital-law legislation. Read the paragraph and, for each independent norm it "
    "states, extract: who it addresses (addressee, addressee_type), what triggers the "
    "obligation (trigger_event), what must be done (action), who receives it "
    "(recipient_body), any deadline as it literally appears in the text (deadline_raw), "
    "any numeric thresholds (thresholds), any conditions that limit when the rule "
    "applies (conditions), and whether the text explicitly defers to another provision "
    "(deference, e.g. from 'onverminderd', 'in afwijking van', 'is niet van toepassing "
    "indien'). "
    + _SCHEMA_FIELDS_NOTE + _MULTI_NORM_NOTE
)

_FRAMING_STEPBACK = (
    "You are verifying a legal-compliance extraction, so read carefully rather than "
    "pattern-matching. First work out, for this one paragraph only, exactly what "
    "rule(s) it imposes and on whom -- do not assume anything the text does not "
    "explicitly state, and do not pull in obligations from other paragraphs you may "
    "recognise from context. Then, for each independent norm, populate: deontic, "
    "addressee, addressee_type, trigger_event, action, recipient_body, deadline_raw, "
    "thresholds, conditions, deference. "
    + _SCHEMA_FIELDS_NOTE + _MULTI_NORM_NOTE
)


def _build_input(framing: str, article_heading: str, unit_text: str) -> str:
    return (
        f"{framing}\n\n"
        f"Article/provision heading: {article_heading}\n\n"
        f"Paragraph text:\n{unit_text}"
    )


# ---------------------------------------------------------------------------
# Source scope -- widened to the full corpus (2026-09-23 decision), superseding the
# incident-reporting-only anchor set now that C1 has been validated against it. One
# deliberate exclusion kept: the Telecommunicatiewet is scoped to Hoofdstuk 11
# ("Bescherming van persoonsgegevens en de persoonlijke levenssfeer") only, not its
# other 359 articles (spectrum policy, cable rights-of-way, telecom market-dominance
# rules, wiretapping procedure) -- none of that is "digital law" in this project's
# sense, and extracting it would waste most of the run's budget on content that cannot
# possibly relate to GDPR/NIS2/DORA/AI Act, while adding noise to candidate generation.
#
# Uitvoeringswet dataverordening now reads from the STANDARD pipeline file
# (data/BWBR0051796_2025-11-21_provisions.json) instead of the hand-built
# Datasets/ file used for the anchor set -- fixing, not working around, the citation-
# graph uid mismatch documented in detect_c1_contradiction.py's module docstring: the
# standard file already carries the same `uid` values the graph indexes, so no
# reconstruction is needed once this is the source of truth going forward.
# ---------------------------------------------------------------------------


@dataclass
class SourceSpec:
    label: str
    path: str
    get_provisions: Callable[[dict], list]
    in_scope: Callable[[dict], bool]


SOURCES = [
    SourceSpec(
        label="Cbw (full)",
        path="data/BWBR0052872_2026-08-15_provisions.json",
        get_provisions=lambda root: root,
        in_scope=lambda p: True,
    ),
    SourceSpec(
        label="UAVG (full)",
        path="data/BWBR0040940_2026-09-01_provisions.json",
        get_provisions=lambda root: root,
        in_scope=lambda p: True,
    ),
    SourceSpec(
        label="Wdo (full)",
        path="data/BWBR0048156_2025-11-11_provisions.json",
        get_provisions=lambda root: root,
        in_scope=lambda p: True,
    ),
    SourceSpec(
        label="Telecommunicatiewet, Hoofdstuk 11 only (privacy/ePrivacy provisions)",
        path="data/BWBR0009950_2026-08-15_provisions.json",
        get_provisions=lambda root: root,
        in_scope=lambda p: bool(p.get("chapter")) and "Hoofdstuk 11" in p["chapter"],
    ),
    SourceSpec(
        label="Uitvoeringswet dataverordening (full)",
        path="data/BWBR0051796_2025-11-21_provisions.json",
        get_provisions=lambda root: root,
        in_scope=lambda p: True,
    ),
    SourceSpec(
        label="GDPR (full)",
        path="data/32016R0679_original_provisions.json",
        get_provisions=lambda root: root,
        in_scope=lambda p: True,
    ),
    SourceSpec(
        label="NIS2 (full)",
        path="data/32022L2555_original_provisions.json",
        get_provisions=lambda root: root,
        in_scope=lambda p: True,
    ),
    SourceSpec(
        label="DORA (full)",
        path="data/32022R2554_original_provisions.json",
        get_provisions=lambda root: root,
        in_scope=lambda p: True,
    ),
    SourceSpec(
        label="AI Act (full, pre-Digital-Omnibus per ADR-0004)",
        path="data/32024R1689_original_provisions.json",
        get_provisions=lambda root: root,
        in_scope=lambda p: True,
    ),
    SourceSpec(
        label="Bijlage 35 (DORA competent-authority designation)",
        path="Datasets/Dutch Laws/bijlage35_dataset.json",
        get_provisions=lambda root: root["bijlage_35"]["substantive_text"]["provisions"],
        in_scope=lambda p: True,  # all 5 sub-provisions are in scope; the annex is short
    ),
]


@dataclass
class Unit:
    provision: dict          # live reference into the loaded JSON -- mutated in place
    paragraph_number: Optional[str]   # DISPLAYED number -- for humans, not for identity
    paragraph_index: Optional[int]    # position in provision["paragraphs"] -- for identity
    text: str
    article_heading: str


def _iter_units(provisions: list[dict]) -> list[Unit]:
    """Skips a paragraph that already has a finalized norms[] entry, so re-running this
    script (e.g. after a --limit smoke test, or after fixing something and resuming) does
    not duplicate norms already written -- the script has no other record of what it has
    already processed, so this check is the only thing that makes re-running safe.

    Tracks "already done" by paragraph_index, not the displayed number (2026-09-24 fix):
    checked directly against real data -- three articles in this corpus have two
    paragraphs sharing the same displayed number (a genuine numbering artifact in the
    source HTML/XML, not a parser bug; see migrate_paragraph_index.py's module
    docstring). Tracking by number alone would make _iter_units() believe the SECOND
    paragraph was already done as soon as the first one got a norm, silently skipping it
    forever. Falls back to number-based tracking only for a norm written before this
    field existed (paragraph_index absent -- pre-migration data)."""
    units = []
    for p in provisions:
        heading = p.get("heading") or f"Article {p.get('article') or p.get('number')}"
        p.setdefault("norms", [])
        # _empty_paragraph_indices (2026-09-24, item 3): a paragraph can now resolve to
        # ZERO norms (both passes agreeing there's no independent rule here) -- that's a
        # genuinely resolved, useful result, not "not yet processed", so it needs its
        # own persisted marker; norms[] alone can no longer tell "done with nothing to
        # show" apart from "never attempted", now that an empty result is possible.
        done_indices = ({n["paragraph_index"] for n in p["norms"] if n.get("paragraph_index") is not None}
                        | set(p.get("_empty_paragraph_indices") or []))
        done_numbers_legacy = {n["number"] for n in p["norms"] if n.get("paragraph_index") is None}
        paragraphs = p.get("paragraphs") or []
        if paragraphs:
            for idx, para in enumerate(paragraphs):
                if idx in done_indices or para["number"] in done_numbers_legacy:
                    continue
                units.append(Unit(p, para["number"], idx, para["text"], heading))
        elif None not in done_indices and None not in done_numbers_legacy:
            units.append(Unit(p, None, None, p["text"], heading))
    return units


# ---------------------------------------------------------------------------
# The two-pass extraction itself.
# ---------------------------------------------------------------------------


def _call_with_network_retries(client, **kwargs):
    """A long run of ~200+ sequential API calls will occasionally hit a transient
    connection blip (seen in practice: one getaddrinfo failure ~120 calls into a run)
    that has nothing to do with the extraction itself. Retry a few times with backoff
    before giving up -- this is separate from _run_pass's verbatim-validation retry,
    which retries because the *content* was wrong, not because the network was."""
    import time

    from openai import APIConnectionError

    delays = [2, 5, 15]
    for attempt, delay in enumerate([0] + delays):
        if delay:
            print(f"    [network] retrying after connection error (attempt {attempt + 1})")
            time.sleep(delay)
        try:
            return client.responses.parse(**kwargs)
        except APIConnectionError:
            if attempt == len(delays):
                raise


def _run_pass(client, model: str, framing: str, heading: str, text: str,
              reasoning_effort: str = "none") -> Optional[list[ExtractedNorm]]:
    prompt = _build_input(framing, heading, text)
    kwargs = dict(
        model=model,
        input=prompt,
        text_format=ExtractedNormsResponse,
        # Default "none": Stage 6 extraction is closer to structured reading than to
        # reasoning (architecture doc, Stage 6) -- it doesn't need the reasoning dial
        # turned up for the ordinary case. Raised deliberately (e.g. "medium") when
        # retrying items that disagreed even after a stronger model -- at that point
        # the open question is specifically whether more reasoning depth, not a
        # different model, resolves the ambiguity.
        reasoning={"effort": reasoning_effort},
    )
    if reasoning_effort == "none":
        # temperature=0 is what makes the two-pass check meaningful (deterministic
        # output per framing) -- but confirmed against the live API that this model
        # family rejects `temperature` outright once reasoning is actually engaged
        # (BadRequestError: "temperature is not supported with this model" at
        # reasoning_effort="medium"), so it's only safe to pass at effort "none".
        kwargs["temperature"] = 0
    for attempt in range(2):  # one retry if ANY returned norm fails verbatim validation
        resp = _call_with_network_retries(client, **kwargs)
        norms = resp.output_parsed.norms
        if all(_verbatim_ok(n, text) for n in norms):
            return norms
        print(f"    [pass validation] attempt {attempt + 1} failed verbatim check, "
              f"{'retrying' if attempt == 0 else 'giving up on this pass'}")
    return None


def _text_fields_agree(a: Optional[str], b: Optional[str], min_overlap: float = 0.5) -> bool:
    """Free-text fields (addressee, action, trigger_event) compared by normalized word
    overlap, not exact string equality (2026-09-24 fix, item 10): two independently-
    framed prompts routinely paraphrase the same verbatim span differently even when
    they agree on substance, and requiring byte-identical text would manufacture
    disagreements out of harmless rewording. Still meaningfully stricter than "both
    non-null": a genuinely different span scores low overlap and is correctly flagged."""
    na, nb = _normalize(a or ""), _normalize(b or "")
    if not na and not nb:
        return True
    if not na or not nb:
        return False
    wa, wb = set(na.split()), set(nb.split())
    if not wa or not wb:
        return na == nb
    return len(wa & wb) / min(len(wa), len(wb)) >= min_overlap


def _list_fields_agree(a: Optional[list], b: Optional[list], min_overlap: float = 0.5) -> bool:
    """recipient_body/thresholds/conditions compared as sets of normalized strings,
    with the same overlap tolerance as _text_fields_agree and for the same reason."""
    sa, sb = {_normalize(x) for x in (a or [])}, {_normalize(x) for x in (b or [])}
    if not sa and not sb:
        return True
    if not sa or not sb:
        return False
    return len(sa & sb) / min(len(sa), len(sb)) >= min_overlap


def _deadlines_agree(a: Optional[str], b: Optional[str]) -> bool:
    """Two real bugs fixed here (2026-09-24), both confirmed against the actual
    function's behavior, not hypothetical: (1) when NEITHER pass's phrasing matched a
    known pattern, both come back {"value": None, "unit": None, ...} and the old check
    (`pa["value"] == pb["value"]`) read None == None as agreement -- meaning two
    COMPLETELY DIFFERENT unparseable deadline phrases were silently treated as the same
    deadline. Now requires the raw text itself to match when neither side parsed.
    (2) the starting event ("from" -- e.g. "vanaf kennisname" vs. "vanaf de melding
    door een derde") was extracted but never compared, so two deadlines with the same
    number/unit but a different trigger point for the clock also silently agreed."""
    pa, pb = _parse_deadline(a), _parse_deadline(b)
    if pa is None or pb is None:
        return pa == pb
    if pa["value"] is None and pb["value"] is None:
        return _normalize(pa["raw"]) == _normalize(pb["raw"])
    return (pa["value"] == pb["value"] and pa["unit"] == pb["unit"]
            and _normalize(pa["from"] or "") == _normalize(pb["from"] or ""))


def _finalize_norm(norm: ExtractedNorm, paragraph_number: Optional[str], paragraph_index: Optional[int],
                    norm_index: int, model: str, reasoning_effort: str = "none") -> dict:
    model_label = model if reasoning_effort == "none" else f"{model} (reasoning:{reasoning_effort})"
    return {
        "number": paragraph_number,
        "paragraph_index": paragraph_index,
        # norm_index (2026-09-24, item 3): position among the norms finalized for THIS
        # paragraph -- needed once a paragraph can yield more than one, so each stays
        # independently addressable (paragraph_index alone is no longer unique once a
        # paragraph produces 2+ norms).
        "norm_index": norm_index,
        "deontic": norm.deontic,
        "addressee": norm.addressee,
        "addressee_type": norm.addressee_type,
        "trigger_event": norm.trigger_event,
        "action": norm.action,
        "recipient_body": norm.recipient_body,
        "deadline": _parse_deadline(norm.deadline_raw),
        "thresholds": norm.thresholds,
        "conditions": norm.conditions,
        "deference": norm.deference,
        "extraction_confidence": norm.extraction_confidence,
        "extraction_model": model_label,
        "extracted_on": date.today().isoformat(),
        "human_verified": False,
    }


def _norm_overlap_score(a: ExtractedNorm, b: ExtractedNorm) -> float:
    """Combined similarity used only to ALIGN two passes' norm lists to each other when
    a paragraph yields more than one (2026-09-24, item 3) -- not itself a pass/fail
    agreement check (that's _norms_agree, run per aligned pair afterwards)."""
    score = 1.0 if a.deontic == b.deontic else 0.0
    for fa, fb in ((a.action, b.action), (a.trigger_event, b.trigger_event), (a.addressee, b.addressee)):
        na, nb = _normalize(fa or ""), _normalize(fb or "")
        if not na or not nb:
            continue
        wa, wb = set(na.split()), set(nb.split())
        if wa and wb:
            score += len(wa & wb) / min(len(wa), len(wb))
    return score


def _align_norms(list1: list[ExtractedNorm], list2: list[ExtractedNorm]
                  ) -> list[tuple[Optional[ExtractedNorm], Optional[ExtractedNorm]]]:
    """Greedy best-match pairing between two passes' norm lists, by descending overlap
    score -- appropriate for the small lists (almost always 0-3) one paragraph produces;
    a paragraph with a genuinely large, ambiguous number of candidate norms is exactly
    the kind of case worth a human's eyes anyway. Unmatched entries on either side pair
    with None -- signals a count/alignment mismatch to the caller, handled as a
    disagreement rather than guessed at."""
    scored = [(_norm_overlap_score(n1, n2), i1, i2)
              for i1, n1 in enumerate(list1) for i2, n2 in enumerate(list2)]
    scored.sort(key=lambda t: -t[0])
    used1, used2 = set(), set()
    pairs = []
    for score, i1, i2 in scored:
        if i1 in used1 or i2 in used2 or score <= 0:
            continue
        used1.add(i1)
        used2.add(i2)
        pairs.append((list1[i1], list2[i2]))
    pairs.extend((n1, None) for i1, n1 in enumerate(list1) if i1 not in used1)
    pairs.extend((None, n2) for i2, n2 in enumerate(list2) if i2 not in used2)
    return pairs


def _norms_agree(n1: ExtractedNorm, n2: ExtractedNorm) -> bool:
    """Field-specific agreement for one aligned pair (2026-09-24 fix, item 10): the old
    check only compared deontic/deadline/deference, then saved pass1's addressee/
    action/recipient_body/thresholds/conditions wholesale -- meaning two passes could
    wildly disagree on WHO the norm addresses or WHAT it requires and still get
    finalized as "agreed", as long as those three specific fields happened to match.
    Every field detection actually reads is now checked; free-text fields use word
    overlap rather than exact-string equality (see _text_fields_agree's own note),
    since two differently-framed prompts routinely paraphrase the same verbatim span."""
    return (
        n1.deontic == n2.deontic
        and n1.addressee_type == n2.addressee_type
        and _deadlines_agree(n1.deadline_raw, n2.deadline_raw)
        and _normalize(n1.deference or "") == _normalize(n2.deference or "")
        and _text_fields_agree(n1.addressee, n2.addressee)
        and _text_fields_agree(n1.action, n2.action)
        and _text_fields_agree(n1.trigger_event, n2.trigger_event)
        and _list_fields_agree(n1.recipient_body, n2.recipient_body)
        and _list_fields_agree(n1.thresholds, n2.thresholds)
        and _list_fields_agree(n1.conditions, n2.conditions)
    )


def extract_unit(client, model: str, unit: Unit,
                  reasoning_effort: str = "none") -> tuple[Optional[list[dict]], Optional[dict]]:
    """Returns (finalized_norms_or_None, review_queue_item_or_None) -- exactly one of
    the two is non-None. `finalized_norms` can be an EMPTY list (2026-09-24, item 3):
    both passes confirming "no independent norm here" is itself a resolved, useful
    result, not a failure -- distinct from None, which means still unresolved.
    Returning the queue item instead of mutating a shared list lets the caller persist
    it immediately, so a crash on unit N+1 doesn't lose unit N's result."""
    pass1 = _run_pass(client, model, _FRAMING_DIRECT, unit.article_heading, unit.text, reasoning_effort)
    pass2 = _run_pass(client, model, _FRAMING_STEPBACK, unit.article_heading, unit.text, reasoning_effort)
    base = {
        "instrument_id": unit.provision.get("instrument_id") or unit.provision.get("provision_id"),
        "article": unit.provision.get("article") or unit.provision.get("number"),
        "paragraph_number": unit.paragraph_number,
        "paragraph_index": unit.paragraph_index,
        "unit_text": unit.text,
    }

    if pass1 is None or pass2 is None:
        return None, {**base, "reason": "validation_failed",
                      "pass1": [n.model_dump() for n in pass1] if pass1 is not None else None,
                      "pass2": [n.model_dump() for n in pass2] if pass2 is not None else None}

    if len(pass1) != len(pass2):
        # The two passes disagree on HOW MANY independent norms this paragraph
        # contains -- not something to guess at (which count is "right"?), so this is
        # queued distinctly from a same-count field disagreement, and NOT auto-resolved
        # by resolve_queue_conservatively() (see that function's own note).
        return None, {**base, "reason": "two_pass_disagreement", "disagreement_kind": "norm_count_mismatch",
                      "pass1": [n.model_dump() for n in pass1], "pass2": [n.model_dump() for n in pass2]}

    aligned = _align_norms(pass1, pass2)
    if any(n1 is None or n2 is None for n1, n2 in aligned):
        return None, {**base, "reason": "two_pass_disagreement",
                      "disagreement_kind": "norm_alignment_ambiguous",
                      "pass1": [n.model_dump() for n in pass1], "pass2": [n.model_dump() for n in pass2]}

    if not all(_norms_agree(n1, n2) for n1, n2 in aligned):
        return None, {**base, "reason": "two_pass_disagreement",
                      "pass1": [n.model_dump() for n in pass1], "pass2": [n.model_dump() for n in pass2]}

    finalized = [_finalize_norm(n1, unit.paragraph_number, unit.paragraph_index, idx, model, reasoning_effort)
                 for idx, (n1, n2) in enumerate(aligned)]
    return finalized, None


# ---------------------------------------------------------------------------
# Retrying the review queue on a different model -- e.g. escalating from
# gpt-5.6-luna to gpt-5.6-terra per the 2026-09-20 decision, once Luna's own
# queue rate gives a concrete signal that it's worth trying a stronger model
# on specifically the items that disagreed, rather than re-running everything.
# ---------------------------------------------------------------------------


def _provision_key(p: dict) -> tuple:
    """The same identity extract_unit() used when it wrote a queue item's
    instrument_id/article -- reconstructing it here is what lets a queue item be
    relocated back to the exact provision (and source file) it came from."""
    return (
        str(p.get("instrument_id") or p.get("provision_id")),
        str(p.get("article") or p.get("number")),
    )


def retry_queue(client, model: str, reasoning_effort: str = "none", concurrency: int = 10) -> None:
    """Parallelized 2026-09-23 for full-corpus scale (768-item queues are impractical
    one at a time). A single lock guards both the per-source file writes and the queue
    file's own state -- the critical section is just local dict/list bookkeeping plus a
    JSON dump, not the network call, so serializing it costs nothing next to the API
    latency this is actually trying to parallelize."""
    queue_path = ROOT / "data" / "stage6_review_queue.json"
    queue = json.loads(queue_path.read_text(encoding="utf-8")) if queue_path.exists() else []
    if not queue:
        print("review queue is empty -- nothing to retry")
        return

    loaded = []  # (root, path, {provision_key: provision})
    for spec in SOURCES:
        path = ROOT / spec.path
        root = json.loads(path.read_text(encoding="utf-8"))
        by_key = {_provision_key(p): p for p in spec.get_provisions(root)}
        loaded.append((root, path, by_key))

    retried_with_label = model if reasoning_effort == "none" else f"{model} (reasoning:{reasoning_effort})"
    lock = threading.Lock()
    remaining_queue: list = []
    n_resolved = 0
    dirty_paths: set = set()

    def relocate(item):
        key = (str(item["instrument_id"]), str(item["article"]))
        for candidate_root, candidate_path, by_key in loaded:
            if key in by_key:
                return by_key[key], candidate_path, candidate_root
        return None, None, None

    unresolved_lookup = []  # items that couldn't even be relocated -- no retry possible
    to_retry = []
    for item in queue:
        provision, path, root = relocate(item)
        if provision is None:
            unresolved_lookup.append(item)
        else:
            to_retry.append((item, provision, path, root))
    remaining_queue.extend(unresolved_lookup)
    for item in unresolved_lookup:
        print(f"  [warn] could not relocate provision for "
              f"{(item['instrument_id'], item['article'])} -- keeping queue entry as-is")

    def process(entry):
        item, provision, path, root = entry
        key = (str(item["instrument_id"]), str(item["article"]))
        unit = Unit(provision, item["paragraph_number"], item.get("paragraph_index"), item["unit_text"],
                    provision.get("heading") or f"Article {item['article']}")
        norms, new_item = extract_unit(client, model, unit, reasoning_effort)
        with lock:
            nonlocal n_resolved
            if norms is not None:
                if norms:
                    provision.setdefault("norms", []).extend(norms)
                elif unit.paragraph_index is not None:
                    provision.setdefault("_empty_paragraph_indices", []).append(unit.paragraph_index)
                n_resolved += 1
                dirty_paths.add(path)
            else:
                new_item["retried_with"] = retried_with_label
                remaining_queue.append(new_item)
        return key, item["paragraph_number"], norms is not None

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(process, entry): entry for entry in to_retry}
        for i, fut in enumerate(as_completed(futures), 1):
            key, para, resolved = fut.result()
            print(f"  [{i}/{len(to_retry)}] retried {key} para {para} with {model} "
                  f"(reasoning: {reasoning_effort}) -- {'resolved' if resolved else 'still unresolved'}",
                  flush=True)
            if i % 25 == 0 or i == len(to_retry):
                with lock:
                    for root, path, _ in loaded:
                        if path in dirty_paths:
                            path.write_text(json.dumps(root, ensure_ascii=False, indent=1), encoding="utf-8")
                    queue_path.write_text(json.dumps(remaining_queue, ensure_ascii=False, indent=1),
                                           encoding="utf-8")

    # Final flush, covers any tail not caught by the periodic checkpoint above.
    for root, path, _ in loaded:
        if path in dirty_paths:
            path.write_text(json.dumps(root, ensure_ascii=False, indent=1), encoding="utf-8")
    queue_path.write_text(json.dumps(remaining_queue, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\nretried {len(queue)} queued unit(s) with {model} (reasoning: {reasoning_effort}): "
          f"{n_resolved} resolved, {len(remaining_queue)} still unresolved")


# ---------------------------------------------------------------------------
# Conservatively resolving whatever is left after model/reasoning escalation
# stops being worth it (2026-09-21 decision). The two-pass check exists to
# avoid GUESSING on a genuine judgement call -- but leaving an item stuck in
# the review queue forever, before Stage 4/5's detection engine has even run,
# asks a human to pre-clear an abstract sentence that may never end up
# mattering to any actual finding. The system already requires expert
# confirmation before ANY finding is reportable (Part 6, report_eligible),
# so the honest fix is to push these through now with a documented,
# non-hiding default, not to gate the whole pipeline on a lawyer's calendar.
#
# The asymmetry that makes a *default* safe here: `deference` is the one
# field that can make a real problem invisible (Part 5, C1's resolution
# filter drops a pair entirely once one side defers) -- so an uncertain
# deference call defaults to null, never guessed at, so nothing gets
# silently suppressed. `deontic` decides whether a norm is even considered
# for detection at all (only OBLIGATION/PROHIBITION/PERMISSION/COMPETENCE
# are eligible) -- so an uncertain deontic call defaults toward whichever
# reading keeps the norm eligible, for the same reason: excluding it by
# mistake is the direction that hides something, including it by mistake
# just means a human dismisses a harmless candidate later, same as any
# other false positive the system already expects to produce.
# ---------------------------------------------------------------------------

ACTIONABLE_DEONTICS = {"OBLIGATION", "PROHIBITION", "PERMISSION", "COMPETENCE"}


def _pick_deontic(d1: str, d2: str) -> str:
    if d1 in ACTIONABLE_DEONTICS and d2 not in ACTIONABLE_DEONTICS:
        return d1
    if d2 in ACTIONABLE_DEONTICS and d1 not in ACTIONABLE_DEONTICS:
        return d2
    return d1  # both actionable, both not, or identical -- no basis to prefer one


def resolve_queue_conservatively() -> None:
    queue_path = ROOT / "data" / "stage6_review_queue.json"
    queue = json.loads(queue_path.read_text(encoding="utf-8")) if queue_path.exists() else []
    if not queue:
        print("review queue is empty -- nothing to resolve")
        return

    loaded = []
    for spec in SOURCES:
        path = ROOT / spec.path
        root = json.loads(path.read_text(encoding="utf-8"))
        by_key = {_provision_key(p): p for p in spec.get_provisions(root)}
        loaded.append((root, path, by_key))

    dropped = []  # validation_failed items -- no verbatim-grounded data to fall back on
    dirty_paths = set()
    n_resolved = 0

    for item in queue:
        key = (str(item["instrument_id"]), str(item["article"]))
        provision = path = root = None
        for candidate_root, candidate_path, by_key in loaded:
            if key in by_key:
                provision, path, root = by_key[key], candidate_path, candidate_root
                break
        if provision is None:
            print(f"  [warn] could not relocate provision for {key} -- dropping without a norm")
            dropped.append(item)
            continue

        # pass1/pass2 are LISTS of norm dicts now (2026-09-24, item 3) -- one unit can
        # yield zero, one, or several independent norms. norm_count_mismatch/
        # norm_alignment_ambiguous (extract_unit's own distinct disagreement_kinds) are
        # NOT auto-resolved here: there's no safe "conservative default" for "the two
        # passes disagree on HOW MANY norms exist" the way there is for a single
        # mismatched field on an already-agreed-count pair -- guessing which count is
        # right is exactly the kind of judgement call this function exists to avoid.
        if item.get("disagreement_kind") in ("norm_count_mismatch", "norm_alignment_ambiguous"):
            dropped.append(item)
            continue

        if item["reason"] == "validation_failed":
            # Checked directly against the real queue (2026-09-24 fix): a meaningful
            # share of these items DO have one pass that fully passed verbatim
            # validation -- the OTHER pass is what's null (a model error, refusal, or a
            # verbatim check that still failed after its own retry). The old code
            # dropped these uniformly, discarding genuinely grounded extractions on the
            # assumption that "validation_failed" meant neither pass was usable, which
            # isn't what the data actually shows. Every norm in the single grounded
            # pass's list becomes a provisional norm, same non-suppressing deference
            # default as the two-pass case below, tagged uncertain for exactly the
            # reason it's less confirmed (one reading, not two independently agreeing
            # ones) -- only genuinely dropped when BOTH passes are null.
            grounded = item.get("pass1") if item.get("pass1") is not None else item.get("pass2")
            if grounded is None:
                dropped.append(item)
                continue
            if not grounded and item.get("paragraph_index") is not None:
                provision.setdefault("_empty_paragraph_indices", []).append(item["paragraph_index"])
            for idx, g in enumerate(grounded):
                norm = {
                    "number": item["paragraph_number"],
                    "paragraph_index": item.get("paragraph_index"),
                    "norm_index": idx,
                    "deontic": g["deontic"],
                    "addressee": g["addressee"],
                    "addressee_type": g["addressee_type"],
                    "trigger_event": g["trigger_event"],
                    "action": g["action"],
                    "recipient_body": g["recipient_body"],
                    "deadline": _parse_deadline(g["deadline_raw"]),
                    "thresholds": g["thresholds"],
                    "conditions": g["conditions"],
                    "deference": None,  # non-suppressing default -- see module note above
                    "extraction_confidence": g["extraction_confidence"],
                    "extraction_model": "conservative-default (single-pass: the other pass "
                                         "failed validation entirely)",
                    "extracted_on": date.today().isoformat(),
                    "human_verified": False,
                    "extraction_uncertain": True,
                    "uncertainty_note": (
                        "only one of two extraction passes produced a verbatim-grounded "
                        "result; the other failed validation even after its own retry, or "
                        "returned nothing usable. This rests on a single unconfirmed "
                        "reading, not two independently agreeing ones -- revisit if it "
                        "ends up inside an actual candidate finding."
                    ),
                }
                provision.setdefault("norms", []).append(norm)
            n_resolved += 1
            dirty_paths.add(path)
            continue

        # Plain field-level disagreement, matched norm count (item 3): re-align the two
        # passes' stored lists the same way extract_unit did in memory, then apply the
        # SAME per-field conservative-default merge as before, per aligned pair.
        p1_norms = [ExtractedNorm(**d) for d in item["pass1"]]
        p2_norms = [ExtractedNorm(**d) for d in item["pass2"]]
        aligned = _align_norms(p1_norms, p2_norms)
        if any(n1 is None or n2 is None for n1, n2 in aligned):
            # Defensive only -- extract_unit wouldn't have written this reason if
            # alignment were ambiguous, but never guess if it somehow is.
            dropped.append(item)
            continue
        if not aligned and item.get("paragraph_index") is not None:
            provision.setdefault("_empty_paragraph_indices", []).append(item["paragraph_index"])
        for idx, (p1, p2) in enumerate(aligned):
            diffs = [f for f in ("deontic", "deadline_raw", "deference")
                     if getattr(p1, f) != getattr(p2, f)]
            deontic = _pick_deontic(p1.deontic, p2.deontic)
            base = p1 if deontic == p1.deontic else p2

            norm = {
                "number": item["paragraph_number"],
                "paragraph_index": item.get("paragraph_index"),
                "norm_index": idx,
                "deontic": deontic,
                "addressee": base.addressee,
                "addressee_type": base.addressee_type,
                "trigger_event": base.trigger_event,
                "action": base.action,
                "recipient_body": base.recipient_body,
                "deadline": _parse_deadline(base.deadline_raw),
                "thresholds": base.thresholds,
                "conditions": base.conditions,
                "deference": None,  # non-suppressing default -- see module note above
                "extraction_confidence": min(p1.extraction_confidence, p2.extraction_confidence),
                "extraction_model": "conservative-default (two passes disagreed; see uncertainty_note)",
                "extracted_on": date.today().isoformat(),
                "human_verified": False,
                "extraction_uncertain": True,
                "uncertainty_note": (
                    f"two independent extraction passes disagreed on {diffs}; "
                    f"pass1={{'deontic': {p1.deontic!r}, 'deference': {p1.deference!r}}}, "
                    f"pass2={{'deontic': {p2.deontic!r}, 'deference': {p2.deference!r}}} "
                    "-- resolved to the non-suppressing default rather than a human pre-clearing "
                    "it; revisit if this norm ends up inside an actual candidate finding."
                ),
            }
            provision.setdefault("norms", []).append(norm)
        n_resolved += 1
        dirty_paths.add(path)

    for root, path, _ in loaded:
        if path in dirty_paths:
            path.write_text(json.dumps(root, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"  wrote back -> {path.relative_to(ROOT)}")

    dropped_path = ROOT / "data" / "stage6_unresolved_extractions.json"
    existing_dropped = json.loads(dropped_path.read_text(encoding="utf-8")) if dropped_path.exists() else []
    existing_dropped.extend(dropped)
    dropped_path.write_text(json.dumps(existing_dropped, ensure_ascii=False, indent=1), encoding="utf-8")

    queue_path.write_text(json.dumps([], ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"{n_resolved} norms[] entries finalized with the non-suppressing default "
          f"(flagged extraction_uncertain=true), {len(dropped)} dropped -- no verbatim-"
          f"grounded data to fall back on -- logged to data/stage6_unresolved_extractions.json")


def _save_queue_item(item: dict) -> None:
    """Persisted immediately, one item at a time -- see main()'s docstring note on
    crash-safety. Replaces any existing queue record for the same unit rather than
    piling up a duplicate next to it."""
    queue_path = ROOT / "data" / "stage6_review_queue.json"
    existing = json.loads(queue_path.read_text(encoding="utf-8")) if queue_path.exists() else []
    key = (item["instrument_id"], item["article"], item["paragraph_number"])
    existing = [r for r in existing if (r["instrument_id"], r["article"], r["paragraph_number"]) != key]
    existing.append(item)
    queue_path.write_text(json.dumps(existing, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    """Writes the source file back to disk after every single unit, not once per source
    at the end -- a ~200-call run over a real network will occasionally hit a transient
    connection error partway through a source (seen in practice), and without per-unit
    persistence everything done in that source since its last write would be silently
    lost when the process dies. The extra disk I/O this costs is negligible next to an
    API call's latency."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--dry-run", action="store_true",
                     help="print scope and unit counts, make no API calls")
    ap.add_argument("--only", default=None, help="substring match against a source's label")
    ap.add_argument("--limit", type=int, default=None,
                     help="stop after this many units total, across all sources")
    ap.add_argument("--retry-queue", action="store_true",
                     help="re-attempt data/stage6_review_queue.json's items with --model "
                          "instead of running the anchor set from scratch")
    ap.add_argument("--resolve-queue", action="store_true",
                     help="finalize whatever remains in data/stage6_review_queue.json using "
                          "the non-suppressing conservative default (see resolve_queue_conservatively "
                          "docstring), instead of retrying with a model. No API calls made.")
    ap.add_argument("--reasoning-effort", default="none",
                     choices=["none", "minimal", "low", "medium", "high", "xhigh", "max"],
                     help="default none (Stage 6 extraction doesn't need it); raise this "
                          "deliberately when retrying items that disagreed even under a "
                          "stronger model, to test reasoning depth as a separate lever "
                          "from model choice")
    ap.add_argument("--concurrency", type=int, default=10,
                     help="parallel units in flight per source -- each unit still makes its "
                          "own 2 sequential passes internally, so actual concurrent API calls "
                          "run up to ~2x this. Full-corpus scale (2026-09-23: ~2,000 new "
                          "paragraphs) is impractical run strictly sequentially.")
    args = ap.parse_args()

    client = None
    if not args.dry_run:
        from openai import OpenAI
        client = OpenAI()  # reads OPENAI_API_KEY from the environment

    if args.resolve_queue:
        resolve_queue_conservatively()
        return

    if args.retry_queue:
        retry_queue(client, args.model, args.reasoning_effort, args.concurrency)
        return

    n_processed = 0
    n_finalized = 0
    n_queued = 0

    from concurrent.futures import ThreadPoolExecutor, as_completed

    for spec in SOURCES:
        if args.only and args.only.lower() not in spec.label.lower():
            continue

        path = ROOT / spec.path
        root = json.loads(path.read_text(encoding="utf-8"))
        all_provisions = spec.get_provisions(root)
        in_scope_provisions = [p for p in all_provisions if spec.in_scope(p)]
        units = _iter_units(in_scope_provisions)

        print(f"\n=== {spec.label} ({spec.path}) ===")
        print(f"  {len(in_scope_provisions)} provision(s) in scope, {len(units)} extraction unit(s)", flush=True)

        if args.dry_run:
            continue

        remaining = args.limit - n_processed if args.limit is not None else None
        if remaining is not None and remaining <= 0:
            break  # --limit budget exhausted -- stop entirely, no more sources either
        batch = units[:remaining] if remaining is not None else units
        if not batch:
            # THIS source has nothing to do (2026-09-24 fix: a real, confirmed bug --
            # this used to `break`, which abandoned every LATER source too, not just
            # this one. Never surfaced before because a full-corpus run rarely had an
            # early source hit zero while later sources still had units queued --
            # exposed directly by a targeted re-extraction where Wdo legitimately had
            # 0 targeted paragraphs while GDPR/NIS2/DORA/AI Act had over 200 waiting,
            # and the run silently stopped after 8 units instead of processing 220.
            continue

        write_lock = threading.Lock()

        def process_unit(unit: Unit):
            norms, queue_item = extract_unit(client, args.model, unit, args.reasoning_effort)
            with write_lock:
                if norms is not None:
                    if norms:
                        unit.provision.setdefault("norms", []).extend(norms)
                    elif unit.paragraph_index is not None:
                        # Confirmed by both passes: no independent norm in this
                        # paragraph -- a real, resolved result (item 3), not "not yet
                        # processed"; recorded so _iter_units doesn't re-offer it forever.
                        unit.provision.setdefault("_empty_paragraph_indices", []).append(unit.paragraph_index)
                else:
                    _save_queue_item(queue_item)
                # Crash-safe, same reasoning as before: written after every unit, not just
                # at the end -- the lock serializes the write, not the (parallel) API calls.
                path.write_text(json.dumps(root, ensure_ascii=False, indent=1), encoding="utf-8")
            return norms is not None

        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            future_to_unit = {ex.submit(process_unit, u): u for u in batch}
            for i, fut in enumerate(as_completed(future_to_unit), 1):
                unit = future_to_unit[fut]
                n_processed += 1
                try:
                    finalized = fut.result()
                except Exception as e:
                    print(f"  [{i}/{len(batch)}] {unit.article_heading} -- ERROR: {e}", flush=True)
                    n_queued += 1
                    continue
                if finalized:
                    n_finalized += 1
                else:
                    n_queued += 1
                print(f"  [{i}/{len(batch)}] {unit.article_heading} "
                      f"{'para ' + unit.paragraph_number if unit.paragraph_number else '(whole provision)'} "
                      f"-- {'finalized' if finalized else 'queued'}", flush=True)

        if args.limit is not None and n_processed >= args.limit:
            break

    if args.dry_run:
        return

    print(f"\n{n_processed} unit(s) processed, {n_finalized} norms[] entries finalized, "
          f"{n_queued} sent to the review queue -> data/stage6_review_queue.json", flush=True)


if __name__ == "__main__":
    main()
