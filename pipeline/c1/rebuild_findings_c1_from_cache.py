# -*- coding: utf-8 -*-
"""
WARNING (2026-09-28): do not run this on its own. It rewrites the WHOLE of findings_c1.json
from the strict duty-conflict cache and knows nothing about the findings other detectors append
(definitional_mismatch, legal_tension, missing_context) -- running it alone once silently
dropped every definitional finding. Use rebuild_c1_all.py, which runs this and then re-appends
the others in the right order.

One-off (2026-09-24): rebuilds data/findings_c1.json from the CURRENT adjudication
cache and CURRENT code, with no new LLM calls. Needed after adding paragraph_index to
build_finding()'s provisions dict (alongside norm_index, per an external review) --
the cache itself didn't change, only what build_finding() writes out, so this just
re-runs the deterministic + cache-backed finding-construction logic (the same as
migrate_norm_identity_fix.py's tail section) without repeating that script's cache
migration (which must only ever run once against the pre-fix cache).
"""
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PIPELINE = _HERE.parent
for _d in (_PIPELINE, _PIPELINE / "c1", _PIPELINE / "preprocessing", _PIPELINE / "c2"):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))
import detect_c1_contradiction as c1
import networkx as nx

ROOT = Path(__file__).resolve().parent.parent.parent
DATA = ROOT / "data"


def main():
    from openai import OpenAI
    client = OpenAI()
    model = c1.DEFAULT_MODEL

    print("Regenerating candidates (same defaults as detect_c1_contradiction.main())...", flush=True)
    records = c1.load_all_norm_records()
    g = nx.read_gexf(DATA / "graph.gexf")
    import similarity
    semantic_pairs = similarity.semantic_candidate_pairs(
        client, [r.text for r in records], k=8,
        groups=[r.instrument_id for r in records], k_within=3)
    exceptions = c1.load_conditional_exceptions()
    candidates = c1.generate_candidates(records, g, semantic_pairs)
    prepared = [p for p in (c1._prepare_candidate(c, exceptions) for c in candidates)
                if p["subtype"] is not None]
    AI_JUDGED = ("duty_conflict", "deontic_polarity_conflict")
    needs_llm = [p for p in prepared if p["subtype"] in AI_JUDGED]
    no_llm = [p for p in prepared if p["subtype"] not in AI_JUDGED]
    print(f"  {len(needs_llm)} AI-judged candidates, {len(no_llm)} deterministic", flush=True)

    cache = json.loads((DATA / "cache" / "c1_adjudication_cache.json").read_text(encoding="utf-8"))

    findings, suppressed = [], []

    def finalize(p, llm_verdict=None, needs_recheck_reason=None, challenge=None):
        tier, tier_reasons = c1.compute_confidence_tier(p["a"], p["b"], p["subtype"], p["rk"],
                                                          p.get("det_result"), llm_verdict,
                                                          needs_recheck_reason, challenge)
        finding = c1.build_finding(p["a"], p["b"], p["c"], p["subtype"], p.get("det_result"),
                                    llm_verdict, tier, tier_reasons, needs_recheck_reason,
                                    p.get("unresolved_exception"), challenge)
        if p["suppressing_text"]:
            finding["status"] = "managed"
            finding["resolution_filter"] = {"checked": True, "deference_found": p["suppressing_text"],
                                             "result": "suppressed"}
            suppressed.append(finding)
        else:
            findings.append(finding)

    for p in no_llm:
        finalize(p)

    _VERDICT_FIELD_DEFAULTS = {"compliant_alternative": None, "outcome_changing_facts": None}
    n_from_cache = 0
    for p in needs_llm:
        key = f"{model}::{c1._pair_id(p['a'], p['b'])}"
        if key not in cache:
            continue
        entry = cache[key]
        challenge = c1.ChallengeVerdict(**entry["challenge"]) if entry.get("challenge") else None
        verdict = c1.DutyConflictVerdict(**{**_VERDICT_FIELD_DEFAULTS, **entry["verdict"]})
        needs_recheck_reason = entry["needs_recheck_reason"]
        n_from_cache += 1
        if needs_recheck_reason:
            finalize(p, verdict, needs_recheck_reason)
        elif verdict.verdict == "NOT_A_CONFLICT":
            continue
        else:
            finalize(p, verdict, None, challenge)

    # Graded second pass (2026-09-28, detect_c1_legal_tension.py): a strict INSUFFICIENT_EVIDENCE/
    # needs_recheck finding whose pair the graded cascade resolved to COMPATIBLE/UNRELATED is not a
    # finding any more -- removed here, ids kept under "resolved_by_graded_pass" so it stays auditable.
    import detect_c1_legal_tension as lt
    superseded_ids = lt.superseded_finding_ids(lt.final_verdicts())
    resolved = [f["finding_id"] for f in findings if f["finding_id"] in superseded_ids]
    findings = [f for f in findings if f["finding_id"] not in superseded_ids]

    out_path = DATA / "results" / "c1" / "findings_c1.json"
    out_path.write_text(json.dumps({"findings": findings, "suppressed": suppressed,
                                    "resolved_by_graded_pass": resolved},
                                    ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  {n_from_cache} cache-backed pair(s) processed -> {len(findings)} finding(s), "
          f"{len(suppressed)} suppressed -> {out_path.relative_to(ROOT)}", flush=True)


if __name__ == "__main__":
    main()
