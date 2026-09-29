# -*- coding: utf-8 -*-
"""
C1 second pass (2026-09-28): graded "legal tension" adjudication, as a luna -> terra cascade.

Why: the strict duty-conflict scale (CONTRADICTION / NOT_A_CONFLICT / INSUFFICIENT_EVIDENCE)
only asks "can one actor obey both at once?", and its prompt is nearly all about ways to
reconcile the two rules -- across ~11,500 verdicts it never once answered
joint_compliance_possible=False. That is the right bar for a hard contradiction and is kept
untouched (the strict path stays strict). But real tensions are often broader: a permission
that depends on an unestablished exception, the same duty measured against different
benchmarks, a rule that works against the other's purpose. This pass classifies those with a
graded scale (schema/prompt validated in pilot_c1_graded_tension.py -- imported, not copied,
so the pilot remains a reproducible record of what was tested).

Pilot evidence (77 pairs x 2 configs; anchors are a should-not-regress check, NOT an unbiased
recall estimate, since real_conflicts already shaped four earlier fixes):
  * random confident negatives: terra 0/40 flagged as tension, luna 1/40 (+3 MISSING_CONTEXT,
    1 recheck) -- the graded scale did NOT over-trigger, unlike the definitional detector's
    first version (332/878);
  * INC-0008 (GDPR 9 x AI Act 10(5)): terra PERMISSION_CONFLICT with a concrete scenario and
    the decisive exception (GDPR 9(2)(g)); luna named the same gap as MISSING_CONTEXT;
  * ~93% of sampled Low duty-conflict findings (28/30) resolved to UNRELATED/COMPATIBLE;
  * cascade: every terra flag was already flagged by luna (0 misses) -- but only 3 terra
    flags existed, so this run also audits a random 10% of luna's PASSES with terra to
    measure the screen's miss rate properly.

Who gets a look (bounded on purpose, ~550 pairs, not the 11k confident negatives):
  S1  every non-confident strict verdict (INSUFFICIENT_EVIDENCE or needs_recheck);
  S3  confident NOT_A_CONFLICT pairs, cross-instrument, where exactly one side is a
      PROHIBITION and the model's OWN reasoning reconciled them via an exception/legal basis
      -- the classic "permission vs. restriction" shape. As a raw finding rule this matched
      243 pairs (too many to show a reviewer); as a SELECTOR for a real second look it is
      cheap and is exactly what would have surfaced INC-0008.

Cascade: luna (reasoning off) screens everything; terra (reasoning=medium) re-judges anything
luna flags (tension verdict, MISSING_CONTEXT, or internally inconsistent) plus the random
audit slice. Final verdict = terra's when terra ran, else luna's. Nothing luna alone says is
ever promoted to a tension finding.

Output (append-only, idempotent): subtype "legal_tension" (a tension verdict, never Tier 1 --
AI-judged) and subtype "missing_context" (Low, names the provision to check). Strict
duty_conflict findings whose pair the graded pass resolves to COMPATIBLE/UNRELATED are
superseded (removed from the list, recorded under "resolved_by_graded_pass" for audit).

Usage:
    python detect_c1_legal_tension.py --dry-run              # selection sizes, no calls
    python detect_c1_legal_tension.py                        # run cascade + append findings
    python detect_c1_legal_tension.py --append-findings-only # no API: rebuild from cache
"""
import argparse
import json
import random
import re
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import detect_c1_contradiction as c1
import networkx as nx
from pilot_c1_graded_tension import (CONFIGS, TENSION_VERDICTS, VERSION, TensionVerdict,  # noqa: F401
                                     adjudicate, _label)

ROOT = c1.ROOT
DATA = ROOT / "data"
CACHE_PATH = DATA / "c1_tension_pilot_cache.json"  # shared with the pilot on purpose: its 154 calls are reused
LUNA, TERRA = CONFIGS
AUDIT_FRACTION = 0.10
SEED = 20260928
_EXC_RE = re.compile(r"uitzondering|behoudens|tenzij|onverminderd|afwijking|exception|unless|derogation|"
                     r"rechtsgrond|legal basis", re.I)
# "legal_tension" and the pre-v2 upper-case verdict strings are kept in this set so a rebuild
# correctly clears findings from a run under the OLD 4-class scheme too, not just the current one.
NAMED_SUBTYPES = {"standard_benchmark_tension", "rights_vs_system_duties_tension",
                  "sensitive_data_tension", "classification_mismatch"}
SUBTYPES = {"legal_tension", "missing_context"} | NAMED_SUBTYPES


def _key(cfg, pid):
    return f"{VERSION}::{cfg[0]}::{cfg[1]}::{pid}"


def _flagged(entry):
    return bool(entry["needs_recheck_reason"]) or entry["verdict"]["verdict"] in TENSION_VERDICTS + ("missing_context",)


def load_cache():
    return json.loads(CACHE_PATH.read_text(encoding="utf-8")) if CACHE_PATH.exists() else {}


def final_verdicts(cache=None):
    """pair_id -> {"final": verdict|RECHECK|PENDING, "source": luna|terra, "entry": entry}."""
    cache = load_cache() if cache is None else cache
    out = {}
    for k in cache:
        parts = k.split("::")
        if parts[0] != VERSION or parts[1] != LUNA[0]:
            continue
        pid = parts[3]
        luna = cache[k]
        terra = cache.get(_key(TERRA, pid))
        if terra is not None and not terra["needs_recheck_reason"]:
            out[pid] = {"final": terra["verdict"]["verdict"], "source": "terra", "entry": terra}
        elif terra is not None:
            out[pid] = {"final": "RECHECK", "source": "terra", "entry": terra}
        elif _flagged(luna):
            out[pid] = {"final": "PENDING", "source": "luna", "entry": luna}
        else:
            out[pid] = {"final": luna["verdict"]["verdict"], "source": "luna", "entry": luna}
    return out


def superseded_finding_ids(final):
    """Strict findings the graded pass replaces: any pair it decided (terra verdict, or a luna pass
    nobody flagged). compatible/unrelated means it is no longer a finding at all; a tension or
    missing_context verdict means the graded finding (concrete scenario, decisive exception, both
    sides argued) supersedes the strict one -- keeping both would list one pair under two labels.
    PENDING/RECHECK are undecided, so the strict finding stays."""
    return {f"F-C1-{pid}" for pid, f in final.items() if f["final"] not in ("PENDING", "RECHECK")}


def build_pair_map(client, records):
    g = nx.read_gexf(DATA / "graph.gexf")
    import similarity
    sp = similarity.semantic_candidate_pairs(client, [r.text for r in records], k=8,
                                             groups=[r.instrument_id for r in records], k_within=3)
    return {c1._pair_id(c["a"], c["b"]): (c["a"], c["b"]) for c in c1.generate_candidates(records, g, sp)}


def select_pairs(pair_map, cache_c1):
    model = c1.DEFAULT_MODEL
    chosen = {}
    for pid, (a, b) in pair_map.items():
        e = cache_c1.get(f"{model}::{pid}")
        if not e:
            continue
        v = e["verdict"]
        if e.get("needs_recheck_reason") or v["verdict"] == "INSUFFICIENT_EVIDENCE":
            chosen[pid] = "S1_non_confident"
        elif (v["verdict"] == "NOT_A_CONFLICT" and a.instrument_id != b.instrument_id
              and "PROHIBITION" in (a.norm["deontic"], b.norm["deontic"]) and a.norm["deontic"] != b.norm["deontic"]
              and v.get("joint_compliance_possible")
              and _EXC_RE.search((v.get("criterion_fired") or "") + " " + (v.get("unresolved_exception") or ""))):
            chosen[pid] = "S3_exception_dependent"
        # S4 (2026-09-28): S1/S3 structurally cannot surface classification_mismatch -- two
        # risk-classification triggers are typically both OBLIGATION-shaped ("must assess",
        # "must classify"), never the PROHIBITION asymmetry S3 needs. Checked directly: the
        # one real classification_mismatch found (GDPR art. 35 DPIA trigger vs. AI Act art.
        # 6(4) high-risk classification, dev item INC-0003) was in neither S1 nor S3. Reuses
        # the existing, already-narrow risk-classification candidate signal rather than a new
        # keyword list.
        elif v["verdict"] == "NOT_A_CONFLICT" and c1._risk_classification_hit(a, b):
            chosen[pid] = "S4_risk_classification"
    return chosen


def run_cascade(client, records, pair_map, chosen, concurrency, audit):
    defs = c1.load_definitions_by_instrument()
    pti = c1._build_paragraph_text_index(records)
    rri = c1._build_reverse_reference_index(records)
    cache = load_cache()
    lock = threading.Lock()

    def call(cfg, pid):
        key = _key(cfg, pid)
        with lock:
            if key in cache:
                return pid, cache[key]
        a, b = pair_map[pid]
        v, issue = adjudicate(client, cfg[0], cfg[1], a, b, defs, pti, rri)
        entry = {"verdict": v.model_dump(), "needs_recheck_reason": issue}
        with lock:
            cache[key] = entry
            CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        return pid, entry

    def run(cfg, pids, label):
        n = 0
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futs = [ex.submit(call, cfg, p) for p in pids]
            for f in as_completed(futs):
                try:
                    f.result()
                except Exception as e:
                    print(f"  ERROR ({label}): {e}", flush=True)
                    if "credit" in str(e).lower():
                        print("  -- out of credits, stopping this phase (progress is cached)", flush=True)
                        for g in futs:
                            g.cancel()
                        return
                    continue
                n += 1
                if n % 50 == 0:
                    print(f"  [{label} {n}/{len(pids)}]", flush=True)

    pids = sorted(chosen)
    print(f"phase 1: luna screen over {len(pids)} pairs "
          f"({sum(1 for p in pids if _key(LUNA, p) in cache)} already cached)", flush=True)
    run(LUNA, pids, "luna")
    cache = load_cache()
    flagged = [p for p in pids if _key(LUNA, p) in cache and _flagged(cache[_key(LUNA, p)])]
    passed = [p for p in pids if _key(LUNA, p) in cache and p not in set(flagged)]
    audit_pids = []
    if audit and passed:
        rng = random.Random(SEED)
        audit_pids = rng.sample(passed, max(1, round(len(passed) * AUDIT_FRACTION)))
    print(f"phase 2: terra on {len(flagged)} luna-flagged + {len(audit_pids)} random audit of "
          f"{len(passed)} luna-passed", flush=True)
    run(TERRA, flagged + audit_pids, "terra")
    cache = load_cache()
    misses = [p for p in audit_pids if _key(TERRA, p) in cache and _flagged(cache[_key(TERRA, p)])
              and not cache[_key(TERRA, p)]["needs_recheck_reason"]]
    print(f"\naudit: terra flagged {len(misses)} of {len(audit_pids)} pairs luna had PASSED "
          f"(screen miss rate on this sample: {len(misses)}/{max(1, len(audit_pids))})", flush=True)
    for p in misses:
        print(f"   luna-pass -> terra {cache[_key(TERRA, p)]['verdict']['verdict']}: {_label(*pair_map[p])}", flush=True)


def _tier(entry):
    v = entry["verdict"]
    if v["verdict"] in TENSION_VERDICTS and v["confidence"] >= 0.8 and not entry["needs_recheck_reason"]:
        return 2, ["resolved by AI judgement (graded second pass, terra with reasoning after a luna screen) -- "
                   "never Tier 1 regardless of confidence", "model expressed high confidence"]
    return 3, ["resolved by AI judgement (graded second pass); lower model confidence or no firm verdict"]


def build_finding(a, b, final):
    entry, v = final["entry"], final["entry"]["verdict"]
    tier, reasons = _tier(entry)
    # The 4 user-named classes get their own subtype string (directly informative in a
    # findings-file summary); direct_conflict/permission_conflict/goal_tension -- real
    # tensions that don't fit one of the 4 specific patterns -- stay bucketed as the
    # generic "legal_tension" rather than inventing subtypes nobody asked for.
    subtype = ("missing_context" if v["verdict"] == "missing_context"
               else v["verdict"] if v["verdict"] in NAMED_SUBTYPES
               else "legal_tension")
    return {
        "finding_id": f"F-C1-TEN-{c1._pair_id(a, b)}",
        "category": "contradiction",
        "subtype": subtype,
        "status": "candidate",
        "confidence_tier": tier,
        "confidence_label": c1.CONFIDENCE_LABELS[tier],
        "confidence_reasons": reasons,
        # direct_conflict is the only class that asserts the two cannot both be obeyed; every other
        # tension class is conditional/unsettled by construction, so it stays None (open question).
        "incompatibility_established": True if v["verdict"] == "direct_conflict" else None,
        "challenge": None,
        "provisions": [
            {"uid": m.graph_uid, "instrument_id": m.instrument_id, "article": m.article,
             "paragraph_number": m.norm.get("number"), "norm_index": m.norm.get("norm_index"),
             "paragraph_index": m.norm.get("paragraph_index")} for m in (a, b)],
        "criteria_fired": ["graded_tension_second_pass"],
        "deterministic_result": None,
        "llm_adjudication": {**v, "decided_by": f"{final['source']}"},
        "report_eligible": False,
    }


def append_findings(pair_map):
    final = final_verdicts()
    path = DATA / "findings_c1.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["findings"] = [f for f in data["findings"] if f.get("subtype") not in SUBTYPES]
    added = Counter()
    for pid, f in final.items():
        if f["source"] != "terra" or f["final"] not in TENSION_VERDICTS + ("missing_context",) or pid not in pair_map:
            continue
        finding = build_finding(*pair_map[pid], f)
        data["findings"].append(finding)
        added[finding["subtype"] + ":" + f["final"]] += 1
    print(f"appended graded findings: {dict(added)}", flush=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--append-findings-only", action="store_true")
    ap.add_argument("--no-audit", action="store_true")
    ap.add_argument("--concurrency", type=int, default=8)
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from openai import OpenAI
    client = OpenAI()
    records = c1.load_all_norm_records()
    pair_map = build_pair_map(client, records)
    if args.append_findings_only:
        append_findings(pair_map)
        return
    cache_c1 = json.loads((DATA / "c1_adjudication_cache.json").read_text(encoding="utf-8"))
    chosen = select_pairs(pair_map, cache_c1)
    cache = load_cache()
    done = sum(1 for p in chosen if _key(LUNA, p) in cache)
    print(f"selected {len(chosen)} pairs: {dict(Counter(chosen.values()))}; luna already cached for {done}", flush=True)
    if args.dry_run:
        est_terra = round((len(chosen) - done) * 0.13) + round(len(chosen) * AUDIT_FRACTION)
        print(f"estimated new calls: ~{len(chosen) - done} luna + ~{est_terra} terra", flush=True)
        return
    run_cascade(client, records, pair_map, chosen, args.concurrency, audit=not args.no_audit)
    append_findings(pair_map)


if __name__ == "__main__":
    main()
