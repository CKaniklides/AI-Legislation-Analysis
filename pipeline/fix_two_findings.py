# -*- coding: utf-8 -*-
"""
One-off (2026-09-24): after fixing check_deference_suppresses() (paragraph-level
"eerste lid" references) and route_subtype()'s competence_competition (addressee must
actually differ), two specific findings in data/findings_c1.json are stale -- computed
under the OLD, now-fixed logic. Rather than re-running the entire ~15,000-pair batch
(almost all of which is unaffected and already cached) just to refresh two records,
this recomputes exactly those two pairs with the current code and writes the correct
result into the existing findings file.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import detect_c1_contradiction as c1

DATA = Path(__file__).resolve().parent.parent / "data"
MODEL = c1.DEFAULT_MODEL


def main():
    from openai import OpenAI
    client = OpenAI()

    records = c1.load_all_norm_records()
    exceptions = c1.load_conditional_exceptions()

    cbw27 = [r for r in records if r.instrument_id == "BWBR0052872" and r.article == "27"]
    cbw77 = [r for r in records if r.instrument_id == "BWBR0052872" and r.article == "77"][0]
    cbw78 = [r for r in records if r.instrument_id == "BWBR0052872" and r.article == "78"
             and r.norm["deontic"] == "COMPETENCE"][0]

    pairs = [("Cbw art.27 vs art.27 (standard_collision, now expected: suppressed)", cbw27[0], cbw27[1]),
             ("Cbw art.77 vs art.78 (competence_competition, now expected: duty_conflict)", cbw77, cbw78)]

    findings_path = DATA / "findings_c1.json"
    data = json.loads(findings_path.read_text(encoding="utf-8"))
    stale_ids = {"F-C1-b379640a229c", "F-C1-d0e33535a301"}
    data["findings"] = [f for f in data["findings"] if f["finding_id"] not in stale_ids]

    cache_path = DATA / "c1_adjudication_cache.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}

    for label, a, b in pairs:
        print(f"\n=== {label} ===")
        c = {"a": a, "b": b, "graph_hit": True, "trigger_hit": False,
             "semantic_hit": False, "concept_hit": False}
        p = c1._prepare_candidate(c, exceptions)
        print("subtype:", p["subtype"], "| suppressing_text:", p["suppressing_text"])

        if p["suppressing_text"]:
            tier, reasons = c1.compute_confidence_tier(a, b, p["subtype"], p["rk"], p["det_result"], None)
            finding = c1.build_finding(a, b, c, p["subtype"], p["det_result"], None, tier, reasons,
                                        None, p["unresolved_exception"])
            finding["status"] = "managed"
            finding["resolution_filter"] = {"checked": True, "deference_found": p["suppressing_text"],
                                             "result": "suppressed"}
            data["suppressed"].append(finding)
            print("-> suppressed, added to suppressed[]")
            continue

        if p["subtype"] in ("duty_conflict", "deontic_polarity_conflict"):
            key = f"{MODEL}::{c1._pair_id(a, b)}"
            if key in cache:
                entry = cache[key]
                defaults = {"compliant_alternative": None, "outcome_changing_facts": None}
                verdict = c1.DutyConflictVerdict(**{**defaults, **entry["verdict"]})
                challenge = c1.ChallengeVerdict(**entry["challenge"]) if entry.get("challenge") else None
                result = c1.AdjudicationResult(verdict, entry["needs_recheck_reason"], challenge)
                print("-> found in cache")
            else:
                result = c1.adjudicate_duty_conflict(client, MODEL, a, b)
                cache[key] = {"verdict": result.verdict.model_dump(),
                              "needs_recheck_reason": result.needs_recheck_reason,
                              "challenge": result.challenge.model_dump() if result.challenge else None}
                cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
                print("-> new live adjudication:", result.verdict.verdict)

            if result.verdict.verdict == "NOT_A_CONFLICT" and not result.needs_recheck_reason:
                print("-> NOT_A_CONFLICT, no finding added")
                continue

            tier, reasons = c1.compute_confidence_tier(a, b, p["subtype"], p["rk"], p["det_result"],
                                                         result.verdict, result.needs_recheck_reason,
                                                         result.challenge)
            finding = c1.build_finding(a, b, c, p["subtype"], p["det_result"], result.verdict, tier,
                                        reasons, result.needs_recheck_reason, p["unresolved_exception"],
                                        result.challenge)
            data["findings"].append(finding)
            print(f"-> finding added: {finding['confidence_label']}, verdict={result.verdict.verdict}")
        else:
            tier, reasons = c1.compute_confidence_tier(a, b, p["subtype"], p["rk"], p["det_result"], None)
            finding = c1.build_finding(a, b, c, p["subtype"], p["det_result"], None, tier, reasons,
                                        None, p["unresolved_exception"])
            data["findings"].append(finding)
            print(f"-> deterministic finding added: {finding['confidence_label']}")

    findings_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nwrote back -> {findings_path}")


if __name__ == "__main__":
    main()
