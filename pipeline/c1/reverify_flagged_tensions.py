# -*- coding: utf-8 -*-
"""
One-off (2026-09-28): re-adjudicates, under v3's strengthened prompt, exactly the 12 pairs
whose v2 verdict (3 direct_conflict, 9 rights_vs_system_duties_tension) did not survive a
careful legal re-read (see pilot_c1_graded_tension.py's VERSION comment for the two failure
modes found). Targets are read directly from the CURRENT findings_c1.json before this run
replaces them, so this is reproducible even after the file changes. ~12 terra calls.
"""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
_HERE = Path(__file__).resolve().parent
_PIPELINE = _HERE.parent
for _d in (_PIPELINE, _PIPELINE / "c1", _PIPELINE / "preprocessing", _PIPELINE / "c2"):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))
import detect_c1_contradiction as c1
import detect_c1_legal_tension as lt
from pilot_c1_graded_tension import adjudicate
from detect_c1_legal_tension import TERRA
from dotenv import load_dotenv


def prov_nid(p):
    return f"{p['instrument_id']}:{p['article']}:{p.get('paragraph_index')}:{p.get('paragraph_number')}:{p.get('norm_index')}"


def main():
    d = json.loads((c1.DATA / "results" / "c1" / "findings_c1.json").read_text(encoding="utf-8"))["findings"]
    targets = [f for f in d if (f.get("llm_adjudication") or {}).get("verdict")
               in ("direct_conflict", "rights_vs_system_duties_tension")]
    print(f"{len(targets)} target findings to re-verify")

    load_dotenv(c1.ROOT / ".env")
    from openai import OpenAI
    client = OpenAI()
    records = c1.load_all_norm_records()
    by_nid = {}
    for r in records:
        by_nid.setdefault(c1._norm_id(r), r)
    defs = c1.load_definitions_by_instrument()
    pti = c1._build_paragraph_text_index(records)
    rri = c1._build_reverse_reference_index(records)

    results = []
    for f in targets:
        a = by_nid.get(prov_nid(f["provisions"][0]))
        b = by_nid.get(prov_nid(f["provisions"][1]))
        old_verdict = f["llm_adjudication"]["verdict"]
        if a is None or b is None:
            print(f"{f['finding_id']} ({old_verdict}): norm no longer resolvable, skipping")
            continue
        v, issue = adjudicate(client, TERRA[0], TERRA[1], a, b, defs, pti, rri)
        results.append({"finding_id": f["finding_id"], "old": old_verdict, "new": v.verdict,
                        "confidence": v.confidence, "issue": issue,
                        "pair": f"{a.instrument_id} {a.article}({a.norm.get('number')}) <-> {b.instrument_id} {b.article}({b.norm.get('number')})"})
        changed = "CHANGED" if v.verdict != old_verdict else "SAME"
        print(f"[{changed}] {f['finding_id']}: {old_verdict} -> {v.verdict} (conf {v.confidence}) issue={issue}")
        print(f"    {results[-1]['pair']}")
        if v.verdict == old_verdict:
            print(f"    STILL FLAGGED -- scenario: {v.concrete_scenario}")

    (c1.DATA / "results" / "c1" / "reverify_flagged_tensions_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    survived = [r for r in results if r["new"] == r["old"]]
    print(f"\n{len(survived)} of {len(results)} survived re-verification under the strengthened prompt unchanged")


if __name__ == "__main__":
    main()
