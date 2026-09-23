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
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Literal, Optional

from dotenv import load_dotenv
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
    """What the model itself returns for one paragraph/unit. Verbatim-checked fields
    (addressee, action, trigger_event, deadline_raw) are validated against the source
    text by _verbatim_ok() below, never trusted outright."""
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
    (re.compile(r"\bonverwijld\b", re.I), lambda m: {"value": 0, "unit": "immediate"}),
    (re.compile(r"\bonmiddellijk\b", re.I), lambda m: {"value": 0, "unit": "immediate"}),
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
    "paragraph, return null (or an empty list for the list fields), never invent a value. "
    "For deadline_raw specifically: a sentence often states both a vague qualifier "
    "('onverwijld', 'zonder onredelijke vertraging') AND a specific figure ('binnen 24 "
    "uur', 'uiterlijk 72 uur') as a fallback or outer bound on the same duty -- when both "
    "appear, extract the phrase containing the specific number, not the vague qualifier "
    "alone, since the number is what a later comparison against another law's deadline "
    "actually needs. Extract the vague qualifier only when no specific figure is present "
    "anywhere in the paragraph."
)

_FRAMING_DIRECT = (
    "You are extracting the compliance obligation from one paragraph of Dutch or EU "
    "digital-law legislation. Read the paragraph and extract: who it addresses "
    "(addressee, addressee_type), what triggers the obligation (trigger_event), what "
    "must be done (action), who receives it (recipient_body), any deadline as it "
    "literally appears in the text (deadline_raw), any numeric thresholds "
    "(thresholds), any conditions that limit when the rule applies (conditions), and "
    "whether the text explicitly defers to another provision (deference, e.g. from "
    "'onverminderd', 'in afwijking van', 'is niet van toepassing indien'). "
    + _SCHEMA_FIELDS_NOTE
)

_FRAMING_STEPBACK = (
    "You are verifying a legal-compliance extraction, so read carefully rather than "
    "pattern-matching. First work out, for this one paragraph only, exactly what rule "
    "it imposes and on whom -- do not assume anything the text does not explicitly "
    "state, and do not pull in obligations from other paragraphs you may recognise "
    "from context. Then populate: deontic, addressee, addressee_type, trigger_event, "
    "action, recipient_body, deadline_raw, thresholds, conditions, deference. "
    + _SCHEMA_FIELDS_NOTE
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
    paragraph_number: Optional[str]
    text: str
    article_heading: str


def _iter_units(provisions: list[dict]) -> list[Unit]:
    """Skips a paragraph that already has a finalized norms[] entry, so re-running this
    script (e.g. after a --limit smoke test, or after fixing something and resuming) does
    not duplicate norms already written -- the script has no other record of what it has
    already processed, so this check is the only thing that makes re-running safe."""
    units = []
    for p in provisions:
        heading = p.get("heading") or f"Article {p.get('article') or p.get('number')}"
        p.setdefault("norms", [])
        already_done = {n["number"] for n in p["norms"]}
        paragraphs = p.get("paragraphs") or []
        if paragraphs:
            for para in paragraphs:
                if para["number"] in already_done:
                    continue
                units.append(Unit(p, para["number"], para["text"], heading))
        elif None not in already_done:
            units.append(Unit(p, None, p["text"], heading))
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
              reasoning_effort: str = "none") -> Optional[ExtractedNorm]:
    prompt = _build_input(framing, heading, text)
    kwargs = dict(
        model=model,
        input=prompt,
        text_format=ExtractedNorm,
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
    for attempt in range(2):  # one retry on a verbatim-validation failure
        resp = _call_with_network_retries(client, **kwargs)
        norm = resp.output_parsed
        if _verbatim_ok(norm, text):
            return norm
        print(f"    [pass validation] attempt {attempt + 1} failed verbatim check, "
              f"{'retrying' if attempt == 0 else 'giving up on this pass'}")
    return None


def _deadlines_agree(a: Optional[str], b: Optional[str]) -> bool:
    pa, pb = _parse_deadline(a), _parse_deadline(b)
    if pa is None or pb is None:
        return pa == pb
    return pa["value"] == pb["value"] and pa["unit"] == pb["unit"]


def _finalize_norm(norm: ExtractedNorm, paragraph_number: Optional[str], model: str,
                    reasoning_effort: str = "none") -> dict:
    model_label = model if reasoning_effort == "none" else f"{model} (reasoning:{reasoning_effort})"
    return {
        "number": paragraph_number,
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


def extract_unit(client, model: str, unit: Unit,
                  reasoning_effort: str = "none") -> tuple[Optional[dict], Optional[dict]]:
    """Returns (finalized_norm_or_None, review_queue_item_or_None) -- exactly one of the
    two is non-None. Returning the queue item instead of mutating a shared list lets the
    caller persist it immediately, so a crash on unit N+1 doesn't lose unit N's result."""
    pass1 = _run_pass(client, model, _FRAMING_DIRECT, unit.article_heading, unit.text, reasoning_effort)
    pass2 = _run_pass(client, model, _FRAMING_STEPBACK, unit.article_heading, unit.text, reasoning_effort)
    base = {
        "instrument_id": unit.provision.get("instrument_id") or unit.provision.get("provision_id"),
        "article": unit.provision.get("article") or unit.provision.get("number"),
        "paragraph_number": unit.paragraph_number,
        "unit_text": unit.text,
    }

    if pass1 is None or pass2 is None:
        return None, {**base, "reason": "validation_failed",
                      "pass1": pass1.model_dump() if pass1 else None,
                      "pass2": pass2.model_dump() if pass2 else None}

    agree = (
        pass1.deontic == pass2.deontic
        and _deadlines_agree(pass1.deadline_raw, pass2.deadline_raw)
        and _normalize(pass1.deference or "") == _normalize(pass2.deference or "")
    )
    if not agree:
        return None, {**base, "reason": "two_pass_disagreement",
                      "pass1": pass1.model_dump(), "pass2": pass2.model_dump()}

    return _finalize_norm(pass1, unit.paragraph_number, model, reasoning_effort), None


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
        unit = Unit(provision, item["paragraph_number"], item["unit_text"],
                    provision.get("heading") or f"Article {item['article']}")
        norm, new_item = extract_unit(client, model, unit, reasoning_effort)
        with lock:
            nonlocal n_resolved
            if norm is not None:
                provision.setdefault("norms", []).append(norm)
                n_resolved += 1
                dirty_paths.add(path)
            else:
                new_item["retried_with"] = retried_with_label
                remaining_queue.append(new_item)
        return key, item["paragraph_number"], norm is not None

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

        if item["reason"] == "validation_failed":
            # Neither pass produced a span that was actually in the source text -- there
            # is nothing verbatim-grounded to fall back on, so this paragraph gets no
            # norms[] entry at all rather than one built on an already-rejected span.
            dropped.append(item)
            continue

        p1, p2 = item["pass1"], item["pass2"]
        diffs = [f for f in ("deontic", "deadline_raw", "deference") if p1[f] != p2[f]]
        deontic = _pick_deontic(p1["deontic"], p2["deontic"])
        base = p1 if deontic == p1["deontic"] else p2

        norm = {
            "number": item["paragraph_number"],
            "deontic": deontic,
            "addressee": base["addressee"],
            "addressee_type": base["addressee_type"],
            "trigger_event": base["trigger_event"],
            "action": base["action"],
            "recipient_body": base["recipient_body"],
            "deadline": _parse_deadline(base["deadline_raw"]),
            "thresholds": base["thresholds"],
            "conditions": base["conditions"],
            "deference": None,  # non-suppressing default -- see module note above
            "extraction_confidence": min(p1["extraction_confidence"], p2["extraction_confidence"]),
            "extraction_model": "conservative-default (two passes disagreed; see uncertainty_note)",
            "extracted_on": date.today().isoformat(),
            "human_verified": False,
            "extraction_uncertain": True,
            "uncertainty_note": (
                f"two independent extraction passes disagreed on {diffs}; "
                f"pass1={{'deontic': {p1['deontic']!r}, 'deference': {p1['deference']!r}}}, "
                f"pass2={{'deontic': {p2['deontic']!r}, 'deference': {p2['deference']!r}}} "
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
        batch = units[:remaining] if remaining is not None else units
        if not batch:
            break

        write_lock = threading.Lock()

        def process_unit(unit: Unit):
            norm, queue_item = extract_unit(client, args.model, unit, args.reasoning_effort)
            with write_lock:
                if norm is not None:
                    unit.provision["norms"].append(norm)
                else:
                    _save_queue_item(queue_item)
                # Crash-safe, same reasoning as before: written after every unit, not just
                # at the end -- the lock serializes the write, not the (parallel) API calls.
                path.write_text(json.dumps(root, ensure_ascii=False, indent=1), encoding="utf-8")
            return norm is not None

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
