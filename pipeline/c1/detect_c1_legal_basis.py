# -*- coding: utf-8 -*-
"""
C1 legal-basis path (2026-09-29): permission-vs-prohibition contradictions that the strict duty-conflict
test is structurally unable to see.

Why a separate path. The strict test asks "can an actor subject to both obey both?". For a PERMISSION
against a PROHIBITION the answer is always yes -- refrain -- so it can never fire, however directly the
two collide. Yet a permission and a prohibition of the same act are contradictory in the deontic sense
(P(x) and F(x) cannot both hold). The one dev-set item the source study types a real inconsistency on
this pattern, INC-0001 (GDPR art. 6(1)(f) vs. the AI Act), says it plainly: "even if the balancing would
favour the data controller under GDPR, the AI Act may still restrict the processing".

It was also unreachable for a second, independent reason: GDPR art. 6(1) -- all six legal bases -- was
never extracted as a norm (only 6(2) and 6(4) were). The six bases are parsed here, deterministically and
verbatim, from the paragraph's own lettered list, rather than re-extracted by an LLM.

Verdicts (strict -- a new category built after two over-triggering categories on 2026-09-28, so the
guards against "cumulative requirements" and "avoidable duties" are carried over):
  BASIS_DEFEATED     the other provision prohibits processing (or a use of it) for which this basis would,
                     on its own terms, be available -- a permission/prohibition contradiction.
                     Finding subtype: permission_prohibition_conflict.
  BASIS_CONDITIONED  no prohibition, but the other provision changes whether this basis's OWN test
                     (necessity, the legitimate-interest balancing) is met. Finding subtype:
                     legal_basis_conditioned (a legal-tension-grade finding, never a contradiction).
  NO_INTERACTION     unrelated, cumulative, or already excluded by the basis's own conditions.
  INSUFFICIENT_EVIDENCE

Candidates for a basis: every AI Act PROHIBITION addressed to regulated entities, every AI Act
risk-management / human-oversight / impact-assessment duty (the safeguards the source says
"recalibrate" the balancing), and a seeded random CONTROL of unrelated AI Act duties that should come out
NO_INTERACTION -- the false-positive check.

    python detect_c1_legal_basis.py --dry-run
    python detect_c1_legal_basis.py                      # basis (f) only, the dev-set case
    python detect_c1_legal_basis.py --bases a,b,c,d,e,f  # all six
    python detect_c1_legal_basis.py --append-findings-only
"""
import argparse
import json
import random
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Literal, Optional

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
_HERE = Path(__file__).resolve().parent
_PIPELINE = _HERE.parent
for _d in (_PIPELINE, _PIPELINE / "c1", _PIPELINE / "preprocessing", _PIPELINE / "c2"):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))
import detect_c1_contradiction as c1
from pydantic import BaseModel

ROOT, DATA = c1.ROOT, c1.DATA
CACHE_PATH = DATA / "cache" / "c1_legal_basis_cache.json"
# lb_v2 (2026-09-29): lb_v1's first run was reviewed pair by pair before trusting it, and two defects were
# found. (1) The AI Act side was shown only as its paragraph text; art. 5(1)'s eight prohibited practices
# share one paragraph, so the model argued about whichever point fitted best (norms for 5(1)(a), (d), (e),
# (g), (h) all came back with emotion-recognition or scraping scenarios) -- the specific norm is now pinned.
# (2) GDPR art. 6 alone hides that special-category data (biometrics, health, ethnicity) also needs an
# art. 9(2) exception, so cases GDPR itself already forbids -- where the two laws AGREE -- read as
# "defeated"; art. 9(1)-(2) is now shown as context for the basis.
VERSION = "lb_v2"
MODEL, EFFORT = "gpt-5.6-terra", "medium"
SEED = 20260929
N_CONTROL = 12
# Only BASIS_DEFEATED becomes a finding. BASIS_CONDITIONED stays a verdict (it gives the model somewhere to
# put "relevant but not prohibiting" other than DEFEATED) but is not emitted: in lb_v1 it fired on 1/19
# targeted safeguard duties and 1/12 random controls -- no better than chance, so it is not a signal.
SUBTYPES = {"BASIS_DEFEATED": "permission_prohibition_conflict"}
REPORTED = ("BASIS_DEFEATED", "BASIS_CONDITIONED")
# GDPR provisions that apply alongside a legal basis and change what it permits (shown as context, verbatim).
QUALIFYING_PROVISIONS = {("32016R0679", "6"): [("32016R0679", "9", (0, 1))]}
# instrument, article, list position of the paragraph holding the lettered legal-basis list
LEGAL_BASIS_SOURCES = [("32016R0679", "6", 0)]
SAFEGUARD_RE = re.compile(r"risicobeheer|menselijk toezicht|effectbeoordeling|grondrechten|toezichtmaatregelen", re.I)
_POINT_RE = re.compile(r"(?m)^([a-z])\)\s*(.+?)\s*$")


def load_legal_basis_records() -> list:
    out = []
    for inst, art, pidx in LEGAL_BASIS_SOURCES:
        path = next(p for p, _ in c1.SOURCES if inst in p)
        root = json.loads((ROOT / path).read_text(encoding="utf-8"))
        prov = next(p for p, get in [(p, g) for p0, g in c1.SOURCES if p0 == path for p in g(root)]
                    if str(p.get("article")) == art)
        para = prov["paragraphs"][pidx]
        for letter, point in _POINT_RE.findall(para["text"]):
            norm = {"deontic": "PERMISSION", "kind": "LEGAL_BASIS", "legal_basis": letter,
                    "number": str(para.get("number")), "paragraph_index": pidx, "norm_index": f"basis_{letter}",
                    "addressee": "de verwerkingsverantwoordelijke", "addressee_type": "REGULATED_ENTITY",
                    "action": point, "trigger_event": None, "conditions": [], "deference": None}
            out.append(c1.NormRecord(norm, prov, inst, art, prov.get("heading") or f"Artikel {art}",
                                     para["text"], c1._graph_uid(prov, inst)))
    return out


class LegalBasisVerdict(BaseModel):
    permitted_processing: str
    restricted_act: str
    same_actor: bool
    actor_note: str
    does_basis_own_test_absorb_b: str
    strongest_case_for_conflict: str
    strongest_case_for_no_conflict: str
    verdict: Literal["BASIS_DEFEATED", "BASIS_CONDITIONED", "NO_INTERACTION", "INSUFFICIENT_EVIDENCE"]
    concrete_scenario: Optional[str]
    affected_element_of_basis: Optional[str]
    evidence_span_a: Optional[str]
    evidence_span_b: Optional[str]
    confidence: float


def qualifying_context(a) -> str:
    out = []
    for inst, art, pidxs in QUALIFYING_PROVISIONS.get((a.instrument_id, a.article), []):
        path = next(p for p, _ in c1.SOURCES if inst in p)
        root = json.loads((ROOT / path).read_text(encoding="utf-8"))
        get = next(g for p0, g in c1.SOURCES if p0 == path)
        prov = next(p for p in get(root) if str(p.get("article")) == art)
        for i in pidxs:
            para = prov["paragraphs"][i]
            out.append(f"  {inst} art. {art}, lid {para.get('number')}: {para['text']}")
    if not out:
        return ""
    return ("\nOther provision(s) of the same instrument that apply alongside A's basis (context, verbatim -- "
            "decide yourself whether they bear on this pair):\n" + "\n".join(out) + "\n")


def _issue(v: LegalBasisVerdict, a, b) -> Optional[str]:
    if not (0.0 <= v.confidence <= 1.0):
        return f"confidence {v.confidence} outside [0, 1]"
    if v.verdict in REPORTED:
        if not v.concrete_scenario:
            return f"verdict={v.verdict} but no concrete_scenario"
        if not v.evidence_span_b:
            return f"verdict={v.verdict} but no evidence_span_b quoted from provision B"
    if v.verdict == "BASIS_DEFEATED" and not v.same_actor:
        return "verdict=BASIS_DEFEATED but same_actor=false -- a prohibition on a different actor cannot defeat this actor's legal basis"
    if v.verdict == "BASIS_CONDITIONED":
        if not v.affected_element_of_basis:
            return "verdict=BASIS_CONDITIONED but affected_element_of_basis is empty (quote the part of the basis's own test B bears on)"
        if not c1._verbatim_in(v.affected_element_of_basis, a.text):
            return "affected_element_of_basis is not a verbatim substring of the legal-basis text"
    for span, rec, label in ((v.evidence_span_a, a, "a"), (v.evidence_span_b, b, "b")):
        if span and not c1._verbatim_in(span, rec.text):
            return f"evidence_span_{label} is not a verbatim substring of provision {label.upper()}'s text"
    return None


def build_prompt(a, b, ctx, retry_note=None):
    note = f"\nYour previous response was rejected: {retry_note}. Fix that specific problem.\n" if retry_note else ""
    return (
        "Provision A is a LEGAL BASIS: a condition under which processing of personal data is lawful (a "
        f"permission). The basis under examination is point {a.norm['legal_basis']}) of the list below; the other "
        "points and the closing sentence are shown because they qualify it. Provision B is from another "
        "instrument. Question: does B take away, or change, what A's basis permits?\n\n"
        "Verdicts:\n"
        "- BASIS_DEFEATED: B prohibits processing (or a use of it) for which A's basis would, ON ITS OWN TERMS, "
        "be available to the same actor -- so an activity lawful under A alone is forbidden under B. This is a "
        "permission/prohibition contradiction even though the actor can comply by refraining. Requires: the "
        "same actor can be A's controller and B's addressee (same_actor=true); a realistic scenario in which A's "
        "own conditions are plausibly met (necessity; for a balancing test, a balancing that could plausibly "
        "favour the controller; not excluded by A's own closing sentence) and B nonetheless prohibits.\n"
        "- BASIS_CONDITIONED: B does not prohibit, but it changes whether A's OWN test is met -- e.g. B's "
        "requirements become factors that decide A's necessity or balancing. Quote, verbatim from A, the element "
        "of A's test that B bears on (affected_element_of_basis).\n"
        "- NO_INTERACTION: B is about a different activity or actor; OR B is simply an additional duty the "
        "controller must ALSO meet (cumulative requirements are compatible, not a conditioning of A); OR A's own "
        "conditions already exclude the processing in every scenario where B applies, so the two instruments "
        "agree. If A's balancing would necessarily fail wherever B applies, that is agreement -- NO_INTERACTION.\n"
        "- INSUFFICIENT_EVIDENCE: the texts shown do not let you decide.\n\n"
        "Guards, from cases wrongly flagged in earlier runs: do not build a scenario that needs an actor to opt "
        "into a voluntary mechanism, or to hold two unrelated roles the text does not give it. Most pairs are "
        "NO_INTERACTION.\n\n"
        "Method: argue the strongest case for a conflict and the strongest case against (grounded in the texts), "
        "state whether A's own test already absorbs B, THEN decide. Quote evidence_span_a/b verbatim.\n\n"
        "Invented illustrations (not from these laws):\n"
        "  BASIS_DEFEATED: A: processing is lawful if necessary for the controller's legitimate interests unless "
        "the data subject's rights override them. B: using systems that infer employees' emotions at work is "
        "prohibited. A retailer with a documented, narrow, safeguarded purpose could argue A's balancing favours "
        "it; B forbids the use regardless of any balancing.\n"
        "  BASIS_CONDITIONED: same A; B: operators of such systems must assess their impact on fundamental "
        "rights before use. B's assessment is exactly the material A's balancing weighs, so B changes how A's test "
        "comes out without forbidding anything.\n"
        "  NO_INTERACTION: same A; B: providers must keep technical documentation for ten years. An additional "
        "duty, not a change to A's test." + note + "\n\n"
        f"PROVISION A -- {a.instrument_id} art. {a.article}, lid {a.norm['number']}, point {a.norm['legal_basis']}) "
        f"({a.heading}) [LEGAL BASIS]\n{a.text}\n\n"
        f"PROVISION B -- {b.instrument_id} art. {b.article} ({b.heading}) [{b.norm['deontic']}]\n{b.text}\n"
        f"The norm of B under comparison (the paragraph may state several; judge ONLY this one): "
        f"deontic={b.norm['deontic']}, action={b.norm.get('action')!r}, conditions={b.norm.get('conditions') or []}\n"
        + ctx
    )


def adjudicate(client, a, b, ctx, model=MODEL, effort=EFFORT):
    issue, v = None, None
    for _ in range(2):
        kwargs = dict(model=model, input=build_prompt(a, b, ctx, issue), text_format=LegalBasisVerdict,
                      reasoning={"effort": effort})
        if effort == "none":
            kwargs["temperature"] = 0
        for t in range(3):
            try:
                resp = client.responses.parse(**kwargs)
                break
            except Exception as e:
                if t == 2 or "credit" in str(e).lower():
                    raise
                time.sleep(3 * (t + 1))
        v = resp.output_parsed
        issue = _issue(v, a, b)
        if issue is None:
            return v, None
    return v, issue


def build_candidates(records, bases, letters):
    ai = [r for r in records if r.instrument_id == "32024R1689" and "REGULATED_ENTITY" in c1._effective_addressee_types(r)]
    restriction = [r for r in ai if r.norm["deontic"] == "PROHIBITION"]
    safeguard = [r for r in ai if r.norm["deontic"] == "OBLIGATION" and SAFEGUARD_RE.search(r.text)]
    chosen_ids = {id(r) for r in restriction + safeguard}
    rest = [r for r in ai if id(r) not in chosen_ids and r.norm["deontic"] == "OBLIGATION"]
    control = random.Random(SEED).sample(rest, min(N_CONTROL, len(rest)))
    out = {}
    for a in bases:
        if a.norm["legal_basis"] not in letters:
            continue
        for stratum, group in (("restriction", restriction), ("safeguard_duty", safeguard), ("control", control)):
            for b in group:
                out[c1._pair_id(a, b)] = (a, b, stratum)
    return out


def _key(pid):
    return f"{VERSION}::{MODEL}::{EFFORT}::{pid}"


def load_cache():
    return json.loads(CACHE_PATH.read_text(encoding="utf-8")) if CACHE_PATH.exists() else {}


def run(client, records, cands, concurrency):
    defs = c1.load_definitions_by_instrument()
    pti, rri = c1._build_paragraph_text_index(records), c1._build_reverse_reference_index(records)
    cache, lock = load_cache(), threading.Lock()
    todo = [pid for pid in cands if _key(pid) not in cache]
    print(f"{len(cands)} pairs; {len(cands) - len(todo)} cached; running {len(todo)} {MODEL} calls", flush=True)

    def one(pid):
        a, b, stratum = cands[pid]
        ctx = (qualifying_context(a) + c1._definitions_context(a, defs, "A") + c1._definitions_context(b, defs, "B")
               + c1._reference_context(b, pti, rri, "B"))
        v, issue = adjudicate(client, a, b, ctx)
        with lock:
            cache[_key(pid)] = {"verdict": v.model_dump(), "needs_recheck_reason": issue, "stratum": stratum}
            CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        for f in as_completed([ex.submit(one, p) for p in todo]):
            f.result()


def summarise(cands):
    cache = load_cache()
    by = Counter()
    for pid, (a, b, stratum) in cands.items():
        e = cache.get(_key(pid))
        if e:
            by[(stratum, e["verdict"]["verdict"] + (" (recheck)" if e["needs_recheck_reason"] else ""))] += 1
    for stratum in ("restriction", "safeguard_duty", "control"):
        print(f"  {stratum:15s} {dict((k[1], n) for k, n in by.items() if k[0] == stratum)}")
    for verdict in REPORTED:
        rate = {s: (sum(n for (s2, v), n in by.items() if s2 == s and v.split()[0] == verdict),
                    sum(n for (s2, _), n in by.items() if s2 == s)) for s in ("restriction", "safeguard_duty", "control")}
        print(f"  {verdict:18s} " + "  ".join(f"{s} {k}/{t}" for s, (k, t) in rate.items()))


def build_finding(a, b, stratum, entry):
    v = entry["verdict"]
    recheck = entry["needs_recheck_reason"]
    tier = 2 if (v["confidence"] >= 0.8 and not recheck) else 3
    return {
        "finding_id": f"F-C1-LB-{c1._pair_id(a, b)}",
        "category": "contradiction",
        "subtype": SUBTYPES[v["verdict"]],
        "status": "needs_recheck" if recheck else "candidate",
        "confidence_tier": tier,
        "confidence_label": c1.CONFIDENCE_LABELS[tier],
        "confidence_reasons": ["AI judgement (legal-basis path, terra with reasoning) -- never Tier 1"]
                              + (["model expressed high confidence"] if tier == 2 else [])
                              + ([f"needs recheck: {recheck}"] if recheck else []),
        # a permission and a prohibition of the same act cannot both hold, but an actor can always comply
        # with both by refraining -- so "cannot both be obeyed" (this field's meaning elsewhere) stays None.
        "incompatibility_established": None,
        "deontic_contradiction": v["verdict"] == "BASIS_DEFEATED",
        "challenge": None,
        "provisions": [
            {"uid": m.graph_uid, "instrument_id": m.instrument_id, "article": m.article,
             "paragraph_number": m.norm.get("number"), "norm_index": m.norm.get("norm_index"),
             "paragraph_index": m.norm.get("paragraph_index"),
             **({"legal_basis": m.norm["legal_basis"]} if m.norm.get("kind") == "LEGAL_BASIS" else {})}
            for m in (a, b)],
        "criteria_fired": ["legal_basis_path", f"stratum:{stratum}"],
        "deterministic_result": None,
        "llm_adjudication": {**v, "decided_by": f"{MODEL}/{EFFORT}"},
        "report_eligible": False,
    }


def append_findings(cands):
    cache = load_cache()
    path = DATA / "results" / "c1" / "findings_c1.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    stale = set(SUBTYPES.values()) | {"legal_basis_conditioned"}  # lb_v1 also emitted the latter
    data["findings"] = [f for f in data["findings"] if f.get("subtype") not in stale]
    added = Counter()
    for pid, (a, b, stratum) in cands.items():
        e = cache.get(_key(pid))
        if e and e["verdict"]["verdict"] in SUBTYPES:
            data["findings"].append(build_finding(a, b, stratum, e))
            added[f"{SUBTYPES[e['verdict']['verdict']]} ({stratum})"] += 1
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"appended legal-basis findings: {dict(added)}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bases", default="f")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--append-findings-only", action="store_true")
    ap.add_argument("--no-append", action="store_true", help="adjudicate and summarise, leave findings_c1.json alone")
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args()
    letters = set(args.bases.split(","))
    records = c1.load_all_norm_records()
    bases = load_legal_basis_records()
    cands = build_candidates(records, bases, letters)
    print(f"legal bases parsed: {[b.norm['legal_basis'] for b in bases]}; examining {sorted(letters)}; "
          f"{len(cands)} candidate pairs {dict(Counter(s for _, _, s in cands.values()))}", flush=True)
    if args.dry_run:
        return
    if not args.append_findings_only:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
        from openai import OpenAI
        run(OpenAI(), records, cands, args.concurrency)
        summarise(cands)
    if not args.no_append:
        append_findings(cands)


if __name__ == "__main__":
    main()
