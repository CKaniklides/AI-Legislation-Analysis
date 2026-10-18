"""
Actor filter, Step 3 validation: review the relations between actor IDs.

  sample  draws pairs of actor IDs with the relation the system gives them and
          writes a review workbook (Instructions, Review, Scores, Labels).
  score   reads the filled workbook: agreement per relation, sign errors,
          missed relations, score error and a confusion matrix.

What is reviewed
  One case is one pair of actor IDs, with the relation the system gives it:
    found     a relation in actor_relations.jsonl (subtype, part_of, sibling, ...)
    different no relation, and both IDs valid in a common document (-0.25)
    unknown   no relation, and the IDs never share a document (0)
  The reviewer chooses the correct relation from the same list of labels.

How cases are drawn (default 40)
  found      16  all found relations are few, so they are spread over the
                 labels (round robin) and checked almost completely
  different  14  drawn with probability proportional to the number of norm
  unknown    10  pairs the ID pair affects, so frequent pairs (Commissie ~
                 lidstaat) are more likely; the plain share of errors in these
                 strata then estimates the share of affected norm PAIRS
  Found relations answer "are the relations right?" (precision). The other two
  strata answer "which relations are missing?" (misses), weighted by impact.

Scores
  agreement        reviewer label = system label (direction included)
  sign agreement   reviewer and system score have the same sign (+, 0, -)
  missed relation  system 'different'/'unknown', reviewer gives a positive label
  score error      mean |system score - score of the reviewer's label|
  each with a 95% Wilson interval in 'score'.

Inputs (in --dir)
  actor_inventory.jsonl, actor_relations.jsonl, norm_actor_map.jsonl
Outputs
  sample: actor_relations_validation.xlsx (or --out)
  score:  printed, and <workbook>.scores.json

Usage
  python actor_relations_validate.py sample --dir data/conflict/actor
  python actor_relations_validate.py score --file data/conflict/actor/actor_relations_validation.xlsx
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import random
from collections import Counter, defaultdict
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.datavalidation import DataValidation

from actor_lexicon import in_scope

# Relation labels as the reviewer sees them, with their scores.
# 'A' and 'B' are the two actors of the row.
LABELS = [
    ("same / equivalent", 1.0, "the same actor, or the same actor under two documents"),
    ("instance: A is B", 0.9, "A is the named body that fills role B (or the reverse)"),
    ("subtype: A is a kind of B", 0.8, "every A is a B"),
    ("subtype: B is a kind of A", 0.8, "every B is an A"),
    ("subtype, two steps: A ... B", 0.6, "A is a kind of a kind of B"),
    ("subtype, two steps: B ... A", 0.6, "B is a kind of a kind of A"),
    ("part of: A in B", 0.5, "A belongs to B (chair of a board, office of the Commission)"),
    ("part of: B in A", 0.5, "B belongs to A"),
    ("can be the same entity", 0.4, "different roles often held by one organisation"),
    ("linked roles", 0.2, "different actors whose roles are tied (controller and processor)"),
    ("sibling", 0.15, "two kinds of the same actor (provider and importer)"),
    ("different", -0.25, "clearly different actors"),
    ("unknown", 0.0, "used by the system only: the IDs never share a document"),
]
REVIEW_LABELS = [l for l, _, _ in LABELS if l != "unknown"] + ["unsure"]
SCORE = {l: s for l, s, _ in LABELS}
SYSTEM_LABEL = {"subtype": "subtype: A is a kind of B", "subtype2": "subtype, two steps: A ... B",
                "part_of": "part of: A in B", "sibling": "sibling", "same": "same / equivalent",
                "equivalent": "same / equivalent", "instance": "instance: A is B",
                "can_be_same": "can be the same entity", "linked": "linked roles"}
QUOTA_40 = {"found": 16, "different": 14, "unknown": 10}
STRATA = list(QUOTA_40)

FONT = "Arial"
HEAD = PatternFill("solid", start_color="D9E1F2")
INPUT = PatternFill("solid", start_color="FFFF00")
SIDE = Side(style="thin", color="BFBFBF")
BORDER = Border(left=SIDE, right=SIDE, top=SIDE, bottom=SIDE)

COLS = [  # header, width; review columns are filled by the reviewer
    ("case", 6), ("stratum", 10), ("documents", 14),
    ("actor A", 26), ("A: definition or source", 50), ("A: as written in norms", 30),
    ("actor B", 26), ("B: definition or source", 50), ("B: as written in norms", 30),
    ("system: relation", 26), ("system: score", 9), ("system: evidence", 36),
    ("norm pairs affected", 10),
    ("REVIEW: correct relation", 28), ("REVIEW: notes", 30),
    ("reviewer score", 9), ("agrees", 8), ("same sign", 8), ("score error", 8),
]
C = {h: i + 1 for i, (h, _) in enumerate(COLS)}
FIRST = 2


def col(h: str) -> str:
    from openpyxl.utils import get_column_letter
    return get_column_letter(C[h])


# --------------------------------------------------------------------------
# Population of ID pairs
# --------------------------------------------------------------------------

def load(d: Path):
    read = lambda n: [json.loads(l) for l in open(d / n, encoding="utf-8") if l.strip()]
    return read("actor_inventory.jsonl"), read("actor_relations.jsonl"), read("norm_actor_map.jsonl")


def population(entries, relations, norms):
    """All pairs of distinct IDs used in the norms, plus every found relation,
    with the system relation and the number of norm pairs each pair affects."""
    by_id = {e["id"]: e for e in entries}
    uses = Counter()
    docs_of = defaultdict(set)
    written = defaultdict(Counter)
    for n in norms:
        ids = {a["actor_id"] for a in n.get("actors", []) if a.get("actor_id")}
        for a in n.get("actors", []):
            if a.get("actor_id"):
                written[a["actor_id"]][a["unit"]] += 1
        for i in ids:
            uses[i] += 1
            docs_of[i].add(n["document"])
    rel = {}
    for r in relations:
        rel[(r["a"], r["b"])] = r
    all_docs = sorted({d for s in docs_of.values() for d in s})

    def common_docs(a, b):
        return [d for d in all_docs if a in by_id and b in by_id
                and in_scope(by_id[a], d) and in_scope(by_id[b], d)]

    pairs = {}
    ids = sorted(uses)
    for a, b in itertools.combinations(ids, 2):
        pairs[(a, b)] = None
    for (a, b) in rel:
        if (a, b) not in pairs and (b, a) not in pairs:
            pairs[(a, b)] = None
    out = []
    for (a, b) in pairs:
        r = rel.get((a, b)) or rel.get((b, a))
        if r:
            a, b = r["a"], r["b"]          # orient as the relation: A rel B
            stratum, label, ev = "found", SYSTEM_LABEL.get(r["relation"], r["relation"]), \
                f"{r['source']}: {r['evidence']}"
            docs = r["documents"]
        else:
            docs = common_docs(a, b)
            stratum = "different" if docs else "unknown"
            label, ev = stratum, ""
        out.append({"a": a, "b": b, "stratum": stratum, "label": label, "evidence": ev,
                    "documents": docs or sorted(docs_of[a] | docs_of[b]),
                    "weight": uses[a] * uses[b]})
    return out, by_id, written


def draw(pop, quota, rng):
    picked = []
    # found: round robin over labels, pairs used in norms first
    found = defaultdict(list)
    for p in pop:
        if p["stratum"] == "found":
            found[p["label"]].append(p)
    for lst in found.values():
        rng.shuffle(lst)
        lst.sort(key=lambda p: p["weight"] == 0)
    take = []
    while len(take) < quota["found"] and any(found.values()):
        for k in sorted(found):
            if found[k] and len(take) < quota["found"]:
                take.append(found[k].pop(0))
    picked += take
    # different / unknown: probability proportional to weight, without replacement
    for s in ("different", "unknown"):
        cand = [p for p in pop if p["stratum"] == s and p["weight"] > 0]
        keyed = sorted(cand, key=lambda p: -rng.random() ** (1.0 / p["weight"]))
        picked += keyed[:quota[s]]
    return picked


# --------------------------------------------------------------------------
# Workbook
# --------------------------------------------------------------------------

def header(ws, row, heads):
    for j, h in enumerate(heads, start=1):
        c = ws.cell(row=row, column=j, value=h)
        c.font, c.fill, c.border = Font(name=FONT, bold=True), HEAD, BORDER
        c.alignment = Alignment(wrap_text=True, vertical="top")


def describe(e: dict | None) -> str:
    if not e:
        return ""
    if e["kind"] == "defined":
        return f"{e['source']}: {e.get('definition') or ''}"[:600]
    extra = f" Aliases: {'; '.join(e['aliases'])}." if e.get("aliases") else ""
    part = f" Part of {e['part_of']}." if e.get("part_of") else ""
    return f"Known actor (not defined), class {e.get('class')}.{extra}{part}"


def write(cases, by_id, written, shares, out: Path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Instructions"
    text = [
        ("Actor relation review", True),
        ("", False),
        ("Each row of the Review sheet is a pair of actors (A and B) and the relation the system "
         "gives them. Choose the correct relation in the yellow column, using the definitions "
         "and how the actors are written in the norms. Judge the legal relation between the "
         "two actors, not whether two particular norms conflict.", False),
        ("The Labels sheet lists every relation with its score. Mind the direction: "
         "'subtype: A is a kind of B' is not the same as 'B is a kind of A'.", False),
        ("Strata: 'found' rows check relations the system found; 'different' and 'unknown' "
         "rows check pairs where it found none, to see which relations are missing.", False),
        ("", False),
        ("Fill in (yellow cells only)", True),
        ("REVIEW: correct relation - one label from the list, or 'unsure'.", False),
        ("REVIEW: notes - for example the article that shows the relation.", False),
        ("The grey columns after the notes are filled in by formulas.", False),
        ("", False),
        ("Example row (format only)", True),
    ]
    for i, (t, b) in enumerate(text, start=1):
        c = ws.cell(row=i, column=1, value=t)
        c.font = Font(name=FONT, bold=b, size=12 if i == 1 else 10)
        c.alignment = Alignment(wrap_text=True, vertical="top")
    r = len(text) + 1
    header(ws, r, [h for h, _ in COLS[:15]])
    ex = ["0", "found", "32016R0679", "32016R0679:betrokken_toezichthoudende_autoriteit", "...",
          "betrokken toezichthoudende autoriteit", "32016R0679:toezichthoudende_autoriteit", "...",
          "toezichthoudende autoriteit", "subtype: A is a kind of B", "0.8", "label: ...", "120",
          "subtype: A is a kind of B", ""]
    for j, v in enumerate(ex, start=1):
        c = ws.cell(row=r + 1, column=j, value=v)
        c.font, c.border = Font(name=FONT, italic=True, color="808080"), BORDER
    ws.column_dimensions["A"].width = 110

    lab = wb.create_sheet("Labels")
    header(lab, 1, ["relation", "score", "meaning"])
    for i, (l, s, m) in enumerate(LABELS + [("unsure", None, "the reviewer cannot decide")],
                                  start=2):
        for j, v in enumerate((l, s, m), start=1):
            lab.cell(row=i, column=j, value=v).font = Font(name=FONT)
    last_label = len(LABELS) + 2
    for letter, w in zip("ABC", (30, 8, 70)):
        lab.column_dimensions[letter].width = w

    rv = wb.create_sheet("Review")
    header(rv, 1, [h for h, _ in COLS])
    for i, (h, w) in enumerate(COLS, start=1):
        rv.column_dimensions[col(h)].width = w
    LBL = f"Labels!$A$2:$A${last_label}"
    SCR = f"Labels!$B$2:$B${last_label}"
    for i, c in enumerate(cases):
        r = FIRST + i
        ea, eb = by_id.get(c["a"]), by_id.get(c["b"])
        wa = ", ".join(u for u, _ in written[c["a"]].most_common(3))
        wb_ = ", ".join(u for u, _ in written[c["b"]].most_common(3))
        vals = {"case": i + 1, "stratum": c["stratum"], "documents": ", ".join(c["documents"]),
                "actor A": c["a"], "A: definition or source": describe(ea),
                "A: as written in norms": wa, "actor B": c["b"],
                "B: definition or source": describe(eb), "B: as written in norms": wb_,
                "system: relation": c["label"], "system: score": SCORE.get(c["label"], 0.0),
                "system: evidence": c["evidence"][:200], "norm pairs affected": c["weight"]}
        for h, v in vals.items():
            rv.cell(row=r, column=C[h], value=v)
        n, s, rs = col("REVIEW: correct relation"), col("system: score"), col("reviewer score")
        sl = col("system: relation")
        rv.cell(row=r, column=C["reviewer score"],
                value=f'=IF(OR({n}{r}="",{n}{r}="unsure"),"",INDEX({SCR},MATCH({n}{r},{LBL},0)))')
        rv.cell(row=r, column=C["agrees"],
                value=f'=IF({rs}{r}="","",IF({n}{r}={sl}{r},1,0))')
        rv.cell(row=r, column=C["same sign"],
                value=f'=IF({rs}{r}="","",IF(SIGN({rs}{r})=SIGN({s}{r}),1,0))')
        rv.cell(row=r, column=C["score error"],
                value=f'=IF({rs}{r}="","",ABS({rs}{r}-{s}{r}))')
        for j in range(1, len(COLS) + 1):
            cell = rv.cell(row=r, column=j)
            cell.font, cell.border = Font(name=FONT), BORDER
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            if j in (C["REVIEW: correct relation"], C["REVIEW: notes"]):
                cell.fill = INPUT
            elif j >= C["reviewer score"]:
                cell.fill = PatternFill("solid", start_color="EDEDED")
    last = FIRST + len(cases) - 1
    dv = DataValidation(type="list", formula1=f'"{",".join(REVIEW_LABELS)}"', allow_blank=True)
    rv.add_data_validation(dv)
    dv.add(f"{col('REVIEW: correct relation')}{FIRST}:{col('REVIEW: correct relation')}{last}")
    rv.freeze_panes = "E2"

    sc = wb.create_sheet("Scores")
    sc["A1"] = "Relation validation scores (update as the Review sheet is filled in)"
    sc["A1"].font = Font(name=FONT, bold=True, size=12)
    rng = lambda h: f"Review!${col(h)}${FIRST}:${col(h)}${last}"
    ST, RS = rng("stratum"), rng("reviewer score")
    AG, SG, SE, SS = rng("agrees"), rng("same sign"), rng("score error"), rng("system: score")
    header(sc, 3, ["stratum", "cases", "reviewed", "agreement", "same sign",
                   "positive per reviewer", "mean score error", "share of all ID pairs"])
    for i, s in enumerate(STRATA):
        r = 4 + i
        sc.cell(row=r, column=1, value=s)
        sc.cell(row=r, column=2, value=f'=COUNTIF({ST},A{r})')
        sc.cell(row=r, column=3, value=f'=COUNTIFS({ST},A{r},{RS},">-2")')
        sc.cell(row=r, column=4, value=f'=IF(C{r}=0,"",SUMIFS({AG},{ST},A{r})/C{r})')
        sc.cell(row=r, column=5, value=f'=IF(C{r}=0,"",SUMIFS({SG},{ST},A{r})/C{r})')
        sc.cell(row=r, column=6, value=f'=IF(C{r}=0,"",COUNTIFS({ST},A{r},{RS},">0")/C{r})')
        sc.cell(row=r, column=7, value=f'=IF(C{r}=0,"",SUMIFS({SE},{ST},A{r})/C{r})')
        sc.cell(row=r, column=8, value=round(shares.get(s, 0), 4)).font = Font(name=FONT, color="0000FF")
    sc.cell(row=7, column=1, value="all")
    sc["B7"], sc["C7"] = "=SUM(B4:B6)", "=SUM(C4:C6)"
    sc["D7"] = f'=IF(C7=0,"",SUM({AG})/C7)'
    sc["E7"] = f'=IF(C7=0,"",SUM({SG})/C7)'
    sc["F7"] = f'=IF(C7=0,"",COUNTIF({RS},">0")/C7)'
    sc["G7"] = f'=IF(C7=0,"",SUM({SE})/C7)'
    sc["A9"] = "Reading the strata"
    notes = [
        "found: agreement = precision of the relations the system found.",
        "different, unknown: 'positive per reviewer' = share of affected norm pairs whose "
        "relation the system misses (pairs were drawn in proportion to norm pairs affected).",
        "same sign: system and reviewer agree on +, 0 or -; a pair the system calls 'unknown' (0) "
        "never has the same sign as a reviewed label, so read 'unknown' by the column F.",
        "share of all ID pairs (blue): share of all ID pairs in each stratum when the sample was "
        "drawn, computed by actor_relations_validate.py.",
    ]
    for i, t in enumerate(notes):
        sc.cell(row=10 + i, column=1, value=t).font = Font(name=FONT)
    sc["A9"].font = Font(name=FONT, bold=True)
    for r in range(4, 8):
        for c in range(1, 8):
            cell = sc.cell(row=r, column=c)
            if c != 8:
                cell.font = Font(name=FONT, bold=(r == 7 and c == 1))
            if c in (4, 5, 6):
                cell.number_format = "0.0%"
            if c == 7:
                cell.number_format = "0.00"
    sc.cell(row=4, column=8).number_format = sc.cell(row=5, column=8).number_format = \
        sc.cell(row=6, column=8).number_format = "0.0%"
    sc.column_dimensions["A"].width = 30
    for letter in "BCDEFGH":
        sc.column_dimensions[letter].width = 14

    wb._sheets = [wb[n] for n in ("Instructions", "Review", "Scores", "Labels")]
    wb.active = 1
    wb.save(out)


def cmd_sample(args):
    entries, relations, norms = load(args.dir)
    pop, by_id, written = population(entries, relations, norms)
    rng = random.Random(args.seed)
    size = max(20, min(50, args.size))
    quota = {s: max(4, round(QUOTA_40[s] * size / 40)) for s in STRATA}
    quota["found"] += size - sum(quota.values())
    cases = draw(pop, quota, rng)
    rng.shuffle(cases)
    shares = Counter(p["stratum"] for p in pop)
    shares = {s: v / len(pop) for s, v in shares.items()}
    out = args.out or args.dir / "actor_relations_validation.xlsx"
    write(cases, by_id, written, shares, out)
    got = Counter(c["stratum"] for c in cases)
    print(f"{len(cases)} cases -> {out}")
    print("population: " + ", ".join(f"{s} {sum(1 for p in pop if p['stratum'] == s)}" for s in STRATA))
    print("sample:     " + ", ".join(f"{s} {got[s]}/{quota[s]}" for s in STRATA))


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def wilson(k, n, z=1.96):
    if n == 0:
        return float("nan"), float("nan")
    p = k / n
    c = p + z * z / (2 * n)
    s = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - s) / (1 + z * z / n)), min(1.0, (c + s) / (1 + z * z / n))


def sign(x):
    return (x > 0) - (x < 0)


def cmd_score(args):
    ws = load_workbook(args.file)["Review"]
    head = [c.value for c in ws[1]]
    ix = {h: i for i, h in enumerate(head)}
    rows = []
    for r in ws.iter_rows(min_row=FIRST, values_only=True):
        if r[ix["case"]] is None:
            continue
        rev = (r[ix["REVIEW: correct relation"]] or "").strip()
        rows.append({"stratum": r[ix["stratum"]], "system": r[ix["system: relation"]],
                     "a": r[ix["actor A"]], "b": r[ix["actor B"]], "review": rev,
                     "s_sys": SCORE.get(r[ix["system: relation"]], 0.0),
                     "s_rev": SCORE.get(rev) if rev in SCORE else None})
    done = [x for x in rows if x["s_rev"] is not None]
    rep = {"cases": len(rows), "reviewed": len(done),
           "unsure": sum(1 for x in rows if x["review"] == "unsure"),
           "open": sum(1 for x in rows if not x["review"])}
    print(f"Cases {rep['cases']}: reviewed {rep['reviewed']}, unsure {rep['unsure']}, "
          f"not filled in {rep['open']}\n")

    def line(name, k, n):
        lo, hi = wilson(k, n)
        print(f"  {name:<34}{k:>3}/{n:<3} {k / n if n else float('nan'):>7.1%}   {lo:.1%} - {hi:.1%}")
        return {"k": k, "n": n, "rate": k / n if n else None, "ci95": [lo, hi]}

    rep["strata"] = {}
    for s in STRATA:
        sub = [x for x in done if x["stratum"] == s]
        if not sub:
            continue
        print(f"{s} ({len(sub)} reviewed)")
        d = {"agreement": line("agreement", sum(x["review"] == x["system"] for x in sub), len(sub)),
             "same_sign": line("same sign", sum(sign(x["s_rev"]) == sign(x["s_sys"]) for x in sub),
                               len(sub)),
             "positive": line("positive per reviewer", sum(x["s_rev"] > 0 for x in sub), len(sub)),
             "score_error": sum(abs(x["s_rev"] - x["s_sys"]) for x in sub) / len(sub)}
        print(f"  {'mean score error':<34}{d['score_error']:.2f}")
        rep["strata"][s] = d
    if "found" in rep["strata"]:
        print(f"\nPrecision of found relations: {rep['strata']['found']['agreement']['rate']:.1%} "
              f"(same sign {rep['strata']['found']['same_sign']['rate']:.1%})")
    miss = [x for x in done if x["stratum"] in ("different", "unknown")]
    if miss:
        k = sum(x["s_rev"] > 0 for x in miss)
        lo, hi = wilson(k, len(miss))
        rep["missed_share_of_norm_pairs"] = {"k": k, "n": len(miss), "rate": k / len(miss),
                                             "ci95": [lo, hi]}
        print(f"Missed relations (share of affected norm pairs, no-relation strata): "
              f"{k}/{len(miss)} = {k / len(miss):.1%} ({lo:.1%} - {hi:.1%})")
    wrong_sign = [x for x in done if x["s_sys"] < 0 < x["s_rev"] or x["s_rev"] < 0 < x["s_sys"]]
    rep["sign_flips"] = [f"{x['a']} ~ {x['b']}: system {x['system']}, reviewer {x['review']}"
                         for x in wrong_sign]
    if wrong_sign:
        print("\nOpposite sign (most harmful):")
        for t in rep["sign_flips"]:
            print("  " + t)
    conf = Counter((x["system"], x["review"]) for x in done)
    rep["confusion"] = {f"{a} -> {b}": v for (a, b), v in conf.most_common()}
    print("\nSystem -> reviewer:")
    for (a, b), v in conf.most_common():
        print(f"  {v:>3}  {a}  ->  {b}")
    out = Path(str(args.file) + ".scores.json")
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n-> {out}")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--dir", type=Path, default=Path("data/conflict/actor"))
    s.add_argument("--size", type=int, default=40, help="20-50")
    s.add_argument("--seed", type=int, default=13)
    s.add_argument("--out", type=Path, default=None)
    c = sub.add_parser("score")
    c.add_argument("--file", type=Path,
                   default=Path("data/conflict/actor/actor_relations_validation.xlsx"))
    args = p.parse_args()
    cmd_sample(args) if args.cmd == "sample" else cmd_score(args)


if __name__ == "__main__":
    main()