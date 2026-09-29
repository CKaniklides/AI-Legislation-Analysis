# -*- coding: utf-8 -*-
"""
Evaluation harness for the gold material built by build_gold_sets.py (2026-09-28).

  python evaluate_c1_gold.py --set dev        # tuned-on items: a REGRESSION check, not evidence of generalisation
  python evaluate_c1_gold.py --set heldout    # verified held-out items only; evaluate once per release
  python evaluate_c1_gold.py --set sample     # blind expert labels vs. the frozen predictions

Two things are always reported separately, because they fail for different reasons:
  RECALL OF CANDIDATES   did the pipeline ever put the pair in front of a judge?
  JUDGMENT               given that it did, what did the judge (strict / graded / definitional / C2) say,
                         and does a finding exist?

HOLD-OUT RULE: numbers from --set heldout / --set sample must not drive pipeline changes. If they do, the
items become dev data and new held-out items are needed.
"""
import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from itertools import product
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import detect_c1_contradiction as c1
import detect_c1_legal_tension as lt
import detect_c2_deduplication as c2

ROOT = c1.ROOT
DATA = ROOT / "data"
GOLD = DATA / "gold"
DOC_TO_INSTRUMENT = {"GDPR": "32016R0679", "AI Act": "32024R1689", "NIS2": "32022L2555"}
# The blind sample's label options and its frozen predictions (2026-09-28) use the pre-v2 upper-case
# scale; the current graded pass uses the named lower-case one. Both count as "tension" here.
TENSION = set(lt.TENSION_VERDICTS) | {"DIRECT_CONFLICT", "PERMISSION_CONFLICT", "DIVERGENT_STANDARD", "GOAL_TENSION"}
_NUM = re.compile(r"^\s*(\d+)")


def _articles(field):
    return [m.group(1) for chunk in (field or "").split(",") if (m := _NUM.match(chunk))]


def _finding_index():
    """(finding_id, kind, frozenset of (instrument, article)) for every current C1 + C2 finding."""
    out = []
    for path, kind in (("findings_c1.json", "C1"), ("findings_c2.json", "C2")):
        for f in json.loads((DATA / path).read_text(encoding="utf-8"))["findings"]:
            if f.get("status") not in ("candidate", "needs_recheck"):
                continue
            out.append((f["finding_id"], f"{kind}:{f['subtype'] if kind == 'C1' else 'duplicate_obligation'}",
                        {(p["instrument_id"], p["article"]) for p in f["provisions"]}))
    return out


def _item_scope(item):
    if "reference_a" in item:
        return ({(DOC_TO_INSTRUMENT[item["document_a"]], a) for a in _articles(item["reference_a"]["article"])},
                {(DOC_TO_INSTRUMENT[item["document_b"]], b) for b in _articles(item["reference_b"]["article"])})
    (ia, arts_a), (ib, arts_b) = list(item["provisions"].items())
    return {(ia, a) for a in arts_a}, {(ib, b) for b in arts_b}


LABEL_DETECTOR = {"hard_contradiction": "C1", "legal_tension": "C1", "conceptual_mismatch": "C1", "overlap": "C2"}


def _expected_subtype(it):
    if it.get("label") == "conceptual_mismatch":
        return "definitional_mismatch"
    if it.get("label") == "overlap":
        return None  # any C2 finding
    return it.get("label_subtype")


def eval_items(items, label):
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from openai import OpenAI
    import detect_c1_legal_basis as lb
    records = c1.load_all_norm_records()
    bases = lb.load_legal_basis_records()
    pair_map = lt.build_pair_map(OpenAI(), records)
    final = lt.final_verdicts()
    c1_cache = json.loads((DATA / "c1_adjudication_cache.json").read_text(encoding="utf-8"))
    lb_cands = lb.build_candidates(records, bases, {"a", "b", "c", "d", "e", "f"})
    lb_cache = lb.load_cache()
    findings = _finding_index()
    by_art = defaultdict(list)
    for r in records + bases:
        by_art[(r.instrument_id, r.article)].append(r)

    c2_cache = json.loads((DATA / "c2_adjudication_cache.json").read_text(encoding="utf-8"))

    rows = []
    for it in items:
        sa, sb = _item_scope(it)
        recs_a = [r for k in sa for r in by_art.get(k, [])]
        recs_b = [r for k in sb for r in by_art.get(k, [])]
        ids_a, ids_b = {id(r) for r in recs_a}, {id(r) for r in recs_b}
        detector = LABEL_DETECTOR.get(it.get("label"), "any")
        want = _expected_subtype(it)
        hits = sorted({(fid, kind) for fid, kind, prov in findings
                       if prov & sa and prov & sb and (detector == "any" or kind.startswith(detector))})
        exact = [h for h in hits if want is None or h[1] == f"{detector}:{want}"]
        # expected-type findings between A's articles and B's INSTRUMENT, outside B's cited articles -- reported,
        # never counted as caught (the item's own references define what "caught" means)
        inst_b = {i for i, _ in sb}
        near = sorted({(fid, kind) for fid, kind, prov in findings
                       if want and kind == f"{detector}:{want}" and prov & sa
                       and any(i in inst_b for i, _ in prov) and not prov & sb})
        row = {"id": it["id"], "label": it.get("label"), "subtype": it.get("label_subtype"), "scope": it.get("label_scope"),
               "source_type": it.get("source_type"), "detector": detector, "expected_subtype": want,
               "norm_pairs_possible": len(recs_a) * len(recs_b),
               "findings": [f"{k} {i}" for i, k in hits], "near": [f"{k} {i}" for i, k in near],
               "outcome": ("CAUGHT" if exact else "OTHER_TYPE" if hits else "MISSED")}
        if not recs_a or not recs_b:
            def_hits = [h for h in hits if h[1] == "C1:definitional_mismatch"]
            row["recall"] = "DEFINITIONS_ONLY" if def_hits else "NOT_EXTRACTED"
        elif want == "permission_prohibition_conflict":
            lbc = [(pid, a, b) for pid, (a, b, _) in lb_cands.items() if id(a) in ids_a and id(b) in ids_b]
            v = Counter(lb_cache[lb._key(pid)]["verdict"]["verdict"] for pid, _, _ in lbc if lb._key(pid) in lb_cache)
            row.update(candidates=len(lbc), judged=sum(v.values()), verdicts={"legal_basis": dict(v)},
                       recall="CANDIDATE" if lbc else "NOT_A_CANDIDATE")
        elif detector == "C2":
            pids = [c1._pair_id(a, b) for a, b in product(recs_a, recs_b)]
            keys = [f"{c2.C2_ADJUDICATION_VERSION}::{c1.DEFAULT_MODEL}::{p}" for p in pids]
            verdicts = Counter(c2_cache[k]["verdict"]["duplicate_burden_verdict"] for k in keys if k in c2_cache)
            row.update(candidates=sum(verdicts.values()), judged=sum(verdicts.values()), verdicts=dict(verdicts),
                       recall="CANDIDATE" if verdicts else "NOT_A_CANDIDATE")
        else:
            cands = [(pid, a, b) for pid, (a, b) in pair_map.items()
                     if (id(a) in ids_a and id(b) in ids_b) or (id(a) in ids_b and id(b) in ids_a)]
            graded = Counter(final[pid]["final"] for pid, _, _ in cands if pid in final)
            strict = Counter(c1_cache[f"{c1.DEFAULT_MODEL}::{pid}"]["verdict"]["verdict"]
                             for pid, _, _ in cands if f"{c1.DEFAULT_MODEL}::{pid}" in c1_cache)
            row.update(candidates=len(cands), judged=sum(strict.values()),
                       verdicts={"strict": dict(strict), "graded": dict(graded)},
                       recall="CANDIDATE" if cands else "NOT_A_CANDIDATE")
        rows.append(row)
    print(f"\n=== {label}: {len(rows)} items ===")
    for r in rows:
        print(f"\n{r['id']}  [{r['label']} / {r['subtype']}; scope {r['scope']}; source says: {r['source_type']}]")
        print(f"  expected         : {r['detector']} finding" + (f" of subtype {r['expected_subtype']}" if r["expected_subtype"] else ""))
        if r["recall"] == "DEFINITIONS_ONLY":
            print("  candidate recall : n/a -- definitions articles, compared by the definitional detector")
        else:
            print(f"  candidate recall : {r['recall']}"
                  + (f"  ({r['candidates']} candidate pairs, {r['judged']} judged, of {r['norm_pairs_possible']} possible norm pairs)"
                     if "candidates" in r else ""))
            print(f"  judgment         : {r.get('verdicts') or '-'}")
        print(f"  outcome          : {r['outcome']}"
              + ("  (regime-level item -- pairwise findings are not expected)" if r["scope"] == "regime" and r["outcome"] == "MISSED" else ""))
        for f in r["findings"][:4]:
            print(f"     {f}")
        if r["near"]:
            print(f"  expected-type findings against the same instrument OUTSIDE the cited articles (not counted): {len(r['near'])}")
            for f in r["near"][:4]:
                print(f"     {f}")
    print("\nsummary by label:")
    for lab in ("hard_contradiction", "legal_tension", "conceptual_mismatch", "overlap"):
        rs = [r for r in rows if r["label"] == lab]
        if rs:
            print(f"  {lab:20s} {dict(Counter(r['outcome'] for r in rs))}  of {len(rs)}")
    return rows


def eval_sample():
    sample = json.loads((GOLD / "label_sample_blind.json").read_text(encoding="utf-8"))
    preds_path = GOLD / "label_sample_predictions_frozen.json"
    raw = preds_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != sample["_meta"].get("predictions_sha256"):
        sys.exit("STOP: predictions file no longer matches the SHA-256 recorded before labelling -- it was modified after freezing.")
    preds = {p["sample_id"]: p for p in json.loads(raw.decode("utf-8"))}
    labelled = [i for i in sample["items"] if i.get("label")]
    print(f"labelled {len(labelled)}/{len(sample['items'])} pairs (predictions verified unmodified since freezing)")
    if not labelled:
        print("no expert labels yet -- nothing to evaluate. Send data/gold/label_sample_blind.json to a labeller.")
        return
    bad = [i for i in labelled if i["label"] not in ("DIRECT_CONFLICT", "PERMISSION_CONFLICT", "DIVERGENT_STANDARD", "GOAL_TENSION",
                                                     "COMPATIBLE", "UNRELATED", "CANNOT_TELL")]
    if bad:
        sys.exit(f"invalid labels: {[(i['sample_id'], i['label']) for i in bad]}")
    usable = [i for i in labelled if i["label"] != "CANNOT_TELL"]
    print(f"  {len(labelled) - len(usable)} marked CANNOT_TELL (excluded); {len(usable)} usable")
    conf = Counter()
    wsum = defaultdict(float)
    for i in usable:
        exp_t = i["label"] in TENSION
        pred_t = preds[i["sample_id"]]["final_verdict"] in TENSION
        conf[(exp_t, pred_t)] += 1
        wsum[(exp_t, pred_t)] += 1 / i["inclusion_probability"]
    tp, fp, fn, tn = conf[(True, True)], conf[(False, True)], conf[(True, False)], conf[(False, False)]
    print(f"  tension yes/no (unweighted): TP={tp} FP={fp} FN={fn} TN={tn}")
    print(f"  precision {tp}/{tp + fp}   recall {tp}/{tp + fn}   (small n -- treat as a rough signal only)")
    W = lambda k: wsum[k]
    tot_pos = W((True, True)) + W((True, False))
    print(f"  stratum-weighted estimate of tensions in the candidate population: ~{tot_pos:.0f} of {sum(wsum.values()):.0f} pairs "
          f"({100 * tot_pos / max(1, sum(wsum.values())):.1f}%); pipeline-flagged share of those: "
          f"{100 * W((True, True)) / max(1, tot_pos):.0f}%")
    print("  disagreements:")
    for i in usable:
        exp_t = i["label"] in TENSION
        pv = preds[i["sample_id"]]["final_verdict"]
        if exp_t != (pv in TENSION):
            print(f"    {i['sample_id']} [{i['stratum']}] expert={i['label']} pipeline={pv} comment={i.get('comment')}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", choices=["dev", "heldout", "sample"], required=True)
    args = ap.parse_args()
    if args.set == "dev":
        eval_items(json.loads((GOLD / "dev_set.json").read_text(encoding="utf-8"))["items"], "DEV SET (tuned on -- regression check only)")
    elif args.set == "heldout":
        items = [i for i in json.loads((GOLD / "heldout_candidates.json").read_text(encoding="utf-8"))["items"]
                 if i.get("label_status") == "verified"]
        if not items:
            sys.exit("no verified held-out items yet: every item in heldout_candidates.json is still a draft. "
                     "Evaluation deliberately refuses to run on unverified labels.")
        eval_items(items, "HELD-OUT (verified)")
    else:
        eval_sample()


if __name__ == "__main__":
    main()
