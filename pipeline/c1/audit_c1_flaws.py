# -*- coding: utf-8 -*-
"""
Offline flaw-hunting loop for the C1 (contradiction) pipeline (2026-09-28, diagnosing-bugs Phase 1).
No API calls; runs in well under a minute. Every check prints [RED] with counts and examples if the
invariant is violated, [ok] otherwise, [info] for structural facts that are not bugs by themselves.

    python audit_c1_flaws.py            # writes data/audit_c1_flaws.txt as well

A RED result is a lead to minimise and explain, not yet a proven bug: each check states what it assumes.
"""
import hashlib
import json
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
_HERE = Path(__file__).resolve().parent
_PIPELINE = _HERE.parent
for _d in (_PIPELINE, _PIPELINE / "c1", _PIPELINE / "preprocessing", _PIPELINE / "c2"):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))
import detect_c1_contradiction as c1
import detect_c1_legal_tension as lt
import networkx as nx

ROOT, DATA = c1.ROOT, c1.DATA
OUT = []


def say(tag, msg, examples=()):
    OUT.append(f"[{tag}] {msg}")
    for e in list(examples)[:3]:
        OUT.append(f"        e.g. {e}")


def check(name, red, msg, examples=()):
    say("RED" if red else "ok", f"{name}: {msg}", examples if red else ())


def pid_from_ids(ids):
    return hashlib.sha256("::".join(sorted(ids)).encode("utf-8")).hexdigest()[:12]


def prov_norm_id(p):
    return f"{p['instrument_id']}:{p['article']}:{p.get('paragraph_index')}:{p.get('paragraph_number')}:{p.get('norm_index')}"


def find_spans(obj, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("evidence_span_a", "evidence_span_b") and isinstance(v, str):
                out.append((k, v))
            else:
                find_spans(v, out)
    elif isinstance(obj, list):
        for v in obj:
            find_spans(v, out)


def main():
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from openai import OpenAI

    import detect_c1_legal_basis as lb
    records = c1.load_all_norm_records()
    rec_by_nid = {}
    for r in records + lb.load_legal_basis_records():  # legal-basis findings cite parsed GDPR 6(1) records
        rec_by_nid.setdefault(c1._norm_id(r), []).append(r)

    # ---- L1: loader ---------------------------------------------------------------------------------
    missing_inst, dropped_addr, eligible_seen, dropped_examples = [], Counter(), Counter(), []
    for path, get_provisions in c1.SOURCES:
        root = json.loads((ROOT / path).read_text(encoding="utf-8"))
        for p in get_provisions(root):
            hand_built = "uitvoeringswet" in path.lower() or "bijlage35" in path.lower()
            if "instrument_id" not in p and not hand_built:
                missing_inst.append(f"{path}: article {p.get('article') or p.get('number')}")
            for n in p.get("norms", []):
                if n["deontic"] in c1.ELIGIBLE_DEONTICS:
                    inst = p.get("instrument_id", path)
                    eligible_seen[inst] += 1
                    if n.get("addressee_type") is None:
                        dropped_addr[inst] += 1
                        dropped_examples.append(f"{inst} art {p.get('article') or p.get('number')}({n.get('number')}) "
                                                f"{n['deontic']} addressee={n.get('addressee')!r} action={str(n.get('action'))[:70]!r}")
    check("L1a loader instrument_id", bool(missing_inst),
          f"{len(missing_inst)} provision(s) have no instrument_id and no hand-built path fallback, so "
          f"load_all_norm_records would silently reuse the previous provision's instrument_id (or crash)"
          if missing_inst else "every provision resolves its own instrument_id", missing_inst)
    nd, ne = sum(dropped_addr.values()), sum(eligible_seen.values())
    check("L1b silent drop: addressee_type None", nd > 0,
          f"{nd} of {ne} eligible-deontic norms ({100 * nd / max(1, ne):.1f}%) are dropped before candidate generation "
          f"because addressee_type is null; by source: {dict(dropped_addr)}", dropped_examples)
    for e in dropped_examples:
        OUT.append(f"        dropped: {e}")
    dup = {k: v for k, v in rec_by_nid.items() if len(v) > 1}
    check("L1c norm identity", bool(dup), f"{len(dup)} _norm_id values are shared by 2+ records "
          f"(their pairs would share a pair_id and a cache entry)", [f"{k} x{len(v)}" for k, v in dup.items()])
    no_pidx = [r for r in records if r.norm.get("paragraph_index") is None]
    say("info", f"L1d {len(no_pidx)} of {len(records)} records lack paragraph_index and use the number-keyed text fallback "
                f"(the path that can attach the wrong paragraph's text)")
    weak = []
    for r in records:
        wa = c1._content_words(r.norm.get("action") or "")
        if len(wa) >= 3 and not (wa & c1._content_words(r.text)):
            weak.append(f"{c1._norm_id(r)} action={r.norm.get('action')[:60]!r}")
    check("L1e text attached to norm", bool(weak),
          f"{len(weak)} record(s) whose extracted action shares no content word with the paragraph text attached "
          f"as its evidence (possible wrong paragraph)", weak)

    # ---- candidate population -----------------------------------------------------------------------
    client = OpenAI()
    pair_map = lt.build_pair_map(client, records)
    say("info", f"candidate population: {len(pair_map)} pairs (from {len(records)} records)")
    in_cand = {id(x) for a, b in pair_map.values() for x in (a, b)}
    never = [r for r in records if id(r) not in in_cand]
    by_inst = Counter(r.instrument_id for r in never)
    tot_inst = Counter(r.instrument_id for r in records)
    say("info", f"L2 records that never appear in ANY candidate pair: {len(never)}/{len(records)} "
                f"({100 * len(never) / len(records):.0f}%); worst instruments: "
                + ", ".join(f"{i} {by_inst[i]}/{tot_inst[i]}" for i, _ in sorted(by_inst.items(), key=lambda kv: -kv[1] / tot_inst[kv[0]])[:5]))

    # ---- L2b: which candidate pairs did NO pass ever judge, and is that skewed? -----------------------
    c1_cache = json.loads((DATA / "cache" / "c1_adjudication_cache.json").read_text(encoding="utf-8"))
    graded_final = lt.final_verdicts()
    tot_ip, judged_ip = Counter(), Counter()
    unjudged = 0
    for pid, (a, b) in pair_map.items():
        key = tuple(sorted((a.instrument_id, b.instrument_id)))
        tot_ip[key] += 1
        if f"{c1.DEFAULT_MODEL}::{pid}" in c1_cache or pid in graded_final:
            judged_ip[key] += 1
        else:
            unjudged += 1
    say("info", f"L2b candidate pairs never judged by any pass: {unjudged}/{len(pair_map)} ({100 * unjudged / len(pair_map):.0f}%)")
    skew = sorted(((judged_ip[k] / t, k, t) for k, t in tot_ip.items() if t >= 40), key=lambda x: x[0])
    check("L2b coverage skew", bool(skew) and skew[0][0] < 0.5,
          "instrument pairs (>=40 candidates) with under half their candidates ever judged: "
          + "; ".join(f"{k[0]}x{k[1]} {judged_ip[k]}/{t}" for frac, k, t in skew if frac < 0.5))

    # ---- L3: symmetry of every pair-level predicate --------------------------------------------------
    asym = defaultdict(list)
    for pid, (a, b) in pair_map.items():
        rk_ab, rk_ba = c1.recipient_match_kind(a, b), c1.recipient_match_kind(b, a)
        if rk_ab != rk_ba:
            asym["recipient_match_kind"].append(pid)
        if c1.route_subtype(a, b, rk_ab)[0] != c1.route_subtype(b, a, rk_ba)[0]:
            asym["route_subtype"].append(pid)
        for name, fn in (("_same_function", c1._same_function), ("_trigger_keyword_hit", c1._trigger_keyword_hit),
                         ("_concept_pair_hit", c1._concept_pair_hit), ("_risk_classification_hit", c1._risk_classification_hit),
                         ("_automated_decision_oversight_hit", c1._automated_decision_oversight_hit),
                         ("_addressee_matches", c1._addressee_matches), ("_same_addressee_authority", c1._same_addressee_authority)):
            if bool(fn(a, b)) != bool(fn(b, a)):
                asym[name].append(pid)
        if bool(c1.check_deference_suppresses(a, b)) != bool(c1.check_deference_suppresses(b, a)):
            asym["check_deference_suppresses"].append(pid)
    check("L3 symmetry", bool(asym), "predicates giving different answers for (A,B) vs (B,A): "
          + (", ".join(f"{k}={len(v)}" for k, v in asym.items()) if asym else "none"),
          [f"{k}: {v[0]}" for k, v in asym.items()])

    # ---- L4: candidate generation is independent of record order --------------------------------------
    g = nx.read_gexf(DATA / "graph.gexf")
    rng = random.Random(1)
    sub = rng.sample(records, 500)
    base = {c1._pair_id(c["a"], c["b"]) for c in c1.generate_candidates(sub, g, None)}
    shuf = sub[:]
    rng.shuffle(shuf)
    other = {c1._pair_id(c["a"], c["b"]) for c in c1.generate_candidates(shuf, g, None)}
    check("L4 candidate order-invariance", base != other, f"{len(base)} vs {len(other)} pairs; symmetric difference {len(base ^ other)}")

    # ---- L5: findings integrity ---------------------------------------------------------------------
    data = json.loads((DATA / "results" / "c1" / "findings_c1.json").read_text(encoding="utf-8"))
    fs = data["findings"]
    ids = Counter(f["finding_id"] for f in fs)
    check("L5a unique finding_id", any(v > 1 for v in ids.values()), f"{sum(1 for v in ids.values() if v > 1)} duplicated ids")
    unresolved, selfpair, pairs_seen, dup_pairs = [], [], {}, []
    for f in fs:
        if f["subtype"] == "definitional_mismatch":
            continue
        nids = [prov_norm_id(p) for p in f["provisions"]]
        if any(n not in rec_by_nid for n in nids):
            unresolved.append(f["finding_id"])
        if len(set(nids)) < len(nids):
            selfpair.append(f["finding_id"])
        key = frozenset(nids)
        if key in pairs_seen:
            dup_pairs.append(f"{f['finding_id']} & {pairs_seen[key]} ({f['subtype']})")
        pairs_seen[key] = f["finding_id"]
    check("L5b findings resolve to norms", bool(unresolved), f"{len(unresolved)} non-definitional findings cite a norm not in the loaded corpus", unresolved)
    check("L5c self-pairs", bool(selfpair), f"{len(selfpair)} findings compare a norm with itself", selfpair)
    check("L5d one pair, two findings", bool(dup_pairs), f"{len(dup_pairs)} norm pairs appear in 2+ findings", dup_pairs)

    bad_span, n_span, caught_span = [], 0, 0
    for f in fs:
        if f["subtype"] == "definitional_mismatch" or not f.get("llm_adjudication"):
            continue
        spans = []
        find_spans(f["llm_adjudication"], spans)
        recs = [r for p in f["provisions"] for r in rec_by_nid.get(prov_norm_id(p), [])]
        text_blob = " ".join(r.text for r in recs)
        for k, s in spans:
            n_span += 1
            if not c1._verbatim_in(s, text_blob):
                if f.get("needs_recheck_reason"):  # already caught and demoted -- not a silent bug
                    caught_span += 1
                else:
                    bad_span.append(f"{f['finding_id']} {k}: {s[:70]!r}")
    check("L5e evidence verbatim (silent only)", bool(bad_span),
          f"{len(bad_span)} of {n_span} quoted evidence spans are not verbatim AND not flagged via "
          f"needs_recheck_reason ({caught_span} more are non-verbatim but already caught+demoted -- not counted as red)", bad_span)

    # ---- L6: supersede logic vs graded verdicts ---------------------------------------------------
    final = lt.final_verdicts()
    decided = {pid for pid, v in final.items() if v["final"] not in ("PENDING", "RECHECK")}
    leftover, wrongten = [], []
    for f in fs:
        if f["subtype"] in ("definitional_mismatch",):
            continue
        pid = pid_from_ids([prov_norm_id(p) for p in f["provisions"]])
        if f["finding_id"].startswith("F-C1-TEN-"):
            fin = final.get(pid, {}).get("final")
            if fin not in list(lt.TENSION_VERDICTS) + ["missing_context"]:
                wrongten.append(f"{f['finding_id']} final={fin}")
        elif pid in decided:
            leftover.append(f"{f['finding_id']} ({f['subtype']}) graded final={final[pid]['final']}")
    check("L6a supersede completeness", bool(leftover), f"{len(leftover)} strict findings sit on pairs the graded pass already decided", leftover)
    check("L6b graded findings match their verdict", bool(wrongten), f"{len(wrongten)} graded findings whose current cached final verdict is not a tension/missing-context", wrongten)
    resolved = set(data.get("resolved_by_graded_pass", []))
    check("L6c resolved ids absent", bool(resolved & set(ids)), f"{len(resolved & set(ids))} ids both live and listed as resolved")

    # ---- L7: flag consistency -----------------------------------------------------------------------
    check("L7a report_eligible/human_verified", any(f.get("report_eligible") or f.get("human_verified") for f in fs),
          "no finding is marked report-eligible or human-verified (the system never certifies)")
    est = Counter((f["subtype"], f["confidence_label"], f["incompatibility_established"]) for f in fs)
    hi_unest = [k for k in est if k[1] == "High" and not k[2]]
    say("info", f"L7b (subtype, tier, incompatibility_established) counts: {dict(est)}")
    check("L7c High tier without established incompatibility", bool(hi_unest),
          f"High-confidence findings whose incompatibility is NOT established: {hi_unest} -- 'High' then means 'mechanically clean comparison', not 'likely a real conflict'")

    # ---- L8: threshold arithmetic --------------------------------------------------------------------
    raw_thr = Counter()
    for r in records:
        for t in r.norm.get("thresholds") or []:
            raw_thr[t] += 1
    unit_words = re.compile(r"miljoen|miljard|million|billion|duizend|thousand", re.I)
    bad_units = []
    for t in raw_thr:
        p = c1._parse_threshold(t)
        if p and p[0] == "euro" and unit_words.search(t) and p[1] < 1e5:
            bad_units.append(f"{t!r} -> {p}")
    check("L8a scale words", bool(bad_units), f"{len(bad_units)} threshold strings with a scale word (miljoen/million...) parsed to a small euro amount", bad_units)
    multi = []
    for f in fs:
        if f["subtype"] != "threshold_mismatch":
            continue
        for p in f["provisions"]:
            for r in rec_by_nid.get(prov_norm_id(p), []):
                by_kind = Counter(k for k, _ in c1._parsed_thresholds(r.norm))
                if any(v > 1 for v in by_kind.values()):
                    multi.append(f"{f['finding_id']} {r.instrument_id} art {r.article}: {c1._parsed_thresholds(r.norm)}")
    check("L8b multi-tier fines compared tier-blind", bool(multi),
          f"{len(set(m.split()[0] for m in multi))} threshold_mismatch finding(s) involve a norm with 2+ same-kind amounts; "
          f"route_subtype returns the FIRST unequal pair of tiers, so which tiers were compared is arbitrary", multi)

    for f in fs:
        if f["subtype"] == "threshold_mismatch":
            for p in f["provisions"]:
                for r in rec_by_nid.get(prov_norm_id(p), [])[:1]:
                    say("info", f"L8c {f['finding_id']} {r.instrument_id} art {r.article}({r.norm.get('number')}) "
                                f"trigger={str(r.norm.get('trigger_event'))[:70]!r} action={str(r.norm.get('action'))[:70]!r} "
                                f"thresholds={r.norm.get('thresholds')}")

    text = "\n".join(OUT)
    (DATA / "logs" / "audit_c1_flaws.txt").write_text(text, encoding="utf-8")
    print(text)
    print(f"\nsummary: {sum(1 for l in OUT if l.startswith('[RED]'))} RED, {sum(1 for l in OUT if l.startswith('[ok]'))} ok")


if __name__ == "__main__":
    main()
