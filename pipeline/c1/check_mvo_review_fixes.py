"""Verification suite for the four MvO-review fixes (2026-10-02).

Run:  python pipeline/c1/check_mvo_review_fixes.py        (exits non-zero on failure)


Covers: S7 dual-reporting stratum, the amendment registry / currency_warning,
label_sheet reference inlining, and regression on everything that was already working.

Synthetic cases are built by deep-copying REAL norm records and mutating one field, never
by hand-constructing a mock -- an earlier check script in this project silently passed
while testing nothing because its mock used `addressee_types` (plural) where the code
reads `addressee_type` (singular).
"""
import copy
import json
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PIPELINE = _HERE.parent
for _d in (_PIPELINE, _PIPELINE / "c1", _PIPELINE / "preprocessing", _PIPELINE / "c2"):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))
DATA = _PIPELINE.parent / "data"

import detect_c1_contradiction as c1
import detect_c1_legal_tension as lt
import label_sheet

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail and not cond else ""))


records = c1.load_all_norm_records()
by = {}
for r in records:
    by.setdefault((r.instrument_id, str(r.article), str(r.norm.get("number"))), []).append(r)

GDPR, AIACT, NIS2NL = "32016R0679", "32024R1689", "BWBR0052872"
gdpr33_1 = by[(GDPR, "33", "1")][0]
gdpr33_5 = by[(GDPR, "33", "5")][0]
gdpr34_3 = by[(GDPR, "34", "3")]
ai73_1 = by[(AIACT, "73", "1")][0]
ai73_4 = by[(AIACT, "73", "4")][0]
ai10_5 = by[(AIACT, "10", "5")][0]

# ============================================================ 1. S7 signal
print("\n=== S7: the three reviewer-labelled controls ===")
check("C-1 S-023 GDPR33(1)xAI73(1) fires (reviewer: LEGAL_TENSION)",
      c1._dual_reporting_hit(gdpr33_1, ai73_1))
check("C-2 S-010 GDPR33(5)xAI73(4) fires (reviewer: LEGAL_TENSION)",
      c1._dual_reporting_hit(gdpr33_5, ai73_4))
check("C-3 S-032 GDPR34(3)xAI73(1) silent, ALL variants (reviewer: COMPATIBLE)",
      not any(c1._dual_reporting_hit(x, ai73_1) for x in gdpr34_3),
      f"{sum(c1._dual_reporting_hit(x, ai73_1) for x in gdpr34_3)} variant(s) fired")
check("S7 is symmetric (a,b) == (b,a)",
      c1._dual_reporting_hit(ai73_1, gdpr33_1) == c1._dual_reporting_hit(gdpr33_1, ai73_1))

print("\n=== S7: each gate independently load-bearing ===")
# deontic gate
flipped = copy.deepcopy(gdpr33_1)
flipped.norm["deontic"] = "PROHIBITION"
check("deontic gate: flipping one side to PROHIBITION stops it",
      not c1._dual_reporting_hit(flipped, ai73_1))

# same-instrument gate
same_inst = copy.deepcopy(ai73_4)
same_inst.instrument_id = gdpr33_1.instrument_id
check("same-instrument gate: same instrument_id stops it",
      not c1._dual_reporting_hit(gdpr33_1, same_inst))

# divergence gate -- make recipients and deadlines identical
conv = copy.deepcopy(ai73_1)
conv.norm["recipient_body"] = list(gdpr33_1.norm.get("recipient_body") or [])
conv.norm["deadline"] = copy.deepcopy(gdpr33_1.norm.get("deadline"))
check("divergence gate: identical recipient AND deadline stops it",
      not c1._dual_reporting_hit(gdpr33_1, conv))

# reporting-duty gate: incident trigger but a non-reporting action
notreport = copy.deepcopy(ai73_1)
notreport.norm["action"] = "neemt passende technische maatregelen"
check("reporting-duty gate: incident trigger + non-report action stops it",
      not c1._dual_reporting_hit(gdpr33_1, notreport))

# reporting-duty gate: report action but no incident trigger
noincident = copy.deepcopy(ai73_1)
noincident.norm["trigger_event"] = "de jaarlijkse begroting"
noincident.norm["action"] = "meldt de begroting aan de raad"
check("reporting-duty gate: report action without incident family stops it",
      not c1._dual_reporting_hit(gdpr33_1, noincident))

print("\n=== S7: divergence evidence ===")
d1 = c1.reporting_divergence(gdpr33_1, ai73_1)
check("C-1 divergence identifies DIFFERENT RECIPIENT", d1["different_recipient"] is True, str(d1))
d2 = c1.reporting_divergence(gdpr33_5, ai73_4)
check("C-2 divergence identifies ONE-SIDED CLOCK", d2["one_sided_deadline"] is True, str(d2))
check("divergence returns all 5 documented keys",
      set(d1) == {"different_recipient", "recipients", "different_deadline",
                  "deadlines", "one_sided_deadline"}, str(sorted(d1)))

# ============================================================ 2. stratum wiring
print("\n=== S7: wiring into select_pairs ===")
import networkx as nx
import similarity
g = nx.read_gexf(DATA / "graph.gexf")
sp = similarity.semantic_candidate_pairs(None, [r.text for r in records], k=8,
                                         groups=[r.instrument_id for r in records], k_within=3)
cands = c1.generate_candidates(records, g, sp)
pair_map = {c1._pair_id(c["a"], c["b"]): (c["a"], c["b"]) for c in cands}
cache_c1 = json.loads((DATA / "cache" / "c1_adjudication_cache.json").read_text(encoding="utf-8"))
chosen = lt.select_pairs(pair_map, cache_c1)
from collections import Counter
counts = Counter(chosen.values())
print("   strata:", dict(counts))

check("candidate count unchanged at 31150 (no regression)", len(pair_map) == 31150, str(len(pair_map)))
check("S7 present in selection", counts.get("S7_dual_reporting", 0) > 0)
# 531 -> 788 after the 2026-10-04 --s7-only strict run adjudicated the 269-pair backlog.
# S1 and S5 also grew (346->351, 371->378): those same new strict verdicts created fresh
# eligibility for them too, which is correct -- a pair only enters ANY graded stratum once
# the strict pass has ruled on it.
check("S7 selects 788", counts.get("S7_dual_reporting") == 788, str(counts.get("S7_dual_reporting")))
print("   -- strata S7 must never steal from (it is placed last):")
for nm, want in [("S3_exception_dependent", 305), ("S4_risk_classification", 56),
                 ("S6_rights_vs_system_duties", 21)]:
    check(f"{nm} still {want} (untouched by S7)", counts.get(nm) == want, f"got {counts.get(nm)}")
print("   -- strata that legitimately grew with the new strict coverage:")
for nm, want in [("S1_non_confident", 351), ("S5_standard_benchmark", 378)]:
    check(f"{nm} now {want}", counts.get(nm) == want, f"got {counts.get(nm)}")

# C-1 must actually be SELECTED, not merely signal-positive
check("C-1 S-023 selected into S7 by the real pipeline",
      chosen.get("bbaf7fb0647b") == "S7_dual_reporting", f"got {chosen.get('bbaf7fb0647b')!r}")
check("C-3 S-032 not selected into S7",
      chosen.get("efc53efba8fc") != "S7_dual_reporting", f"got {chosen.get('efc53efba8fc')!r}")

# C-2 was unreachable until 2026-10-04: select_pairs only ever looks at pairs the strict
# pass already ruled on, and S-010 had no strict cache entry. The --s7-only run cleared
# that 269-pair backlog, S-010 came back NOT_A_CONFLICT, and it is now selected. Asserted
# positively so the gap cannot silently reopen -- if a future cache reset loses these
# verdicts, this fails rather than quietly reverting S7 to half-blind.
c2_pid = "755ecaa9ba67"
c2_a, c2_b = pair_map[c2_pid]
check("C-2 S-010 is a candidate pair", c2_pid in pair_map)
check("C-2 S-010 passes every S7 gate", c1._dual_reporting_hit(c2_a, c2_b))
check("C-2 S-010 has a strict verdict (backlog cleared)",
      f"{c1.DEFAULT_MODEL}::{c2_pid}" in cache_c1)
check("C-2 S-010 NOW SELECTED into S7 -- both reviewer-labelled tensions reachable",
      chosen.get(c2_pid) == "S7_dual_reporting", f"got {chosen.get(c2_pid)!r}")

# Count only pairs the strict pass would actually SEND to the LLM. An S7-shaped pair whose
# subtype is deterministic (standard_collision, competence_competition, threshold_mismatch)
# is resolved by arithmetic/lookup and never gets an adjudication cache entry at all -- one
# such pair (Cbw 27(1) x NIS2 23(4), standard_collision) made the naive count read 1 when
# the real backlog was 0.
_exc = c1.load_conditional_exceptions()
_AI_JUDGED = ("duty_conflict", "deontic_polarity_conflict")
uncached_s7 = 0
for _c in cands:
    _a, _b = _c["a"], _c["b"]
    if not c1._dual_reporting_hit(_a, _b):
        continue
    if f"{c1.DEFAULT_MODEL}::{c1._pair_id(_a, _b)}" in cache_c1:
        continue
    if c1._prepare_candidate(_c, _exc)["subtype"] in _AI_JUDGED:
        uncached_s7 += 1
check("no AI-judged S7-shaped pair is still awaiting strict coverage",
      uncached_s7 == 0, f"{uncached_s7} remain")

# ============================================================ 3. amendment registry
print("\n=== amendment registry / currency_warning ===")
reg = c1._load_amendment_registry()
check("registry loads with the AI Act 10(5) entry", (AIACT, "10", "5") in reg)
w = c1.currency_warning(ai10_5)
check("currency_warning fires on AI Act 10(5)", w is not None)
check("warning says 'repealed'", bool(w) and w[0]["change"] == "repealed", str(w))
check("warning carries consolidated source",
      bool(w) and "02024R1689" in (w[0].get("consolidated_source") or ""), str(w))
check("currency_warning is None for unaffected norm", c1.currency_warning(gdpr33_1) is None)
check("currency_warning fires if EITHER side affected", c1.currency_warning(gdpr33_1, ai10_5) is not None)

# article-level entry (paragraph: None) must match any paragraph
saved = dict(c1._AMENDMENT_REGISTRY)
c1._AMENDMENT_REGISTRY[(GDPR, "33", None)] = {"change": "amended", "in_force_from": "x"}
check("article-level registry entry (paragraph=None) matches any paragraph",
      c1.currency_warning(gdpr33_1) is not None)
c1._AMENDMENT_REGISTRY = saved  # restore

print("\n=== findings files ===")
f1 = json.loads((DATA / "results" / "c1" / "findings_c1.json").read_text(encoding="utf-8"))
f2 = json.loads((DATA / "results" / "c2" / "findings_c2.json").read_text(encoding="utf-8"))
flagged = [x for x in f1["findings"] if x.get("currency_warning")]
# 132 -> 137: the --s7-only strict run found 5 net new duty_conflicts among the
# dual-reporting backlog (net of supersession by the graded pass, which rebuild_c1_all.py
# applies and a raw partial run does not).
check("findings_c1 has 137 findings", len(f1["findings"]) == 137, str(len(f1["findings"])))
check("findings_c2 still has 172 findings", len(f2["findings"]) == 172, str(len(f2["findings"])))
check("7 findings carry a backfilled currency_warning", len(flagged) == 7, str(len(flagged)))
check("every flagged finding actually touches a registered provision",
      all(any((p["instrument_id"], str(p["article"]), str(p.get("paragraph_number"))) in reg
              or (p["instrument_id"], str(p["article"]), None) in reg
              for p in x["provisions"]) for x in flagged))
check("backfill changed no verdict field",
      all("subtype" in x and "confidence_tier" in x for x in f1["findings"]))

# ============================================================ 4. label sheet
print("\n=== label_sheet: reference inlining + currency banner ===")
pti = c1._build_paragraph_text_index(records)
tel2 = {"instrument_id": "BWBR0009950", "article": "11.7", "paragraph": "2",
        "text": by[("BWBR0009950", "11.7", "2")][0].text}
refs = label_sheet._referenced_paragraphs(tel2, pti)
check("Telecom 11.7(2) resolves its reference to lid 1", any(r["number"] == "1" for r in refs), str(refs)[:120])
check("resolved lid 1 contains the actual rule ('verboden')",
      any("verboden" in r["text"].lower() for r in refs))
check("a norm never lists ITSELF as a reference", all(r["number"] != "2" for r in refs))

cur = label_sheet._currency({"instrument_id": AIACT, "article": "10", "paragraph": "5"})
check("label sheet warns on AI Act 10(5)", cur is not None and "repealed" in cur)
check("label sheet silent on unaffected provision",
      label_sheet._currency({"instrument_id": GDPR, "article": "33", "paragraph": "1"}) is None)

html = (DATA / "evaluation" / "label_sheet.html").read_text(encoding="utf-8")
d = json.loads(re.search(r"const DATA=(\{.*?\});\n", html, re.S).group(1))
check("exported sheet still has all 50 pairs", len(d["items"]) == 50, str(len(d["items"])))
n_ref = sum(len(it[s]["referenced"]) for it in d["items"] for s in ("a", "b"))
check("exported sheet inlines 35 referenced paragraphs", n_ref == 35, str(n_ref))
n_cur = sum(1 for it in d["items"] for s in ("a", "b") if it[s].get("currency"))
check("exported sheet carries 3 currency warnings", n_cur == 3, str(n_cur))
# Blindness check. PERMISSION_CONFLICT is deliberately NOT in this list: it is one of
# the LABEL_HELP options the reviewer chooses from, so its presence is the answer
# vocabulary, not leaked model output. Only tokens that could carry a pipeline VERDICT
# belong here.
check("sheet leaks no model output (labels stay blind)",
      not any(k in html for k in ("luna", "terra", "final_verdict", "strict_verdict",
                                  "escalated", "confidence_tier", "llm_adjudication")))
check("CSS for both new blocks is present", ".ref{" in html and ".warn{" in html)

# ============================================================ summary
print(f"\n{'=' * 60}")
print(f"PASSED {len(PASS)}   FAILED {len(FAIL)}")
if FAIL:
    print("\nFAILURES:")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
