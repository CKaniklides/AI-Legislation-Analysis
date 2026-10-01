# -*- coding: utf-8 -*-
"""
PILOT (2026-09-28) -- graded-verdict "legal tension" adjudicator, run on a small stratified
sample to decide whether a full re-run is worth the spend. Does NOT touch
findings_c1.json or the strict duty_conflict cache; writes only its own cache and a
results file.

Why this exists: across ~11,500 duty-conflict verdicts the strict 3-way scale
(CONTRADICTION / NOT_A_CONFLICT / INSUFFICIENT_EVIDENCE) never once answered
joint_compliance_possible=False, and its prompt is almost entirely about ways to
RECONCILE two rules. An outside review suggested (a) a graded scale that can express
"permission vs. restriction", "same duty, different benchmark" and "works against the
other's purpose", (b) a prompt that argues both sides before deciding, and (c) trying a
stronger model with reasoning switched on. This pilot tests (a)+(b) on both luna
(no reasoning) and terra (reasoning=medium) so (c) is measured, not assumed.

The definitional-mismatch detector is the cautionary precedent: its permissive first
version flagged 332 of 878 candidates (38%) and was almost all noise; the strictness added
in v2 cut that to 28 of 858. So this pilot deliberately includes RANDOM CONFIDENT
NEGATIVES as a false-positive control -- a graded scale that lights up on those is
over-triggering, not finding something.

The 10-item real_conflicts file has already shaped four fixes, so the "anchor" stratum
is a should-not-regress sanity check, NOT an unbiased recall estimate.
"""
import argparse
import json
import random
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Literal, Optional

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
_HERE = Path(__file__).resolve().parent
_PIPELINE = _HERE.parent
for _d in (_PIPELINE, _PIPELINE / "c1", _PIPELINE / "preprocessing", _PIPELINE / "c2"):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))
import detect_c1_contradiction as c1
import networkx as nx
from pydantic import BaseModel

ROOT = c1.ROOT
DATA = ROOT / "data"
# v2 (2026-09-28): the generic 4-class scale (direct_conflict/permission_conflict/
# divergent_standard/goal_tension) was tested against the dev set and found too coarse --
# PERMISSION_CONFLICT alone was doing the work of two legally distinct patterns (a
# sensitive-data exception, and an unrelated e-ID assurance-level exception), and three
# real dev-set tensions (INC-0003, INC-0006, INC-0007) were being called COMPATIBLE
# outright because none of the 4 classes named what was actually happening: a divergent
# accuracy/security BENCHMARK (INC-0006), an individual RIGHT that a system-level duty
# doesn't guarantee can be exercised (INC-0007), and two instruments' own risk-
# classification triggers not lining up (INC-0003/0004). Named per the user's own
# taxonomy. direct_conflict/permission_conflict/goal_tension are KEPT as residual classes
# for tensions that are real but don't fit one of the four specific patterns (e.g. Wdo
# art. 6(4) vs. 7(1), an e-ID assurance-level exception -- not about sensitive data, not
# a benchmark mismatch, not a rights/system-duty split, not a classification trigger) --
# demoting that already-validated finding to force-fit a named bucket would be worse than
# keeping an honest "other permission tension" label for it.
# v3 (2026-09-28, same day): v2's first full run produced 3 direct_conflict and 9
# rights_vs_system_duties_tension findings that did not survive a careful legal re-read --
# all 3 direct_conflict cases had an avoidable/voluntary duty, a same-instrument general-
# rule-vs-override structure, or an artificial dual-role actor assumption; all 9 rights
# findings mistook "GDPR imposes an independent, additional per-decision requirement the
# AI Act doesn't excuse" (a normal cumulative-requirements case, fully compatible) for a
# structural tension. Added explicit guards for both failure modes and tightened
# rights_vs_system_duties_tension's definition to require a genuine structural mismatch
# (population-level measure vs. individual-level right), not "operator had the capability
# but a specific instance shows they didn't use it". sensitive_data_tension (17) and the
# one classification_mismatch example were NOT affected by either flaw and are unchanged.
VERSION = "v3_tightened_tensions"
SEED = 20260928
TENSION_VERDICTS = ("direct_conflict", "standard_benchmark_tension", "rights_vs_system_duties_tension",
                    "sensitive_data_tension", "classification_mismatch", "permission_conflict", "goal_tension")
CONFIGS = [("gpt-5.6-luna", "none"), ("gpt-5.6-terra", "medium")]


class TensionVerdict(BaseModel):
    shared_subject: Optional[str]
    strongest_case_for_tension: str
    strongest_case_for_compatibility: str
    verdict: Literal["direct_conflict", "standard_benchmark_tension", "rights_vs_system_duties_tension",
                     "sensitive_data_tension", "classification_mismatch", "permission_conflict",
                     "goal_tension", "compatible", "unrelated", "missing_context"]
    concrete_scenario: Optional[str]     # required for any tension verdict
    missing_reference: Optional[str]     # required for missing_context
    decisive_exception: Optional[str]    # required for sensitive_data_tension / permission_conflict
    evidence_span_a: Optional[str]
    evidence_span_b: Optional[str]
    confidence: float


_EXCEPTION_SHAPED = ("sensitive_data_tension", "permission_conflict")


def _issue(v: TensionVerdict, text_a: str, text_b: str) -> Optional[str]:
    if not (0.0 <= v.confidence <= 1.0):
        return f"confidence {v.confidence} outside [0, 1]"
    if v.verdict in TENSION_VERDICTS:
        if not v.concrete_scenario:
            return f"verdict={v.verdict} but no concrete_scenario was given"
        if v.evidence_span_a is None and v.evidence_span_b is None:
            return f"verdict={v.verdict} but no evidence span was quoted from either side"
    if v.verdict in _EXCEPTION_SHAPED and not v.decisive_exception:
        return f"verdict={v.verdict} but decisive_exception is empty"
    if v.verdict == "missing_context" and not v.missing_reference:
        return "verdict=missing_context but missing_reference is empty (must name the specific missing provision)"
    for span, text, label in ((v.evidence_span_a, text_a, "A"), (v.evidence_span_b, text_b, "B")):
        if span is not None and not c1._verbatim_in(span, text):
            return f"evidence_span_{label.lower()} is not a verbatim substring of norm {label}'s provision text"
    return None


def build_prompt(a, b, def_ctx, ref_ctx, retry_note=None):
    note = (f"\nYour previous response was rejected: {retry_note}. Fix that specific problem.\n"
            if retry_note else "")
    return (
        "Two provisions from Dutch/EU digital-law legislation are shown below. Candidate "
        "generation found them related by topic; it did NOT establish that they are in "
        "tension -- that is your job. Classify how they relate.\n\n"
        "Most pairs are compatible or unrelated: two laws each having their own rules is "
        "normal. Do NOT call something a tension merely because one law is more specific "
        "or detailed than a general one, because the two apply to different actors, "
        "triggers or scopes, because one addresses an authority and the other a company, "
        "or because a rule and its own exception sit in the same law -- this includes a "
        "GENERAL grant/duty in one provision and an OVERRIDE or refusal ground in another "
        "provision of the SAME instrument (e.g. 'the Minister recognises X if conditions "
        "are met' next to 'the Minister refuses if there is a serious security risk'): "
        "that is ordinary legislative structure (a general rule plus its override), not a "
        "tension, even where the override is phrased in mandatory language.\n\n"
        "Two further traps, checked directly against real cases that were wrongly flagged "
        "before this instruction was added:\n"
        "- AVOIDABLE DUTIES: if one side is a VOLUNTARY or optional duty (the actor "
        "controls whether it is ever triggered, e.g. opting into a certification scheme), "
        "an actor who never triggers it faces no conflict at all. Do not construct a "
        "scenario where an actor chooses to invoke an optional mechanism in a way that "
        "then collides with the other provision, and call that a direct_conflict -- the "
        "actor had a fully compliant choice available (not invoking it, or invoking it on "
        "a narrower scope).\n"
        "- CUMULATIVE REQUIREMENTS: two requirements that must BOTH be satisfied, where "
        "satisfying the narrower/easier one does not by itself satisfy the broader/harder "
        "one, is the NORMAL shape of overlapping regulation, not a tension -- a diligent "
        "actor complies with both independently. This applies especially to 'a system-"
        "level or organisational duty was satisfied, but an individual right could still "
        "be violated in a specific instance if the actor fails to actually exercise "
        "available oversight/review' -- that is a compliance-vigilance risk for the "
        "actor, not a tension between the instruments, UNLESS the text itself states or "
        "implies that satisfying the system-level duty is DEEMED to satisfy, override or "
        "substitute for the individual right (quote that language if you rely on it).\n\n"
        "Classes:\n"
        "- direct_conflict: an actor subject to both cannot obey both (must vs. must-not, "
        "incompatible deadlines or content).\n"
        "- standard_benchmark_tension: the SAME underlying duty (e.g. secure a system, "
        "ensure its accuracy/quality) is measured against two DIFFERENT benchmarks across "
        "the two instruments, so it is genuinely unclear which one governs, or whether "
        "meeting one automatically meets the other.\n"
        "- rights_vs_system_duties_tension: one provision gives a natural person an "
        "individual, invokable RIGHT, while the other imposes a SYSTEM-LEVEL design/"
        "documentation/process duty on an operator -- AND the text gives a specific reason "
        "the two are STRUCTURALLY misaligned, not just cumulative: e.g. the system-level "
        "duty is defined at population/statistical level (oversight of aggregate system "
        "performance) while the right is defined at the individual/case level, such that "
        "even a fully diligent, good-faith operator who satisfies the system-level duty AS "
        "DEFINED could never, by that alone, know whether any given individual's right was "
        "respected -- a structural gap, not a possible lapse by a non-diligent operator. Do "
        "NOT use this class for 'the operator had the capability but a specific instance "
        "shows they didn't use it' -- that is a cumulative-requirements case (see above), "
        "not this one.\n"
        "- sensitive_data_tension: both provisions concern processing of special-category / "
        "sensitive personal data (health, biometric, ethnic origin, political opinion, "
        "etc.); one restricts or prohibits it, the other permits or requires it (often for "
        "a narrow purpose like bias detection or oversight) -- and whether the permission's "
        "own legal basis actually satisfies the prohibition's exception is not established "
        "by the text shown. Name the missing link in decisive_exception.\n"
        "- classification_mismatch: the two instruments each define their OWN trigger for "
        "when extra safeguards apply (e.g. a risk-tier classification under one law, an "
        "independent necessity-based assessment trigger under another), using different "
        "criteria, such that the same real-world system or activity could plausibly be "
        "classified in-scope under one regime and out-of-scope under the other.\n"
        "- permission_conflict: one provision allows or requires what the other forbids, "
        "unless an exception applies -- and the exception that decides it is not "
        "established by the text shown -- but the subject matter is NOT sensitive personal "
        "data (use sensitive_data_tension for that case specifically). Name the exception "
        "in decisive_exception.\n"
        "- goal_tension: each can be obeyed, but obeying one works against the evident "
        "purpose of the other.\n"
        "- compatible: both can be satisfied, and the texts do not leave a real open "
        "question (e.g. the stricter rule simply satisfies the looser one).\n"
        "- unrelated: they merely share vocabulary or topic.\n"
        "- missing_context: a specific provision NOT shown here is needed to decide. Name it "
        "in missing_reference. Do not use this as a default expression of doubt.\n\n"
        "Method: FIRST write the strongest honest case that the two are in tension, and the "
        "strongest honest case that they are compatible (1-3 sentences each, grounded in "
        "the texts). THEN choose the verdict the text better supports. A tension verdict "
        "requires a concrete_scenario: ONE specific, realistic fact pattern in which an "
        "actor subject to both would actually face the problem -- not an abstract 'could "
        "differ in some case'. Quote evidence_span_a/b verbatim from the provision texts.\n\n"
        "Invented illustrations (generic, not from any law here):\n"
        "  direct_conflict: A: keep incident logs 10 years. B: delete incident logs after 2 years.\n"
        "  standard_benchmark_tension: A: systems must be 'adequately secured'. B: systems must "
        "hold ISO 27001 certification. Same duty, different benchmark.\n"
        "  rights_vs_system_duties_tension: A: a person has the right to a human review of an "
        "automated decision about them. B: an operator must monitor the AGGREGATE accuracy of "
        "its automated system across all decisions, with no provision for per-decision review. "
        "Even a fully diligent operator satisfying B's population-level design duty has no way "
        "to know, from B alone, whether A's individual guarantee was met for any given person -- "
        "this is a structural mismatch of what is measured, not a case of an operator failing to "
        "use an available per-case review it could have used (that would be compatible: the "
        "operator simply also does the per-case review A requires).\n"
        "  sensitive_data_tension: A: processing health data is prohibited. B: health data may "
        "exceptionally be processed for bias detection, provided conditions elsewhere are met. "
        "Whether A's own exceptions cover B's specific purpose is not established here.\n"
        "  classification_mismatch: A classifies a system as high-risk based on its INTENDED "
        "PURPOSE. B triggers its own extra-safeguards duty based on a separate necessity test. "
        "The same system could be high-risk under A but not trigger B's duty, or vice versa.\n"
        "  permission_conflict: A: location data may be used for fraud prevention. B: location "
        "data may not be processed unless a court authorised it. Whether A's permission can "
        "be used depends on B's court-authorisation exception, which A does not establish.\n"
        "  goal_tension: A: keep user complaints indefinitely for audit. B (purpose: minimise "
        "stored personal data): keep personal data no longer than necessary.\n"
        "  compatible: A: report within 72h. B: report within 24h. The stricter satisfies both."
        + note + "\n\n"
        f"PROVISION A -- {a.instrument_id} art. {a.article} ({a.heading}) [{a.norm['deontic']}]\n"
        f"{a.text}\n{pin_line(a)}\n"
        f"PROVISION B -- {b.instrument_id} art. {b.article} ({b.heading}) [{b.norm['deontic']}]\n"
        f"{b.text}\n{pin_line(b)}"
        + def_ctx + ref_ctx
    )


_SHARED = None


def pin_line(r) -> str:
    """Several norms extracted from one paragraph (e.g. AI Act art. 5(1)'s eight prohibited practices,
    points a-h) all carry that whole paragraph as their text. Shown only the text, the model judges
    whichever norm in it fits best -- checked directly (2026-09-29): norms for 5(1)(a), (d) and (h) came
    back with scenarios about 5(1)(e)/(f). This names the one norm under comparison. Empty for a
    paragraph holding a single norm, so those prompts are unchanged."""
    global _SHARED
    if _SHARED is None:
        _SHARED = {k for k, n in Counter((x.instrument_id, x.article, x.text)
                                          for x in c1.load_all_norm_records()).items() if n > 1}
    if (r.instrument_id, r.article, r.text) not in _SHARED:
        return ""
    return (f"(This paragraph states several separate norms. Judge ONLY this one -- ignore the others: "
            f"deontic={r.norm['deontic']}, action={r.norm.get('action')!r}, "
            f"conditions={r.norm.get('conditions') or []})\n")


def adjudicate(client, model, effort, a, b, defs, pti, rri):
    def_ctx = c1._definitions_context(a, defs, "A") + c1._definitions_context(b, defs, "B")
    ref_ctx = c1._reference_context(a, pti, rri, "A") + c1._reference_context(b, pti, rri, "B")
    issue = None
    v = None
    for _attempt in range(2):
        kwargs = dict(model=model, input=build_prompt(a, b, def_ctx, ref_ctx, issue),
                      text_format=TensionVerdict, reasoning={"effort": effort})
        if effort == "none":
            kwargs["temperature"] = 0  # rejected by this model family once reasoning is on
        for net_try in range(3):
            try:
                resp = client.responses.parse(**kwargs)
                break
            except Exception as e:  # network/rate hiccup -- brief backoff, then re-raise
                if net_try == 2 or "credit" in str(e).lower():
                    raise
                time.sleep(3 * (net_try + 1))
        v = resp.output_parsed
        issue = _issue(v, a.text, b.text)
        if issue is None:
            return v, None
    return v, issue


def _label(a, b):
    return (f"{a.instrument_id} {a.article}({a.norm.get('number')}) <-> "
            f"{b.instrument_id} {b.article}({b.norm.get('number')})")


def build_strata(client, records, cache_c1):
    g = nx.read_gexf(DATA / "graph.gexf")
    import similarity
    sp = similarity.semantic_candidate_pairs(client, [r.text for r in records], k=8,
                                             groups=[r.instrument_id for r in records], k_within=3)
    cands = c1.generate_candidates(records, g, sp)
    model = c1.DEFAULT_MODEL
    pair_map = {}
    for c in cands:
        pair_map[c1._pair_id(c["a"], c["b"])] = (c["a"], c["b"])
    old = {}
    for pid, (a, b) in pair_map.items():
        e = cache_c1.get(f"{model}::{pid}")
        if e:
            old[pid] = e
    rng = random.Random(SEED)

    def has(a, inst, art, num=None):
        return a.instrument_id == inst and a.article == art and (num is None or str(a.norm.get("number")) == num)

    def match(pair, ia, aa, na, ib, ab, nb):
        a, b = pair
        return (has(a, ia, aa, na) and has(b, ib, ab, nb)) or (has(b, ia, aa, na) and has(a, ib, ab, nb))

    anchors = {}
    G, A = "32016R0679", "32024R1689"
    for pid, pair in pair_map.items():
        if pid not in old:
            continue
        tag = None
        if match(pair, G, "9", None, A, "10", "5"):
            tag = "INC-0008"
        elif match(pair, G, "32", None, A, "15", None):
            tag = "INC-0006"
        elif (match(pair, G, "22", "1", A, "14", None) or match(pair, G, "22", "1", A, "26", "2")
              or match(pair, G, "15", "2", A, "14", None)):
            tag = "INC-0007"
        elif match(pair, G, "35", "1", A, "6", "4"):
            tag = "INC-0003"
        if tag:
            anchors.setdefault(tag, []).append(pid)
    strata = defaultdict(list)
    for tag, pids in anchors.items():
        keep = pids if tag in ("INC-0008", "INC-0003") else rng.sample(pids, min(6, len(pids)))
        strata["A_anchor"] += [(pid, tag) for pid in keep]

    ie_true = [p for p, e in old.items() if e["verdict"]["verdict"] == "INSUFFICIENT_EVIDENCE"
               and e["verdict"].get("joint_compliance_possible") is True and not e.get("needs_recheck_reason")]
    ie_none = [p for p, e in old.items() if e["verdict"]["verdict"] == "INSUFFICIENT_EVIDENCE"
               and e["verdict"].get("joint_compliance_possible") is None and not e.get("needs_recheck_reason")]
    strata["B_IE_soft"] = [(p, "") for p in rng.sample(ie_true, min(15, len(ie_true)))]
    strata["C_IE_undecided"] = [(p, "") for p in rng.sample(ie_none, min(15, len(ie_none)))]
    neg = [p for p, e in old.items() if e["verdict"]["verdict"] == "NOT_A_CONFLICT"
           and not e.get("needs_recheck_reason")
           and pair_map[p][0].instrument_id != pair_map[p][1].instrument_id]
    strata["D_neg_control"] = [(p, "") for p in rng.sample(neg, min(40, len(neg)))]
    return strata, pair_map, len(ie_true), len(ie_none), len(neg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from openai import OpenAI
    client = OpenAI()

    records = c1.load_all_norm_records()
    cache_c1 = json.loads((DATA / "cache" / "c1_adjudication_cache.json").read_text(encoding="utf-8"))
    strata, pair_map, n_ie_true, n_ie_none, n_neg = build_strata(client, records, cache_c1)
    total = sum(len(v) for v in strata.values())
    print(f"population: {n_ie_true} soft-IE, {n_ie_none} undecided-IE, {n_neg} cross-instrument confident negatives", flush=True)
    for s, items in strata.items():
        print(f"  {s}: {len(items)} pairs" + (f"  {dict(Counter(t for _, t in items))}" if s == 'A_anchor' else ""), flush=True)
    print(f"total {total} pairs x {len(CONFIGS)} configs = {total * len(CONFIGS)} calls", flush=True)
    if args.dry_run:
        return

    defs = c1.load_definitions_by_instrument()
    pti = c1._build_paragraph_text_index(records)
    rri = c1._build_reverse_reference_index(records)
    cache_path = DATA / "cache" / "c1_tension_pilot_cache.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    lock = threading.Lock()

    jobs = []
    for s, items in strata.items():
        for pid, tag in items:
            for model, effort in CONFIGS:
                jobs.append((s, tag, pid, model, effort))

    def work(job):
        s, tag, pid, model, effort = job
        key = f"{VERSION}::{model}::{effort}::{pid}"
        with lock:
            hit = cache.get(key)
        if hit:
            return job, hit
        a, b = pair_map[pid]
        v, issue = adjudicate(client, model, effort, a, b, defs, pti, rri)
        entry = {"verdict": v.model_dump(), "needs_recheck_reason": issue}
        with lock:
            cache[key] = entry
            cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        return job, entry

    results = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = [ex.submit(work, j) for j in jobs]
        for i, f in enumerate(as_completed(futs), 1):
            try:
                job, entry = f.result()
            except Exception as e:
                print(f"  ERROR: {e}", flush=True)
                if "credit" in str(e).lower():
                    print("  -- out of credits, stopping", flush=True)
                    break
                continue
            results.append((job, entry))
            if i % 20 == 0:
                print(f"  [{i}/{len(jobs)}]", flush=True)

    out = []
    for (s, tag, pid, model, effort), entry in results:
        a, b = pair_map[pid]
        out.append({"stratum": s, "anchor": tag, "pair_id": pid, "label": _label(a, b),
                    "model": model, "effort": effort,
                    "old_verdict": cache_c1[f"{c1.DEFAULT_MODEL}::{pid}"]["verdict"]["verdict"],
                    "new": entry["verdict"], "recheck": entry["needs_recheck_reason"]})
    (DATA / "results" / "c1" / "c1_tension_pilot_results.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n=== verdict distribution by stratum and config ===", flush=True)
    for s in strata:
        for model, effort in CONFIGS:
            rows = [o for o in out if o["stratum"] == s and o["model"] == model]
            dist = Counter(("RECHECK" if o["recheck"] else o["new"]["verdict"]) for o in rows)
            tension = sum(n for k, n in dist.items() if k in TENSION_VERDICTS)
            print(f"{s:16s} {model:14s} n={len(rows):3d} tension={tension:3d}  {dict(dist)}", flush=True)


if __name__ == "__main__":
    main()
