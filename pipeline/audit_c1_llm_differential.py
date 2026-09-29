# -*- coding: utf-8 -*-
"""
Controlled differential check of the strict C1 adjudicator (2026-09-28, diagnosing-bugs Phase 4 probe).
Does NOT read or write any cache: it re-asks the model about pairs that already have a cached verdict.

Three arms, each n pairs, half drawn from cached NOT_A_CONFLICT and half from cached
non-confident (INSUFFICIENT_EVIDENCE / needs_recheck) because flips concentrate there:
  CONTROL  pairs with NO reference context, same A/B order      -> re-run noise only
  CONTEXT  pairs WITH reference context in today's prompt        -> noise + cache staleness
                                                                    (the cache key has no prompt version)
  SWAP     mixed pairs, A and B swapped                          -> noise + position bias
A flip = the verdict class differs from the cached one. CONTEXT flip rate above CONTROL suggests stale
cache entries; SWAP above CONTROL suggests order bias. Calls: 3 * n (n=30 -> 90 luna calls, reasoning off).
"""
import argparse
import json
import random
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import detect_c1_contradiction as c1
import detect_c1_legal_tension as lt
from dotenv import load_dotenv

SEED = 20260928


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30)
    args = ap.parse_args()
    load_dotenv(c1.ROOT / ".env")
    from openai import OpenAI
    client = OpenAI()
    records = c1.load_all_norm_records()
    pair_map = lt.build_pair_map(client, records)
    pti, rri = c1._build_paragraph_text_index(records), c1._build_reverse_reference_index(records)
    cache = json.loads((c1.DATA / "c1_adjudication_cache.json").read_text(encoding="utf-8"))
    model = c1.DEFAULT_MODEL

    conf, nonconf = {"ctx": [], "noctx": []}, {"ctx": [], "noctx": []}
    for pid, (a, b) in sorted(pair_map.items()):
        e = cache.get(f"{model}::{pid}")
        if not e:
            continue
        has_ctx = bool(c1._reference_context(a, pti, rri, "A") + c1._reference_context(b, pti, rri, "B"))
        bucket = nonconf if (e.get("needs_recheck_reason") or e["verdict"]["verdict"] == "INSUFFICIENT_EVIDENCE") else conf
        if e["verdict"]["verdict"] == "CONTRADICTION" or (bucket is conf and e["verdict"]["verdict"] != "NOT_A_CONFLICT"):
            continue
        bucket["ctx" if has_ctx else "noctx"].append(pid)
    print(f"pool: confident/with-context {len(conf['ctx'])}, confident/no-context {len(conf['noctx'])}, "
          f"non-confident/with-context {len(nonconf['ctx'])}, non-confident/no-context {len(nonconf['noctx'])}")

    rng = random.Random(SEED)
    h = args.n // 2

    def draw(kind):
        picks = rng.sample(conf[kind], min(h, len(conf[kind]))) + rng.sample(nonconf[kind], min(args.n - h, len(nonconf[kind])))
        return picks

    arms = {"CONTROL": [(p, False) for p in draw("noctx")], "CONTEXT": [(p, False) for p in draw("ctx")]}
    mixed = rng.sample(conf["noctx"] + conf["ctx"], h) + rng.sample(nonconf["noctx"] + nonconf["ctx"], args.n - h)
    arms["SWAP"] = [(p, True) for p in mixed]
    total = sum(len(v) for v in arms.values())
    print(f"arms: { {k: len(v) for k, v in arms.items()} } -> {total} luna calls (reasoning off)")

    def run(item):
        arm, pid, swap = item
        a, b = pair_map[pid]
        if swap:
            a, b = b, a
        try:
            r = c1.adjudicate_duty_conflict(client, model, a, b, pti, rri)
        except Exception as ex:
            return arm, pid, swap, None, str(ex)[:120]
        return arm, pid, swap, r.verdict.verdict, r.needs_recheck_reason if hasattr(r, "needs_recheck_reason") else None

    jobs = [(arm, pid, swap) for arm, lst in arms.items() for pid, swap in lst]
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(run, jobs))

    out, summary = [], {}
    for arm in arms:
        rs = [r for r in results if r[0] == arm and r[3] is not None]
        flips = []
        for _, pid, swap, verdict, _ in rs:
            cached = cache[f"{model}::{pid}"]["verdict"]["verdict"]
            flips.append((cached, verdict, pid))
            out.append({"arm": arm, "pair_id": pid, "cached": cached, "fresh": verdict, "swapped": swap})
        n_flip = sum(1 for c, f, _ in flips if c != f)
        cross = Counter((c, f) for c, f, _ in flips if c != f)
        summary[arm] = {"n": len(rs), "flips": n_flip, "flip_types": {f"{c}->{f}": v for (c, f), v in cross.items()},
                        "fresh_CONTRADICTION": sum(1 for _, f, _ in flips if f == "CONTRADICTION")}
        print(f"{arm:8s} n={len(rs):3d} flipped={n_flip:2d}  {summary[arm]['flip_types']}  fresh CONTRADICTION verdicts: {summary[arm]['fresh_CONTRADICTION']}")
    (c1.DATA / "audit_c1_differential.json").write_text(json.dumps({"summary": summary, "rows": out}, ensure_ascii=False, indent=1), encoding="utf-8")
    errs = [r for r in results if r[3] is None]
    if errs:
        print(f"{len(errs)} calls failed, e.g. {errs[0][4]}")


if __name__ == "__main__":
    main()
