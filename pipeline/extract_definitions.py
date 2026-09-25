
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
from parse_deadline import _parse_deadline

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


class ExtractedDefinition(BaseModel):
    """What the model returns for ONE independent legal definition.

    Verbatim-checked fields (term, definition_raw) are validated against the
    source text by _verbatim_ok() below, never trusted outright.

    A single unit (paragraph, or whole article where there are no paragraphs)
    can contain zero, one, or several independent definitions.
    """

    # Legal location
    document: Optional[str]
    chapter: Optional[str]
    section: Optional[str]

    # Definition
    term: str
    definition_raw: str
    scope: Optional[str]

    # Qualifiers
    conditions: list[str]
    exceptions: list[str]
    references: list[str]

    definition_confidence: float

class ExtractedDefinitionsResponse(BaseModel):
    """The model's actual per-call response for legal definitions.

    An empty list is valid when the text contains no independent definitions.
    """

    definitions: list[ExtractedDefinition]


VERBATIM_FIELDS = ("term", "definition_raw")


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _verbatim_ok(norm: ExtractedDefinition, source_text: str) -> bool:
    haystack = _normalize(source_text)
    for field_name in VERBATIM_FIELDS:
        span = getattr(norm, field_name)
        if span is None:
            continue
        if _normalize(span) not in haystack:
            return False
    return True

# ---------------------------------------------------------------------------
# Prompting. Two independent framings of the same schema -- the two-pass check's
# whole point is that agreement between differently-worded instructions is stronger
# evidence than one pass at high confidence.
# ---------------------------------------------------------------------------


_SCHEMA_FIELDS_NOTE = (
    "The fields `term` and `definition_raw` must be copied verbatim from the "
    "paragraph text. Do not paraphrase, translate, shorten, or reconstruct them. "
    "`term` is the exact legal term being defined. `definition_raw` is the exact "
    "text that states what that term means, including wording that is grammatically "
    "part of the definition. Do not include surrounding introductory wording such "
    "as 'voor de toepassing van deze verordening wordt verstaan onder' unless that "
    "wording is itself part of the definition. Do not include a separate condition, "
    "exception, or cross-reference merely because it appears nearby; put those in "
    "their dedicated fields when they qualify the definition. If no independent "
    "legal definition is present, return an empty list."
)


_MULTI_DEFINITION_NOTE = (
    "\n\nA paragraph may state ZERO, ONE, or SEVERAL independent definitions. "
    "Return one entry in `definitions` for each independently defined legal term. "
    "For example, if the paragraph separately defines 'incident' and 'ernstig "
    "incident', return two definitions. Do not merge separate defined terms merely "
    "because they occur in the same sentence or paragraph. Conversely, do not split "
    "one definition into several definitions merely because its meaning contains "
    "multiple clauses, conditions, examples, or qualifications."
)

_FRAMING_DIRECT = (
    "You are extracting legal definitions from one paragraph of Dutch or EU "
    "digital-law legislation. Identify every independent legal definition explicitly "
    "established in this paragraph. A definition states what a legal term means, "
    "such as wording equivalent to 'wordt verstaan onder', 'betekent', or another "
    "formulation that explicitly establishes the meaning of a term. "
    "For each independent definition extract the defined term (`term`) and the "
    "definition itself (`definition_raw`) exactly as stated in the text. Also "
    "extract any explicit scope, conditions, exceptions, and references that "
    "qualify that definition. "
    "Do not infer a definition merely because a technical or important term is "
    "mentioned. Do not extract obligations, prohibitions, permissions, competence "
    "rules, or procedural requirements as definitions. "
    "A paragraph may contain ZERO, ONE, or SEVERAL independent definitions. "
    "If it contains no independent legal definition, return an empty list. "
    + _SCHEMA_FIELDS_NOTE + _MULTI_DEFINITION_NOTE
)

_FRAMING_STEPBACK = (
    "Read this paragraph as a legal text and determine whether it explicitly "
    "establishes one or more legal definitions. First identify which terms, if any, "
    "are legally defined by this paragraph. Do not assume that a word is a defined "
    "term merely because it is technical, important, capitalized, or used repeatedly. "
    "Then, for each independent definition, populate `term`, `definition_raw`, "
    "`scope`, `conditions`, `exceptions`, and `references`. "
    "Do not extract obligations, prohibitions, permissions, competence rules, or "
    "procedural requirements as definitions. "
    "A paragraph may contain ZERO, ONE, or SEVERAL independent definitions. "
    "If there is no independent legal definition, return an empty list. "
    + _SCHEMA_FIELDS_NOTE + _MULTI_DEFINITION_NOTE
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
        p.setdefault("definitions", [])
        # _empty_paragraph_indices (2026-09-24, item 3): a paragraph can now resolve to
        # ZERO norms (both passes agreeing there's no independent rule here) -- that's a
        # genuinely resolved, useful result, not "not yet processed", so it needs its
        # own persisted marker; norms[] alone can no longer tell "done with nothing to
        # show" apart from "never attempted", now that an empty result is possible.
        done_indices = ({n["paragraph_index"] for n in p["definitions"] if n.get("paragraph_index") is not None}
                        | set(p.get("_empty_paragraph_indices") or []))
        done_numbers_legacy = {n["number"] for n in p["definitions"] if n.get("paragraph_index") is None}
        paragraphs = p.get("paragraphs") or []
        if paragraphs:
            for idx, para in enumerate(paragraphs):
                number = para['number']
                text = para['text']
                if (idx not in done_indices and number not in done_numbers_legacy):
                    units.append(Unit(p,number,idx,text,heading,))

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
              reasoning_effort: str = "none") -> Optional[list[ExtractedDefinition]]:
    prompt = _build_input(framing, heading, text)
    kwargs = dict(
        model=model,
        input=prompt,
        text_format=ExtractedDefinitionsResponse,
        reasoning={"effort": reasoning_effort},
    )
    if reasoning_effort == "none":
        kwargs["temperature"] = 0

    for attempt in range(2):  # one retry if ANY returned norm fails verbatim validation
        resp = _call_with_network_retries(client, **kwargs)
        definitions = resp.output_parsed.definitions
        if all(_verbatim_ok(n, text) for n in definitions):
            return definitions
        print(
            f"    [definition pass validation] attempt {attempt + 1} failed "
            f"verbatim check, {'retrying' if attempt == 0 else 'giving up'}"
        )
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


def _finalize_definition(
    definition: ExtractedDefinition,
    paragraph_number: Optional[str],
    paragraph_index: Optional[int],
    definition_index: int,
    model: str,
    reasoning_effort: str = "none",
) -> dict:
    model_label = model if reasoning_effort == "none" else f"{model} (reasoning:{reasoning_effort})"
    return {
        "number": paragraph_number,
        "paragraph_index": paragraph_index,
        "definition_index": definition_index,
        "term": definition.term,
        "definition_raw": definition.definition_raw,
        "scope": definition.scope,
        "conditions": definition.conditions,
        "exceptions": definition.exceptions,
        "references": definition.references,
        "definition_confidence": definition.definition_confidence,
        "extraction_model": model_label,
        "extracted_on": date.today().isoformat(),
        "human_verified": False,
    }
def _definition_overlap_score(a: ExtractedDefinition, b: ExtractedDefinition) -> float:
    score = 0.0
    for fa, fb in ((a.term, b.term), (a.definition_raw, b.definition_raw)):
        na, nb = _normalize(fa or ""), _normalize(fb or "")
        if not na or not nb:
            continue
        wa, wb = set(na.split()), set(nb.split())
        if wa and wb:
            score += len(wa & wb) / min(len(wa), len(wb))
    return score


def _align_definitions(
    list1: list[ExtractedDefinition],
    list2: list[ExtractedDefinition],
) -> list[tuple[Optional[ExtractedDefinition], Optional[ExtractedDefinition]]]:
    scored = [
        (_definition_overlap_score(d1, d2), i1, i2)
        for i1, d1 in enumerate(list1)
        for i2, d2 in enumerate(list2)
    ]
    scored.sort(key=lambda t: -t[0])

    used1, used2 = set(), set()
    pairs = []

    for score, i1, i2 in scored:
        if i1 in used1 or i2 in used2 or score <= 0:
            continue
        used1.add(i1)
        used2.add(i2)
        pairs.append((list1[i1], list2[i2]))

    pairs.extend((d1, None) for i, d1 in enumerate(list1) if i not in used1)
    pairs.extend((None, d2) for i, d2 in enumerate(list2) if i not in used2)
    return pairs


def _definitions_agree(d1: ExtractedDefinition, d2: ExtractedDefinition) -> bool:
    return (
        _text_fields_agree(d1.term, d2.term)
        and _text_fields_agree(d1.definition_raw, d2.definition_raw)
        and _text_fields_agree(d1.scope, d2.scope)
        and _list_fields_agree(d1.conditions, d2.conditions)
        and _list_fields_agree(d1.exceptions, d2.exceptions)
        and _list_fields_agree(d1.references, d2.references)
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
        return None, {**base, "reason": "two_pass_disagreement", "disagreement_kind": "definition_count_mismatch",
                      "pass1": [n.model_dump() for n in pass1], "pass2": [n.model_dump() for n in pass2]}

    aligned = _align_definitions(pass1, pass2)
    if any(n1 is None or n2 is None for n1, n2 in aligned):
        return None, {**base, "reason": "two_pass_disagreement",
                      "disagreement_kind": "definition_alignment_ambiguous",
                      "pass1": [n.model_dump() for n in pass1], "pass2": [n.model_dump() for n in pass2]}

    if not all(_definitions_agree(n1, n2) for n1, n2 in aligned):
        return None, {**base, "reason": "two_pass_disagreement",
                      "pass1": [n.model_dump() for n in pass1], "pass2": [n.model_dump() for n in pass2]}

    finalized = [_finalize_definition(n1, unit.paragraph_number, unit.paragraph_index, idx, model, reasoning_effort)
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
        definitions, new_item = extract_unit(client, model, unit, reasoning_effort)
        with lock:
            nonlocal n_resolved
            if definitions is not None:
                if definitions:
                    provision.setdefault("definitions", []).extend(definitions)
                elif unit.paragraph_index is not None:
                    provision.setdefault("_empty_paragraph_indices", []).append(unit.paragraph_index)
                n_resolved += 1
                dirty_paths.add(path)
            else:
                new_item["retried_with"] = retried_with_label
                remaining_queue.append(new_item)
        return key, item["paragraph_number"], definitions is not None

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
def resolve_queue_conservatively() -> None:
    """Definitions have no analogue of `deontic`/`deference` -- there's no field here
    whose omission silently hides a real problem downstream, and no field that gates
    eligibility for later processing the way `deontic` did for norms. So there's no
    asymmetric "non-suppressing default" to apply per field; a disagreement is instead
    resolved by taking pass1 as the base entry (an arbitrary but consistent choice --
    neither pass is more authoritative than the other) and flagging every field the two
    passes disagreed on in `uncertainty_note`, so a human reviewing it later knows
    exactly what was left unconfirmed rather than trusting the merged value blindly."""
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
            print(f"  [warn] could not relocate provision for {key} -- dropping without a definition")
            dropped.append(item)
            continue

        # pass1/pass2 are LISTS of definition dicts (2026-09-24, item 3) -- one unit can
        # yield zero, one, or several independent definitions. definition_count_mismatch/
        # definition_alignment_ambiguous (extract_unit's own distinct disagreement_kinds)
        # are NOT auto-resolved here: there's no safe "conservative default" for "the two
        # passes disagree on HOW MANY definitions exist" the way there is for a single
        # mismatched field on an already-agreed-count pair -- guessing which count is
        # right is exactly the kind of judgement call this function exists to avoid.
        if item.get("disagreement_kind") in ("definition_count_mismatch", "definition_alignment_ambiguous"):
            dropped.append(item)
            continue

        if item["reason"] == "validation_failed":
            # Checked directly against the real queue (2026-09-24 fix): a meaningful
            # share of these items DO have one pass that fully passed verbatim
            # validation -- the OTHER pass is what's null (a model error, refusal, or a
            # verbatim check that still failed after its own retry). The old code
            # dropped these uniformly, discarding genuinely grounded extractions on the
            # assumption that "validation_failed" meant neither pass was usable, which
            # isn't what the data actually shows. Every definition in the single
            # grounded pass's list becomes a provisional definition, tagged uncertain for
            # exactly the reason it's less confirmed (one reading, not two independently
            # agreeing ones) -- only genuinely dropped when BOTH passes are null.
            grounded = item.get("pass1") if item.get("pass1") is not None else item.get("pass2")
            if grounded is None:
                dropped.append(item)
                continue
            if not grounded and item.get("paragraph_index") is not None:
                provision.setdefault("_empty_paragraph_indices", []).append(item["paragraph_index"])
            for idx, g in enumerate(grounded):
                definition = {
                    "number": item["paragraph_number"],
                    "paragraph_index": item.get("paragraph_index"),
                    "definition_index": idx,
                    "term": g["term"],
                    "definition_raw": g["definition_raw"],
                    "scope": g["scope"],
                    "conditions": g["conditions"],
                    "exceptions": g["exceptions"],
                    "references": g["references"],
                    "definition_confidence": g["definition_confidence"],
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
                provision.setdefault("definitions", []).append(definition)
            n_resolved += 1
            dirty_paths.add(path)
            continue

        # Plain field-level disagreement, matched definition count (item 3): re-align
        # the two passes' stored lists the same way extract_unit did in memory, then
        # merge each aligned pair -- pass1 as the base, every disagreeing field logged.
        p1_defs = [ExtractedDefinition(**d) for d in item["pass1"]]
        p2_defs = [ExtractedDefinition(**d) for d in item["pass2"]]
        aligned = _align_definitions(p1_defs, p2_defs)
        if any(d1 is None or d2 is None for d1, d2 in aligned):
            # Defensive only -- extract_unit wouldn't have written this reason if
            # alignment were ambiguous, but never guess if it somehow is.
            dropped.append(item)
            continue
        if not aligned and item.get("paragraph_index") is not None:
            provision.setdefault("_empty_paragraph_indices", []).append(item["paragraph_index"])
        for idx, (d1, d2) in enumerate(aligned):
            diffs = [f for f in ("term", "definition_raw", "scope", "conditions", "exceptions", "references")
                     if getattr(d1, f) != getattr(d2, f)]
            base = d1  # arbitrary but consistent -- see function docstring

            definition = {
                "number": item["paragraph_number"],
                "paragraph_index": item.get("paragraph_index"),
                "definition_index": idx,
                "term": base.term,
                "definition_raw": base.definition_raw,
                "scope": base.scope,
                "conditions": base.conditions,
                "exceptions": base.exceptions,
                "references": base.references,
                "definition_confidence": min(d1.definition_confidence, d2.definition_confidence),
                "extraction_model": "conservative-default (two passes disagreed; see uncertainty_note)",
                "extracted_on": date.today().isoformat(),
                "human_verified": False,
                "extraction_uncertain": True,
                "uncertainty_note": (
                    f"two independent extraction passes disagreed on {diffs}; "
                    f"pass1={{'term': {d1.term!r}, 'definition_raw': {d1.definition_raw!r}}}, "
                    f"pass2={{'term': {d2.term!r}, 'definition_raw': {d2.definition_raw!r}}} "
                    "-- resolved to pass1's reading with the disagreement recorded here rather "
                    "than a human pre-clearing it; revisit if this definition ends up inside an "
                    "actual candidate finding."
                ),
            }
            provision.setdefault("definitions", []).append(definition)
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

    print(f"{n_resolved} definitions[] entries finalized with the conservative default "
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
                          "the conservative default (see resolve_queue_conservatively "
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
            definitions, queue_item = extract_unit(client, args.model, unit, args.reasoning_effort)
            with write_lock:
                if definitions is not None:
                    if definitions:
                        unit.provision.setdefault("definitions", []).extend(definitions)
                    elif unit.paragraph_index is not None:
                        # Confirmed by both passes: no independent definition in this
                        # paragraph -- a real, resolved result (item 3), not "not yet
                        # processed"; recorded so _iter_units doesn't re-offer it forever.
                        unit.provision.setdefault("_empty_paragraph_indices", []).append(unit.paragraph_index)
                else:
                    _save_queue_item(queue_item)
                # Crash-safe, same reasoning as before: written after every unit, not just
                # at the end -- the lock serializes the write, not the (parallel) API calls.
                path.write_text(json.dumps(root, ensure_ascii=False, indent=1), encoding="utf-8")
            return definitions is not None

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

    print(f"\n{n_processed} unit(s) processed, {n_finalized} definitions[] entries finalized, "
          f"{n_queued} sent to the review queue -> data/stage6_review_queue.json", flush=True)

    
if __name__ == "__main__":
    main()
