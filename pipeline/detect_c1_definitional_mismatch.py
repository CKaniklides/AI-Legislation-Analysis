# -*- coding: utf-8 -*-
"""
C1 extension (2026-09-28): definitional mismatch detection.

Added after validating C1 against data/real_conflicts (an external, literature-sourced
ground-truth set): INC-0004 there (GDPR's controller/processor model vs. the AI Act's
provider/deployer/importer/distributor model) was never extracted as a normative record
at all -- a definitions article carries no deontic obligation, so it's excluded from
ELIGIBLE_DEONTICS entirely, and the existing duty_conflict machinery structurally cannot
compare it. This is a genuinely different kind of tension from duty_conflict: not "these
two rules impose incompatible duties on the same actor", but "these two instruments
define the same real-world role/concept differently, which can misalign who is
accountable for what, or how something is classified, across regimes" -- exactly
INC-0004's own claim, and a milder version of INC-0003's ("high risk" defined
differently under GDPR vs. the AI Act).

Reuses detect_c1_contradiction.py's own definitions parsing (SOURCES, _parse_definitions)
rather than duplicating it -- that module's load_definitions_by_instrument() already does
the real work of turning a definitions article's raw text into {term: definition}, but
only for injecting CONTEXT into other norms' prompts, so it doesn't keep the term's own
source article. Re-derived here, without touching that existing function or its callers.

Candidate generation is a small, cheap, all-pairs-feasible embedding kNN over every
instrument's defined terms (checked directly before trusting it: GDPR's
"verwerkingsverantwoordelijke" (controller) vs. AI Act's "gebruiksverantwoordelijke"
(deployer) scores 0.72 cosine similarity on "term: definition" text -- the real,
motivating case -- well below the 0.85 threshold used elsewhere in this project for
provision-level text, but the definitions universe here is tiny (a few hundred terms
total across all instruments), so kNN naturally surfaces it without needing a fixed
cutoff at all).

Output: appends findings directly into data/findings_c1.json's existing "findings" list,
subtype="definitional_mismatch", same file and category ("contradiction") as
duty_conflict/deontic_polarity_conflict/standard_collision/threshold_mismatch/
competence_competition -- one detection category, another way a tension can show up.
Never Tier 1 (100% AI-judged, no deterministic check available for this subtype).

Usage:
    python detect_c1_definitional_mismatch.py             # full run
    python detect_c1_definitional_mismatch.py --dry-run   # candidate generation only
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Literal, Optional

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import detect_c1_contradiction as c1
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

DEFINITIONAL_ADJUDICATION_VERSION = "v2_subset_orthogonal_checks"
MIN_DEFINITION_LEN = 15
MAX_TERM_WORDS = 6


def load_definitions_with_article() -> list[dict]:
    """[{instrument_id, article, term, definition}, ...] -- flat list, filtered to
    entries that look like real defined terms (non-empty definition, term isn't a
    stray sentence fragment). Checked directly against real output: NIS2 and DORA's
    definitions articles are structured differently from Cbw/GDPR/AI Act's and produce
    a handful of garbage entries (e.g. "aan de commissie en de samenwerkingsgroep",
    which isn't a defined term at all) -- not worth a bespoke parser fix for a few
    items the LLM step will just call unrelated_concepts on anyway. Candidate
    generation only needs to be a plausible signal, the same principle this project's
    other candidate-generation code relies on throughout."""
    out = []
    seen = set()
    for path, get_provisions in c1.SOURCES:
        root = json.loads((ROOT / path).read_text(encoding="utf-8"))
        for p in get_provisions(root):
            if not any(n["deontic"] == "DEFINITION" for n in p.get("norms", [])):
                continue
            iid = p.get("instrument_id") or (
                "BWBR0051796" if "uitvoeringswet" in path.lower() else
                "BWBR0049497" if "bijlage35" in path.lower() else None)
            article = str(p.get("article") or p.get("number") or "")
            defs = c1._parse_definitions(p.get("text") or "")
            for term, definition in defs.items():
                if not definition or len(definition) < MIN_DEFINITION_LEN:
                    continue
                if len(term.split()) > MAX_TERM_WORDS:
                    continue
                # A "term" containing a digit is a citation ("aanbeveling 2003/361/eg",
                # "verordening (eu) 2022/2554"), not a real defined concept -- checked
                # directly, all 9 such entries in this corpus are instrument citations,
                # not comparable concepts. Excluding them here (parser level) rather
                # than relying on the LLM to notice is cheaper and more reliable.
                if any(c.isdigit() for c in term):
                    continue
                # "deze wet wordt aangehaald als" (this Act is cited as) is a Dutch
                # law's own citation-title clause, not a defined concept -- checked
                # directly, it appeared identically in all 4 Dutch instruments and
                # produced a needs_recheck finding (nothing meaningful to compare).
                if "aangehaald als" in term:
                    continue
                key = (iid, term)
                if key in seen:
                    continue
                seen.add(key)
                out.append({"instrument_id": iid, "article": article, "term": term, "definition": definition})
    return out


class DefinitionalMismatchVerdict(BaseModel):
    concept_relation: Literal["same_concept", "overlapping_but_distinct_roles", "unrelated_concepts"]
    # Structural checks (2026-09-28 fix -- see module docstring's "round 2" note):
    # forces the model to rule out the two failure modes that made the first version
    # over-trigger on ~39% of candidates, instead of trusting a free-text verdict alone.
    is_subset_relationship: bool
    is_orthogonal_attribute: bool
    both_answer_same_accountability_question: bool
    # ONE specific, realistic fact pattern -- required for DEFINITIONAL_MISMATCH, not
    # an abstract "could differ in some case".
    concrete_scenario: Optional[str]
    mismatch_description: str
    practical_consequence: Optional[str]
    verdict: Literal["DEFINITIONAL_MISMATCH", "CONSISTENT", "UNRELATED_CONCEPTS", "INSUFFICIENT_EVIDENCE"]
    evidence_span_a: Optional[str]  # verbatim in definition A's text
    evidence_span_b: Optional[str]  # verbatim in definition B's text
    confidence: float


def _consistency_issue(v: DefinitionalMismatchVerdict, def_a: str, def_b: str) -> Optional[str]:
    if not (0.0 <= v.confidence <= 1.0):
        return f"confidence {v.confidence} outside [0, 1]"
    if v.verdict == "UNRELATED_CONCEPTS" and v.concept_relation != "unrelated_concepts":
        return "verdict=UNRELATED_CONCEPTS but concept_relation doesn't say so"
    if v.verdict == "DEFINITIONAL_MISMATCH":
        if v.concept_relation == "unrelated_concepts":
            return "verdict=DEFINITIONAL_MISMATCH but concept_relation=unrelated_concepts"
        if v.is_subset_relationship:
            return "verdict=DEFINITIONAL_MISMATCH but is_subset_relationship=True (a genus/species " \
                   "relationship, e.g. 'undertaking' vs 'micro-undertaking', is never a mismatch)"
        if v.is_orthogonal_attribute:
            return "verdict=DEFINITIONAL_MISMATCH but is_orthogonal_attribute=True (two different " \
                   "classification axes that normally co-apply are never a mismatch)"
        if not v.both_answer_same_accountability_question:
            return "verdict=DEFINITIONAL_MISMATCH but both_answer_same_accountability_question=False"
        if not v.concrete_scenario:
            return "verdict=DEFINITIONAL_MISMATCH but no concrete_scenario was given"
        if not v.practical_consequence:
            return "verdict=DEFINITIONAL_MISMATCH but no practical_consequence was given"
    for span, text, label in ((v.evidence_span_a, def_a, "A"), (v.evidence_span_b, def_b, "B")):
        if span is not None and c1._normalize_ws(span) not in c1._normalize_ws(text):
            return f"evidence_span_{label.lower()} is not a verbatim substring of definition {label}"
    return None


def adjudicate_definitional_pair(client, model: str, ea: dict, eb: dict) -> tuple[DefinitionalMismatchVerdict, Optional[str]]:
    def build_prompt(retry_note: Optional[str] = None) -> str:
        note = (f"\nYour previous response was rejected: {retry_note}. Reconsider and "
                f"answer again, making sure your verdict matches your own reasoning.\n"
                if retry_note else "")
        return (
            "Two legal instruments each define a term, quoted verbatim below. Candidate "
            "generation found these definitions textually/semantically similar -- it did "
            "NOT establish that they describe the same real-world concept or role; that "
            "is your job, and you should expect MOST candidates to NOT be a real "
            "mismatch -- two adjacent laws having their own, differently-scoped "
            "definitions is the normal, correct state of affairs, not a problem.\n\n"
            "Before any verdict, rule out two specific failure modes that are NOT a "
            "mismatch, however different the wording sounds:\n"
            "1. SUBSET/genus-species (is_subset_relationship): one term is simply a "
            "narrower category wholly contained in the other, e.g. 'undertaking' vs. "
            "'micro-undertaking', or 'incident' vs. 'severe incident'. Every instance of "
            "the narrower term is automatically an instance of the broader one -- "
            "nothing needs resolving, this is completely normal legal drafting.\n"
            "2. ORTHOGONAL ATTRIBUTE (is_orthogonal_attribute): the two terms classify "
            "DIFFERENT AXES of the same situation, which normally and harmlessly BOTH "
            "apply at once with no tension -- e.g. 'biometric data' (a data category) "
            "and 'emotion-recognition system' (a system purpose): a system can obviously "
            "be both without any conflict, since they answer different questions, not "
            "competing answers to the same one. Similarly, two domain-specific incident "
            "definitions (financial-ICT vs. AI) that are each correctly scoped to their "
            "own regime are NOT a mismatch just because one event might satisfy only one "
            "of them -- that is the definitions working as intended, not a gap.\n\n"
            "Only when NEITHER failure mode applies, decide: do these terms refer to the "
            "same real-world concept (same_concept), or to overlapping but distinct "
            "roles/scopes that are genuinely COMPETING answers to the SAME practical "
            "question -- e.g. 'who is legally responsible for this system' or 'does this "
            "event trigger a duty' -- for the SAME underlying real-world scenario "
            "(overlapping_but_distinct_roles, with both_answer_same_accountability_"
            "question=true), or to genuinely unrelated concepts (unrelated_concepts)? "
            "Only call it a DEFINITIONAL_MISMATCH if you can state ONE specific, "
            "realistic scenario (concrete_scenario) where the SAME real activity needs a "
            "single consistent answer and gets a DIFFERENT one from each instrument's "
            "definition -- not an abstract 'could differ in some case'. State the "
            "practical_consequence (an accountability gap, inconsistent classification, "
            "duplicated or dropped responsibility). If the definitions are simply "
            "consistent restatements of the same concept, that is CONSISTENT. If "
            "unrelated, say so. Quote verbatim evidence_span_a/b from the text below; if "
            "you can't support a firm verdict, return INSUFFICIENT_EVIDENCE." + note + "\n\n"
            f"DEFINITION A -- {ea['instrument_id']} art. {ea['article']}, term {ea['term']!r}\n"
            f"{ea['definition']}\n\n"
            f"DEFINITION B -- {eb['instrument_id']} art. {eb['article']}, term {eb['term']!r}\n"
            f"{eb['definition']}\n"
        )

    verdict = None
    issue = None
    for _attempt in range(2):
        resp = client.responses.parse(
            model=model, input=build_prompt(issue), text_format=DefinitionalMismatchVerdict,
            temperature=0, reasoning={"effort": "none"},
        )
        verdict = resp.output_parsed
        issue = _consistency_issue(verdict, ea["definition"], eb["definition"])
        if issue is None:
            return verdict, None
    return verdict, issue  # still inconsistent after retry -- kept, flagged


def compute_tier(verdict: DefinitionalMismatchVerdict, needs_recheck_reason: Optional[str]) -> tuple[int, list[str]]:
    if needs_recheck_reason:
        return 3, [f"internally inconsistent response even after a retry: {needs_recheck_reason}"]
    if verdict.verdict == "INSUFFICIENT_EVIDENCE":
        return 3, ["the model could not reach a firm verdict from the definitions' text alone"]
    if verdict.confidence >= 0.8:
        return 2, ["resolved by AI judgement (definitional comparison), not a deterministic "
                   "computation -- never Tier 1 regardless of how confident the model was",
                   "model expressed high confidence in its own comparison"]
    return 3, ["resolved by AI judgement (definitional comparison), lower model confidence"]


def build_finding(ea: dict, eb: dict, verdict: DefinitionalMismatchVerdict, tier: int,
                   tier_reasons: list[str], needs_recheck_reason: Optional[str] = None) -> dict:
    ids = sorted([f"{ea['instrument_id']}:{ea['term']}", f"{eb['instrument_id']}:{eb['term']}"])
    pair_id = hashlib.sha256("::".join(ids).encode("utf-8")).hexdigest()[:12]
    status = "needs_recheck" if needs_recheck_reason else "candidate"
    incompatibility_established = None
    if not needs_recheck_reason:
        if verdict.verdict == "DEFINITIONAL_MISMATCH":
            incompatibility_established = True
        elif verdict.verdict in ("CONSISTENT", "UNRELATED_CONCEPTS"):
            incompatibility_established = False
    return {
        "finding_id": f"F-C1-DEF-{pair_id}",
        "category": "contradiction",
        "subtype": "definitional_mismatch",
        "status": status,
        "confidence_tier": tier,
        "confidence_label": c1.CONFIDENCE_LABELS[tier],
        "confidence_reasons": tier_reasons,
        "incompatibility_established": incompatibility_established,
        "challenge": None,
        "provisions": [
            {"uid": None, "instrument_id": e["instrument_id"], "article": e["article"],
             "paragraph_number": None, "norm_index": None, "paragraph_index": None,
             "defined_term": e["term"]}
            for e in (ea, eb)
        ],
        "criteria_fired": ["definition_embedding_similarity"],
        "deterministic_result": None,
        "llm_adjudication": verdict.model_dump(),
        "report_eligible": False,
    }


def generate_definition_pairs(client, entries: list[dict], k: int = 8) -> list[tuple[dict, dict]]:
    import similarity
    texts = [f"{e['term']}: {e['definition']}" for e in entries]
    groups = [e["instrument_id"] for e in entries]
    knn_pairs = similarity.semantic_candidate_pairs(client, texts, k=k, groups=groups, k_within=0)
    return [(entries[i], entries[j]) for i, j in knn_pairs
            if entries[i]["instrument_id"] != entries[j]["instrument_id"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=c1.DEFAULT_MODEL)
    ap.add_argument("--dry-run", action="store_true",
                     help="candidate generation only, no LLM calls")
    ap.add_argument("--semantic-k", type=int, default=4,
                     help="checked directly: k=4 already includes the motivating "
                          "verwerkingsverantwoordelijke<->gebruiksverantwoordelijke pair "
                          "(0.72 cosine) at roughly half the candidate volume of k=8")
    args = ap.parse_args()

    from openai import OpenAI
    client = OpenAI()

    entries = load_definitions_with_article()
    print(f"{len(entries)} usable defined terms across all instruments", flush=True)
    pairs = generate_definition_pairs(client, entries, k=args.semantic_k)
    print(f"{len(pairs)} cross-instrument candidate definition-pair(s)", flush=True)

    if args.dry_run:
        for ea, eb in pairs:
            print(f"  {ea['instrument_id']} {ea['term']!r} <-> {eb['instrument_id']} {eb['term']!r}")
        return

    cache_path = DATA / "c1_definitional_cache.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}

    def cache_key(ea: dict, eb: dict) -> str:
        ids = sorted([f"{ea['instrument_id']}:{ea['term']}", f"{eb['instrument_id']}:{eb['term']}"])
        h = hashlib.sha256("::".join(ids).encode("utf-8")).hexdigest()[:12]
        return f"{DEFINITIONAL_ADJUDICATION_VERSION}::{args.model}::{h}"

    findings_path = DATA / "findings_c1.json"
    findings_data = json.loads(findings_path.read_text(encoding="utf-8"))
    existing_ids = {f["finding_id"] for f in findings_data["findings"]}

    new_findings = []
    for i, (ea, eb) in enumerate(pairs, 1):
        key = cache_key(ea, eb)
        if key in cache:
            entry = cache[key]
            verdict = DefinitionalMismatchVerdict(**entry["verdict"])
            needs_recheck_reason = entry["needs_recheck_reason"]
            tag = "[cached] "
        else:
            verdict, needs_recheck_reason = adjudicate_definitional_pair(client, args.model, ea, eb)
            cache[key] = {"verdict": verdict.model_dump(), "needs_recheck_reason": needs_recheck_reason}
            cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
            tag = f"[{i}/{len(pairs)}] "
        label = f"{ea['instrument_id']} {ea['term']!r} <-> {eb['instrument_id']} {eb['term']!r}"
        # A negative verdict (unrelated/consistent) is not a finding. needs_recheck normally keeps
        # a response visible, but only matters when it could hide a positive: if the verdict and
        # concept_relation AGREE it is negative (the recheck was just a non-verbatim quoted span --
        # 15 of the 32 recheck items in this corpus), so drop it. If they CONTRADICT (verdict
        # UNRELATED but relation "overlapping roles" -- the other 17), which field is right is
        # genuinely unclear, so those stay visible as needs_recheck.
        negative_agrees = ((verdict.verdict == "UNRELATED_CONCEPTS" and verdict.concept_relation == "unrelated_concepts")
                           or (verdict.verdict == "CONSISTENT" and verdict.concept_relation == "same_concept"))
        if verdict.verdict in ("UNRELATED_CONCEPTS", "CONSISTENT") and (not needs_recheck_reason or negative_agrees):
            print(f"  {tag}{label} -- {verdict.verdict}", flush=True)
            continue
        tier, tier_reasons = compute_tier(verdict, needs_recheck_reason)
        finding = build_finding(ea, eb, verdict, tier, tier_reasons, needs_recheck_reason)
        if finding["finding_id"] not in existing_ids:
            new_findings.append(finding)
            existing_ids.add(finding["finding_id"])
        rtag = " (needs_recheck)" if needs_recheck_reason else ""
        print(f"  {tag}{label} -- {verdict.verdict}{rtag} [{c1.CONFIDENCE_LABELS[tier]}]", flush=True)

    findings_data["findings"].extend(new_findings)
    findings_path.write_text(json.dumps(findings_data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{len(new_findings)} new definitional_mismatch finding(s) added -> "
          f"{findings_path.relative_to(ROOT)}", flush=True)


if __name__ == "__main__":
    main()
