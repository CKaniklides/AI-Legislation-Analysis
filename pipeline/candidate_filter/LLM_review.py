#!/usr/bin/env python3
"""llm_review.py - LLM review of the selected norm pairs.

Takes the review queue (select_candidates.py) and the norm cards (norm_cards.py),
sends each pair to the LLM once, checks the answer with code, retries once if a
check fails, and writes one finding per pair.

Flow per pair
-------------
  1. Build the user prompt from the two cards (render_pair). Which norm is shown as
     A follows the pair_id hash, so queue order never favours one side; the finding
     records which norm was A and which was B.
  2. One LLM call with structured output (PairReview) and reasoning switched on.
  3. Code checks (check_review):
       - evidence spans appear verbatim in the <text> of their own norm card
         (whitespace and case normalised)
       - CONTRADICTION needs joint_compliance = impossible, TENSION needs
         possible_with_burden, DUPLICATION needs possible
       - duplication_scope is set if and only if the relation is DUPLICATION
       - DUPLICATION, CONTRADICTION, TENSION and RULE_EXCEPTION quote at least one span
       - every free-text field is written in Dutch (stopword test, see wrong_language)
  4. If a check fails: one retry, with every problem named in the prompt. If the
     retry fails too, the pair is kept with status needs_recheck -- never dropped,
     never accepted. Without a failure there is exactly one pass.

Every pair in the queue gets a finding, whatever the outcome (see build_finding):
  status  candidate      DUPLICATION / CONTRADICTION / TENSION / RULE_EXCEPTION, checks passed
          no_issue       RELATED_NO_ISSUE / UNRELATED, checks passed
          unverifiable   INSUFFICIENT_EVIDENCE
          needs_recheck  checks still failed after the retry (or no parsable answer)
          not_reviewed   the API call failed; re-run to retry (nothing is cached)
Top-tier findings and calibration findings are written to separate lists.

Output language
---------------
  All free-text fields (FREE_TEXT_FIELDS) are in Dutch; evidence fields are verbatim
  Dutch quotes. Only the fixed values of the categorical fields (relation,
  joint_compliance, resolution, ...) are English codes.

Cache
-----
  data/conflict/llm_review_cache.jsonl, append-only: one line per reviewed pair, so a
  crash loses at most the call in progress and re-runs never pay twice. The key is
  model + prompt_version + pair_id + both card hashes: changing the prompt, the
  schema, the checks, or a card makes the pair be reviewed again.

API key
-------
  Read from the OPENAI_API_KEY environment variable (or a .env file). Never put the
  key in this file.

Usage
-----
  python llm_review.py --dry-run                 # counts and one example prompt
  python llm_review.py --limit 50                # pilot on the first 50 queue rows
  python llm_review.py --model gpt-5.6-luna --reasoning-effort medium --concurrency 8
  python select_candidates.py --send             # select, then review in one go
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from create_LLM_norms import load_cards, render_pair

import truststore
truststore.inject_into_ssl()


DEFAULT_MODEL = "gpt-5.6-luna"
DEFAULT_REASONING_EFFORT = "medium"
DEFAULT_QUEUE = Path("data/conflict/LLM_review/review_queue.jsonl")
DEFAULT_CARDS = Path("data/new_norms/LLM_norm_cards.jsonl")
DEFAULT_OUT = Path("data/conflict/LLM_review/review_findings.json")
DEFAULT_CACHE = Path("data/conflict/LLM_review/llm_review_cache.jsonl")
CHECKS_VERSION = "1"            # bump when check_review() changes (2: language check)

# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------

YesNoUnclear = Literal["yes", "no", "partial", "unclear"]
Relation = Literal["DUPLICATION", "CONTRADICTION", "TENSION", "RULE_EXCEPTION",
                   "RELATED_NO_ISSUE", "UNRELATED", "INSUFFICIENT_EVIDENCE"]

NL = " Schrijf in het Nederlands."


class PairReview(BaseModel):
    # 1. Scope
    addressee_a: str = Field(description="Wie norm A bindt." + NL)
    addressee_b: str = Field(description="Wie norm B bindt." + NL)
    same_addressee: YesNoUnclear
    shared_situation: Optional[str] = Field(
        description="De concrete situatie waarin beide normen gelden, of null." + NL)
    situations_overlap: YesNoUnclear
    # 2. Requirements
    requirement_a: str = Field(description="Wat norm A precies verplicht, verbiedt of toestaat." + NL)
    requirement_b: str = Field(description="Wat norm B precies verplicht, verbiedt of toestaat." + NL)
    joint_compliance: Literal["possible", "possible_with_burden", "impossible", "not_applicable"]
    # 3. Relation
    relation: Relation
    duplication_scope: Optional[Literal["identical", "a_narrower", "b_narrower", "partial_overlap"]]
    intentional: Optional[bool]
    # 4. Resolution
    resolution: Literal["explicit_reference", "lex_superior", "lex_specialis", "lex_posterior",
                        "none", "unclear", "not_applicable"]
    resolution_basis: Optional[str] = Field(
        description="Waarop de oplossing van de verhouding berust, of null." + NL)
    # 5. Evidence and uncertainty
    evidence_a: Optional[str] = Field(
        description="Letterlijk citaat uit de <text> van norm A, in het origineel, of null.")
    evidence_b: Optional[str] = Field(
        description="Letterlijk citaat uit de <text> van norm B, in het origineel, of null.")
    reasoning_summary: str = Field(description="Korte motivering van de gekozen relatie." + NL)
    outcome_changing_fact: Optional[str] = Field(
        description="Het ontbrekende feit dat de uitkomst zou veranderen, of null." + NL)
    certainty: Literal["high", "medium", "low"]


FREE_TEXT_FIELDS = ("addressee_a", "addressee_b", "shared_situation", "requirement_a",
                    "requirement_b", "resolution_basis", "reasoning_summary",
                    "outcome_changing_fact")

ISSUE_RELATIONS = {"DUPLICATION", "CONTRADICTION", "TENSION", "RULE_EXCEPTION"}
NO_ISSUE_RELATIONS = {"RELATED_NO_ISSUE", "UNRELATED"}
REQUIRED_JOINT_COMPLIANCE = {"CONTRADICTION": "impossible",
                             "TENSION": "possible_with_burden",
                             "DUPLICATION": "possible"}

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You review pairs of legal norms from EU and Dutch legislation on digital law. Your task
is to determine how the two norms relate. The texts are in Dutch.

OUTPUT LANGUAGE
Write every free-text field in Dutch: addressee_a, addressee_b, shared_situation,
requirement_a, requirement_b, resolution_basis, reasoning_summary and
outcome_changing_fact. Use the legal terms of the texts themselves (for example
"verwerkingsverantwoordelijke", not "controller"; "toezichthoudende autoriteit", not
"supervisory authority"). Quote evidence verbatim in the original Dutch. Only the fixed
values of the categorical fields (relation, joint_compliance, resolution, ...) are
English codes; give them exactly as listed below.

The pair was selected by an automatic filter because the two norms MAY be related. That
filter did not establish that they share an addressee, a situation, or any conflict or
overlap. Check all of this yourself; most pairs turn out to be compatible.

INPUT FORMAT
Each norm is given as a <norm side="A"> or <norm side="B"> block:
  source        instrument id | EU or NL | type (regulation, directive, decision, act)
  location      chapter | article | heading | paragraph
  <text>        the verbatim provision text. This is the ONLY source of evidence quotes.
                scope="article" means the text is the whole article; "reduced" means
                sentences were left out, and each gap is marked […].
  <extracted>   an automatic extraction of the norm (deontic, addressee, action, object,
                conditions, ...). It is an aid and may contain errors; the <text> prevails.
                reliability="uncertain" means two extraction passes disagreed; <alt>
                shows the other reading.
  <definitions> defined terms from the norm's own instrument that occur in its text.
                A block <definitions for="A B"> holds definitions shared by both norms.
                Definitions are context, not norms to be compared.

RELATIONS (choose exactly one)

DUPLICATION      Both norms bind the same addressee to the same (or an equivalent)
                 requirement, prohibition, permission or power, in overlapping
                 situations. Complying with one also satisfies the other, in whole or
                 in part. Set duplication_scope to identical, a_narrower (A covers only
                 part of what B covers), b_narrower, or partial_overlap. Set
                 intentional = true if the repetition is deliberate (for example a Dutch
                 provision transposing a directive, or an explicit "overeenkomstig").
CONTRADICTION    In at least one realistic shared situation, the same addressee cannot
                 comply with both norms: one requires what the other forbids, one
                 forbids what the other permits, or the requirements are mutually
                 exclusive.
TENSION          Both norms can be complied with together, but only at a substantial
                 extra burden caused by diverging requirements for the same situation:
                 different deadlines, different recipients, different standards, or
                 double reporting of the same event.
RULE_EXCEPTION   One norm deliberately limits, exempts from or replaces the other
                 ("tenzij", "in afwijking van", "is niet van toepassing", "onverminderd",
                 an explicit reference, or a more specific rule for a subset of cases).
RELATED_NO_ISSUE Same subject, but the norms are compatible and complementary: for
                 example successive steps of one procedure, or different acts concerning
                 the same object.
UNRELATED        Different addressees, different situations or different subject
                 matter.
INSUFFICIENT_EVIDENCE  The text given does not allow a firm judgement. Name the missing
                 fact in outcome_changing_fact.

PROCEDURE: fill in the fields in this order
1. Who does each norm bind? Is it the same actor? Note that an EU directive is
   formally addressed to the Member States ("De lidstaten zorgen ervoor dat entiteiten
   ..."); the actor who actually has to comply is the entity named after "dat".
2. Is there a concrete situation in which both norms apply? Take conditions,
   exceptions and scope into account.
3. What exactly does each norm require, forbid or permit?
4. Can the addressee comply with both? Choose possible, possible_with_burden,
   impossible, or not_applicable (no shared situation).
5. Choose the relation. It must match step 4: CONTRADICTION requires impossible;
   TENSION requires possible_with_burden; DUPLICATION requires possible.
   duplication_scope is filled in for DUPLICATION only, and is null otherwise.
6. Does the legal order itself resolve the relation? Choose:
   explicit_reference  one text refers to the other or makes an exception for it
   lex_superior        EU law prevails over Dutch law; a higher Dutch rule prevails over
                       a lower one
   lex_specialis       the more specific rule prevails for its own cases
   lex_posterior       the later rule of the same rank prevails
   none, unclear, or not_applicable
   An unresolved CONTRADICTION is the most important finding. If the conflict is
   resolved, keep the relation and record how it is resolved.

RULES
- Use only the texts given. Do not add rules from your own knowledge of these laws. If
  you think a decisive provision is missing, choose INSUFFICIENT_EVIDENCE and name it.
- "kan" and "mag" express a permission or power, not an obligation. Do not read
  discretionary language as mandatory.
- evidence_a must be a literal, uninterrupted quote from the <text> of norm A, and
  evidence_b from the <text> of norm B, in the original Dutch. Do not translate, add
  quotation marks, shorten with "...", quote across a […] gap, or merge passages.
  DUPLICATION, CONTRADICTION, TENSION and RULE_EXCEPTION require at least one quote;
  quote the passage your judgement rests on.
- Write every free-text field in Dutch, also the short ones (an addressee, a
  situation). Never write an explanation in English.
- Be concise: each free-text field takes at most a few sentences.
"""

USER_TEMPLATE = """\
Determine how the following two norms relate. Fill in every field, in order. Write all
free-text fields in Dutch.

{pair}"""

RETRY_TEMPLATE = """\
Your previous answer for this pair was rejected by automatic checks:
{problems}
Answer again from scratch, making sure every field is consistent with the rules, that
every free-text field is in Dutch, and that every quote is copied literally from the
<text> of the right norm.

{user}"""


def prompt_version() -> str:
    blob = json.dumps([SYSTEM_PROMPT, USER_TEMPLATE, RETRY_TEMPLATE, CHECKS_VERSION,
                       PairReview.model_json_schema()], sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Pair assembly
# ---------------------------------------------------------------------------

def orient(row: dict, cards: dict[str, dict]) -> tuple[dict, dict]:
    """(card shown as A, card shown as B). Swapped when the last hex digit of the
    pair_id is odd: stable per pair, unrelated to queue order or source."""
    a, b = cards[str(row["norm_a"])], cards[str(row["norm_b"])]
    return (b, a) if int(row["pair_id"][-1], 16) % 2 else (a, b)


def pair_text(row: dict, cards: dict[str, dict], facts: str | None = None) -> str:
    card_a, card_b = orient(row, cards)
    return render_pair(card_a, card_b, facts)


_TEXT_RE = re.compile(r"<text[^>]*>(.*?)</text>", re.S)


def card_text(card: dict) -> str:
    m = _TEXT_RE.search(card["body"])
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

_QUOTE_CHARS = "\"'„“”‘’«» "


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def verbatim_in(span: str, text: str) -> bool:
    """Whitespace- and case-insensitive substring test. Quotation marks the model
    may wrap around a quote are ignored."""
    core = _norm(span).strip(_QUOTE_CHARS)
    return bool(core) and core in _norm(text)


# Function words that occur in only one of the two languages. Words shared by both
# ("in", "is") or with another meaning in Dutch ("of" = or, "die", "door") are left out.
_NL_WORDS = frozenset("de het een en van dat niet op te voor met zijn wordt worden "
                      "aan bij door om als ook naar deze dit moet kan geen".split())
_EN_WORDS = frozenset("the a an and that not to for with are be must which this "
                      "from by it its has have shall may their".split())
_WORD_RE = re.compile(r"[a-zà-ÿ]+")


def wrong_language(v: PairReview) -> list[str]:
    """Free-text fields that read as English: more English than Dutch function words
    (at least one). Short Dutch fields without function words ("De Commissie",
    "Verwerkingsverantwoordelijke") pass; short English ones ("The Commission") fail."""
    bad = []
    for field in FREE_TEXT_FIELDS:
        words = _WORD_RE.findall((getattr(v, field) or "").lower())
        en = sum(w in _EN_WORDS for w in words)
        nl = sum(w in _NL_WORDS for w in words)
        if en and en > nl:
            bad.append(field)
    return bad


def check_review(v: PairReview, text_a: str, text_b: str) -> list[str]:
    """Every problem found, as sentences that can be shown to the model. Empty = OK."""
    problems = []
    for span, text, side in ((v.evidence_a, text_a, "A"), (v.evidence_b, text_b, "B")):
        if span is not None and not verbatim_in(span, text):
            problems.append(f"evidence_{side.lower()} is not a literal quote from the <text> "
                            f"of norm {side}: {span[:120]!r}")
    required = REQUIRED_JOINT_COMPLIANCE.get(v.relation)
    if required and v.joint_compliance != required:
        problems.append(f"relation {v.relation} requires joint_compliance = {required}, "
                        f"but joint_compliance = {v.joint_compliance}")
    if v.relation == "DUPLICATION" and v.duplication_scope is None:
        problems.append("relation DUPLICATION requires duplication_scope to be set")
    if v.relation != "DUPLICATION" and v.duplication_scope is not None:
        problems.append(f"duplication_scope must be null when the relation is {v.relation}")
    if v.relation in ISSUE_RELATIONS and v.evidence_a is None and v.evidence_b is None:
        problems.append(f"relation {v.relation} requires at least one evidence quote "
                        f"(evidence_a or evidence_b)")
    english = wrong_language(v)
    if english:
        problems.append(f"these free-text fields are not written in Dutch: "
                        f"{', '.join(english)}. Write them in Dutch.")
    return problems


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def call_llm(client, model: str, effort: str, user: str, version: str):
    resp = client.responses.parse(
        model=model,
        instructions=SYSTEM_PROMPT,
        input=user,
        text_format=PairReview,
        reasoning={"effort": effort},
        prompt_cache_key=f"pair-review-{version}",
    )
    usage = getattr(resp, "usage", None)
    tokens = {"input": getattr(usage, "input_tokens", None),
              "output": getattr(usage, "output_tokens", None)}
    return resp.output_parsed, tokens


def review_pair(client, model: str, effort: str, row: dict, cards: dict[str, dict],
                version: str) -> dict:
    """At most two calls: one pass, plus one retry if a check fails."""
    card_a, card_b = orient(row, cards)
    text_a, text_b = card_text(card_a), card_text(card_b)
    user = USER_TEMPLATE.format(pair=render_pair(card_a, card_b))
    attempts, verdict, problems = [], None, []
    for attempt in (1, 2):
        prompt = user if attempt == 1 else RETRY_TEMPLATE.format(
            problems="\n".join(f"- {p}" for p in problems), user=user)
        verdict, tokens = call_llm(client, model, effort, prompt, version)
        problems = (["no parsable answer (empty output or refusal)"] if verdict is None
                    else check_review(verdict, text_a, text_b))
        attempts.append({"attempt": attempt, "problems": problems, "tokens": tokens})
        if not problems:
            break
    return {
        "pair_id": row["pair_id"],
        "shown_as": {"A": card_a["norm_id"], "B": card_b["norm_id"]},
        "verdict": verdict.model_dump() if verdict is not None else None,
        "needs_recheck_reason": "; ".join(problems) if problems else None,
        "attempts": attempts,
    }


# ---------------------------------------------------------------------------
# Cache (append-only JSONL)
# ---------------------------------------------------------------------------

def cache_key(model: str, version: str, row: dict) -> str:
    return f"{model}::{version}::{row['pair_id']}::{row['card_hash_a']}::{row['card_hash_b']}"


def load_cache(path: Path) -> dict[str, dict]:
    cache: dict[str, dict] = {}
    if not path.exists():
        return cache
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue            # a line cut off by a crash; that pair is re-run
            cache[entry["key"]] = entry
    return cache


class CacheWriter:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._f = path.open("a", encoding="utf-8")
        self._lock = threading.Lock()

    def append(self, entry: dict) -> None:
        with self._lock:
            self._f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._f.flush()
            os.fsync(self._f.fileno())

    def close(self) -> None:
        self._f.close()


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

CONFIDENCE_LABELS = {2: "Medium", 3: "Low"}
SCORE_FIELDS = ("combined", "action_score", "action_eff", "action_relation", "generic",
                "object_score", "object_norm", "object_key", "object_relation",
                "object_direction")


def _tier(status: str) -> tuple[Optional[int], list[str]]:
    if status == "candidate":
        return 2, ["single LLM pass, all code checks passed",
                   "Tier 1 (High) requires a second independent run, not implemented yet"]
    if status == "unverifiable":
        return 3, ["the model could not reach a firm verdict from the text given"]
    if status == "needs_recheck":
        return 3, ["the answer still failed the code checks after one retry"]
    return None, []


def build_finding(row: dict, cards: dict[str, dict], result: Optional[dict],
                  model: str, version: str, error: Optional[str] = None) -> dict:
    card_a, card_b = cards[str(row["norm_a"])], cards[str(row["norm_b"])]
    verdict = (result or {}).get("verdict")
    if result is None:
        status = "not_reviewed"
    elif result["needs_recheck_reason"]:
        status = "needs_recheck"
    elif verdict["relation"] == "INSUFFICIENT_EVIDENCE":
        status = "unverifiable"
    elif verdict["relation"] in ISSUE_RELATIONS:
        status = "candidate"
    else:
        status = "no_issue"
    tier, reasons = _tier(status)
    attempts = (result or {}).get("attempts", [])
    return {
        "finding_id": f"F-{row['pair_id']}",
        "pair_id": row["pair_id"],
        "relation": verdict["relation"] if verdict else None,
        "status": status,
        "confidence_tier": tier,
        "confidence_label": CONFIDENCE_LABELS.get(tier),
        "confidence_reasons": reasons,
        "selection": row.get("selection"),
        "calibration_band": row.get("calibration_band"),
        "provisions": [
            {"norm_id": c["norm_id"], "instrument_id": c["instrument_id"],
             "card_hash": c["card_hash"],
             "shown_as": next((s for s, n in (result or {}).get("shown_as", {}).items()
                               if n == c["norm_id"]), None)}
            for c in (card_a, card_b)
        ],
        "scope": "internal_coherence" if card_a["instrument_id"] == card_b["instrument_id"]
                 else "cross_law",
        "scores": {k: row.get(k) for k in SCORE_FIELDS if k in row},
        "llm_adjudication": verdict,
        "needs_recheck_reason": (result or {}).get("needs_recheck_reason"),
        "n_attempts": len(attempts),
        "tokens": {"input": sum(a["tokens"]["input"] or 0 for a in attempts),
                   "output": sum(a["tokens"]["output"] or 0 for a in attempts)},
        "error": error,
        "model": model,
        "prompt_version": version,
        "extracted_on": date.today().isoformat(),
        "human_verified": False,
        "report_eligible": False,
    }


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def review_queue(queue: list[dict], cards: dict[str, dict], *, model: str = DEFAULT_MODEL,
                 effort: str = DEFAULT_REASONING_EFFORT, concurrency: int = 8,
                 out: Path = DEFAULT_OUT, cache_path: Path = DEFAULT_CACHE,
                 dry_run: bool = False, client=None) -> dict:
    version = prompt_version()
    cache = load_cache(cache_path)
    todo = [r for r in queue if cache_key(model, version, r) not in cache]
    print(f"{len(queue)} pairs in queue: {len(queue) - len(todo)} cached, "
          f"{len(todo)} to review (model {model}, effort {effort}, prompt {version})")

    if dry_run:
        if queue:
            print("\n--- example prompt (first queue row) ---\n"
                  + USER_TEMPLATE.format(pair=pair_text(queue[0], cards)))
        return {}

    errors: dict[str, str] = {}
    if todo:
        if client is None:
            from openai import OpenAI
            client = OpenAI(max_retries=5)     # key from OPENAI_API_KEY; retries transient errors
        writer = CacheWriter(cache_path)
        try:
            with ThreadPoolExecutor(max_workers=concurrency) as ex:
                futures = {ex.submit(review_pair, client, model, effort, r, cards, version): r
                           for r in todo}
                for done, fut in enumerate(as_completed(futures), 1):
                    r = futures[fut]
                    try:
                        result = fut.result()
                    except Exception as e:          # API failure: not cached, re-run later
                        errors[r["pair_id"]] = f"{type(e).__name__}: {e}"
                        print(f"  [{done}/{len(todo)}] {r['pair_id']} ERROR {errors[r['pair_id']]}")
                        continue
                    entry = {"key": cache_key(model, version, r), **result}
                    writer.append(entry)
                    cache[entry["key"]] = entry
                    v = result["verdict"]
                    label = (f"NEEDS_RECHECK ({result['needs_recheck_reason']})"
                             if result["needs_recheck_reason"] else v["relation"])
                    print(f"  [{done}/{len(todo)}] {r['pair_id']} {label}", flush=True)
        finally:
            writer.close()

    findings = [build_finding(r, cards, cache.get(cache_key(model, version, r)), model,
                              version, errors.get(r["pair_id"])) for r in queue]
    output = {
        "meta": {"model": model, "reasoning_effort": effort, "prompt_version": version,
                 "created": date.today().isoformat(), "n_pairs": len(findings)},
        "findings": [f for f in findings if f["selection"] != "calibration"],
        "calibration": [f for f in findings if f["selection"] == "calibration"],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(output, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, out)

    statuses = Counter(f["status"] for f in findings)
    relations = Counter(f["relation"] for f in findings if f["relation"])
    print(f"\n{len(findings)} findings -> {out}")
    print(f"  status:   {dict(statuses)}")
    print(f"  relation: {dict(relations)}")
    if errors:
        print(f"  {len(errors)} pairs not reviewed because of API errors; re-run to retry.",
              file=sys.stderr)
    return output


def read_queue(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def main() -> None:
    p = argparse.ArgumentParser(description="LLM review of the selected norm pairs.")
    p.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    p.add_argument("--cards", type=Path, default=DEFAULT_CARDS)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT,
                   choices=["minimal", "low", "medium", "high"])
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--limit", type=int, default=None, help="review only the first N queue rows")
    p.add_argument("--dry-run", action="store_true",
                   help="print counts and one example prompt; no API calls")
    args = p.parse_args()

    queue = read_queue(args.queue)
    if args.limit:
        queue = queue[:args.limit]
    cards = load_cards(args.cards)
    missing = {str(r[k]) for r in queue for k in ("norm_a", "norm_b")} - set(cards)
    if missing:
        sys.exit(f"{len(missing)} queued norm_ids have no card, e.g. {sorted(missing)[:3]}. "
                 f"Rebuild the cards and the queue.")
    review_queue(queue, cards, model=args.model, effort=args.reasoning_effort,
                 concurrency=args.concurrency, out=args.out, cache_path=args.cache,
                 dry_run=args.dry_run)


if __name__ == "__main__":
    main()