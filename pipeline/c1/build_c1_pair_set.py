# -*- coding: utf-8 -*-
"""
Builds the set of norm pairs C1 judges on the second-generation norms (23 Oct 2026):
the candidate filter's selection PLUS the pairs C1's own targeted signals find.

    NORMS_GENERATION=2 python pipeline/c1/build_c1_pair_set.py            # all pairs
    NORMS_GENERATION=2 python pipeline/c1/build_c1_pair_set.py --pilot    # the pilot subset too

Why both. On the second-generation norms C1's own generator produced 183,371 AI-judged pairs,
far beyond any budget, so the candidate filter (pipeline/candidate_filter) selects a fixed
budget instead. Checked against ten known article pairs, the filter alone reached only two:
it scores only pairs whose OBJECTS share a key, and duties described in different words never
get a score at all (GDPR 33(1) "meldt deze", Cbw 27 "dient in een melding"). C1's targeted
signals were each added for such cases and reach seven of the eight the filter missed, at
6,087 cross-law pairs. Two broad signals are left out: similar text and a citation link
(together the bulk of the 183,371), and the standards keyword family, which matched 37,240
pairs on the new norms. The concept-pair families are left out as well (6,871 more pairs for
no additional known pair).

Output: data/conflict_all/c1_pairs.jsonl, one row per pair in the candidate filter's queue
format (norm_a, norm_b as filter norm ids), with `selection` saying where it came from:
top / tie_extension / calibration (the filter) or c1_signal (C1), and `c1_signals`.
--pilot also writes c1_pairs_pilot.jsonl: every dual-reporting pair, the 60 calibration pairs
and 100 of the filter's top pairs chosen by pair-id hash.
"""
import argparse
import hashlib
import json
import os
import sys
from itertools import combinations
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
_HERE = Path(__file__).resolve().parent
for _d in (_HERE, _HERE.parent, _HERE.parent / "preprocessing"):
    sys.path.insert(0, str(_d))
import networkx as nx  # noqa: E402
import detect_c1_contradiction as c1  # noqa: E402

OUT = c1.DATA / "conflict_all"
TARGETED = {"trigger_hit": c1._trigger_keyword_hit, "dual_reporting_hit": c1._dual_reporting_hit,
            "automation_oversight_hit": c1._automated_decision_oversight_hit,
            "risk_classification_hit": c1._risk_classification_hit}
_STRONG_FOR_CROSS_ROLE = ("automation_oversight_hit", "risk_classification_hit")


def pair_id(x: str, y: str) -> str:
    a, b = sorted((x, y))
    return hashlib.sha256(f"{a}::{b}".encode("utf-8")).hexdigest()[:16]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", type=Path, default=OUT / "review_queue.jsonl")
    ap.add_argument("--pilot", action="store_true")
    args = ap.parse_args()
    if c1.NORMS_GENERATION != 2:
        sys.exit("Run with NORMS_GENERATION=2: this pair set is for the second-generation norms.")

    rows = {}
    for line in args.queue.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        rows[pair_id(r["norm_a"], r["norm_b"])] = {**r, "c1_signals": []}
    n_filter = len(rows)

    recs = c1.load_all_norm_records()
    n_signal = 0
    for a, b in combinations(recs, 2):
        if a.instrument_id == b.instrument_id:
            continue
        rel = c1._compute_actor_relation(a, b)
        if rel == "different_unrelated_actors":
            continue
        hits = [k for k, f in TARGETED.items() if f(a, b)]
        # Same rule as generate_candidates(): a pair between different roles (entity x authority,
        # data subject x entity) needs a substantive signal, not a keyword alone.
        if rel == "different_actors_same_process" and not any(k in _STRONG_FOR_CROSS_ROLE for k in hits):
            hits = []
        if not hits:
            continue
        ia, ib = a.norm["filter_norm_id"], b.norm["filter_norm_id"]
        pid = pair_id(ia, ib)
        if pid in rows:
            rows[pid]["c1_signals"] = hits
            continue
        na, nb = sorted((ia, ib))
        rows[pid] = {"pair_id": pid, "norm_a": na, "norm_b": nb, "selection": "c1_signal", "c1_signals": hits}
        n_signal += 1

    # Deterministic checks, within one law too (23 Oct 2026): C1's own generator (graph, keywords,
    # concept pairs; no embeddings) run locally, keeping only the pairs routed to a computed
    # sub-type (different deadlines, thresholds, the same power for two bodies). They need no
    # model call, and the cross-law-only signals above miss e.g. Cbw art. 26 x art. 27.
    g = nx.read_gexf(c1.DATA / "graph.gexf")
    n_det = 0
    for c in c1.generate_candidates(recs, g, None):
        a, b = c["a"], c["b"]
        subtype, _ = c1.route_subtype(a, b, c1.recipient_match_kind(a, b))
        if subtype in (None, "duty_conflict", "deontic_polarity_conflict"):
            continue
        pid = pair_id(a.norm["filter_norm_id"], b.norm["filter_norm_id"])
        if pid not in rows:
            na, nb = sorted((a.norm["filter_norm_id"], b.norm["filter_norm_id"]))
            rows[pid] = {"pair_id": pid, "norm_a": na, "norm_b": nb, "selection": "c1_deterministic",
                         "c1_signals": [subtype]}
            n_det += 1
    print(f"{n_det} pairs added for C1's deterministic checks (no model calls)")

    # (23 Oct 2026) Adding the previous run's article pairs was tried and reverted on the user's
    # decision: the pair set holds only what this run's own selection finds.

    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / "c1_pairs.jsonl"
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows.values()), encoding="utf-8")
    both = sum(1 for r in rows.values() if r["selection"] != "c1_signal" and r["c1_signals"])
    print(f"{len(rows)} pairs -> {out}: {n_filter} from the candidate filter ({both} also found by a "
          f"C1 signal), {n_signal} only from C1's targeted signals")

    if args.pilot:
        pilot = [r for r in rows.values() if "dual_reporting_hit" in r["c1_signals"] or r["selection"] == "calibration"]
        tops = sorted((r for r in rows.values() if r["selection"] == "top"), key=lambda r: r["pair_id"])[:100]
        pilot += [r for r in tops if r not in pilot]
        p = OUT / "c1_pairs_pilot.jsonl"
        p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in pilot), encoding="utf-8")
        print(f"{len(pilot)} pilot pairs -> {p}")


if __name__ == "__main__":
    main()
