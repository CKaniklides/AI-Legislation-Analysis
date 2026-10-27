#!/usr/bin/env python3
"""pair_review.py - quick pair-level validation of the object filter.

Builds pair_review.xlsx: a stratified sample of norm pairs to judge by hand,
with a Summary sheet whose formulas update as judgements are entered.

Strata
  <band> <relation>  scored pairs, by score band x relation of the deciding key
                     (head, tail, eurovoc, noun); --per-cell pairs each.
  zero same-group    pairs that score 0 although both norms sit in the same
                     article (first --group-parts parts of norm_id). A cheap
                     recall check: related pairs the filter misses show up here.
  known              optional pairs you know are related (--known CSV with
                     columns norm_a,norm_b): where do they score?

What you get from about 130 judgements (roughly an hour)
  - precision per stratum (strict: same; lenient: same or related)
  - precision of all pairs at or above a threshold, weighted by stratum size,
    so the sample stands for the whole pair file
  - precision when EuroVoc or a shared other noun decides (weight questions)
  - two recall proxies (zero same-group, known pairs)
  - relation type per direction flag (does 'same' mean duplicate?)

Scores and keys are in hidden columns of the Review sheet, so pairs are judged
blind; rows are shuffled. Unhide them after judging to inspect errors.

Usage
  python pair_review.py
  python pair_review.py --per-cell 15 --zero-pairs 20 --known known_pairs.csv
  python pair_review.py --norm-text norms.jsonl --text-field text

Reads  <data>/object_pair_scores.jsonl, <data>/norm_keys.jsonl
       optional: --norm-text JSONL (norm_id + text), --known CSV
Writes <data>/pair_review.xlsx
"""
import argparse
import csv
import json
import random
from collections import defaultdict
from itertools import combinations
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

RELATIONS = ["head", "tail", "eurovoc", "noun"]
JUDGEMENTS = ["same", "related", "different", "unclear"]
REL_TYPES = ["duplicate", "contradiction", "lex specialis", "overlap", "none", "unclear"]
DIRECTIONS = ["same", "a_narrower", "b_narrower", "none"]

FONT = Font(name="Arial", size=10)
BOLD = Font(name="Arial", size=10, bold=True)
INPUT = PatternFill("solid", start_color="FFFF00")
HEAD_FILL = PatternFill("solid", start_color="D9D9D9")
WRAP = Alignment(wrap_text=True, vertical="top")


def read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def band_bounds(edges):
    lows = [0.0] + edges
    highs = edges + [1.0]
    return list(zip(lows, highs))


def band_of(score, bounds):
    for lo, hi in bounds:
        if lo <= score < hi or (hi == 1.0 and score >= lo):
            return lo, hi
    return bounds[-1]


def load_inputs(args):
    data = Path(args.data)
    norms = {}
    for r in read_jsonl(data / args.norms):
        units = [u["unit"] for u in r.get("units") or []]
        norms[str(r["norm_id"])] = {"object": r.get("object") or "", "units": units}
    pairs = read_jsonl(data / args.pairs)
    texts = {}
    if args.norm_text:
        for r in read_jsonl(args.norm_text):
            texts[str(r[args.id_field])] = str(r.get(args.text_field) or "")[:args.text_chars]
    known = []
    if args.known:
        with open(args.known, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                a, b = sorted((str(r["norm_a"]).strip(), str(r["norm_b"]).strip()))
                known.append((a, b))
    return norms, pairs, texts, known


def build_sample(args, norms, pairs, known):
    rng = random.Random(args.seed)
    bounds = band_bounds(args.bands)
    by_pair = {(p["norm_a"], p["norm_b"]): p for p in pairs}

    # scored strata
    strata = defaultdict(list)
    for p in pairs:
        lo, hi = band_of(p["score"], bounds)
        strata[(lo, hi, p["relation"])].append(p)
    info, rows = [], []
    for (lo, hi), rel in [(b, r) for b in bounds for r in RELATIONS]:
        pop = strata.get((lo, hi, rel), [])
        if not pop:
            continue
        name = f"{lo:.2f}-{hi:.2f} {rel}"
        info.append({"name": name, "from": lo, "relation": rel, "population": len(pop)})
        for p in rng.sample(pop, min(args.per_cell, len(pop))):
            rows.append(dict(p, stratum=name))

    # zero-score pairs within the same group (article)
    groups = defaultdict(list)
    for n in sorted(norms):
        if norms[n]["units"]:
            groups[":".join(n.split(":")[:args.group_parts])].append(n)
    zero = [(a, b) for ns in groups.values() for a, b in combinations(ns, 2)
            if (a, b) not in by_pair]
    if zero and args.zero_pairs:
        info.append({"name": "zero same-group", "from": None, "relation": "",
                     "population": len(zero)})
        for a, b in rng.sample(zero, min(args.zero_pairs, len(zero))):
            rows.append({"norm_a": a, "norm_b": b, "score": 0.0, "stratum": "zero same-group"})

    # known related pairs
    if known:
        info.append({"name": "known", "from": None, "relation": "", "population": len(known)})
        for a, b in known:
            rows.append(dict(by_pair.get((a, b), {"norm_a": a, "norm_b": b, "score": 0.0}),
                             stratum="known"))

    rng.shuffle(rows)
    return info, rows


def write_workbook(path, args, norms, texts, info, rows):
    wb = Workbook()

    # ---------------- Review ----------------
    ws = wb.active
    ws.title = "Review"
    cols = [("#", 5), ("Norm A", 22), ("Object A", 45), ("Matched unit A", 30),
            ("Norm B", 22), ("Object B", 45), ("Matched unit B", 30)]
    if texts:
        cols += [("Text A", 60), ("Text B", 60)]
    cols += [("Same thing?", 13), ("Relation type", 15), ("Note", 30)]
    hidden = [("Stratum", 18), ("Score", 8), ("Key relation", 11), ("Key", 18),
              ("Spec", 8), ("Direction", 11)]
    cols += hidden
    col = {name: get_column_letter(i + 1) for i, (name, _) in enumerate(cols)}
    for i, (name, width) in enumerate(cols, 1):
        c = ws.cell(row=1, column=i, value=name)
        c.font, c.fill, c.alignment = BOLD, HEAD_FILL, WRAP
        ws.column_dimensions[get_column_letter(i)].width = width
    for name, _ in hidden:
        ws.column_dimensions[col[name]].hidden = True

    for r, p in enumerate(rows, 2):
        a, b = p["norm_a"], p["norm_b"]
        na, nb = norms.get(a, {}), norms.get(b, {})
        values = {
            "#": r - 1, "Norm A": a, "Object A": na.get("object", ""),
            "Matched unit A": p.get("unit_a") or " | ".join(na.get("units", [])),
            "Norm B": b, "Object B": nb.get("object", ""),
            "Matched unit B": p.get("unit_b") or " | ".join(nb.get("units", [])),
            "Text A": texts.get(a, ""), "Text B": texts.get(b, ""),
            "Stratum": p["stratum"], "Score": p.get("score", 0.0),
            "Key relation": p.get("relation", ""), "Key": p.get("key", ""),
            "Spec": p.get("spec", ""), "Direction": p.get("direction") or "none",
        }
        for i, (name, _) in enumerate(cols, 1):
            c = ws.cell(row=r, column=i, value=values.get(name))
            c.font, c.alignment = FONT, WRAP
            if name in ("Same thing?", "Relation type", "Note"):
                c.fill = INPUT
    last = len(rows) + 1
    for name, options in (("Same thing?", JUDGEMENTS), ("Relation type", REL_TYPES)):
        dv = DataValidation(type="list", formula1='"' + ",".join(options) + '"', allow_blank=True)
        ws.add_data_validation(dv)
        dv.add(f"{col[name]}2:{col[name]}{last}")
    ws.freeze_panes = "B2"

    def rng_(name):
        return f"Review!${col[name]}$2:${col[name]}${last}"
    STR, JUD, TYP, SCO, DIR = (rng_("Stratum"), rng_("Same thing?"), rng_("Relation type"),
                               rng_("Score"), rng_("Direction"))

    # ---------------- Summary ----------------
    s = wb.create_sheet("Summary")
    s["A1"] = "Object filter: pair review summary"
    s["A1"].font = Font(name="Arial", size=12, bold=True)
    s["A3"], s["B3"] = "Threshold (use a band edge)", args.bands[0]
    s["B3"].fill = INPUT
    s["C3"] = "Change this cell; the estimates below follow."
    head = ["Stratum", "Band from", "Key relation", "Population", "Sampled", "same", "related",
            "different", "unclear", "Judged", "Strict", "Lenient", "Pop (judged)",
            "Pop x strict", "Pop x lenient"]
    for i, h in enumerate(head, 1):
        c = s.cell(row=5, column=i, value=h)
        c.font, c.fill = BOLD, HEAD_FILL
    first = 6
    for k, st in enumerate(info):
        r = first + k
        s.cell(row=r, column=1, value=st["name"])
        s.cell(row=r, column=2, value=st["from"])
        s.cell(row=r, column=3, value=st["relation"])
        s.cell(row=r, column=4, value=st["population"])
        s.cell(row=r, column=5, value=f"=COUNTIF({STR},$A{r})")
        for j, jv in enumerate(JUDGEMENTS):
            s.cell(row=r, column=6 + j, value=f'=COUNTIFS({STR},$A{r},{JUD},"{jv}")')
        s.cell(row=r, column=10, value=f"=F{r}+G{r}+H{r}")
        s.cell(row=r, column=11, value=f'=IF(J{r}=0,"",F{r}/J{r})')
        s.cell(row=r, column=12, value=f'=IF(J{r}=0,"",(F{r}+G{r})/J{r})')
        s.cell(row=r, column=13, value=f"=IF(J{r}=0,0,D{r})")
        s.cell(row=r, column=14, value=f"=IF(J{r}=0,0,D{r}*F{r}/J{r})")
        s.cell(row=r, column=15, value=f"=IF(J{r}=0,0,D{r}*(F{r}+G{r})/J{r})")
        for c in (11, 12):
            s.cell(row=r, column=c).number_format = "0%"
    end = first + len(info) - 1
    R = lambda c: f"${c}${first}:${c}${end}"

    r = end + 2
    s.cell(row=r, column=1, value="Estimates (weighted by stratum population)").font = BOLD
    est = [
        ("Scored pairs at or above threshold", f'=SUMIFS({R("D")},{R("B")},">="&$B$3)', None),
        ("Precision at or above threshold, strict",
         f'=IFERROR(SUMIFS({R("N")},{R("B")},">="&$B$3)/SUMIFS({R("M")},{R("B")},">="&$B$3),"")', "0%"),
        ("Precision at or above threshold, lenient",
         f'=IFERROR(SUMIFS({R("O")},{R("B")},">="&$B$3)/SUMIFS({R("M")},{R("B")},">="&$B$3),"")', "0%"),
    ]
    for rel in RELATIONS:
        est.append((f"Lenient precision when '{rel}' decides (all bands)",
                    f'=IFERROR(SUMIFS({R("O")},{R("C")},"{rel}")/SUMIFS({R("M")},{R("C")},"{rel}"),"")',
                    "0%"))
    est += [
        ("Recall proxy: zero same-group pairs judged same or related",
         f'=IFERROR(INDEX({R("L")},MATCH("zero same-group",{R("A")},0)),"")', "0%"),
        ("Recall proxy: known pairs scoring at or above threshold",
         f'=IFERROR(COUNTIFS({STR},"known",{SCO},">="&$B$3)/COUNTIF({STR},"known"),"")', "0%"),
    ]
    for k, (label, formula, fmt) in enumerate(est, 1):
        s.cell(row=r + k, column=1, value=label)
        c = s.cell(row=r + k, column=4, value=formula)
        if fmt:
            c.number_format = fmt

    r = r + len(est) + 2
    s.cell(row=r, column=1, value="Relation type by direction flag (counts)").font = BOLD
    for j, t in enumerate(REL_TYPES):
        c = s.cell(row=r + 1, column=2 + j, value=t)
        c.font, c.fill = BOLD, HEAD_FILL
    for i, d in enumerate(DIRECTIONS):
        s.cell(row=r + 2 + i, column=1, value=d)
        for j, t in enumerate(REL_TYPES):
            s.cell(row=r + 2 + i, column=2 + j, value=f'=COUNTIFS({DIR},$A{r + 2 + i},{TYP},"{t}")')

    s.column_dimensions["A"].width = 58
    for c in "BCDEFGHIJKLMNO":
        s.column_dimensions[c].width = 12
    for row in s.iter_rows():
        for c in row:
            if c.font != BOLD and not (c.font and c.font.size == 12):
                c.font = FONT

    # ---------------- Instructions ----------------
    g = wb.create_sheet("Instructions")
    lines = [
        ("How to judge", True),
        ("Fill in the yellow cells of the Review sheet, one row per pair. Judge only the objects.", False),
        ("Same thing?  same: both norms are about the same thing.", False),
        ("             related: overlapping, or one is a part, kind or case of the other.", False),
        ("             different: not about the same thing.  unclear: cannot tell (not counted).", False),
        ("Relation type (optional, from the full norms): duplicate, contradiction, lex specialis,", False),
        ("             overlap (related, but no conflict or repetition), none, unclear.", False),
        ("Score, key and stratum are in hidden columns (right of Note): unhide them after judging.", False),
        ("Strict precision counts 'same'; lenient counts 'same' or 'related'.", False),
        ("With about 10 pairs per stratum each figure is rough (about +/-15 points); the weighted", False),
        ("estimates combine strata and are more stable. Sample sizes: Summary, column E.", False),
        ("", False),
        ("Example (format only; not part of the sample)", True),
        ("Object A: de risicobeoordeling | Object B: een beoordeling van de risico's", False),
        ("Same thing? same | Relation type: duplicate | Note: same assessment, other wording", False),
        ("", False),
        (f"Settings: per cell {args.per_cell}, zero same-group {args.zero_pairs}, "
         f"bands {args.bands}, group parts {args.group_parts}, seed {args.seed}", False),
    ]
    for i, (text, bold) in enumerate(lines, 1):
        g.cell(row=i, column=1, value=text).font = BOLD if bold else FONT
    g.column_dimensions["A"].width = 100

    wb.save(path)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--data", default="data/conflict/object")
    p.add_argument("--pairs", default="object_pair_scores.jsonl")
    p.add_argument("--norms", default="norm_keys.jsonl")
    p.add_argument("--out", default="pair_review.xlsx")
    p.add_argument("--per-cell", type=int, default=10, help="pairs per band x relation")
    p.add_argument("--bands", type=float, nargs="+", default=[0.5, 0.75],
                   help="inner band edges; the first is the default threshold")
    p.add_argument("--zero-pairs", type=int, default=15, help="zero-score same-group pairs")
    p.add_argument("--group-parts", type=int, default=2,
                   help="norm_id parts (split on ':') that define a group, e.g. CELEX:article")
    p.add_argument("--known", default=None, help="CSV with columns norm_a,norm_b")
    p.add_argument("--norm-text", default=None, help="JSONL with full norm texts")
    p.add_argument("--id-field", default="norm_id")
    p.add_argument("--text-field", default="text")
    p.add_argument("--text-chars", type=int, default=1500)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()
    args.bands = sorted(args.bands)

    norms, pairs, texts, known = load_inputs(args)
    info, rows = build_sample(args, norms, pairs, known)
    out = Path(args.data) / args.out
    write_workbook(out, args, norms, texts, info, rows)
    print(f"{len(rows)} pairs in {len(info)} strata -> {out}")
    for st in info:
        n = sum(1 for r in rows if r["stratum"] == st["name"])
        print(f"  {st['name']:<22} sampled {n:>3} of {st['population']}")


if __name__ == "__main__":
    main()