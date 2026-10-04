# -*- coding: utf-8 -*-
"""
Assigns an explicit, explainable review_priority to every C1 finding, and backfills the
scope field the non-strict detectors never set.

Why this exists (2026-10-04). This system is a triage tool: Ministry lawyers work the list
top-down and accept or decline. The ORDER is therefore the product, and it was not
ordered -- 50 of 74 candidates sat in confidence_tier 2, and the model's own confidence
was useless for ranking (all 15 externally reviewed pairs scored 0.90-0.98, and the single
pair the pipeline was most confident about in a non-neutral direction was the one the
reviewer overturned).

Evidence completeness was evaluated as a ranking signal and rejected: every LLM finding
carries a concrete_scenario and both evidence spans, because the verdict validators
already refuse responses without them. A signal that fires 100% of the time ranks nothing.

The signal that does discriminate is INDEPENDENT AGREEMENT -- whether two models, run at
different reasoning effort, reached the same positive verdict. Measured on the current
findings: 23 of 25 graded findings agreed, and of the 2 contested ones, the pattern "luna
says compatible, terra claims a tension" is exactly the shape of this project's one
externally confirmed false positive (S-008: luna COMPATIBLE 0.97 vs terra
PERMISSION_CONFLICT; the legal reviewer sided with luna).

Priority is a rank, not a score: a lawyer should be able to read WHY a finding is near the
top without trusting an opaque number. Nothing is hidden or deleted -- this only orders.
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

DATA = c1.ROOT / "data"
FINDINGS = DATA / "results" / "c1" / "findings_c1.json"

P_DETERMINISTIC = 1   # no model judgement at all -- arithmetic or a direct lookup
P_AGREED = 2          # two models, different effort, same positive verdict
P_SINGLE = 3          # one model ruled; no independent confirmation available
P_CONTESTED = 4       # models disagreed and the positive side won -- see the S-008 note above
P_LOWEST = 5


def _model_agreement(finding: dict, tension_cache: dict) -> "tuple[str, str] | None":
    """(luna_verdict, terra_verdict) for a graded-tension finding, or None when this
    finding did not come from the two-model cascade."""
    fid = finding.get("finding_id", "")
    if not fid.startswith("F-C1-TEN-"):
        return None
    pid = fid.replace("F-C1-TEN-", "")
    luna = (tension_cache.get(lt._key(lt.LUNA, pid)) or {}).get("verdict", {}).get("verdict")
    terra = (tension_cache.get(lt._key(lt.TERRA, pid)) or {}).get("verdict", {}).get("verdict")
    if luna is None or terra is None:
        return None
    return luna, terra


def rank(finding: dict, tension_cache: dict) -> "tuple[int, list[str]]":
    reasons = []

    if finding.get("deterministic_result") is not None:
        priority, reasons = P_DETERMINISTIC, ["resolved deterministically -- no model judgement involved"]
    else:
        pair = _model_agreement(finding, tension_cache)
        if pair is None:
            priority = P_SINGLE
            reasons = ["single model verdict; no independent second opinion available"]
        elif pair[0] == pair[1]:
            priority = P_AGREED
            reasons = [f"two models at different reasoning effort independently agreed: {pair[0]}"]
        else:
            priority = P_CONTESTED
            reasons = [f"models disagreed (screen={pair[0]}, reviewer={pair[1]}) and the positive side won -- "
                       f"this project's one externally confirmed false positive had this shape"]

    # Demotion, applied on top: a finding resting on text a later amendment changed or
    # repealed may be moot regardless of how sound the reasoning is, so it should not sit
    # above live-law findings in a reviewer's queue.
    if finding.get("currency_warning"):
        priority = min(P_LOWEST, priority + 1)
        reasons.append("demoted: rests on text a later amendment changed or repealed")

    return priority, reasons


def backfill_scope(finding: dict) -> None:
    """Only the strict detector ever set scope, leaving it null on 69 of 74 candidates and
    making 'cross-law vs internal' unfilterable -- which is the project's central
    distinction. Computable from the provisions themselves, so there is no reason for it
    to be missing."""
    if finding.get("scope"):
        return
    ps = finding.get("provisions") or []
    ids = {p.get("instrument_id") for p in ps if p.get("instrument_id")}
    if len(ids) >= 2:
        finding["scope"] = "cross_law"
    elif len(ids) == 1:
        finding["scope"] = "internal_coherence"


def group_by_article(findings: "list[dict]") -> "list[dict]":
    """One entry per (article pair, subtype), with the paragraph-level findings inside.

    Added 2026-10-05 after the full S7 run: 174 dual-reporting findings collapsed to 57
    distinct article pairs, and NIS2 art. 23 x AI Act art. 73 alone appeared 13 times --
    because both articles run to ~11 paragraphs and the detector compares paragraph
    against paragraph. A reviewer opening the tool would meet the same legal question
    ("do these two reporting regimes overlap?") thirteen separate times and reasonably
    conclude the tool repeats itself.

    Grouping, not deleting: every paragraph-level finding is preserved inside its group,
    because WHICH paragraphs collide is the detail a lawyer needs once they accept the
    article-level question is real. C2 already emits exactly this shape
    (findings_c2_by_article.json, group_by_article there); C1 simply never did.
    """
    from collections import defaultdict
    groups: "dict[tuple, list[dict]]" = defaultdict(list)
    for f in findings:
        ps = f.get("provisions") or []
        if len(ps) < 2:
            continue
        key = tuple(sorted([f"{ps[0]['instrument_id']}:{ps[0]['article']}",
                            f"{ps[1]['instrument_id']}:{ps[1]['article']}"])) + (f.get("subtype"),)
        groups[key].append(f)

    out = []
    for (ref_a, ref_b, subtype), members in groups.items():
        out.append({
            "article_pair": [ref_a, ref_b],
            "subtype": subtype,
            "paragraph_finding_count": len(members),
            "review_priority": min(m.get("review_priority", 9) for m in members),
            "best_confidence_tier": min(m.get("confidence_tier", 9) for m in members),
            "any_currency_warning": any(m.get("currency_warning") for m in members),
            "finding_ids": [m["finding_id"] for m in members],
            "findings": members,
        })
    out.sort(key=lambda g: (g["review_priority"], g["best_confidence_tier"],
                            -g["paragraph_finding_count"]))
    return out


def main() -> None:
    data = json.loads(FINDINGS.read_text(encoding="utf-8"))
    tension_cache = lt.load_cache()

    from collections import Counter
    dist = Counter()
    for f in data["findings"]:
        backfill_scope(f)
        p, reasons = rank(f, tension_cache)
        f["review_priority"] = p
        f["review_priority_reasons"] = reasons
        if f.get("status") == "candidate":
            dist[p] += 1

    # Candidates first, then by priority, so the file itself reads in review order.
    order = {"candidate": 0, "needs_recheck": 1, "unverifiable": 2}
    data["findings"].sort(key=lambda f: (order.get(f.get("status"), 3),
                                          f.get("review_priority", 9),
                                          f.get("subtype", "")))
    FINDINGS.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    candidates = [f for f in data["findings"] if f.get("status") == "candidate"]
    grouped = group_by_article(candidates)
    grouped_path = FINDINGS.parent / "findings_c1_by_article.json"
    c1.atomic_write_json(grouped_path, {"article_pairs": grouped})

    labels = {P_DETERMINISTIC: "deterministic", P_AGREED: "two models agreed",
              P_SINGLE: "single model", P_CONTESTED: "contested", P_LOWEST: "lowest"}
    print("review queue by priority:")
    for p in sorted(dist):
        print(f"  P{p} {labels.get(p, ''):20s} {dist[p]}")
    print(f"  total candidates: {sum(dist.values())}")
    scoped = Counter(f.get("scope") for f in data["findings"] if f.get("status") == "candidate")
    print(f"  scope backfilled: {dict(scoped)}")
    print(f"\ngrouped: {len(candidates)} paragraph-level finding(s) -> "
          f"{len(grouped)} article-pair question(s)  [{grouped_path.name}]")


if __name__ == "__main__":
    main()
