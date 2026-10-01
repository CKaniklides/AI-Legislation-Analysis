# -*- coding: utf-8 -*-
"""
Targeted validation (2026-09-28) of the new 7-class tension taxonomy (standard_benchmark_tension,
rights_vs_system_duties_tension, sensitive_data_tension, classification_mismatch, plus the
permission_conflict/direct_conflict/goal_tension residuals) against the SPECIFIC known cases that
motivated it, before deciding whether a full re-run over the S1+S3 selection set is worth it.
Runs TERRA directly (reasoning=medium), bypassing the luna screen, since these are already known
tension-relevant pairs -- not a discovery run. ~14 calls.
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

TARGETS = [
    ("INC-0003 DPIA-vs-classification", "32016R0679", "35", "32024R1689", "6"),
    ("INC-0006 security-standard", "32016R0679", "32", "32024R1689", "15"),
    ("INC-0007a transparency/rights", "32016R0679", "13", "32024R1689", "26"),
    ("INC-0007b access-rights", "32016R0679", "15", "32024R1689", "26"),
    ("INC-0007c automated-decision-right", "32016R0679", "22", "32024R1689", "26"),
    ("INC-0008 sensitive-data (existing finding)", "32016R0679", "9", "32024R1689", "10"),
    ("existing Cbw64/GDPR9", "BWBR0052872", "64", "32016R0679", "9"),
    ("existing Cbw64/UAVG22", "BWBR0052872", "64", "BWBR0040940", "22"),
    ("existing Telecom11.13/AIAct10", "BWBR0009950", "11.13", "32024R1689", "10"),
    ("existing Wdo19/Telecom11.13", "BWBR0048156", "19", "BWBR0009950", "11.13"),
    ("existing Wdo6/7 (expect permission_conflict residual)", "BWBR0048156", "6", "BWBR0048156", "7"),
]


def main():
    load_dotenv(c1.ROOT / ".env")
    from openai import OpenAI
    client = OpenAI()
    records = c1.load_all_norm_records()
    pair_map = lt.build_pair_map(client, records)
    defs = c1.load_definitions_by_instrument()
    pti = c1._build_paragraph_text_index(records)
    rri = c1._build_reverse_reference_index(records)
    by_inst_art = {}
    for a, b in pair_map.values():
        for r in (a, b):
            by_inst_art.setdefault((r.instrument_id, r.article), []).append(r)

    for label, ia, arta, ib, artb in TARGETS:
        cands = [(a, b) for a, b in pair_map.values()
                 if {(a.instrument_id, a.article), (b.instrument_id, b.article)} == {(ia, arta), (ib, artb)}]
        if not cands:
            print(f"{label}: NO CANDIDATE PAIR FOUND for {ia} {arta} <-> {ib} {artb}")
            continue
        a, b = cands[0]
        v, issue = adjudicate(client, TERRA[0], TERRA[1], a, b, defs, pti, rri)
        print(f"{label}")
        print(f"   {a.instrument_id} art.{a.article}({a.norm.get('number')}) <-> {b.instrument_id} art.{b.article}({b.norm.get('number')})")
        print(f"   verdict={v.verdict}  confidence={v.confidence}  issue={issue}")
        if v.verdict not in ("compatible", "unrelated", "missing_context"):
            print(f"   scenario: {v.concrete_scenario}")
        print()


if __name__ == "__main__":
    main()
