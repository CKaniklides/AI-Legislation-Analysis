# -*- coding: utf-8 -*-
"""
One-off migration (2026-09-24) for the _norm_id() fix: detect_c1_contradiction.py's
identity function omitted norm_index, so any two DIFFERENT norms sharing a paragraph
collapsed to the same identity -- and since _pair_id() (the adjudication cache key AND
the finding_id) is built directly from it, 847 groups of genuinely different candidate
pairs shared one _pair_id. 124 of those groups already had a live cache entry: any
sibling pair sharing that (buggy) id would have been silently treated as "already
cached" and handed another pair's verdict, never actually adjudicated itself. Checked
directly: 13 of the 149 AI-judged findings already in findings_c1.json have evidence
text that doesn't verbatim-match anything at their own stated paragraph.

_norm_id() is now fixed (includes norm_index) -- correct going forward, but every key in
the existing 6,973-entry data/c1_adjudication_cache.json is now stale, since the string
format changed for every entry, not just the colliding ones. The cache stores no
reverse-mapping (which pair each entry was FOR), so a key can't just be renamed; this
script rebuilds the cache from the candidates themselves:

  1. Regenerate every AI-judged candidate exactly as detect_c1_contradiction.main() does
     (same defaults: semantic_k=8, semantic_k_within=3 -- reproduces the same 14,958
     candidates confirmed earlier today).
  2. Compute each candidate's OLD pair_id (the pre-fix formula, reproduced locally here
     since the live module now only has the fixed version) and group by it.
  3. A group of size 1 whose old key IS in the old cache is safe to carry forward
     unchanged under its NEW key -- nothing about that entry was ever at risk.
  4. A group of size >1 (an actual collision) is NEVER carried forward automatically,
     even for the one member that might have been the "real" source of that cached
     entry -- there is no way to tell which one from the cache alone, so every member of
     an at-risk group gets a genuinely fresh LLM call under its own correct key.
  5. Pairs that were NEVER cached before (the ~8,600-pair remainder the project
     explicitly chose not to run) are left untouched -- this migration does not expand
     today's already-decided scope, only corrects identity within it.
  6. findings_c1.json is then rebuilt from scratch using ONLY cache-backed pairs (the
     migrated-safe ones plus the freshly re-adjudicated at-risk ones) -- exactly
     reproducing main()'s own cached-pair finding-construction logic, so the file's
     coverage is unchanged, just correctly attributed.

Run once. Not idempotent by design -- re-running would try to migrate an already-fixed
cache (harmless, since the "old" and "new" pair_id formulas would then coincide on
already-migrated entries, but pointless).
"""
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import detect_c1_contradiction as c1
import networkx as nx

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def _old_norm_id(r: c1.NormRecord) -> str:
    """The pre-fix formula, reproduced here on purpose (not imported) -- the live module
    only has the corrected version now, and this script specifically needs BOTH the old
    and new formulas to tell which cache entries were ever at risk."""
    return f"{r.instrument_id}:{r.article}:{r.norm.get('paragraph_index')}:{r.norm.get('number')}"


def _old_pair_id(a: c1.NormRecord, b: c1.NormRecord) -> str:
    import hashlib
    ids = sorted([_old_norm_id(a), _old_norm_id(b)])
    return hashlib.sha256("::".join(ids).encode("utf-8")).hexdigest()[:12]


def main():
    from openai import OpenAI
    client = OpenAI()
    model = c1.DEFAULT_MODEL  # confirmed directly: the only model ever used in the
                               # existing cache (single prefix, checked before writing
                               # this script)

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

    old_cache = json.loads((DATA / "c1_adjudication_cache.json").read_text(encoding="utf-8"))

    from collections import defaultdict
    by_old_pid = defaultdict(list)
    for p in needs_llm:
        by_old_pid[_old_pair_id(p["a"], p["b"])].append(p)

    safe_new_cache = {}
    at_risk = []
    at_risk_groups = 0
    for old_pid, group in by_old_pid.items():
        old_key = f"{model}::{old_pid}"
        if len(group) == 1:
            if old_key in old_cache:
                p = group[0]
                new_key = f"{model}::{c1._pair_id(p['a'], p['b'])}"
                safe_new_cache[new_key] = old_cache[old_key]
            # else: never cached before -- correctly left untouched, out of scope
        else:
            if old_key in old_cache:
                at_risk.extend(group)  # every member needs a genuinely fresh call
                at_risk_groups += 1
            # else: a collision group that was never actually adjudicated either way --
            # also correctly left untouched, out of scope

    print(f"  {len(safe_new_cache)} cache entries carried forward safely (no collision)", flush=True)
    print(f"  {len(at_risk)} pair(s) across {at_risk_groups} at-risk group(s) need a "
          f"fresh, correctly-keyed LLM call", flush=True)

    backup_path = DATA / "c1_adjudication_cache_pre_norm_index_fix_backup.json"
    backup_path.write_text(json.dumps(old_cache, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  old cache backed up -> {backup_path.relative_to(ROOT)}", flush=True)

    cache = dict(safe_new_cache)
    cache_lock = threading.Lock()
    cache_path = DATA / "c1_adjudication_cache.json"

    def _save_cache():
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")

    _save_cache()  # migrated-safe entries persisted immediately, before spending anything

    print(f"\nAdjudicating {len(at_risk)} at-risk pair(s) fresh (concurrency=8)...", flush=True)
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=8) as ex:
        future_to_p = {ex.submit(c1.adjudicate_duty_conflict, client, model, p["a"], p["b"]): p
                       for p in at_risk}
        for done, fut in enumerate(as_completed(future_to_p), 1):
            p = future_to_p[fut]
            a, b = p["a"], p["b"]
            label = f"{a.instrument_id} art.{a.article} <-> {b.instrument_id} art.{b.article}"
            try:
                result: c1.AdjudicationResult = fut.result()
            except Exception as e:
                print(f"  [{done}/{len(at_risk)}] {label} -- ERROR: {e}", flush=True)
                continue
            new_key = f"{model}::{c1._pair_id(a, b)}"
            with cache_lock:
                cache[new_key] = {
                    "verdict": result.verdict.model_dump(),
                    "needs_recheck_reason": result.needs_recheck_reason,
                    "challenge": result.challenge.model_dump() if result.challenge else None,
                }
                _save_cache()
            print(f"  [{done}/{len(at_risk)}] {label} -- {result.verdict.verdict}", flush=True)

    print(f"\nCache migration complete: {len(cache)} total entries -> "
          f"{cache_path.relative_to(ROOT)}", flush=True)

    # --- Rebuild findings_c1.json from scratch, using ONLY cache-backed pairs (the
    # migrated-safe ones plus the freshly re-adjudicated at-risk ones) -- reproduces
    # main()'s own cached-pair finding-construction path exactly, so coverage is
    # unchanged (still exactly the pairs that were ever actually adjudicated), just
    # correctly attributed under the fixed identity.
    print("\nRebuilding findings_c1.json from the corrected cache...", flush=True)
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
            continue  # never adjudicated -- correctly out of scope, same as before
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

    out_path = DATA / "findings_c1.json"
    out_path.write_text(json.dumps({"findings": findings, "suppressed": suppressed},
                                    ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  {n_from_cache} cache-backed pair(s) processed -> {len(findings)} finding(s), "
          f"{len(suppressed)} suppressed -> {out_path.relative_to(ROOT)}", flush=True)


if __name__ == "__main__":
    main()
