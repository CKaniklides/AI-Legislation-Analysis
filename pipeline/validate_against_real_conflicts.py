# -*- coding: utf-8 -*-
"""
One-off validation (2026-09-28): data/real_conflicts is a hand-curated ground-truth set
of 10 real, literature-documented GDPR/AI-Act/NIS2 tensions (from an EU Parliament
research study, not from this project), split into "contradiction" (5 entries -- C1's
job) and "overlap" (4 entries, plus one duplicate id -- C2's job) types. This checks,
for every one of them, exactly where this project's own pipeline currently stands:
never extracted, extracted but never a candidate (and which signal(s) would have had to
fire), a candidate that was never adjudicated (cost-driven stop), or adjudicated with a
specific verdict. This is the actual recall check that "zero contradictions found"
needed against a source the project didn't build itself.
"""
import json
import sys
from collections import defaultdict
from itertools import product
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import detect_c1_contradiction as c1
import detect_c2_deduplication as c2
import networkx as nx

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

DOC_TO_INSTRUMENT = {"GDPR": "32016R0679", "AI Act": "32024R1689", "NIS2": "32022L2555"}


import re

_LEADING_ARTICLE_NUM_RE = re.compile(r"^\s*(\d+)")


def parse_articles(article_field: str) -> list[str]:
    if not article_field:
        return []
    parts = []
    for chunk in article_field.split(","):
        m = _LEADING_ARTICLE_NUM_RE.match(chunk)
        if m:
            parts.append(m.group(1))
    return parts


def main():
    out = []
    def p(*a):
        out.append(" ".join(str(x) for x in a))

    entries = json.loads((DATA / "real_conflicts").read_text(encoding="utf-8"))["inconsistencies"]

    records = c1.load_all_norm_records()
    g = nx.read_gexf(DATA / "graph.gexf")
    by_key = defaultdict(list)
    for r in records:
        by_key[(r.instrument_id, r.article)].append(r)

    from openai import OpenAI
    client = OpenAI()
    import similarity
    c1_semantic_pairs = similarity.semantic_candidate_pairs(
        client, [r.text for r in records], k=8,
        groups=[r.instrument_id for r in records], k_within=3)
    idx_of = {id(r): i for i, r in enumerate(records)}

    c1_cache = json.loads((DATA / "c1_adjudication_cache.json").read_text(encoding="utf-8"))
    c1_model = c1.DEFAULT_MODEL

    c2_cache = json.loads((DATA / "c2_adjudication_cache.json").read_text(encoding="utf-8"))
    # Build C2's own candidate-eligible population + embedding index once, mirroring
    # generate_pairs()'s own logic, so per-pair signal checks below match production.
    c2_all = [r for r in records if c2._c2_eligible(r)]
    c2_obligations = [r for r in c2_all
                      if len(c1._content_words(c2._gate_text(r))) >= c2.MIN_TRIGGER_CONTENT_WORDS]
    c2_texts = [r.norm.get("trigger_event") or r.norm.get("action") or "" for r in c2_obligations]
    c2_idx_of = {id(r): i for i, r in enumerate(c2_obligations)}
    c2_knn_pairs = similarity.semantic_candidate_pairs(
        client, c2_texts, k=6, groups=[r.instrument_id for r in c2_obligations], k_within=3)
    c2_scored_pairs = c2._cosine_scores(client, c2_texts, c2_knn_pairs)
    c2_semantic_hit_idx = {(i, j) for i, j, score in c2_scored_pairs if score >= c2.CLUSTER_SIMILARITY_THRESHOLD}

    findings_c1_data = json.loads((DATA / "findings_c1.json").read_text(encoding="utf-8"))
    def_findings = [f for f in findings_c1_data["findings"] if f["subtype"] == "definitional_mismatch"]

    for entry_i, e in enumerate(entries):
        p("=" * 90)
        p(f"{e['id']} (#{entry_i}) [{e['conflict_type']}] {e['document_a']} art.{e['reference_a']['article']}"
          f" <-> {e['document_b']} art.{e['reference_b']['article']}  (source confidence {e['confidence']},"
          f" human_check={e.get('human_check', e.get('human,_check'))})")
        inst_a = DOC_TO_INSTRUMENT[e["document_a"]]
        inst_b = DOC_TO_INSTRUMENT[e["document_b"]]
        def_hits = [f for f in def_findings if f["status"] == "candidate"
                    and {f["provisions"][0]["instrument_id"], f["provisions"][1]["instrument_id"]} == {inst_a, inst_b}]
        if def_hits:
            p(f"  [definitional_mismatch: {len(def_hits)} genuine finding(s) exist between these two "
              f"instruments -- not article-specific, see docs/c1_findings_report.md Part 2]")
        arts_a = parse_articles(e["reference_a"]["article"])
        arts_b = parse_articles(e["reference_b"]["article"])

        for art_a, art_b in product(arts_a, arts_b):
            recs_a = by_key.get((inst_a, art_a), [])
            recs_b = by_key.get((inst_b, art_b), [])
            tag = f"  {inst_a} art.{art_a} <-> {inst_b} art.{art_b}"
            if not recs_a or not recs_b:
                missing = []
                if not recs_a:
                    missing.append(f"{inst_a} art.{art_a}")
                if not recs_b:
                    missing.append(f"{inst_b} art.{art_b}")
                p(tag, "-- NOT EXTRACTED:", ", ".join(missing))
                continue

            best = None  # best (most informative) status found across all norm sub-pairs
            details = []
            for ra, rb in product(recs_a, recs_b):
                # same_addr now uses effective addressee sets (2026-09-28 fix): a
                # MEMBER_STATE-addressed NIS2 Directive norm that's effectively
                # entity-facing ("Member States shall ensure that entities do X") is
                # allowed to match REGULATED_ENTITY too -- see
                # c1._effective_addressee_types's own note.
                same_addr = bool(c1._effective_addressee_types(ra) & c1._effective_addressee_types(rb))
                # --- C1 candidate diagnostics ---
                ia, ib = idx_of.get(id(ra)), idx_of.get(id(rb))
                graph_hit = c1._graph_connected(g, ra.graph_uid, rb.graph_uid)
                trigger_hit = c1._trigger_keyword_hit(ra, rb)
                concept_hit = c1._concept_pair_hit(ra, rb)
                risk_classification_hit = c1._risk_classification_hit(ra, rb)
                automation_oversight_hit = c1._automated_decision_oversight_hit(ra, rb)
                semantic_hit = ia is not None and ib is not None and (
                    (min(ia, ib), max(ia, ib)) in c1_semantic_pairs)
                c1_would_be_candidate = same_addr and (graph_hit or trigger_hit or semantic_hit or concept_hit
                                                         or risk_classification_hit or automation_oversight_hit)
                key = f"{c1_model}::{c1._pair_id(ra, rb)}"
                c1_verdict = None
                if key in c1_cache:
                    entry_cache = c1_cache[key]
                    c1_verdict = entry_cache["verdict"]["verdict"]
                    if entry_cache.get("needs_recheck_reason"):
                        c1_verdict += " (needs_recheck)"

                # --- C2 candidate diagnostics ---
                c2_elig_a, c2_elig_b = c2._c2_eligible(ra), c2._c2_eligible(rb)
                c2_verdict = None
                c2_would_be_candidate = None
                if c2_elig_a and c2_elig_b:
                    ja, jb = c2_idx_of.get(id(ra)), c2_idx_of.get(id(rb))
                    c2_semantic = (ja is not None and jb is not None and ra.instrument_id != rb.instrument_id
                                   and (min(ja, jb), max(ja, jb)) in c2_semantic_hit_idx)
                    c2_trigger = ra.instrument_id != rb.instrument_id and c1._trigger_keyword_hit(ra, rb)
                    c2_penalty = ra.instrument_id != rb.instrument_id and c2._penalty_keyword_hit(ra, rb)
                    c2_would_be_candidate = same_addr and (c2_trigger or c2_penalty or c2_semantic)
                    c2key = f"{c2.C2_ADJUDICATION_VERSION}::{c1_model}::{c1._pair_id(ra, rb)}"
                    if c2key in c2_cache:
                        c2_verdict = c2_cache[c2key]["verdict"]["duplicate_burden_verdict"]
                else:
                    c2_would_be_candidate = False

                details.append((ra, rb, same_addr, c1_would_be_candidate, c1_verdict,
                                 c2_elig_a, c2_elig_b, c2_would_be_candidate, c2_verdict))

            # Summarize: did ANY norm sub-pair get adjudicated? what's the best verdict?
            any_c1_verdict = [d for d in details if d[4] is not None]
            any_c1_candidate = [d for d in details if d[3]]
            any_c2_eligible = [d for d in details if d[5] and d[6]]
            p(tag)
            p(f"    norm sub-pairs checked: {len(details)} ({len(recs_a)} x {len(recs_b)})")
            if any_c1_verdict:
                verdicts = sorted({d[4] for d in any_c1_verdict})
                p(f"    C1: ADJUDICATED -- verdict(s) seen: {verdicts}")
            elif any_c1_candidate:
                p(f"    C1: candidate signal fired ({len(any_c1_candidate)} sub-pair(s)) but NEVER ADJUDICATED (cost-stop)")
            else:
                n_mismatch = sum(1 for d in details if not d[2])
                n_matched_addr = len(details) - n_mismatch
                if n_matched_addr == 0:
                    p(f"    C1: NEVER A CANDIDATE -- all {len(details)} sub-pair(s) had addressee_type mismatch")
                elif n_mismatch == 0:
                    p(f"    C1: NEVER A CANDIDATE -- addressee matched on all {len(details)} sub-pair(s), "
                      f"but no discovery signal fired (graph/trigger/semantic/concept)")
                else:
                    p(f"    C1: NEVER A CANDIDATE -- {n_matched_addr}/{len(details)} sub-pair(s) had matching "
                      f"addressee but no signal fired; {n_mismatch} had addressee_type mismatch")
            if any_c2_eligible:
                c2_verdicts = sorted({d[8] for d in any_c2_eligible if d[8] is not None})
                c2_candidates = [d for d in any_c2_eligible if d[7]]
                if c2_verdicts:
                    p(f"    C2: ADJUDICATED -- verdict(s) seen: {c2_verdicts}")
                elif c2_candidates:
                    p(f"    C2: candidate signal fired ({len(c2_candidates)} sub-pair(s)) but NEVER ADJUDICATED")
                else:
                    p(f"    C2: {len(any_c2_eligible)} sub-pair(s) in-scope, but no discovery signal fired")
            else:
                addr_set_a = {d[0].norm.get('addressee_type') for d in details}
                addr_set_b = {d[1].norm.get('addressee_type') for d in details}
                p(f"    C2: OUT OF SCOPE -- addressee_type(s) seen: A={addr_set_a} B={addr_set_b}"
                  f" (C2 only considers REGULATED_ENTITY)")

    (DATA / "real_conflicts_validation.txt").write_text("\n".join(out), encoding="utf-8")
    print("\n".join(out))


if __name__ == "__main__":
    main()
