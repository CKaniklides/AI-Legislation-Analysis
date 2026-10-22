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

Outputs (2026-09-29: one set of files per document; the source files are never modified):
    data/stage_4/stage4_{name}_norms.json               the source provisions + their norms[]
    data/stage_4/stage4_{name}_norms_review_queue.json  units still unresolved for that document
    data/stage_4/stage4_{name}_norms_unresolved.json    units --resolve-queue had to drop
`{name}` is SourceSpec.name (GDPR, NIS2, DORA, AI_act, Cbw, ...). If the norms file
already exists a run resumes from it; otherwise it starts from the source file. To
re-extract a document from scratch use --rewrite (below): it wipes that document's
stage4_{name}_* files and starts over from the source file with every norm stripped.

Usage:
    python extract_norms.py --dry-run          # verify scope/counts, no API key needed
    python extract_norms.py --only "Cbw"       # run one source
    python extract_norms.py --limit 5          # smoke-test on the first 5 units total
    python extract_norms.py                    # run the full anchor set
    python extract_norms.py --rewrite --only "GDPR"   # wipe GDPR's stage-4 files, re-extract
    python extract_norms.py --rewrite --dry-run       # show what a rewrite would wipe, change nothing
"""
import argparse
import json
import os
import re
import sys
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Optional

from dotenv import load_dotenv
from parse_deadline import _parse_deadline

import truststore
truststore.inject_into_ssl()


if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(ROOT / ".env")  # picks up OPENAI_API_KEY from a .env file in the project root
NEW_NORM_DIR = ROOT / "data" / "new_norms"  # every file this script creates lives here
DEFAULT_MODEL = "gpt-5.6-luna"  # single config value -- see module docstring


# "NO NORM" removed: a non-normative unit is represented ONLY as {"norms": []}.
# One representation means the two passes cannot "disagree" about the same thing.
Deontic = Literal[
    "OBLIGATION", "PROHIBITION", "PERMISSION", "EXEMPTION", "DELEGATION",
    "COMPETENCE", "RIGHT",
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
    """What the model returns for ONE independent norm. Verbatim fields (see
    VERBATIM_SCALAR / VERBATIM_LIST) are validated against the source text by
    _verbatim_violations(), never trusted outright.

    A single unit (paragraph, or whole article where there are no paragraphs) can
    contain zero, one, or several of these -- see ExtractedNormsResponse."""
    deontic: Deontic
    actor: Optional[str]
    # addressee_type: Optional[AddresseeType]
    trigger_event: Optional[str]
    action_span: list[str]
    action_lemma: Optional[str]          # normalised, NOT verbatim
    object: Optional[str]
    recipient: list[str]
    deadline_raw: Optional[str]  # e.g. "binnen 24 uur" -- parsed deterministically, not by the model
    thresholds: list[str]
    conditions: list[str]


class ExtractedNormsResponse(BaseModel):
    """The model's per-call response. An empty list is the ONLY way to say that a
    unit states no norm (definitions, scope, cross-references, procedural text)."""
    norms: list[ExtractedNorm]


# Must match ExtractedNorm exactly; checked at import by _check_field_lists().
VERBATIM_SCALAR = ["actor", "object", "trigger_event", "deadline_raw"]
VERBATIM_LIST = ["action_span", "recipient", "thresholds", "conditions"]
VERBATIM_FIELDS = VERBATIM_SCALAR + VERBATIM_LIST


# Typographic characters that occur in official texts (EUR-Lex / wetten.overheid.nl)
# but that the model reproduces as plain ASCII: curly quotes and apostrophes
# ("kmo’s", "“logs”"), dash variants and non-breaking hyphens. Mapped to one form on
# BOTH sides of every comparison, so a correct span never fails on typography alone.
_CHAR_MAP = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'", "`": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u2033": '"',
    "\u00ab": '"', "\u00bb": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-",
    "\u2212": "-", "\u00ad": "",  # soft hyphen: invisible, dropped
})


def _normalize(s: str) -> str:
    """Comparison form of a text span. NFKC first: it folds compatibility characters
    such as the ligature 'ĳ' ("aansprakelĳkheidsverzekering", AI Act art. 31(9)) into
    'ij' and non-breaking spaces into spaces; then typographic quotes and dashes are
    mapped to ASCII, whitespace collapsed, and case folded."""
    s = unicodedata.normalize("NFKC", s).translate(_CHAR_MAP)
    return re.sub(r"\s+", " ", s).strip().lower()


def _verbatim_violations(norm: ExtractedNorm, source_text: str) -> list[str]:
    """Every verbatim span that is NOT a substring of the source; [] means all OK."""
    haystack = _normalize(source_text)
    bad = []
    for field_name in VERBATIM_FIELDS:
        value = getattr(norm, field_name)
        spans = value if isinstance(value, list) else ([value] if value else [])
        for span in spans:
            if _normalize(span) not in haystack:
                bad.append(f"{field_name}: {span!r}")
    return bad


def _verbatim_ok(norm: ExtractedNorm, source_text: str) -> bool:
    return not _verbatim_violations(norm, source_text)


def _check_field_lists() -> None:
    fields = set(ExtractedNorm.model_fields)
    unknown = set(VERBATIM_FIELDS) - fields
    if unknown:
        raise RuntimeError(f"VERBATIM_FIELDS names fields not in ExtractedNorm: {unknown}")


# ---------------------------------------------------------------------------
# Prompting. Two independent framings of the same schema.
# Field names in the prompt text must match ExtractedNorm exactly:
#   deontic, actor, trigger_event, action_span, action_lemma, object, recipient,
#   deadline_raw, thresholds, conditions.
# ---------------------------------------------------------------------------

_ROLE = """You are an expert in Dutch and EU legislation. You extract norms from the \
Dutch-language text of legal provisions. You process one unit (a paragraph, or an \
article without numbered paragraphs) at a time.
"""

_DEONTIC_TYPES = """
DEONTIC TYPES
Assign exactly one type per norm.

- OBLIGATION: the actor must perform the action. Markers: "moet", "is verplicht", \
"is gehouden", "draagt zorg voor", "zorgt ervoor dat". IMPORTANT: in legislation the \
plain present tense usually states an obligation, not a fact: "Onze Minister stelt de \
subsidie vast", "De lidstaten zorgen ervoor dat ..." are OBLIGATIONS.
- PROHIBITION: the actor must not perform the action. Markers: "het is verboden", \
"mag niet", "niemand mag", "is niet toegestaan", or plain present tense with negation \
("verstrekt geen ...").
- PERMISSION: the actor may perform the action; an explicit grant of freedom. Markers: \
"mag", "is toegestaan", and "kan" when the action is a factual act that does not change \
anyone's legal position.
- EXEMPTION: lifts an obligation or prohibition for a specific case or party. Markers: \
"in afwijking van", "is niet van toepassing op", "geldt niet voor", "vrijstelling", \
"ontheffing". The actor is the party that is exempted; the object is the lifted duty. \
If the text only limits the scope of the whole act ("Deze wet is niet van toepassing \
op ..."), it is NOT a norm.
- COMPETENCE: the actor is competent to change legal positions: decide, grant, refuse, \
revoke, impose, appoint, establish. Markers: "is bevoegd", and "kan" + a legal-act verb \
(besluiten, verlenen, weigeren, intrekken, opleggen, aanwijzen, vaststellen).
- DELEGATION: a mandate or competence to lay down further rules. Markers: "Bij of \
krachtens algemene maatregel van bestuur worden/kunnen regels worden gesteld", "Bij \
ministeriële regeling ...", "De Commissie is bevoegd gedelegeerde handelingen vast te \
stellen", "De Commissie stelt uitvoeringshandelingen vast".
- RIGHT: an entitlement that another party must honour. Markers: "heeft recht op", \
"heeft het recht", "heeft aanspraak op". The actor is the RIGHT-HOLDER; the party that \
must honour the right goes in `recipient` if stated.

THE WORD "kan": decide between COMPETENCE (legal-act verb, usually a public authority), \
PERMISSION (factual act), or no norm at all (the sentence only describes a possibility).

NOT NORMS: definitions ("wordt verstaan onder", "geldt als", "wordt aangemerkt als"), \
scope and subject-matter provisions ("Deze wet is van toepassing op", "Deze verordening \
stelt regels vast"), entry into force, citation titles, and amendment instructions. \
Return NO entry for these. If the whole unit states no norm, return {"norms": []}.
"""

_FIELDS = """
FIELDS
All fields describe ONLY the current norm, never a sibling norm in the same unit.

- deontic: one of the DEONTIC TYPES above. Decide it first.
- actor: the party that bears the obligation, prohibition, permission, exemption, \
competence or right, including its determiner ("de aanbieder"). In a passive sentence \
take it from the door-phrase ("wordt verleend door Onze Minister" -> "Onze Minister"). \
If no party is stated, return null; do not infer one. Keep pronouns verbatim ("hij").
- action_span: the verb and its verb cluster as a LIST of verbatim fragments in text \
order, including modal verbs, negation and separable particles. Examples: "trekt de \
vergunning in" -> ["trekt", "in"]; "kan de vergunning intrekken" -> ["kan", "intrekken"]; \
"verstrekt geen gegevens" -> ["verstrekt geen"]. Exclude objects, adverbs and adjuncts.
- action_lemma: the normalised infinitive of the main action, with separable particle \
rejoined, WITHOUT modal verbs, negation or object: "intrekken", "melden", "verstrekken". \
Keep fixed expressions whole: "zorg dragen voor", "in behandeling nemen". Not verbatim. \
Null if the text names no action (e.g. an exemption that only names the lifted duty).
- object: the thing that undergoes the action, including determiner ("de vergunning").
- recipient: a LIST of the parties to or for whom the action is done, with preposition \
(["aan de toezichthouder"], ["aan de vreemdeling"]). Recipients can be any party, not \
only public bodies. For RIGHT: the party that must honour the right. [] if none stated.
- trigger_event: the event that activates the norm ("na ontvangst van de aanvraag", \
"zodra de aanbieder kennis neemt van het incident").
- deadline_raw: the time limit for this norm. A sentence often gives both a vague \
qualifier ("onverwijld", "zonder onredelijke vertraging") and a specific figure ("binnen \
24 uur", "uiterlijk 72 uur") for the same duty: then extract the phrase containing the \
number. Extract the vague qualifier only when no figure is stated for this norm.
- thresholds: numeric limits that restrict the norm ("meer dan 50 werknemers"). A \
threshold may also appear inside a condition; then list it in both fields.
- conditions: phrases that limit when the norm applies, typically introduced by \
"indien", "tenzij", "voor zover", "mits", "zonder", "behoudens". An exception introduced \
by "tenzij" or "behoudens" stays a condition of the SAME norm. Cross-references such as \
"in afwijking van het eerste lid" or "onverminderd artikel 5" are NOT conditions: leave \
them out of every field.

ADJUNCTS: words you leave out of the roles because they are adjuncts are NOT discarded: \
if they limit the norm, they go to trigger_event, deadline_raw, thresholds or conditions.

VERBATIM RULE: every value of actor, action_span, object, recipient, trigger_event, \
deadline_raw, thresholds and conditions must be an exact, word-for-word substring of the \
unit text. Do not paraphrase, summarise or complete it. If a field does not apply, \
return null (or [] for list fields); never invent a value.
"""

_MULTI_NORM = """
MULTIPLE NORMS
A unit may state ZERO, ONE or SEVERAL independent norms; return one entry each.

SPLIT into separate norms when:
- the deontic type differs ("meldt het incident ... en verstrekt geen persoonsgegevens \
aan derden" = an OBLIGATION and a PROHIBITION), even within one sentence and with the \
same actor;
- the actions differ ("bewaart de gegevens en meldt het incident" = two norms);
- points (a), (b), (c) each impose their own obligation.

DO NOT SPLIT when:
- one duty has several conditions, qualifying clauses or a deadline;
- a list spells out the content of ONE deliverable ("de melding bevat a, b en c" = one \
norm; a, b and c belong to that norm);
- an exception is introduced by "tenzij" or "behoudens" (a condition of the same norm).

CONTEXT: if surrounding paragraphs are supplied, use them ONLY to understand references \
("deze verplichting", "het eerste lid", "hij"). Never extract norms from the context, and \
never copy context text into fields.
"""

# Examples are stored as data and rendered into the prompt, so they can be validated
# against the schema and the verbatim rule (see _check_examples). Synthetic paragraphs.
_EXAMPLE_DATA: list[tuple[str, dict]] = [
    (
        "De aanbieder meldt een significant incident onverwijld en in ieder geval binnen "
        "24 uur aan het CSIRT, en verstrekt geen persoonsgegevens aan derden zonder "
        "toestemming van de betrokkene.",
        {"norms": [
            {"deontic": "OBLIGATION", "actor": "De aanbieder", "trigger_event": None,
             "action_span": ["meldt"], "action_lemma": "melden",
             "object": "een significant incident", "recipient": ["aan het CSIRT"],
             "deadline_raw": "in ieder geval binnen 24 uur", "thresholds": [],
             "conditions": []},
            {"deontic": "PROHIBITION", "actor": "De aanbieder", "trigger_event": None,
             "action_span": ["verstrekt geen"], "action_lemma": "verstrekken",
             "object": "persoonsgegevens", "recipient": ["aan derden"],
             "deadline_raw": None, "thresholds": [],
             "conditions": ["zonder toestemming van de betrokkene"]},
        ]},
    ),
    (
        "Onze Minister kan de vergunning intrekken indien de vergunninghouder de aan de "
        "vergunning verbonden voorschriften niet naleeft.",
        {"norms": [
            {"deontic": "COMPETENCE", "actor": "Onze Minister", "trigger_event": None,
             "action_span": ["kan", "intrekken"], "action_lemma": "intrekken",
             "object": "de vergunning", "recipient": [], "deadline_raw": None,
             "thresholds": [],
             "conditions": ["indien de vergunninghouder de aan de vergunning verbonden "
                            "voorschriften niet naleeft"]},
        ]},
    ),
    (
        "In afwijking van het eerste lid is de meldingsplicht niet van toepassing op "
        "ondernemingen met minder dan 10 werknemers.",
        {"norms": [
            {"deontic": "EXEMPTION", "actor": "ondernemingen", "trigger_event": None,
             "action_span": ["is", "niet van toepassing"], "action_lemma": None,
             "object": "de meldingsplicht", "recipient": [], "deadline_raw": None,
             "thresholds": ["minder dan 10 werknemers"], "conditions": []},
        ]},
    ),
    (
        "De gebruiker heeft het recht de over hem verwerkte gegevens bij de aanbieder in "
        "te zien.",
        {"norms": [
            {"deontic": "RIGHT", "actor": "De gebruiker", "trigger_event": None,
             "action_span": ["heeft het recht", "in te zien"], "action_lemma": "inzien",
             "object": "de over hem verwerkte gegevens",
             "recipient": ["bij de aanbieder"], "deadline_raw": None,
             "thresholds": [], "conditions": []},
        ]},
    ),
    (
        "Bij algemene maatregel van bestuur kunnen regels worden gesteld over de inhoud "
        "van de melding.",
        {"norms": [
            {"deontic": "DELEGATION", "actor": None, "trigger_event": None,
             "action_span": ["kunnen", "worden gesteld"], "action_lemma": "regels stellen",
             "object": "regels", "recipient": [], "deadline_raw": None,
             "thresholds": [], "conditions": []},
        ]},
    ),
    (
        "In deze wet wordt verstaan onder aanbieder: een natuurlijke persoon of "
        "rechtspersoon die een digitale dienst verleent.",
        {"norms": []},
    ),
]


def _render_examples() -> str:
    parts = ["\nEXAMPLES (synthetic units)\n"]
    for paragraph, output in _EXAMPLE_DATA:
        parts.append(f'Unit: "{paragraph}"\nOutput:\n'
                     f"{json.dumps(output, ensure_ascii=False)}\n")
    return "\n".join(parts)


def _check_examples() -> None:
    """Fail loudly if a schema change breaks an example or its verbatim spans."""
    for paragraph, output in _EXAMPLE_DATA:
        parsed = ExtractedNormsResponse.model_validate(output)
        for norm in parsed.norms:
            bad = _verbatim_violations(norm, paragraph)
            if bad:
                raise RuntimeError(f"Example violates verbatim rule: {bad}\n{paragraph}")


_check_field_lists()
_check_examples()

_EXAMPLES = _render_examples()

_OUTPUT = """
OUTPUT
Return only the JSON object required by the response schema, with a "norms" list. \
Fill `deontic` before the other fields. No text outside the JSON.
"""

_RULES = _DEONTIC_TYPES + _FIELDS + _MULTI_NORM + _EXAMPLES + _OUTPUT

# Two framings for A/B comparison. Same rules; only the task framing differs.
FRAMING_DIRECT = _ROLE + """
TASK
Extract every independent norm stated in the unit below.
""" + _RULES

FRAMING_CHECKLIST = _ROLE + """
TASK
Read carefully rather than pattern-matching, and work in this order:
1. Decide whether the unit states any norm at all, or is a definition, scope, \
subject-matter or other non-normative provision. If the latter, return {"norms": []}.
2. Identify each party that receives an obligation, prohibition, permission, exemption, \
competence, delegation or right, and the marker that expresses it.
3. Apply the MULTIPLE NORMS rules to decide how many entries to return.
4. Fill all fields for each norm, using only this unit.
""" + _RULES

# ---------------------------------------------------------------------------
# Drop-in replacement for everything from `_build_input` to the end of the script.
# Depends on the header (imports, ROOT, STAGE4_DIR, DEFAULT_MODEL, _parse_deadline)
# and on the schema/prompt section (ExtractedNorm, ExtractedNormsResponse, _normalize,
# _verbatim_violations, FRAMING_DIRECT, FRAMING_CHECKLIST).
# ---------------------------------------------------------------------------

# Bump whenever ExtractedNorm or the prompt changes in a way that makes earlier
# extractions incomparable. Norms, empty-unit markers and queue items from another
# version count as "not done" and are re-extracted; old norms are archived under
# _stale_norms, never deleted.
SCHEMA_VERSION = 2
EMPTY_PARAS_KEY = f"_empty_paragraph_indices_v{SCHEMA_VERSION}"
EMPTY_WHOLE_KEY = f"_empty_whole_provision_v{SCHEMA_VERSION}"
STALE_NORMS_KEY = "_stale_norms"


CONTEXT_MAX_CHARS = 6000  # budget for sibling paragraphs per call


def _article_context(provision: dict, paragraph_index: Optional[int]) -> Optional[str]:
    """The OTHER paragraphs of the same article, so the model can resolve references
    such as "de in lid 1 bedoelde melding" (AI Act art. 73(2)) or "lid 1, punt h)"
    (art. 5(2)-(4)). None for whole-provision units and single-paragraph articles.

    Document order, because references point mostly backwards ("het eerste lid").
    Over budget, a paragraph is truncated rather than dropped, so the model still sees
    that it exists and how it starts. The verbatim check runs against the unit text
    only, so any span the model copies from this context fails validation."""
    paragraphs = provision.get("paragraphs") or []
    if paragraph_index is None or len(paragraphs) < 2:
        return None
    parts, remaining = [], CONTEXT_MAX_CHARS
    for idx, para in enumerate(paragraphs):
        if idx == paragraph_index:
            continue
        label = f"[lid {para.get('number') or idx + 1}]"
        text = para["text"]
        if len(text) > remaining:
            text = (text[:max(remaining, 200)].rstrip() + " [...]")
        parts.append(f"{label} {text}")
        remaining = max(remaining - len(text), 0)
    return "\n\n".join(parts)


def _build_input(article_heading: str, unit_text: str, context: Optional[str] = None,
                 feedback: Optional[str] = None) -> str:
    """User-side input only; the framing goes in `instructions` (see _run_pass), which
    keeps the long static prompt as a cacheable prefix shared by every call. The unit
    text comes after the context and is labelled as the only extraction source."""
    parts = [f"Article/provision heading: {article_heading}"]
    if context:
        parts.append(
            "Context -- the other paragraphs of the same article. Use it ONLY to "
            "understand references such as \"het eerste lid\" or \"de in lid 1 bedoelde "
            "melding\". Do not extract norms from it and do not copy text from it into "
            "any field.\n" + context
        )
    parts.append(f"Unit text -- extract norms from this text only:\n{unit_text}")
    if feedback:
        parts.append(feedback)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Source scope -- widened to the full corpus (2026-09-23 decision). The
# Telecommunicatiewet is scoped to Hoofdstuk 11 only; see the 2026-09-23 note in the
# architecture doc. `name` is used in output file names: keep it filesystem-safe and
# stable -- renaming one orphans that document's existing stage-4 files.
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
        path="data/laws/nis2_nl/BWBR0052872_2026-08-15_provisions.json",
        get_provisions=lambda root: root,
        in_scope=lambda p: True,
    ),
    SourceSpec(
        label="UAVG (full)",
        path="data/laws/uitv_avg/BWBR0040940_2026-09-01_provisions.json",
        get_provisions=lambda root: root,
        in_scope=lambda p: True,
    ),
    SourceSpec(
        label="Wdo (full)",
        path="data/laws/wdo/BWBR0048156_2025-11-11_provisions.json",
        get_provisions=lambda root: root,
        in_scope=lambda p: True,
    ),
    SourceSpec(
        label="Telecommunicatiewet, Hoofdstuk 11 only (privacy/ePrivacy provisions)",
        path="data/laws/telecom/BWBR0009950_2026-08-15_provisions.json",
        get_provisions=lambda root: root,
        in_scope=lambda p: bool(p.get("chapter")) and "Hoofdstuk 11" in p["chapter"],
    ),
    SourceSpec(
        label="Uitvoeringswet dataverordening (full)",
        path="data/laws/uitv_dataverordening/BWBR0051796_2025-11-21_provisions.json",
        get_provisions=lambda root: root,
        in_scope=lambda p: True,
    ),
    SourceSpec(
        label="GDPR (full)",
        path="data/laws/gdpr/32016R0679_original_provisions.json",
        get_provisions=lambda root: root,
        in_scope=lambda p: True,
    ),
    SourceSpec(
        label="NIS2 (full)",
        path="data/laws/nis2/32022L2555_original_provisions.json",
        get_provisions=lambda root: root,
        in_scope=lambda p: True,
    ),
    SourceSpec(
        label="DORA (full)",
        path="data/laws/dora/32022R2554_original_provisions.json",
        get_provisions=lambda root: root,
        in_scope=lambda p: True,
    ),
    SourceSpec(
        label="AI Act (full, pre-Digital-Omnibus per ADR-0004)",
        path="data/laws/ai_act/32024R1689_original_provisions.json",
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



# ---------------------------------------------------------------------------
# Where things live (2026-09-29). Every file this script creates is per document and
# sits in data/stage_4/. The source files under data/ and Datasets/ are read-only as
# far as this script is concerned.
# ---------------------------------------------------------------------------


def _file_label(spec: SourceSpec) -> str:
    # Labels are prose ("Hoofdstuk 11 only (privacy/ePrivacy provisions)"); a "/" in a file name
    # would create a sub-folder, and \ : * ? " < > | are not allowed in Windows file names.
    return re.sub(r'[\\/:*?"<>|]', "-", spec.label)


def norms_path(spec: SourceSpec) -> Path:
    return NEW_NORM_DIR / f"new_{_file_label(spec)}_norms.json"


def queue_path_for(spec: SourceSpec) -> Path:
    return NEW_NORM_DIR / f"new_{_file_label(spec)}_norms_review_queue.json"


def unresolved_path_for(spec: SourceSpec) -> Path:
    return NEW_NORM_DIR / f"new_{_file_label(spec)}_norms_unresolved.json"


def _atomic_write_json(path: Path, data) -> None:
    """Write to a temp file, then swap it in. These files are rewritten after every
    single unit AND are what a later run resumes from, so a crash in the middle of a
    plain write_text() would truncate the very file that holds all progress so far."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _load_root(spec: SourceSpec):
    """The stage-4 norms file if this document has been run before (resume), else the
    source file. Norms from an older schema version (including any written into the
    source file itself by early runs) are archived by _iter_units and re-extracted."""
    out = norms_path(spec)
    src = out if out.exists() else ROOT / spec.path
    return json.loads(src.read_text(encoding="utf-8"))


def _read_queue_file(path: Path) -> list:
    """Queue items of the CURRENT schema version only. Older items cannot be parsed
    into ExtractedNorm; their units are not marked done, so a normal run re-extracts
    them under the current schema."""
    if not path.exists():
        return []
    items = json.loads(path.read_text(encoding="utf-8"))
    current = [i for i in items if i.get("schema_version") == SCHEMA_VERSION]
    if len(current) != len(items):
        print(f"  [info] ignoring {len(items) - len(current)} queue item(s) from an older "
              f"schema in {path.name}; those units will be re-extracted")
    return current


def _load_queue(spec: SourceSpec) -> list:
    return _read_queue_file(queue_path_for(spec))


def _queue_key(item: dict) -> tuple:
    # paragraph_index, not paragraph_number: some articles have two paragraphs with the
    # same displayed number (see _iter_units), and keying on the number would make one
    # paragraph's queue item overwrite the other's.
    return (str(item["instrument_id"]), str(item["article"]), item.get("paragraph_index"))


def _save_queue_item(item: dict, queue_path: Path) -> None:
    """Persisted immediately, one item at a time -- see main()'s docstring note on
    crash-safety. Replaces any existing queue record for the same unit."""
    key = _queue_key(item)
    existing = [r for r in _read_queue_file(queue_path) if _queue_key(r) != key]
    existing.append(item)
    _atomic_write_json(queue_path, existing)


@dataclass
class Unit:
    provision: dict          # live reference into the loaded JSON -- mutated in place
    paragraph_number: Optional[str]   # DISPLAYED number -- for humans, not for identity
    paragraph_index: Optional[int]    # position in provision["paragraphs"]; None = whole provision
    text: str
    article_heading: str
    context: Optional[str] = None     # sibling paragraphs; see _article_context


def _archive_stale_entries(p: dict) -> None:
    """Moves norms of another schema version to _stale_norms (kept for reference,
    never read by this pipeline) and retires the pre-v2 empty-unit marker."""
    norms = p.get("norms") or []
    stale = [n for n in norms if n.get("schema_version") != SCHEMA_VERSION]
    if stale:
        p.setdefault(STALE_NORMS_KEY, []).extend(stale)
    p["norms"] = [n for n in norms if n.get("schema_version") == SCHEMA_VERSION]
    if "_empty_paragraph_indices" in p:
        p.setdefault("_stale_empty_paragraph_indices", []).extend(p.pop("_empty_paragraph_indices"))


def _mark_empty(provision: dict, paragraph_index: Optional[int]) -> None:
    """Records a CONFIRMED zero-norm result (both passes agree). Needed because an empty
    norms[] alone cannot distinguish "done, nothing found" from "never attempted".
    Whole-provision units (paragraph_index None) get their own flag; previously their
    empty results were never recorded, so they were re-extracted on every run."""
    if paragraph_index is None:
        provision[EMPTY_WHOLE_KEY] = True
    else:
        marked = provision.setdefault(EMPTY_PARAS_KEY, [])
        if paragraph_index not in marked:
            marked.append(paragraph_index)


# ---------------------------------------------------------------------------
# Deterministic skip of provisions that never state a norm: definitions, subject
# matter, scope, citation titles, entry into force, amendments and repeals of OTHER
# acts. Decided from the heading (and, for definition articles, the opening words),
# never by the model: these provisions cost two calls each, the definition articles
# are the longest units in the corpus (AI Act art. 3: ~2,700 words), and asking the
# model about them only creates chances for spurious norms and pass disagreements.
#
# Heading patterns are matched on the heading WITHOUT its "Artikel N." prefix.
#   * anywhere in the title: definities, begripsbepalingen, toepassingsgebied,
#     citeertitel ("Materieel toepassingsgebied", GDPR art. 2, still matches);
#   * only at the start: onderwerp, inwerkingtreding, "wijziging(en) van",
#     "intrekking van". Anchored on purpose: AI Act art. 7 "Wijzigingen in bijlage III"
#     (a delegation) and art. 36 "Wijzigingen in de aanmelding" (obligations) must NOT
#     be skipped, and they don't match "wijziging(en) van".
# Skips are recorded on the provision as `_skip_reason`, printed in every run, and
# can be overridden per provision via FORCE_INCLUDE_UIDS.
# ---------------------------------------------------------------------------

_HEADING_PREFIX = re.compile(r"^\s*artikel\s+[\w.:/-]+\s*[.:]?\s*", re.IGNORECASE)
_NON_NORMATIVE_ANYWHERE = re.compile(
    r"\b(definities|begripsbepalingen|toepassingsgebied|citeertitel)\b", re.IGNORECASE)
_NON_NORMATIVE_START = re.compile(
    r"^(onderwerp|inwerkingtreding|wijziging(en)?\s+van|intrekking\s+van)\b", re.IGNORECASE)
_DEFINITION_OPENING = re.compile(
    r"^\s*(voor de toepassing van |in )deze (wet|verordening|richtlijn|regeling|"
    r"algemene maatregel van bestuur)\b.{0,80}?\bwordt\b.{0,40}?\bverstaan onder\b",
    re.IGNORECASE | re.DOTALL)

# uids of provisions to extract even though a pattern above matches them.
FORCE_INCLUDE_UIDS: set[str] = set()


def _skip_reason(p: dict) -> Optional[str]:
    if p.get("uid") in FORCE_INCLUDE_UIDS:
        return None
    title = _HEADING_PREFIX.sub("", p.get("heading") or "", count=1)
    if _NON_NORMATIVE_ANYWHERE.search(title) or _NON_NORMATIVE_START.search(title):
        return f"non_normative_heading: {title.strip()!r}"
    # Definition articles without a telling heading (common in Dutch laws). Only for
    # provisions without numbered paragraphs: an article whose lid 1 defines terms can
    # still impose norms in its other leden.
    if not p.get("paragraphs") and _DEFINITION_OPENING.search(p.get("text") or ""):
        return "definition_article: opens with 'wordt verstaan onder'"
    return None


def _iter_units(provisions: list[dict]) -> list[Unit]:
    """Units not yet done under the current schema version. "Done" is tracked by
    paragraph_index, not the displayed number: three articles in this corpus have two
    paragraphs sharing a displayed number (a source numbering artifact; see
    migrate_paragraph_index.py), and number-based tracking would skip the second one
    forever. Re-running is safe: done units are never offered again. Provisions that
    _skip_reason() classifies as non-normative yield no units."""
    units = []
    for p in provisions:
        _archive_stale_entries(p)
        reason = _skip_reason(p)
        if reason:
            p["_skip_reason"] = reason
            continue
        p.pop("_skip_reason", None)
        heading = p.get("heading") or f"Article {p.get('article') or p.get('number')}"
        done = ({n.get("paragraph_index") for n in p["norms"]}
                | set(p.get(EMPTY_PARAS_KEY) or []))
        paragraphs = p.get("paragraphs") or []
        if paragraphs:
            for idx, para in enumerate(paragraphs):
                if idx not in done:
                    units.append(Unit(p, para["number"], idx, para["text"], heading,
                                      _article_context(p, idx)))
        elif None not in done and not p.get(EMPTY_WHOLE_KEY):
            units.append(Unit(p, None, None, p["text"], heading))
    return units


# ---------------------------------------------------------------------------
# The two-pass extraction itself.
# ---------------------------------------------------------------------------


def _model_label(model: str, reasoning_effort: str) -> str:
    return model if reasoning_effort == "none" else f"{model} (reasoning:{reasoning_effort})"


def _call_with_network_retries(client, **kwargs):
    """Retries transient connection errors with backoff (seen in practice: one
    getaddrinfo failure ~120 calls into a run). Separate from _run_pass's content
    retry, which retries because the *output* was wrong, not the network."""
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


def _run_pass(client, model: str, framing: str, unit: Unit,
              reasoning_effort: str = "none") -> Optional[list[ExtractedNorm]]:
    """One extraction pass. Returns the norms only if EVERY verbatim span of every norm
    is a substring of the unit text; otherwise retries once WITH the violations fed
    back (an identical retry at temperature 0 would mostly reproduce the same output),
    then gives up (None). Also returns None if the API gives no parsed output twice."""
    feedback = None
    for attempt in range(2):
        kwargs = dict(
            model=model,
            instructions=framing,
            input=_build_input(unit.article_heading, unit.text, unit.context, feedback),
            text_format=ExtractedNormsResponse,
            # Default "none": extraction is structured reading, not open-ended
            # reasoning. Raised deliberately when retrying items that still disagreed
            # under a stronger model, to test reasoning depth as a separate lever.
            reasoning={"effort": reasoning_effort},
        )
        if reasoning_effort == "none":
            # This model family rejects `temperature` once reasoning is engaged
            # (BadRequestError at effort "medium"), so only pass it at "none".
            kwargs["temperature"] = 0
        resp = _call_with_network_retries(client, **kwargs)
        parsed = resp.output_parsed
        if parsed is None:
            print(f"    [pass] attempt {attempt + 1}: no parsed output (refusal or parse failure)")
            feedback = None
            continue
        violations = [v for n in parsed.norms for v in _verbatim_violations(n, unit.text)]
        if not violations:
            return parsed.norms
        print(f"    [pass validation] attempt {attempt + 1} failed verbatim check "
              f"({len(violations)} span(s)), {'retrying with feedback' if attempt == 0 else 'giving up on this pass'}")
        feedback = (
            "Your previous answer contained values that are not exact substrings of the "
            "unit text: " + "; ".join(violations) + ". Return the complete answer again. "
            "Copy every verbatim value character for character from the unit text, or "
            "use null / [] if the text contains no such span."
        )
    return None


# --- Agreement between the two passes ----------------------------------------------
# All compared spans are verbatim (enforced by _run_pass), so the two passes cannot
# paraphrase; what they differ on is span BOUNDARIES ("de aanbieder" vs "aanbieder",
# "indien X" vs "X"). Word-set containment tolerates boundary differences while still
# flagging genuinely different spans.

def _word_set(s: Optional[str]) -> set:
    return set(_normalize(s or "").split())


def _overlap(wa: set, wb: set) -> float:
    if not wa and not wb:
        return 1.0
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))


def _text_fields_agree(a: Optional[str], b: Optional[str], min_overlap: float = 0.5) -> bool:
    return _overlap(_word_set(a), _word_set(b)) >= min_overlap


def _list_fields_agree(a: Optional[list], b: Optional[list], min_overlap: float = 0.5) -> bool:
    """Compared as the union of words across all items, so the same span split into
    items differently, or with different boundaries, still agrees."""
    wa = set().union(*(_word_set(x) for x in (a or [])))
    wb = set().union(*(_word_set(x) for x in (b or [])))
    return _overlap(wa, wb) >= min_overlap


def _lemma_agree(a: Optional[str], b: Optional[str]) -> bool:
    """action_lemma is normalised by the model, not verbatim: exact match after
    whitespace/case normalisation. "melden" vs "verstrekken" must disagree."""
    return _normalize(a or "") == _normalize(b or "")


def _deadlines_agree(a: Optional[str], b: Optional[str]) -> bool:
    """Compares PARSED deadlines. When neither side parsed to a value, requires the raw
    text to match (two different unparseable phrases are not the same deadline), and
    the clock's starting event ("from") must match too (2026-09-24 fixes)."""
    pa, pb = _parse_deadline(a), _parse_deadline(b)
    if pa is None or pb is None:
        return pa == pb
    if pa["value"] is None and pb["value"] is None:
        return _normalize(pa["raw"]) == _normalize(pb["raw"])
    return (pa["value"] == pb["value"] and pa["unit"] == pb["unit"]
            and _normalize(pa["from"] or "") == _normalize(pb["from"] or ""))


# Every field downstream detection reads. action_span is deliberately NOT compared:
# for split verbs the two passes can segment it differently while agreeing on the
# action, which action_lemma captures.
_FIELD_CHECKS: dict[str, Callable] = {
    "deontic": lambda a, b: a == b,
    "actor": _text_fields_agree,
    "action_lemma": _lemma_agree,
    "object": _text_fields_agree,
    "trigger_event": _text_fields_agree,
    "deadline_raw": _deadlines_agree,
    "recipient": _list_fields_agree,
    "thresholds": _list_fields_agree,
    "conditions": _list_fields_agree,
}


def _norm_diffs(n1: ExtractedNorm, n2: ExtractedNorm) -> list[str]:
    """Names of the fields on which two aligned norms disagree; [] = full agreement."""
    return [f for f, agree in _FIELD_CHECKS.items() if not agree(getattr(n1, f), getattr(n2, f))]


def _norms_agree(n1: ExtractedNorm, n2: ExtractedNorm) -> bool:
    return not _norm_diffs(n1, n2)


def _norm_overlap_score(a: ExtractedNorm, b: ExtractedNorm) -> float:
    """Similarity used only to ALIGN the two passes' norm lists; agreement itself is
    judged afterwards per aligned pair by _norm_diffs."""
    score = 1.0 if a.deontic == b.deontic else 0.0
    if a.action_lemma and _lemma_agree(a.action_lemma, b.action_lemma):
        score += 1.0
    for f in ("object", "actor", "trigger_event"):
        wa, wb = _word_set(getattr(a, f)), _word_set(getattr(b, f))
        if wa and wb:
            score += _overlap(wa, wb)
    return score


def _align_norms(list1: list[ExtractedNorm], list2: list[ExtractedNorm]
                  ) -> list[tuple[Optional[ExtractedNorm], Optional[ExtractedNorm]]]:
    """Greedy best-match pairing by descending overlap score -- fine for the small lists
    (almost always 0-3) one unit produces. Unmatched entries pair with None, which the
    caller treats as a disagreement rather than guessing."""
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


def _finalize_norm(norm: ExtractedNorm, paragraph_number: Optional[str],
                    paragraph_index: Optional[int], norm_index: int,
                    extraction_model: str, **extra) -> dict:
    """The single place that turns an ExtractedNorm into a stored norms[] entry, used
    by normal extraction AND by the conservative resolver, so the stored shape cannot
    drift between the two paths. `extra` adds uncertainty metadata."""
    entry = {
        "schema_version": SCHEMA_VERSION,
        "number": paragraph_number,
        "paragraph_index": paragraph_index,
        # Position among the norms of THIS unit; with several norms per unit,
        # paragraph_index alone no longer identifies a norm.
        "norm_index": norm_index,
        "deontic": norm.deontic,
        "actor": norm.actor,
        "trigger_event": norm.trigger_event,
        "action_span": norm.action_span,
        "action_lemma": norm.action_lemma,
        "object": norm.object,
        "recipient": norm.recipient,
        "deadline_raw": norm.deadline_raw,
        "deadline": _parse_deadline(norm.deadline_raw),
        "thresholds": norm.thresholds,
        "conditions": norm.conditions,
        "extraction_model": extraction_model,
        "human_verified": False,
    }
    entry.update(extra)
    return entry


def extract_unit(client, model: str, unit: Unit,
                  reasoning_effort: str = "none") -> tuple[Optional[list[dict]], Optional[dict]]:
    """Returns (finalized_norms_or_None, review_queue_item_or_None) -- exactly one is
    non-None. finalized_norms can be an EMPTY list: both passes confirming "no norm
    here" is a resolved result, distinct from None (unresolved)."""
    pass1 = _run_pass(client, model, FRAMING_DIRECT, unit, reasoning_effort)
    pass2 = _run_pass(client, model, FRAMING_CHECKLIST, unit, reasoning_effort)
    base = {
        "schema_version": SCHEMA_VERSION,
        "instrument_id": unit.provision.get("instrument_id") or unit.provision.get("provision_id"),
        "article": unit.provision.get("article") or unit.provision.get("number"),
        "paragraph_number": unit.paragraph_number,
        "paragraph_index": unit.paragraph_index,
        "unit_text": unit.text,
    }

    def dump(norms):
        return [n.model_dump() for n in norms] if norms is not None else None

    if pass1 is None or pass2 is None:
        return None, {**base, "reason": "validation_failed", "pass1": dump(pass1), "pass2": dump(pass2)}

    if len(pass1) != len(pass2):
        # Disagreement on HOW MANY norms exist: no safe default, never auto-resolved.
        return None, {**base, "reason": "two_pass_disagreement", "disagreement_kind": "norm_count_mismatch",
                      "pass1": dump(pass1), "pass2": dump(pass2)}

    aligned = _align_norms(pass1, pass2)
    if any(n1 is None or n2 is None for n1, n2 in aligned):
        return None, {**base, "reason": "two_pass_disagreement", "disagreement_kind": "norm_alignment_ambiguous",
                      "pass1": dump(pass1), "pass2": dump(pass2)}

    diff_fields = sorted({f for n1, n2 in aligned for f in _norm_diffs(n1, n2)})
    if diff_fields:
        return None, {**base, "reason": "two_pass_disagreement", "disagreement_kind": "field_mismatch",
                      "diff_fields": diff_fields, "pass1": dump(pass1), "pass2": dump(pass2)}

    label = _model_label(model, reasoning_effort)
    finalized = [_finalize_norm(n1, unit.paragraph_number, unit.paragraph_index, idx, label)
                 for idx, (n1, _n2) in enumerate(aligned)]
    return finalized, None


def _store_result(unit: Unit, norms: list[dict]) -> None:
    """Writes a RESOLVED result (caller holds the lock)."""
    if norms:
        unit.provision.setdefault("norms", []).extend(norms)
    else:
        _mark_empty(unit.provision, unit.paragraph_index)


# ---------------------------------------------------------------------------
# Retrying the review queue on a different model / reasoning effort -- e.g.
# escalating from gpt-5.6-luna to gpt-5.6-terra (2026-09-20 decision) on only the
# items that disagreed. Works per document: each queue lives next to its norms file.
# ---------------------------------------------------------------------------


def _provision_key(p: dict) -> tuple:
    """The same identity extract_unit() writes into a queue item's instrument_id/article,
    so a queue item can be relocated to the exact provision it came from."""
    return (
        str(p.get("instrument_id") or p.get("provision_id")),
        str(p.get("article") or p.get("number")),
    )


def _retry_source_queue(client, model: str, reasoning_effort: str, concurrency: int,
                         spec: SourceSpec, queue: list) -> tuple[int, int]:
    """Retries one document's queue; returns (n_resolved, n_still_unresolved). One lock
    guards the in-memory bookkeeping and file writes, not the network calls."""
    root = _load_root(spec)
    by_key = {_provision_key(p): p for p in spec.get_provisions(root)}
    out_path, q_path = norms_path(spec), queue_path_for(spec)

    label = _model_label(model, reasoning_effort)
    lock = threading.Lock()
    remaining_queue: list = []
    n_resolved = 0
    dirty = False

    to_retry = []
    for item in queue:
        key = (str(item["instrument_id"]), str(item["article"]))
        provision = by_key.get(key)
        if provision is None:
            remaining_queue.append(item)
            print(f"  [warn] could not relocate provision for {key} -- keeping queue entry as-is")
        else:
            to_retry.append((item, provision))

    def flush():
        if dirty:
            _atomic_write_json(out_path, root)
        _atomic_write_json(q_path, remaining_queue)

    def process(entry):
        nonlocal n_resolved, dirty
        item, provision = entry
        unit = Unit(provision, item["paragraph_number"], item.get("paragraph_index"), item["unit_text"],
                    provision.get("heading") or f"Article {item['article']}",
                    _article_context(provision, item.get("paragraph_index")))
        norms, new_item = extract_unit(client, model, unit, reasoning_effort)
        with lock:
            if norms is not None:
                _store_result(unit, norms)
                n_resolved += 1
                dirty = True
            else:
                new_item["retried_with"] = label
                remaining_queue.append(new_item)
        return _queue_key(item), item["paragraph_number"], norms is not None

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(process, entry): entry for entry in to_retry}
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                key, para, resolved = fut.result()
            except Exception as e:
                # The original item stays out of remaining_queue only if processing
                # finished; on an exception, keep it so nothing is lost.
                item, _ = futures[fut]
                with lock:
                    remaining_queue.append(item)
                print(f"  [{i}/{len(to_retry)}] ERROR: {e} -- keeping queue entry", flush=True)
                continue
            print(f"  [{i}/{len(to_retry)}] retried {key} para {para} with {label} "
                  f"-- {'resolved' if resolved else 'still unresolved'}", flush=True)
            if i % 25 == 0 or i == len(to_retry):
                with lock:
                    flush()

    flush()  # covers the tail, and the case where nothing could be relocated
    return n_resolved, len(remaining_queue)


def retry_queue(client, model: str, reasoning_effort: str = "none", concurrency: int = 10,
                 only: Optional[str] = None) -> None:
    n_queued = n_resolved = n_remaining = 0
    for spec in SOURCES:
        if only and only.lower() not in spec.label.lower():
            continue
        queue = _load_queue(spec)
        if not queue:
            continue
        print(f"\n=== {spec.label}: retrying {len(queue)} queued unit(s) "
              f"({queue_path_for(spec).relative_to(ROOT)}) ===", flush=True)
        resolved, remaining = _retry_source_queue(client, model, reasoning_effort, concurrency, spec, queue)
        n_queued += len(queue)
        n_resolved += resolved
        n_remaining += remaining

    if n_queued == 0:
        print("review queue is empty -- nothing to retry")
        return
    print(f"\nretried {n_queued} queued unit(s) with {_model_label(model, reasoning_effort)}: "
          f"{n_resolved} resolved, {n_remaining} still unresolved")


# ---------------------------------------------------------------------------
# Conservatively resolving whatever is left once model/reasoning escalation stops
# being worth it (2026-09-21 decision). Every finding already requires expert
# confirmation before it is reportable (Part 6, report_eligible), so instead of gating
# the pipeline on a lawyer pre-clearing abstract sentences, remaining items are pushed
# through with a documented default and flagged extraction_uncertain=True.
#
# The rule is NON-SUPPRESSION: an uncertain call must never make a potential finding
# invisible; including something by mistake only costs a human dismissing a harmless
# candidate later. Concretely:
#   * deontic: default toward the reading that keeps the norm eligible for detection
#     (ACTIONABLE_DEONTICS); the other reading is kept as deontic_alternative.
#   * a single-pass EMPTY result is NOT accepted as "no norm here": that would hide
#     any norm the failed pass might have found. Such units go to the unresolved file.
#   * count/alignment mismatches (since 22 Oct 2026): norms both passes found are resolved
#     per pair as above; a norm only one pass found is kept and flagged as resting on a
#     single reading. Dropping the unit used to hide norms both passes agreed on.
# ---------------------------------------------------------------------------

# Deontics eligible for C1 detection. RIGHT and DELEGATION are currently excluded;
# keep this set and C1's eligibility rule in sync.
ACTIONABLE_DEONTICS = {"OBLIGATION", "PROHIBITION", "PERMISSION", "COMPETENCE", "EXEMPTION"}


def _pick_deontic(d1: str, d2: str) -> str:
    if d1 in ACTIONABLE_DEONTICS and d2 not in ACTIONABLE_DEONTICS:
        return d1
    if d2 in ACTIONABLE_DEONTICS and d1 not in ACTIONABLE_DEONTICS:
        return d2
    return d1  # no basis to prefer one; the other is stored as deontic_alternative


def _resolve_source_queue(spec: SourceSpec, queue: list) -> tuple[int, int]:
    """Resolves one document's queue; returns (n_resolved, n_dropped). Dropped units are
    logged to the unresolved file and are NOT marked done, so a later normal run will
    offer them for extraction again."""
    root = _load_root(spec)
    by_key = {_provision_key(p): p for p in spec.get_provisions(root)}

    dropped = []
    dirty = False
    n_resolved = 0

    for item in queue:
        key = (str(item["instrument_id"]), str(item["article"]))
        provision = by_key.get(key)
        if provision is None:
            print(f"  [warn] could not relocate provision for {key} -- dropping without a norm")
            dropped.append({**item, "drop_reason": "provision_not_relocated"})
            continue

        number, index = item["paragraph_number"], item.get("paragraph_index")

        # Count/alignment mismatches used to be dropped whole, which threw away norms both passes
        # agreed on (22 Oct 2026: NIS2 art. 23(1) and (4), DORA art. 19(3)-(4) were lost because one
        # pass split off one extra norm). They now follow the same non-suppressing rule as the rest:
        # norms both passes found are resolved per pair below; a norm only one pass found is kept,
        # flagged as resting on a single reading. Both passes here are verbatim-validated.

        if item["reason"] == "validation_failed":
            # One pass may still be fully verbatim-grounded; its norms become provisional.
            grounded = item.get("pass1") if item.get("pass1") is not None else item.get("pass2")
            if grounded is None:
                dropped.append({**item, "drop_reason": "no_grounded_pass"})
                continue
            if not grounded:
                # Non-suppression: one unconfirmed "no norm" must not close the unit.
                dropped.append({**item, "drop_reason": "single_pass_empty_unconfirmed"})
                continue
            for idx, g in enumerate(grounded):
                provision.setdefault("norms", []).append(_finalize_norm(
                    ExtractedNorm(**g), number, index, idx,
                    "conservative-default (single pass; the other failed validation)",
                    extraction_uncertain=True,
                    uncertainty_note=(
                        "only one of two extraction passes produced a verbatim-grounded "
                        "result; this rests on a single unconfirmed reading -- revisit if "
                        "it ends up inside an actual candidate finding."
                    ),
                ))
            n_resolved += 1
            dirty = True
            continue

        # Field-level disagreement with matching norm count: re-align and resolve per pair.
        p1_norms = [ExtractedNorm(**d) for d in item["pass1"]]
        p2_norms = [ExtractedNorm(**d) for d in item["pass2"]]
        aligned = _align_norms(p1_norms, p2_norms)
        for idx, (p1, p2) in enumerate(aligned):
            if p1 is None or p2 is None:
                provision.setdefault("norms", []).append(_finalize_norm(
                    p1 or p2, number, index, idx,
                    "conservative-default (found by one of two passes; see uncertainty_note)",
                    extraction_uncertain=True,
                    uncertainty_note=(
                        "only one of two extraction passes found this norm (the passes found a "
                        "different number of norms in this unit); kept by the non-suppressing "
                        "default -- revisit if it ends up inside an actual candidate finding."
                    ),
                ))
                continue
            diffs = _norm_diffs(p1, p2)
            deontic = _pick_deontic(p1.deontic, p2.deontic)
            base, other = (p1, p2) if deontic == p1.deontic else (p2, p1)
            extra = {
                "extraction_uncertain": bool(diffs),
                "uncertainty_note": (
                    f"two independent extraction passes disagreed on {diffs}; the stored "
                    "reading follows the non-suppressing default and the other reading is "
                    "kept in alternative_reading -- revisit if this norm ends up inside an "
                    "actual candidate finding." if diffs else None
                ),
                "alternative_reading": other.model_dump() if diffs else None,
            }
            if other.deontic != base.deontic:
                extra["deontic_alternative"] = other.deontic
            provision.setdefault("norms", []).append(_finalize_norm(
                base, number, index, idx,
                "conservative-default (two passes disagreed; see uncertainty_note)", **extra))
        n_resolved += 1
        dirty = True

    if dirty:
        _atomic_write_json(norms_path(spec), root)
        print(f"  wrote back -> {norms_path(spec).relative_to(ROOT)}")

    if dropped:
        dropped_path = unresolved_path_for(spec)
        existing = json.loads(dropped_path.read_text(encoding="utf-8")) if dropped_path.exists() else []
        existing.extend(dropped)
        _atomic_write_json(dropped_path, existing)
        print(f"  {len(dropped)} dropped -> {dropped_path.relative_to(ROOT)}")

    _atomic_write_json(queue_path_for(spec), [])
    return n_resolved, len(dropped)


def resolve_queue_conservatively(only: Optional[str] = None) -> None:
    n_queued = n_resolved = n_dropped = 0
    for spec in SOURCES:
        if only and only.lower() not in spec.label.lower():
            continue
        queue = _load_queue(spec)
        if not queue:
            continue
        print(f"\n=== {spec.label}: resolving {len(queue)} queued unit(s) "
              f"({queue_path_for(spec).relative_to(ROOT)}) ===", flush=True)
        resolved, dropped = _resolve_source_queue(spec, queue)
        n_queued += len(queue)
        n_resolved += resolved
        n_dropped += dropped

    if n_queued == 0:
        print("review queue is empty -- nothing to resolve")
        return
    print(f"\n{n_resolved} unit(s) finalized with the non-suppressing default (norms flagged "
          f"extraction_uncertain where the passes differed), {n_dropped} dropped -> "
          f"data/new_norms/new_*_norms_unresolved.json (re-offered by the next normal run)")


def main():
    """Writes the document's stage-4 norms file after every single unit: a long run
    over a real network occasionally hits a transient error partway through, and
    without per-unit persistence that progress would be lost."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--dry-run", action="store_true",
                     help="print scope, unit counts and output paths, make no API calls")
    ap.add_argument("--only", default=None,
                     help="substring match against a source's label (also applies to "
                          "--retry-queue and --resolve-queue)")
    ap.add_argument("--limit", type=int, default=None,
                     help="stop after this many units total, across all sources")
    ap.add_argument("--retry-queue", action="store_true",
                     help="re-attempt each document's review queue with --model")
    ap.add_argument("--resolve-queue", action="store_true",
                     help="finalize what remains in each review queue with the "
                          "non-suppressing conservative default. No API calls made.")
    ap.add_argument("--reasoning-effort", default="none",
                     choices=["none", "minimal", "low", "medium", "high", "xhigh", "max"],
                     help="default none; raise deliberately when retrying items that "
                          "still disagreed under a stronger model")
    ap.add_argument("--concurrency", type=int, default=10,
                     help="parallel units in flight per source (each makes 2 sequential "
                          "passes, so up to ~2x this many concurrent API calls)")
    args = ap.parse_args()

    if args.resolve_queue:  # no API calls, so no client (and no API key) needed
        resolve_queue_conservatively(args.only)
        return

    client = None
    if not args.dry_run:
        from openai import OpenAI
        client = OpenAI()   # reads OPENAI_API_KEY from the environmenthqPqA1OzcTCs3wpkxQ' # Inser key here
    if args.retry_queue:
        retry_queue(client, args.model, args.reasoning_effort, args.concurrency, args.only)
        return

    n_processed = n_finalized = n_queued = n_errors = 0

    from concurrent.futures import ThreadPoolExecutor, as_completed

    for spec in SOURCES:
        if args.only and args.only.lower() not in spec.label.lower():
            continue

        root = _load_root(spec)
        out_path, q_path = norms_path(spec), queue_path_for(spec)
        in_scope_provisions = [p for p in spec.get_provisions(root) if spec.in_scope(p)]
        units = _iter_units(in_scope_provisions)
        n_stale = sum(len(p.get(STALE_NORMS_KEY) or []) for p in in_scope_provisions)

        skipped = [p for p in in_scope_provisions if p.get("_skip_reason")]

        print(f"\n=== {spec.label} ({spec.path}) ===")
        print(f"  {len(in_scope_provisions)} provision(s) in scope, {len(units)} extraction unit(s)"
              + (f", {n_stale} archived norm(s) from older schema versions" if n_stale else ""))
        if skipped:
            print(f"  {len(skipped)} provision(s) skipped as non-normative "
                  f"(override via FORCE_INCLUDE_UIDS):")
            for p in skipped:
                print(f"    - art. {p.get('article') or p.get('number')}: {p['_skip_reason']}")
        print(f"  norms -> {out_path.relative_to(ROOT)}")
        print(f"  queue -> {q_path.relative_to(ROOT)}", flush=True)

        if args.dry_run:
            continue

        remaining = args.limit - n_processed if args.limit is not None else None
        if remaining is not None and remaining <= 0:
            break  # --limit budget exhausted -- stop entirely

        # Ensure the stage-4 file exists for every source this run reaches (also
        # persists the archiving of stale norms done by _iter_units).
        _atomic_write_json(out_path, root)

        batch = units[:remaining] if remaining is not None else units
        if not batch:
            continue  # this source is done; later sources may still have work

        write_lock = threading.Lock()

        def process_unit(unit: Unit) -> bool:
            norms, queue_item = extract_unit(client, args.model, unit, args.reasoning_effort)
            with write_lock:
                if norms is not None:
                    _store_result(unit, norms)
                else:
                    _save_queue_item(queue_item, q_path)
                _atomic_write_json(out_path, root)
            return norms is not None

        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            future_to_unit = {ex.submit(process_unit, u): u for u in batch}
            for i, fut in enumerate(as_completed(future_to_unit), 1):
                unit = future_to_unit[fut]
                n_processed += 1
                where = f"para {unit.paragraph_number}" if unit.paragraph_number else "(whole provision)"
                try:
                    finalized = fut.result()
                except Exception as e:
                    # Not queued: the unit simply stays "not done" and is offered again
                    # on the next run.
                    n_errors += 1
                    print(f"  [{i}/{len(batch)}] {unit.article_heading} {where} -- ERROR: {e}", flush=True)
                    continue
                if finalized:
                    n_finalized += 1
                else:
                    n_queued += 1
                print(f"  [{i}/{len(batch)}] {unit.article_heading} {where} "
                      f"-- {'finalized' if finalized else 'queued'}", flush=True)

        if args.limit is not None and n_processed >= args.limit:
            break

    if args.dry_run:
        return

    print(f"\n{n_processed} unit(s) processed: {n_finalized} finalized, {n_queued} queued "
          f"(data/new_norms/new_*_norms_review_queue.json), {n_errors} error(s) "
          f"(not saved; re-offered on the next run)", flush=True)


if __name__ == "__main__":
    main()