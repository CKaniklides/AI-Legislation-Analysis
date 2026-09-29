# -*- coding: utf-8 -*-
"""
Builds data/gold/ (2026-09-28) -- the evaluation material the project was missing.

Why: the 10-item data/real_conflicts file has shaped enough fixes (risk-classification keywords,
the automated-decision/oversight signal, the data-subject-right rule, the NIS2 addressee rule,
the definitional detector, the art. 9(2) context fix) that it can no longer say how the system
does on cases it has not seen. This separates:

  dev_set.json                  the 10 items the system HAS been tuned on, normalised (duplicate
                                id, malformed key fixed; original file left untouched) and
                                annotated with the source study's OWN type for each, which
                                turned out to matter (see _meta).
  heldout_candidates.json       pairs from authoritative sources the system was NOT tuned on.
                                Small, and every label is a DRAFT until someone with legal
                                training verifies it -- it is candidates, not yet a held-out set.
  label_sample_blind.json       50 pipeline candidate pairs for expert labelling, verdicts hidden.
  label_sample_predictions_frozen.json
                                the pipeline's predictions for those 50, frozen (SHA-256 recorded
                                in the sample's _meta) BEFORE any label exists, so no later tuning
                                can be back-fitted to them. Do not open before labelling.

HOLD-OUT RULE: never tune on heldout_candidates.json or label_sample_blind.json. Evaluate once
per release with evaluate_c1_gold.py; if a result changes the pipeline, that item is now dev data
and a fresh held-out item is needed.

Usage:
    python build_gold_sets.py                    # dev set + held-out candidates + blind sample (no API)
    python build_gold_sets.py --run-predictions  # also run + freeze pipeline predictions (~50 luna + few terra calls)
"""
import argparse
import hashlib
import json
import random
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import detect_c1_contradiction as c1

ROOT = c1.ROOT
DATA = ROOT / "data"
GOLD = DATA / "gold"
STUDY = ("Graux, Garstka, Murali, Cave, Botterman, 'Interplay between the AI Act and the EU digital "
         "legislative framework', European Parliament (ITRE), PE 778.575, October 2025")
SEED = 20260928
LABELS = ["DIRECT_CONFLICT", "PERMISSION_CONFLICT", "DIVERGENT_STANDARD", "GOAL_TENSION",
          "COMPATIBLE", "UNRELATED", "CANNOT_TELL"]

# Corrected labels (2026-09-29). The source file uses one word, "contradiction", for four different
# things, so a strict duty-conflict test was being scored against items that were never duty conflicts.
# Four classes, each with the detector meant to catch it:
#   hard_contradiction   -- the same act permitted/required by one provision and forbidden by the
#                           other. Includes permission-vs-prohibition, which an actor can always
#                           "comply with" by refraining -- so the strict "can the actor obey both?" test
#                           is structurally blind to it. Detector: C1 legal-basis path / strict pass.
#   legal_tension        -- jointly satisfiable, but an open legal question (named subtype).
#                           Detector: C1 graded tension pass.
#   overlap              -- the same burden imposed twice. Detector: C2.
#   conceptual_mismatch  -- the same real-world concept defined/allocated differently.
#                           Detector: C1 definitional detector.
LABELS_BY_ITEM = {
    "INC-0001": dict(label="hard_contradiction", label_subtype="permission_prohibition_conflict", label_scope="pairwise",
        expected_detector="C1 legal-basis path (detect_c1_legal_basis.py)",
        label_rationale="GDPR 6(1)(f) is a conditional permission (a legal basis). The source's core claim: 'even if the balancing would favour the data controller under GDPR, the AI Act may still restrict the processing' -- processing lawful under a pure 6(1)(f) analysis prohibited or restricted by the AI Act. A permission and a prohibition of the same act are contradictory in the deontic sense even though an actor can always comply by refraining, which is why the strict duty-conflict test cannot see it. The cited AI Act articles (9, 14, 27) support the weaker half of the claim (the balancing is 'recalibrated'); the source names no specific prohibiting article. Whether 6(1)(f)'s own balancing clause already absorbs the AI Act restriction is the question for the expert."),
    "INC-0002": dict(label="overlap", label_subtype="duplicate_impact_assessment", label_scope="pairwise",
        expected_detector="C2", label_rationale="Source: 'duplication of compliance burdens' (DPIA and FRIA)."),
    "INC-0003": dict(label="legal_tension", label_subtype="classification_mismatch", label_scope="pairwise",
        expected_detector="C1 graded tension pass",
        label_rationale="Source: the two instruments 'do not define high risk in the same way' -- 'not strictly a legal inconsistency but the product of two different regulatory logics'."),
    "INC-0004": dict(label="conceptual_mismatch", label_subtype="role_allocation", label_scope="pairwise",
        expected_detector="C1 definitional detector",
        label_rationale="Controller/processor (GDPR art. 4) vs. provider/deployer (AI Act art. 3): 'functionally distinct' role models, a definitional matter."),
    "INC-0005": dict(label="overlap", label_subtype="documentation_and_logging", label_scope="pairwise",
        expected_detector="C2", label_rationale="Source: the provisions 'converge in their aim' -- parallel security, records and logging duties."),
    "INC-0006": dict(label="legal_tension", label_subtype="standard_benchmark_tension", label_scope="pairwise",
        expected_detector="C1 graded tension pass",
        label_rationale="Source table types it Overlap; its text names the tension: 'uncertainty as to which benchmark must be met to avoid liability under both frameworks'."),
    "INC-0007": dict(label="legal_tension", label_subtype="rights_vs_system_duties_tension", label_scope="regime",
        expected_detector="C1 graded tension pass (regime-level; not expected pairwise)",
        label_rationale="Source table types it Overlap; its text: 'while the GDPR articulates transparency and oversight as individual rights, the AI Act frames them as organisational responsibilities embedded in product design'. A claim about two regimes as wholes, spanning 7 articles; individual article pairs are expected to come out compatible."),
    "INC-0008": dict(label="legal_tension", label_subtype="sensitive_data_tension", label_scope="pairwise",
        expected_detector="C1 graded tension pass",
        label_rationale="Source types it Inconsistency, but the conflict runs GDPR-prohibits / AI-Act-permits, and GDPR's own exception list admits a Union-law basis (9(2)(g)); the EDPB-EDPS read AI Act 10(5) as exactly such a case (JO 1/2026 para 16). A permission leaning on an exception the text does not establish -- a tension, not a hard contradiction."),
    "INC-0009a": dict(label="overlap", label_subtype="risk_management", label_scope="pairwise",
        expected_detector="C2", label_rationale="Source: risk-management systems 'which would apply cumulatively'."),
    "INC-0009b": dict(label="overlap", label_subtype="incident_reporting", label_scope="pairwise",
        expected_detector="C2", label_rationale="Source: 'potentially cumulative reporting duties'."),
}

# What the SOURCE STUDY itself calls each dev item (Annex Table 3 for GDPR, Table 10 for NIS2, printed
# page numbers), plus which pipeline change each item motivated and where the pipeline is expected to
# surface it. This is the part the dev file did not record.
DEV_ANNOTATIONS = {
    "INC-0001": dict(source_type="Inconsistency", source_ref="Annex Table 3 row 1, p. 93",
        shaped_fixes=[], expected_path="not reachable: GDPR 6(1)(f) has no extracted comparable norm and the study's argument is indirect (AI Act 'recalibrates' the balancing test); recitals are not in the corpus"),
    "INC-0002": dict(source_type="Overlap", source_ref="Annex Table 3 row 2, p. 93",
        shaped_fixes=["C2 impact-assessment keyword family"], expected_path="C2 duplication (RELATED_BURDEN)"),
    "INC-0003": dict(source_type="not strictly an inconsistency", source_ref="body text p. 35: 'it is not strictly a legal inconsistency but the product of two different regulatory logics'",
        shaped_fixes=["C1 risk-classification keyword signal"], expected_path="regime-level; no pairwise expectation"),
    "INC-0004": dict(source_type="not strictly an inconsistency", source_ref="body text p. 35 (same passage as INC-0003); p. 38 'functionally distinct'",
        shaped_fixes=["definitional_mismatch detector"], expected_path="definitional_mismatch"),
    "INC-0005": dict(source_type="Overlap", source_ref="Annex Table 3 row 4, p. 93",
        shaped_fixes=["C2 documentation keyword family"], expected_path="C2 duplication"),
    "INC-0006": dict(source_type="Overlap", source_ref="Annex Table 3 row 4, p. 93; body p. 36 ('interpretative challenges', 'uncertainty as to which benchmarks')",
        shaped_fixes=[], expected_path="regime-level; DIVERGENT_STANDARD would be the only pairwise fit"),
    "INC-0007": dict(source_type="Overlap", source_ref="Annex Table 3 row 3, p. 93",
        shaped_fixes=["automated-decision/oversight signal", "data-subject-right addressee rule"], expected_path="regime-level; no pairwise expectation"),
    "INC-0008": dict(source_type="Inconsistency", source_ref="Annex Table 3 row 5, p. 94",
        shaped_fixes=["paragraph-context fix (GDPR art. 9(2))", "graded second pass"], expected_path="legal_tension: PERMISSION_CONFLICT",
        counter_reading="EDPB-EDPS Joint Opinion 1/2026, para 16: per Recital 70 AI Act, art. 10(5) 'regulate[s] a specific case of processing of special categories of personal data as a matter of substantial public interest within the meaning of Article 9(2)(g) GDPR', so all GDPR conditions fully apply. The regulators treat it as reconcilable by design; the study calls it an 'uneasy tension'. Label 'contested', not 'contradiction'."),
    "INC-0009a": dict(source_type="Overlap", source_ref="Annex Table 10 row 1, p. 101",
        shaped_fixes=["NIS2 effective-addressee rule"], expected_path="C2 duplication"),
    "INC-0009b": dict(source_type="Overlap", source_ref="Annex Table 10 row 2, p. 101",
        shaped_fixes=["NIS2 effective-addressee rule"], expected_path="C2 duplication"),
}

HELDOUT = [
    dict(id="H-001", source=STUDY + " -- Annex Table 3 row 6, p. 94; body p. 37", same_source_as_dev=True,
         provisions={"32024R1689": ["12", "19", "26"], "32016R0679": ["15", "16", "17", "18", "19", "20", "21", "22"]},
         source_type="Gap",
         quote="Controllers remain bound to honour requests they cannot technically fulfil, while providers are required to preserve evidence for conformity.",
         scope_note="A 'gap' (no rule reconciles the two) is not a pairwise duty conflict; it could only surface as GOAL_TENSION. Whether gaps are in scope for C1 is a team decision. Note GDPR art. 17(3)(b) (retention for a legal obligation) may itself reconcile erasure and log retention -- for the labeller to judge."),
    dict(id="H-002", source=STUDY + " -- Annex Table 3 row 7, p. 94; body pp. 37-38", same_source_as_dev=True,
         provisions={"32024R1689": ["57", "58", "59"], "32016R0679": ["6", "9"]},
         source_type="Gap",
         quote="AI sandboxes permit testing but do not relax GDPR obligations, especially with sensitive data.",
         scope_note="The study's reasoning relies on Recital 140 AI Act (substantial public interest under GDPR 9(2)(g)); recitals are NOT in the corpus, so the pipeline cannot see it."),
    dict(id="H-003", source="EDPB-EDPS Joint Opinion 1/2026 on the Digital Omnibus on AI (adopted 20 Jan 2026), footnotes 26-27 and para 27", same_source_as_dev=False,
         provisions={"32024R1689": ["57"], "32016R0679": ["58", "83"]},
         source_type="limit on another law's enforcement power",
         quote="if a national DPA is actively involved in the supervision of the AI system in the sandbox and provided guidance for compliance with respect to the GDPR, no administrative fines can be imposed under the GDPR, pursuant to Article 57(12) AI Act.",
         scope_note="Independent of the dev set (different document, authors, and a description of CURRENT text, not a proposal). A competence-level interaction: an AI Act provision constraining GDPR fining powers."),
    dict(id="H-004", source=STUDY + " -- Annex Table 3 row 8, p. 94", same_source_as_dev=True,
         provisions={"32024R1689": ["64", "65", "66", "67", "68", "69", "70", "74"], "32016R0679": [str(n) for n in range(51, 77)]},
         source_type="Inconsistency",
         quote="GDPR centralises oversight (one-stop-shop, EDPB), AI Act decentralises it (Member State authorities, AI Office), creating a risk of parallel investigations and inconsistent remedies.",
         scope_note="Chapter-level (governance) claim; measure at chapter level, not article-pair level."),
]


def build_dev():
    raw = json.loads((DATA / "real_conflicts").read_text(encoding="utf-8"))["inconsistencies"]
    seen, out = Counter(), []
    for e in raw:
        eid = e["id"]
        seen[eid] += 1
        if eid == "INC-0009":
            eid = "INC-0009a" if e["reference_a"]["article"] == "21" else "INC-0009b"
        ann = DEV_ANNOTATIONS[eid]
        hc = e.get("human_check", e.get("human,_check"))
        item = {k: v for k, v in e.items() if k not in ("id", "human,_check", "human_check")}
        ann = {k: v for k, v in ann.items() if k != "expected_path"}
        item.update(id=eid, human_check=hc, original_label=e["conflict_type"], **LABELS_BY_ITEM[eid], **ann)
        out.append(item)
    out[[o["id"] for o in out].index("INC-0007")]["additional_references_from_text"] = [
        "AI Act art. 86 (right to explanation) -- cited in the study's text and in this item's reasoning but omitted from reference_b"]
    contra = [o for o in out if o["original_label"] == "contradiction"]
    meta = {
        "purpose": "Development set: items the pipeline HAS been tuned on. Never use as evidence of generalisation.",
        "derived_from": "data/real_conflicts (left untouched). Fixes: duplicate id INC-0009 -> INC-0009a/b; malformed key 'human,_check' -> 'human_check'; corrected label/label_subtype/expected_detector added; the file's own conflict_type kept as original_label.",
        "source_study": STUDY,
        "label_classes": {
            "hard_contradiction": "same act permitted/required by one provision and forbidden by the other (incl. permission vs. prohibition)",
            "legal_tension": "jointly satisfiable but an open legal question; label_subtype names which kind",
            "overlap": "the same burden imposed twice (C2's job)",
            "conceptual_mismatch": "the same concept defined or allocated differently (definitional detector)"},
        "key_finding": (f"The source file called {len(contra)} items 'contradiction'. Under the corrected labels they are "
                        f"{dict(Counter(o['label'] for o in contra))}: only one is a hard contradiction, and it is a "
                        f"permission-vs-prohibition case the strict duty-conflict test is structurally unable to see."),
        "additional_authoritative_confirmations": [
            {"pair": "GDPR 33 vs NIS2 23 vs DORA 19 (breach/incident notification deadlines)", "source": "EDPB-EDPS Joint Opinion 2/2026, para 79",
             "quote": "shorter deadlines apply under other reporting obligations, namely: NIS2 Directive (24 or 72 hours depending on the obligation), DORA (24 or 72 hours depending on the obligation), eIDAS Regulation (24 hours) and CER Directive (24 hours)",
             "note": "the same anchor cluster the pipeline was built around -- dev-side, not independent"},
            {"pair": "ePrivacy Directive art. 4 (Telecommunicatiewet 11.3/11.3a) vs GDPR 32-34 / NIS2 21-23", "source": "EDPB-EDPS Joint Opinion 2/2026, para 118",
             "quote": "The EDPB and the EDPS welcome such deletion to avoid overlap with other legal instruments.",
             "note": "the same overlap the pipeline already reports as its Telecommunicatiewet/GDPR duplication findings -- dev-side"}],
        "coverage_limits": ["Corpus contains articles only -- no recitals in any source.", "DORA is almost uncovered by the sources found (2 mentions in the study)."],
        "label_counts": dict(Counter(o["label"] for o in out)),
    }
    return {"_meta": meta, "items": out}


def build_heldout():
    return {"_meta": {
        "status": "CANDIDATES ONLY -- not yet a usable held-out set. Every label is a draft; label_status must be set to 'verified' by someone with legal training before any item counts.",
        "yield": f"{len(HELDOUT)} candidates. Authoritative sources checked: the study's full annex (Tables 3-11), EDPB-EDPS Joint Opinions 1/2026 and 2/2026. Nearly everything in those documents either duplicates the dev set or comments on PROPOSED amendments rather than current text, so it was excluded.",
        "not_available_yet": "EDPB-Commission joint guidelines on GDPR/AI Act interplay (announced, unpublished as of the latest reports found) -- the best future source.",
        "rules": "Never tune on these. Evaluate once per release; an item that changes the pipeline becomes dev data."},
        "items": [dict(i, label_status="draft_unverified", verified_by=None, label=None, comment=None,
                       label_options=LABELS) for i in HELDOUT]}


def build_sample(client, records):
    import detect_c1_legal_tension as lt
    pair_map = lt.build_pair_map(client, records)
    graded = set(lt.final_verdicts())
    rng = random.Random(SEED)
    pids = sorted(pair_map)
    uniform = rng.sample(pids, 30)
    xpool = [p for p in pids if p not in graded and p not in set(uniform)
             and pair_map[p][0].instrument_id != pair_map[p][1].instrument_id
             and "PROHIBITION" in (pair_map[p][0].norm["deontic"], pair_map[p][1].norm["deontic"])
             and pair_map[p][0].norm["deontic"] != pair_map[p][1].norm["deontic"]]
    xs = rng.sample(xpool, 20)
    rows = []
    for stratum, chosen, pool in (("U_uniform_random", uniform, len(pids)), ("X_unreviewed_prohibition_cross_instrument", xs, len(xpool))):
        for pid in chosen:
            a, b = pair_map[pid]
            rows.append({"sample_id": None, "pair_id": pid, "stratum": stratum,
                         "inclusion_probability": round(len(chosen) / pool, 6),
                         "provision_a": _prov(a), "provision_b": _prov(b),
                         "label": None, "comment": None, "labeler": None})
    rng.shuffle(rows)
    for i, r in enumerate(rows, 1):
        r["sample_id"] = f"S-{i:03d}"
    return rows, pair_map


def _prov(r):
    return {"instrument_id": r.instrument_id, "article": r.article, "paragraph": r.norm.get("number"),
            "heading": r.heading, "deontic": r.norm["deontic"], "text": r.text}


def run_predictions(client, records, rows, pair_map):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    import detect_c1_legal_tension as lt
    from pilot_c1_graded_tension import adjudicate
    defs = c1.load_definitions_by_instrument()
    pti, rri = c1._build_paragraph_text_index(records), c1._build_reverse_reference_index(records)
    cache_c1 = json.loads((DATA / "c1_adjudication_cache.json").read_text(encoding="utf-8"))

    def go(row):
        a, b = pair_map[row["pair_id"]]
        luna = adjudicate(client, lt.LUNA[0], lt.LUNA[1], a, b, defs, pti, rri)
        res = {"luna": luna[0].model_dump(), "luna_recheck": luna[1]}
        flagged = bool(luna[1]) or luna[0].verdict in lt.TENSION_VERDICTS + ("MISSING_CONTEXT",)
        final = luna[0]
        if flagged:
            terra = adjudicate(client, lt.TERRA[0], lt.TERRA[1], a, b, defs, pti, rri)
            res.update(terra=terra[0].model_dump(), terra_recheck=terra[1])
            final = terra[0]
        old = cache_c1.get(f"{c1.DEFAULT_MODEL}::{row['pair_id']}")
        res.update(sample_id=row["sample_id"], final_verdict=final.verdict, escalated=flagged,
                   strict_verdict=(old or {}).get("verdict", {}).get("verdict"))
        return res

    with ThreadPoolExecutor(max_workers=6) as ex:
        preds = list(ex.map(go, rows))
    return preds


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-predictions", action="store_true")
    ap.add_argument("--dev-only", action="store_true",
                    help="rebuild dev_set.json only; never touches the frozen blind sample (a full run without "
                         "--run-predictions would rewrite label_sample_blind.json without its predictions hash)")
    args = ap.parse_args()
    GOLD.mkdir(exist_ok=True)

    dev = build_dev()
    (GOLD / "dev_set.json").write_text(json.dumps(dev, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"dev_set.json: {len(dev['items'])} items; labels: {dev['_meta']['label_counts']}")
    print("  " + dev["_meta"]["key_finding"])
    if args.dev_only:
        return
    (GOLD / "heldout_candidates.json").write_text(json.dumps(build_heldout(), ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"heldout_candidates.json: {len(HELDOUT)} candidates (draft, unverified)")

    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from openai import OpenAI
    client = OpenAI()
    records = c1.load_all_norm_records()
    rows, pair_map = build_sample(client, records)
    sample = {"_meta": {"purpose": "Blind expert labelling of pipeline candidate pairs -- estimates how often the graded pass's blind spot hides a real tension, and how often flagged tensions are right.",
                        "instructions": "For each pair choose ONE label from label_options. Judge from the two provision texts (and your legal knowledge) only; you have not been shown any model output. Use CANNOT_TELL freely. 'Tension' classes need a realistic scenario in comment.",
                        "label_options": LABELS,
                        "strata": Counter(r["stratum"] for r in rows) and dict(Counter(r["stratum"] for r in rows)),
                        "weighting": "stratum inclusion probabilities are recorded per row; weight by 1/inclusion_probability for population estimates",
                        "hold_out_rule": "never tune on this file"},
              "items": rows}
    if args.run_predictions:
        preds = run_predictions(client, records, rows, pair_map)
        pred_bytes = json.dumps(preds, ensure_ascii=False, indent=1).encode("utf-8")
        (GOLD / "label_sample_predictions_frozen.json").write_bytes(pred_bytes)
        sample["_meta"]["predictions_sha256"] = hashlib.sha256(pred_bytes).hexdigest()
        sample["_meta"]["predictions_frozen_before_labels"] = True
        dist = Counter(p["final_verdict"] for p in preds)
        print(f"predictions frozen: {dict(dist)}; escalated to terra: {sum(p['escalated'] for p in preds)}")
    (GOLD / "label_sample_blind.json").write_text(json.dumps(sample, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"label_sample_blind.json: {len(rows)} pairs, strata {sample['_meta']['strata']}")


if __name__ == "__main__":
    main()
