# -*- coding: utf-8 -*-
"""
One-off (2026-09-28): data/real_conflicts (an external, literature-sourced ground-truth
set) documents GDPR art.32/30 vs AI Act art.12 as a human-verified real duplication
(security/logging duty duplication). Validating C1 against that set found these exact
16 norm-pairs sitting as real candidates (addressee matches, a discovery signal fired)
in the 4,454-pair remainder deliberately left unchecked after the earlier cost-based
stopping decision. Small and directly justified -- adjudicated here individually rather
than reopening the whole remainder.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import detect_c1_contradiction as c1

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def main():
    from openai import OpenAI
    client = OpenAI()
    model = c1.DEFAULT_MODEL

    records = c1.load_all_norm_records()
    gdpr32 = [r for r in records if r.instrument_id == "32016R0679" and r.article == "32"]
    gdpr30 = [r for r in records if r.instrument_id == "32016R0679" and r.article == "30"]
    aiact12 = [r for r in records if r.instrument_id == "32024R1689" and r.article == "12"]

    cache_path = DATA / "c1_adjudication_cache.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8"))

    todo = []
    for a in gdpr32 + gdpr30:
        for b in aiact12:
            if a.norm.get("addressee_type") != b.norm.get("addressee_type"):
                continue
            key = f"{model}::{c1._pair_id(a, b)}"
            if key not in cache:
                todo.append((a, b))

    print(f"{len(todo)} pairs to adjudicate", flush=True)
    paragraph_text_index = c1._build_paragraph_text_index(records)
    reverse_reference_index = c1._build_reverse_reference_index(records)

    for a, b in todo:
        label = f"{a.instrument_id} art.{a.article}({a.norm.get('number')}) <-> {b.instrument_id} art.{b.article}({b.norm.get('number')})"
        result = c1.adjudicate_duty_conflict(client, model, a, b, paragraph_text_index, reverse_reference_index)
        key = f"{model}::{c1._pair_id(a, b)}"
        cache[key] = {
            "verdict": result.verdict.model_dump(),
            "needs_recheck_reason": result.needs_recheck_reason,
            "challenge": result.challenge.model_dump() if result.challenge else None,
        }
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        tag = " (needs_recheck)" if result.needs_recheck_reason else ""
        print(f"  {label} -- {result.verdict.verdict}{tag}", flush=True)


if __name__ == "__main__":
    main()
