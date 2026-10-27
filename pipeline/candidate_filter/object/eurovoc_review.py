"""
Validation of the Step 7 object unit -> EuroVoc mapping, with an EU vs Dutch comparison.

Two steps:
  1. sample  - draw a stratified sample of object-unit occurrences (one unit in one
               norm) plus a census of the most frequent unit texts, and write them to
               an Excel sheet for blind labelling (cascade level, confidence and
               percentile are hidden).
  2. review  - read the filled-in sheet back and report precision per stratum, per
               source (EU / NL) and the EU - NL difference, with 95% intervals.

Design (justified in the workbook's 'Design' sheet)
  Unit of analysis  an object unit as it occurs in a norm, so the estimates describe
                    norm objects, not distinct strings.
  Census            the --census-size most frequent unit texts (default 20) are each
                    judged once and stand for all their occurrences. Chosen by a fixed
                    frequency rule before any judging, never by hand or by quality.
  Strata            source (EU / NL, from hierarchy.instrument_id or the norm_id prefix)
                    x cascade level: phrase, head + compound, embedding low / mid / high
                    (the range above the fallback percentile split in three).
  Allocation        per source: --floor units per cell (default 8), the rest of
                    --per-source (default 50) shared across the embedding bands in
                    proportion to their size. Default total: 20 + 2 x 50 = 120.
  Not in the total  unmapped units and norms whose object was dropped (no unit): their
                    shares are exact counts, shown in 'Design'. Optional small checks
                    (--check-unmapped, --check-dropped) are reported separately.

Labels (as before): correct, partial, wrong, unclear. Strict precision = correct;
lenient precision = correct + partial; unclear is excluded.

Usage
  python eurovoc_review.py sample --records <norm records> [--dir data/conflict/object/eurovoc]
  python eurovoc_review.py sample --records <norm records> --per-source 75 --check-unmapped 10
  python eurovoc_review.py review --xlsx <dir>/mapping_review.xlsx
"""

from __future__ import annotations

import argparse
import json
import math
import random
from collections import Counter, defaultdict
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

FONT = "Arial"
JUDGEMENTS = ["correct", "partial", "wrong", "unclear"]
INPUT_FILL = PatternFill("solid", start_color="FFFF00")
HEADER_FILL = PatternFill("solid", start_color="1F3864")
THIN = Side(style="thin", color="BFBFBF")
WRAP = Alignment(wrap_text=True, vertical="top")
Z = 1.96

SOURCES = ["EU", "NL"]
LEXICAL_CELLS = ["phrase", "head + compound"]
CENSUS = "census"
CHECK_UNMAPPED = "check | unmapped"
CHECK_DROPPED = "check | dropped object"


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------

def _get(obj, dotted: str):
    for key in dotted.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def load_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_records(path: Path) -> list[dict]:
    raw = path.read_text(encoding="utf-8").strip()
    return json.loads(raw) if raw.startswith("[") else \
        [json.loads(l) for l in raw.splitlines() if l.strip()]


def excerpt(paragraph: str | None, obj: str, width: int) -> str:
    """Text around the object in its paragraph; the start of the paragraph if the
    object is not found literally."""
    if not paragraph:
        return ""
    text = " ".join(paragraph.split())
    i = text.lower().find(obj.lower()[:60]) if obj else -1
    if i < 0:
        return text[:2 * width] + ("…" if len(text) > 2 * width else "")
    start, end = max(0, i - width), min(len(text), i + len(obj) + width)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def source_of(norm_id: str, record: dict | None, field: str) -> str:
    """NL for Dutch acts (BWBR identifiers), EU otherwise."""
    inst = _get(record, field) if record else None
    inst = str(inst) if inst else norm_id.split(":", 1)[0]
    return "NL" if inst.upper().startswith("BWBR") else "EU"


def embedding_bands(threshold: float) -> list[tuple[float, float]]:
    step = (100.0 - threshold) / 3
    return [(threshold + i * step, threshold + (i + 1) * step) for i in range(3)]


def band_names(bands) -> list[str]:
    names = ["low", "mid", "high"]
    return [f"emb {n} (p{lo:.0f}-{hi:.0f})" for n, (lo, hi) in zip(names, bands)]


def cell_of(level: str, candidates: list[dict], bands, names) -> str:
    if level == "phrase":
        return "phrase"
    if level in ("head", "compound"):
        return "head + compound"
    pct = (candidates or [{}])[0].get("percentile")
    pct = 0.0 if pct is None else float(pct)
    for (lo, hi), name in zip(bands, names):
        if pct < hi:
            return name
    return names[-1]


def strata_names(names) -> list[str]:
    return [f"{s} | {c}" for s in SOURCES for c in LEXICAL_CELLS + names]


# --------------------------------------------------------------------------
# Statistics (shared with review)
# --------------------------------------------------------------------------

def wilson(p: float, n: float, z: float = Z) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 1.0
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(max(0.0, p * (1 - p) / n + z * z / (4 * n * n))) / d
    return max(0.0, c - h), min(1.0, c + h)


def combine(parts):
    """Weighted proportion from parts (weight, p, var_p, n_items); census parts have
    var_p = 0. Returns (p, lo, hi, n_eff) or None. Wilson on the effective n, capped at
    the number of judged items so 0% / 100% strata cannot fake precision."""
    parts = [x for x in parts if x[0] > 0 and x[3] > 0]
    if not parts:
        return None
    total = sum(w for w, *_ in parts)
    p = sum(w / total * pi for w, pi, _, _ in parts)
    var = sum((w / total) ** 2 * v for w, _, v, _ in parts)
    n_items = sum(n for *_, n in parts)
    n_eff = min(n_items, p * (1 - p) / var) if var > 0 else n_items
    lo, hi = wilson(p, n_eff)
    return p, lo, hi, n_eff


def newcombe(a, b):
    """Difference a - b of two (p, lo, hi, ...) estimates, Newcombe's hybrid interval."""
    d = a[0] - b[0]
    lo = d - math.sqrt((a[0] - a[1]) ** 2 + (b[2] - b[0]) ** 2)
    hi = d + math.sqrt((a[2] - a[0]) ** 2 + (b[0] - b[1]) ** 2)
    return d, lo, hi


# --------------------------------------------------------------------------
# Sampling
# --------------------------------------------------------------------------

def allocate(N: dict[str, int], per_source: int, floor: int, emb_cells: list[str]) -> dict[str, int]:
    """Floor per cell, the rest across the embedding cells in proportion to their size
    (largest remainder), never more than a cell holds."""
    alloc = {c: min(floor, N.get(c, 0)) for c in N}
    rest = max(0, per_source - sum(alloc.values()))
    while rest > 0:
        room = {c: N[c] - alloc[c] for c in emb_cells if N.get(c, 0) - alloc[c] > 0}
        if not room:
            room = {c: N[c] - alloc[c] for c in N if N[c] - alloc[c] > 0}
        if not room:
            break
        size = sum(N[c] for c in room)
        quota = {c: rest * N[c] / size for c in room}
        give = {c: min(room[c], int(quota[c])) for c in room}
        left = rest - sum(give.values())
        for c in sorted(room, key=lambda c: -(quota[c] - int(quota[c]))):
            if left <= 0:
                break
            if give[c] < room[c]:
                give[c] += 1
                left -= 1
        if not sum(give.values()):
            break
        for c, g in give.items():
            alloc[c] += g
        rest -= sum(give.values())
    return alloc


def sample(args) -> None:
    out = args.out or args.dir / "mapping_review.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    norms = load_jsonl(args.dir / "norm_object_mapping.jsonl")

    cfg_path = args.dir / "mapping_config.json"
    if args.fallback_percentile is not None:
        threshold = args.fallback_percentile
    elif cfg_path.is_file():
        threshold = float(json.loads(cfg_path.read_text(encoding="utf-8"))["fallback_percentile"])
    else:
        pcts = [u["candidates"][0]["percentile"] for n in norms for u in n.get("units") or []
                if u.get("level") == "embedding" and u.get("candidates")
                and u["candidates"][0].get("percentile") is not None]
        threshold = math.floor(min(pcts)) if pcts else 0.0
        print(f"Warning: {cfg_path} not found; fallback percentile taken as the lowest "
              f"embedding percentile, {threshold:g}. Pass --fallback-percentile to override.")
    bands = embedding_bands(threshold)
    names = band_names(bands)

    records = {str(_get(r, args.id_field)): r for r in load_records(args.records)}

    # --- occurrences ---------------------------------------------------------
    occ, unmapped, dropped, missing_pct = [], [], [], 0
    no_object = Counter()
    norms_by_source = Counter()
    for n in norms:
        nid = str(n["norm_id"])
        rec = records.get(nid)
        src = source_of(nid, rec, args.source_field)
        norms_by_source[src] += 1
        units = n.get("units")
        if units is None:   # mapping without Step 6 units: the raw object is the unit
            units = [{"text": n["object"], "level": n.get("level"),
                      "candidates": n.get("candidates")}] if n.get("object") else []
        if not units:
            if n.get("object"):
                dropped.append({"norm_id": nid, "source": src, "text": n["object"],
                                "raw": n["object"]})
            else:
                no_object[src] += 1
            continue
        for u in units:
            item = {"norm_id": nid, "source": src, "text": u["text"],
                    "raw": n.get("object") or "", "level": u.get("level"),
                    "cands": u.get("candidates") or []}
            if not item["level"]:
                unmapped.append(item)
                continue
            if item["level"] == "embedding" and (item["cands"] or [{}])[0].get("percentile") is None:
                missing_pct += 1
            item["cell"] = cell_of(item["level"], item["cands"], bands, names)
            occ.append(item)
    if missing_pct:
        print(f"Warning: {missing_pct} embedding unit(s) without a percentile; counted in the lowest band.")

    # --- census --------------------------------------------------------------
    freq = Counter(o["text"] for o in occ)
    ranked = sorted(freq.items(), key=lambda x: (-x[1], x[0]))
    census = [t for t, _ in ranked[:args.census_size]]
    if 0 < args.census_size < len(ranked) and ranked[args.census_size - 1][1] == ranked[args.census_size][1]:
        print(f"Warning: rank {args.census_size} and {args.census_size + 1} share a frequency "
              f"({ranked[args.census_size][1]}); the tie is broken alphabetically.")
    census_set = set(census)
    freq_src = defaultdict(Counter)
    for o in occ:
        freq_src[o["text"]][o["source"]] += 1
    occ_by_src = Counter(o["source"] for o in occ)
    census_w = {t: Counter(o["source"] for o in occ if o["text"] == t) for t in census}

    # --- strata and sample ---------------------------------------------------
    pools = defaultdict(list)
    for o in occ:
        if o["text"] not in census_set:
            pools[f"{o['source']} | {o['cell']}"].append(o)
    cells = LEXICAL_CELLS + names
    N = {f"{s} | {c}": len(pools[f"{s} | {c}"]) for s in SOURCES for c in cells}
    alloc = {}
    for s in SOURCES:
        a = allocate({c: N[f"{s} | {c}"] for c in cells}, args.per_source, args.floor, names)
        alloc.update({f"{s} | {c}": k for c, k in a.items()})

    rng = random.Random(args.seed)
    rows = []
    for st in strata_names(names):
        pool = sorted(pools[st], key=lambda o: (o["norm_id"], o["text"]))
        for o in rng.sample(pool, alloc[st]):
            rows.append({"kind": "sample", "stratum": st, "source": o["source"], "cell": o["cell"],
                         "text": o["text"], "raw": o["raw"] if o["raw"] != o["text"] else "",
                         "cands": o["cands"], "level": o["level"], "n_norms": freq[o["text"]],
                         "norm_ids": [o["norm_id"]],
                         "contexts": [(o["source"], o["norm_id"], o["raw"] or o["text"])],
                         "w": {}})
    for t in census:
        uses = [o for o in occ if o["text"] == t]
        # up to --census-contexts example norms, taking turns between the sources
        by_src = {s: rng.sample(us, len(us)) for s in SOURCES
                  if (us := [o for o in uses if o["source"] == s])}
        shown = []
        while len(shown) < args.census_contexts and any(by_src.values()):
            for s in SOURCES:
                if by_src.get(s) and len(shown) < args.census_contexts:
                    shown.append(by_src[s].pop())
        rows.append({"kind": "census", "stratum": CENSUS,
                     "source": "+".join(s for s in SOURCES if census_w[t][s]), "cell": CENSUS,
                     "text": t, "raw": "", "cands": uses[0]["cands"], "level": uses[0]["level"],
                     "n_norms": freq[t], "norm_ids": [o["norm_id"] for o in shown],
                     "contexts": [(o["source"], o["norm_id"], o["raw"] or o["text"]) for o in shown],
                     "w": dict(census_w[t])})
    for label, pool, k in [(CHECK_UNMAPPED, unmapped, args.check_unmapped),
                           (CHECK_DROPPED, dropped, args.check_dropped)]:
        pool = sorted(pool, key=lambda o: (o["norm_id"], o["text"]))
        for o in rng.sample(pool, min(k, len(pool))):
            rows.append({"kind": "check", "stratum": label, "source": o["source"], "cell": label,
                         "text": o["text"], "raw": o["raw"] if o["raw"] != o["text"] else "",
                         "cands": [], "level": None, "n_norms": "", "norm_ids": [o["norm_id"]],
                         "contexts": [(o["source"], o["norm_id"], o["raw"] or o["text"])], "w": {}})
    rng.shuffle(rows)   # order must not reveal stratum or confidence

    for r in rows:
        r["context"] = "\n".join(
            (f"[{i}] {s} {nid}: " if len(r["contexts"]) > 1 else "")
            + excerpt(_get(records.get(nid), args.text_field), obj, args.excerpt_chars)
            for i, (s, nid, obj) in enumerate(r["contexts"], 1))

    info = {
        "threshold": threshold, "bands": bands, "names": names, "N": N, "alloc": alloc,
        "census": census, "census_w": census_w, "freq": freq, "ranked": ranked,
        "occ_by_src": occ_by_src, "unmapped": Counter(o["source"] for o in unmapped),
        "dropped": Counter(o["source"] for o in dropped), "no_object": no_object,
        "norms_by_source": norms_by_source,
        "freq_src": freq_src,
        "compound": Counter(o["source"] for o in occ if o["level"] == "compound"),
        "units_by_src": Counter(o["source"] for o in occ) + Counter(o["source"] for o in unmapped),
        "args": args,
    }
    write_workbook(rows, info, out)

    print(f"Wrote {len(rows)} rows -> {out}")
    print(f"Mapped unit occurrences: EU {occ_by_src['EU']:,}, NL {occ_by_src['NL']:,}")
    print(f"Census: {len(census)} texts covering EU {sum(census_w[t]['EU'] for t in census):,}, "
          f"NL {sum(census_w[t]['NL'] for t in census):,} occurrences")
    print(f"{'stratum':34} {'N_h':>6} {'n':>4}")
    for st in strata_names(names):
        print(f"{st:34} {N[st]:>6,} {alloc[st]:>4}")
    for label, k in [(CHECK_UNMAPPED, args.check_unmapped), (CHECK_DROPPED, args.check_dropped)]:
        if k:
            print(f"{label:34} {'':>6} {sum(r['stratum'] == label for r in rows):>4}")


# --------------------------------------------------------------------------
# Workbook
# --------------------------------------------------------------------------

def style_header(ws, row: int, ncols: int, col0: int = 1) -> None:
    for c in range(col0, col0 + ncols):
        cell = ws.cell(row=row, column=c)
        cell.font = Font(name=FONT, bold=True, color="FFFFFF")
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(vertical="center", wrap_text=True)


def body(cell, wrap: bool = False, bold: bool = False, italic: bool = False):
    cell.font = Font(name=FONT, bold=bold, italic=italic)
    cell.alignment = Alignment(vertical="top", wrap_text=wrap)
    return cell


def pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def expected_widths(info) -> dict[str, float]:
    """Worst-case (p = 0.5) 95% half-widths in points, per source and for the difference.
    The census carries no sampling error, so it only shrinks the weight of the sampled cells."""
    var = {}
    for s in SOURCES:
        total = info["occ_by_src"][s]
        v = 0.0
        for c in LEXICAL_CELLS + info["names"]:
            st = f"{s} | {c}"
            n, N = info["alloc"][st], info["N"][st]
            if n:
                v += (N / total) ** 2 * max(0.0, 1 - n / N) * 0.25 / n
        var[s] = v
    out = {s: 100 * Z * math.sqrt(var[s]) for s in SOURCES}
    se_d = math.sqrt(var["EU"] + var["NL"])
    out["diff"] = 100 * Z * se_d
    out["detect"] = 100 * (Z + 0.8416) * se_d
    return out


def design_note(info) -> str:
    a = info["args"]
    occ, N = info["occ_by_src"], info["N"]
    emb_share = {s: sum(N[f"{s} | {c}"] for c in info["names"]) /
                 max(1, occ[s] - sum(info["census_w"][t][s] for t in info["census"])) for s in SOURCES}
    hw = expected_widths(info)
    return (
        "What is estimated. The share of norm objects whose EuroVoc mapping is right, for EU law "
        "and for Dutch law, and the difference between the two. The sampling unit is therefore an "
        "object unit as it occurs in a norm, not a distinct string: a text used in 90 norms counts "
        "90 times, as it does downstream, and each sampled unit is judged in the context of its "
        "own norm.\n\n"
        "Why these strata. Source (EU / NL) is a stratum because it is the comparison of interest; "
        "stratifying guarantees both sources get the same number of judgements regardless of their "
        "share. Within a source the strata follow the mapping cascade, because each level makes a "
        "different kind of claim and is expected to fail at a different rate: phrase (exact "
        "label), head + compound (lexical match on part of the object; compound is merged with "
        "head because it holds only "
        f"{info['compound']['EU']} EU and {info['compound']['NL']} Dutch units, too few to "
        "estimate on its own), and the embedding "
        f"matches split into three equal bands above the fallback percentile ({info['threshold']:g}). "
        f"Embedding matches are {pct(emb_share['EU'])} of sampled EU units and "
        f"{pct(emb_share['NL'])} of Dutch ones, so their confidence bands carry most of the weight.\n\n"
        f"Why these sizes. {a.per_source} judgements per source: the floor of {a.floor} per cell "
        "keeps every cell measured, and the rest goes to the embedding bands in proportion to "
        "their size, because that is where the weight is. At 50% precision in every cell this "
        f"gives about +/-{hw['EU']:.0f} points for EU law and +/-{hw['NL']:.0f} for Dutch law, and "
        f"about +/-{hw['diff']:.0f} points on the EU - NL difference; only differences of roughly "
        f"{hw['detect']:.0f} points or more can be detected reliably (80% power). The sample is not "
        "proportional, so overall figures are weighted by N_h (the 'review' step), never by row "
        "count.\n\n"
        "What is left out. Unmapped units and norms whose object was dropped make no mapping claim; "
        "their shares are exact counts (table below). Norms without any object never reach the "
        "mapping. Optional checks of these, if drawn, are reported separately and never enter the "
        "precision figures."
    )


def census_note(info) -> str:
    return (
        f"Why a census of the {len(info['census'])} most frequent texts. The mapping is computed per "
        "text, so one judgement settles every occurrence of a text, and an error in a frequent "
        "text repeats in every norm that uses it. The object texts are heavy-tailed: most occur "
        "once, a few occur dozens of times. A random sample of this size would draw the frequent "
        "texts by chance or not at all, which makes the estimate swing on luck; judging them all "
        "removes that part of the sampling error at the cost of one judgement each.\n\n"
        "How the texts were chosen. Not by hand: the texts are the top of the frequency ranking "
        "of mapped unit occurrences (all sources together), ties broken alphabetically, fixed "
        "before any judging. In particular they were not chosen because their mapping looked right "
        "or wrong, which would bias the estimate. The table lists each text with its counts.\n\n"
        f"Why {len(info['census'])}. Coverage grows quickly over the first texts and then flattens "
        "(coverage table to the right): the census costs one judgement per text, so it stops where "
        "a further text covers little more than a sampled unit would. It matters most for Dutch "
        "law, whose fixed formulas (e.g. 'nadere regels', 'regels') make a few texts a large share "
        "of all objects; leaving them to chance would make the EU - NL comparison depend on "
        "whether they happened to be drawn.\n\n"
        "How to judge a census row. Several contexts are shown, taken in turn from both sources. "
        "Judge the mapping for the way the text is used in them; if it fits some uses but not "
        "others, choose 'partial' and say so in the notes."
    )


def write_workbook(rows, info, out: Path) -> None:
    a = info["args"]
    names, N, alloc = info["names"], info["N"], info["alloc"]
    census, census_w, occ = info["census"], info["census_w"], info["occ_by_src"]
    wb = Workbook()

    # ---------------- README ----------------
    ws = wb.active
    ws.title = "README"
    readme = [
        ("Validation of the object → EuroVoc mapping (Step 7)", True),
        ("", False),
        ("What to do", True),
        ("1. Go to the Validate sheet. Every row is part of the sample; the order is random.", False),
        ("2. Fill in the yellow columns: judgement (drop-down), better_concept (optional), notes.", False),
        ("3. Judge candidate 1 mainly, for the object as used in the context; use candidates 2-3 to decide 'partial'.", False),
        ("4. Leave the 'key' sheet alone until you are done; it holds the scores you should not see.", False),
        ("5. When finished, save and run: python eurovoc_review.py review --xlsx <this file>", False),
        ("", False),
        ("Judgement codes", True),
        ("correct  – candidate 1 is the right concept, or an acceptable close one.", False),
        ("partial  – candidate 1 is in the right area but too broad/narrow, or a lower candidate is right.", False),
        ("wrong    – no candidate fits the object.", False),
        ("unclear  – the object itself cannot be judged (placeholder, fragment, unresolved reference).", False),
        ("", False),
        ("Special rows", True),
        ("Rows with several numbered contexts are frequent texts judged once for all their uses: judge the", False),
        ("  usual use; if the mapping fits some uses but not others, choose 'partial' and note it.", False),
        ("Rows without candidates (only if checks were drawn): the object was left unmapped or dropped.", False),
        ("  correct = rightly so; wrong = a EuroVoc concept clearly exists / a real object was lost.", False),
        ("", False),
        ("Columns", True),
        ("object      – the unit that was mapped (a cleaned unit from Step 6).", False),
        ("raw_object  – the original object it came from, if different.", False),
        ("cand_1..3   – candidate EuroVoc concepts, best first.", False),
        ("n_norms     – how many norms use this unit text.", False),
        ("context     – text around the object in its norm.", False),
        ("", False),
        ("Example of a filled-in row (not part of the data)", True),
    ]
    for i, (text, bold) in enumerate(readme, 1):
        c = ws.cell(row=i, column=1, value=text)
        c.font = Font(name=FONT, bold=bold, size=13 if i == 1 else 11)
    ex_row = len(readme) + 1
    ex_headers = ["object", "cand_1", "cand_2", "judgement", "better_concept", "notes"]
    ex_values = ["informatie", "informatie", "overdracht van informatie", "correct", "",
                 "generic, but the concept matches"]
    for j, (h, v) in enumerate(zip(ex_headers, ex_values), 1):
        ws.cell(row=ex_row, column=j, value=h)
        c = ws.cell(row=ex_row + 1, column=j, value=v)
        c.font = Font(name=FONT)
        if h in ("judgement", "better_concept", "notes"):
            c.fill = INPUT_FILL
    style_header(ws, ex_row, len(ex_headers))
    ws.column_dimensions["A"].width = 95
    for col in "BCDEF":
        ws.column_dimensions[col].width = 20

    # ---------------- Validate (blind) ----------------
    ws = wb.create_sheet("Validate")
    headers = ["row", "object", "raw_object", "cand_1", "cand_2", "cand_3", "n_norms",
               "context", "judgement", "better_concept", "notes", "norm_id"]
    ws.append(headers)
    style_header(ws, 1, len(headers))
    for i, r in enumerate(rows, start=1):
        c = (r["cands"] or []) + [{}] * 3
        ws.append([i, r["text"], r["raw"], c[0].get("label"), c[1].get("label"),
                   c[2].get("label"), r["n_norms"], r["context"], None, None, None,
                   ", ".join(r["norm_ids"])])
        for col in range(1, len(headers) + 1):
            cell = body(ws.cell(row=i + 1, column=col),
                        wrap=headers[col - 1] in ("object", "raw_object", "context", "notes"))
            cell.border = Border(bottom=THIN)
            if headers[col - 1] in ("judgement", "better_concept", "notes"):
                cell.fill = INPUT_FILL
    last = len(rows) + 1
    jcol = get_column_letter(headers.index("judgement") + 1)
    dv = DataValidation(type="list", formula1='"' + ",".join(JUDGEMENTS) + '"',
                        allow_blank=True, showErrorMessage=True,
                        errorTitle="Judgement", error="Choose: " + ", ".join(JUDGEMENTS))
    ws.add_data_validation(dv)
    dv.add(f"{jcol}2:{jcol}{last}")
    ws.freeze_panes = "C2"
    widths = {"row": 6, "object": 32, "raw_object": 32, "cand_1": 24, "cand_2": 22, "cand_3": 22,
              "n_norms": 8, "context": 70, "judgement": 12, "better_concept": 22, "notes": 30,
              "norm_id": 24}
    for j, h in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(j)].width = widths[h]

    # ---------------- key (hidden) ----------------
    ks = wb.create_sheet("key")
    kh = ["row", "kind", "stratum", "source", "level", "conf_1", "pct_1", "w_EU", "w_NL", "text"]
    ks.append(kh)
    for i, r in enumerate(rows, start=1):
        c0 = (r["cands"] or [{}])[0]
        ks.append([i, r["kind"], r["stratum"], r["source"], r["level"], c0.get("confidence"),
                   c0.get("percentile"), r["w"].get("EU", 0) if r["kind"] == "census" else None,
                   r["w"].get("NL", 0) if r["kind"] == "census" else None, r["text"]])
    for row in ks.iter_rows():
        for cell in row:
            cell.font = Font(name=FONT)
    ks.sheet_state = "hidden"

    # ---------------- Design ----------------
    ds = wb.create_sheet("Design")
    ds.cell(row=1, column=1, value="Sample design").font = Font(name=FONT, bold=True, size=13)
    ds.merge_cells("A2:H2")
    body(ds.cell(row=2, column=1, value=design_note(info)), wrap=True)
    ds.row_dimensions[2].height = 330

    # strata table (read back by 'review': columns stratum, source, N_h)
    head = 4
    th = ["stratum", "source", "population N_h (unit occurrences)", "share of source's mapped units",
          "n judged", "why this stratum and this n"]
    for j, h in enumerate(th, 1):
        ds.cell(row=head, column=j, value=h)
    style_header(ds, head, len(th))
    why = {
        "phrase": "Exact label match; expected to be the most reliable level. Floor only.",
        "head + compound": "Lexical match on part of the object (head word or compound part). "
                           "Compound merged in: too few units to stand alone. Floor only.",
    }
    for k, nm in enumerate(names):
        why[nm] = ("Embedding match, " + ["lowest", "middle", "highest"][k] + " confidence band. "
                   "Floor plus a share of the rest in proportion to its size.")
    r0 = head + 1
    table_rows = []
    for s in SOURCES:
        census_occ = sum(census_w[t][s] for t in census)
        table_rows.append((f"{s} | {CENSUS}", s, census_occ, len(census),
                           f"Occurrences of the {len(census)} census texts in {s} law; each text is "
                           "judged once (shared with the other source)."))
        for c in LEXICAL_CELLS + names:
            st = f"{s} | {c}"
            table_rows.append((st, s, N[st], alloc[st], why[c]))
    last_t = r0 + len(table_rows) - 1
    for k, (st, s, n_h, n, w) in enumerate(table_rows):
        r = r0 + k
        body(ds.cell(row=r, column=1, value=st))
        body(ds.cell(row=r, column=2, value=s))
        body(ds.cell(row=r, column=3, value=n_h)).number_format = "#,##0"
        body(ds.cell(row=r, column=4,
                     value=f"=IFERROR(C{r}/SUMIF($B${r0}:$B${last_t},B{r},$C${r0}:$C${last_t}),0)")
             ).number_format = "0.0%"
        body(ds.cell(row=r, column=5, value=n))
        body(ds.cell(row=r, column=6, value=w), wrap=True)

    # exact counts
    r = last_t + 2
    ds.cell(row=r, column=1, value="Exact counts (no sampling)").font = Font(name=FONT, bold=True)
    eh = ["", "EU", "NL"]
    for j, h in enumerate(eh, 1):
        ds.cell(row=r + 1, column=j, value=h)
    style_header(ds, r + 1, 3)
    ub, nb = info["units_by_src"], info["norms_by_source"]
    exact = [
        ("norms", nb["EU"], nb["NL"], "#,##0"),
        ("object units", ub["EU"], ub["NL"], "#,##0"),
        ("mapped units", occ["EU"], occ["NL"], "#,##0"),
        ("unmapped units", info["unmapped"]["EU"], info["unmapped"]["NL"], "#,##0"),
        ("norms with object dropped (no unit)", info["dropped"]["EU"], info["dropped"]["NL"], "#,##0"),
        ("norms without any object", info["no_object"]["EU"], info["no_object"]["NL"], "#,##0"),
    ]
    rr = r + 2
    for label, eu, nl, fmt_ in exact:
        body(ds.cell(row=rr, column=1, value=label))
        body(ds.cell(row=rr, column=2, value=eu)).number_format = fmt_
        body(ds.cell(row=rr, column=3, value=nl)).number_format = fmt_
        rr += 1
    units_row, unm_row, norms_row, drop_row = r + 3, r + 5, r + 2, r + 6
    for label, num, den in [("unmapped share of units", unm_row, units_row),
                            ("dropped share of norms", drop_row, norms_row)]:
        body(ds.cell(row=rr, column=1, value=label))
        for col in ("B", "C"):
            body(ds.cell(row=rr, column={"B": 2, "C": 3}[col],
                         value=f"=IFERROR({col}{num}/{col}{den},0)")).number_format = "0.0%"
        rr += 1

    # census justification and table
    r = rr + 1
    ds.cell(row=r, column=1, value="Census of frequent texts").font = Font(name=FONT, bold=True, size=12)
    ds.merge_cells(start_row=r + 1, start_column=1, end_row=r + 1, end_column=8)
    body(ds.cell(row=r + 1, column=1, value=census_note(info)), wrap=True)
    ds.row_dimensions[r + 1].height = 300
    ch = ["rank", "text", "EU occurrences", "NL occurrences", "total", "share of all mapped units",
          "cumulative share", "why this text"]
    hr = r + 3
    for j, h in enumerate(ch, 1):
        ds.cell(row=hr, column=j, value=h)
    style_header(ds, hr, len(ch))
    total_mapped = occ["EU"] + occ["NL"]
    freq = info["freq"]
    for k, t in enumerate(census, start=1):
        row = hr + k
        eu, nl = census_w[t]["EU"], census_w[t]["NL"]
        where = ("used in both sources" if eu and nl else
                 "used only in EU law" if eu else "used only in Dutch law")
        if eu and nl:
            lean = "mostly Dutch" if nl >= 2 * eu else "mostly EU" if eu >= 2 * nl else "balanced"
            where += f" ({lean})"
        body(ds.cell(row=row, column=1, value=k))
        body(ds.cell(row=row, column=2, value=t))
        body(ds.cell(row=row, column=3, value=eu))
        body(ds.cell(row=row, column=4, value=nl))
        body(ds.cell(row=row, column=5, value=f"=C{row}+D{row}"))
        body(ds.cell(row=row, column=6, value=f"=E{row}/{total_mapped}")).number_format = "0.0%"
        body(ds.cell(row=row, column=7, value=f"=SUM($E${hr + 1}:E{row})/{total_mapped}")).number_format = "0.0%"
        body(ds.cell(row=row, column=8,
                     value=f"Frequency rank {k} of {len(freq):,} distinct texts; {where}. "
                           f"One judgement covers its {eu + nl} norm occurrences."), wrap=True)
    nxt = info["ranked"][len(census)] if len(info["ranked"]) > len(census) else None
    if nxt:
        body(ds.cell(row=hr + len(census) + 1, column=2,
                     value=f"first text left out: '{nxt[0]}' ({nxt[1]} occurrences)"), italic=True)

    # coverage table (justifies the census size)
    cr = hr
    cc = 10
    for j, h in enumerate(["census size", "EU coverage", "NL coverage"]):
        ds.cell(row=cr, column=cc + j, value=h)
    style_header(ds, cr, 3, col0=cc)
    for k, size in enumerate((5, 10, 20, 30, 50, 100), start=1):
        top = [t for t, _ in info["ranked"][:size]]
        eu = sum(info["freq_src"][t]["EU"] for t in top)
        nl = sum(info["freq_src"][t]["NL"] for t in top)
        body(ds.cell(row=cr + k, column=cc, value=size), bold=(size == len(census)))
        body(ds.cell(row=cr + k, column=cc + 1, value=eu / max(1, occ["EU"])),
             bold=(size == len(census))).number_format = "0.0%"
        body(ds.cell(row=cr + k, column=cc + 2, value=nl / max(1, occ["NL"])),
             bold=(size == len(census))).number_format = "0.0%"
    for col, w in zip("ABCDEFGHIJKL", [32, 30, 14, 14, 9, 13, 12, 60, 2, 12, 12, 12]):
        ds.column_dimensions[col].width = w

    # ---------------- Summary (live formulas, unweighted per stratum) ----------------
    sm = wb.create_sheet("Summary")
    sh = ["stratum", "population N_h", "rows", "judged", "correct", "partial", "wrong", "unclear",
          "precision strict", "precision lenient", "strict 95% low", "strict 95% high"]
    sm.append(sh)
    style_header(sm, 1, len(sh))
    sm.cell(row=1, column=14, value="z")
    sm.cell(row=2, column=14, value=Z)
    srng = f"key!$C$2:$C${last}"
    jrng = f"Validate!${jcol}$2:${jcol}${last}"
    strata = [CENSUS] + strata_names(names) + \
        [s for s, k in ((CHECK_UNMAPPED, a.check_unmapped), (CHECK_DROPPED, a.check_dropped)) if k]
    pop = {CENSUS: len(census), **N}
    for i, st in enumerate(strata, start=2):
        sm.cell(row=i, column=1, value=st)
        sm.cell(row=i, column=2, value=pop.get(st, ""))
        sm.cell(row=i, column=3, value=f'=COUNTIFS({srng},A{i})')
        sm.cell(row=i, column=4, value=f'=COUNTIFS({srng},A{i},{jrng},"<>")')
        for j, code in enumerate(JUDGEMENTS, start=5):
            sm.cell(row=i, column=j, value=f'=COUNTIFS({srng},A{i},{jrng},"{code}")')
        n = f"(D{i}-H{i})"
        sm.cell(row=i, column=9, value=f'=IFERROR(E{i}/{n},"")')
        sm.cell(row=i, column=10, value=f'=IFERROR((E{i}+F{i})/{n},"")')
        core = f"(I{i}+$N$2^2/(2*{n}))/(1+$N$2^2/{n})"
        half = f"$N$2*SQRT(I{i}*(1-I{i})/{n}+$N$2^2/(4*{n}^2))/(1+$N$2^2/{n})"
        sm.cell(row=i, column=11, value=f'=IFERROR(MAX(0,{core}-{half}),"")')
        sm.cell(row=i, column=12, value=f'=IFERROR(MIN(1,{core}+{half}),"")')
        for col in range(1, len(sh) + 1):
            body(sm.cell(row=i, column=col))
        for col in (9, 10, 11, 12):
            sm.cell(row=i, column=col).number_format = "0%"
    note_row = len(strata) + 3
    notes = [
        "Per-stratum figures only; they are unweighted and the census row counts texts, not occurrences.",
        "Weighted precision per source, the EU - NL difference and the mix-adjusted comparison come "
        "from: python eurovoc_review.py review --xlsx <this file>",
        "Strict = correct; lenient = correct + partial; unclear is excluded. Intervals: Wilson, z in N2.",
        "Check strata (if any) measure whether leaving an object unmapped / dropping it was right; "
        "they never enter the precision totals.",
    ]
    for k, t in enumerate(notes):
        body(sm.cell(row=note_row + k, column=1, value=t), italic=True)
    for col, w in zip("ABCDEFGHIJKL", [34, 14, 7, 8, 8, 8, 8, 8, 15, 16, 14, 14]):
        sm.column_dimensions[col].width = w

    wb.active = wb.sheetnames.index("Validate")
    wb.save(out)


# --------------------------------------------------------------------------
# Review
# --------------------------------------------------------------------------

def review(args) -> None:
    wb = load_workbook(args.xlsx, data_only=True)
    vs, ks = wb["Validate"], wb["key"]
    vh = [c.value for c in vs[1]]
    jcol = vh.index("judgement")
    labels = {}
    for row in vs.iter_rows(min_row=2, values_only=True):
        if row[0] is not None:
            labels[int(row[0])] = (str(row[jcol]).strip().lower() if row[jcol] else None, row[1])
    key = {}
    kh = [c.value for c in ks[1]]
    for row in ks.iter_rows(min_row=2, values_only=True):
        if row[0] is not None:
            key[int(row[0])] = dict(zip(kh, row))

    # population per stratum from the Design table
    N, census_occ = {}, {}
    for row in wb["Design"].iter_rows(values_only=True):
        if row and isinstance(row[0], str) and " | " in row[0] and isinstance(row[2], (int, float)):
            if row[0].endswith(f"| {CENSUS}"):
                census_occ[row[1]] = int(row[2])
            elif not row[0].startswith("check"):
                N[row[0]] = int(row[2])
    if not N:
        raise SystemExit("No strata table found in the 'Design' sheet.")

    judged = {i: lab for i, (lab, _) in labels.items() if lab in JUDGEMENTS}
    print(f"Rows: {len(labels)}   judged: {len(judged)}   "
          f"unclear: {sum(v == 'unclear' for v in judged.values())}   "
          f"blank: {sum(1 for v in labels.values() if not v[0])}\n")
    if not judged:
        print("Nothing to review yet - fill in 'judgement' first.")
        return

    ok = {"strict": {"correct"}, "lenient": {"correct", "partial"}}

    def cell_stats(stratum, mode):
        xs = [judged[i] for i, k in key.items() if k["stratum"] == stratum and i in judged
              and judged[i] != "unclear"]
        return sum(x in ok[mode] for x in xs), len(xs)

    strata = list(N)
    print("Per stratum (95% Wilson):")
    print(f"  {'stratum':34} {'N_h':>6} {'n':>3}  {'strict':22} {'lenient':22}")
    for st in strata:
        ks_, n = cell_stats(st, "strict")
        kl, _ = cell_stats(st, "lenient")
        if not n:
            print(f"  {st:34} {N[st]:>6,} {0:>3}  (no judgements)")
            continue
        s = wilson(ks_ / n, n)
        l = wilson(kl / n, n)
        print(f"  {st:34} {N[st]:>6,} {n:>3}  {ks_/n:4.0%} [{s[0]:4.0%}-{s[1]:4.0%}]      "
              f"{kl/n:4.0%} [{l[0]:4.0%}-{l[1]:4.0%}]")

    # census: occurrence-weighted per source, no sampling error
    def census_part(source, mode):
        rows = [(k, judged[i]) for i, k in key.items() if k["kind"] == "census" and i in judged
                and judged[i] != "unclear"]
        w = sum(k[f"w_{source}"] or 0 for k, _ in rows)
        hit = sum(k[f"w_{source}"] or 0 for k, lab in rows if lab in ok[mode])
        return w, (hit / w if w else 0.0), len(rows)

    print("\nCensus (occurrence-weighted, exact for the judged texts):")
    for s in SOURCES:
        w, p, n = census_part(s, "strict")
        _, pl, _ = census_part(s, "lenient")
        print(f"  {s}: strict {p:5.0%}   lenient {pl:5.0%}   over {w:,} of {census_occ.get(s, 0):,} occurrences")
    wrong = [(k["text"], (k["w_EU"] or 0) + (k["w_NL"] or 0)) for i, k in key.items()
             if k["kind"] == "census" and judged.get(i) == "wrong"]
    if wrong:
        print("  wrong census texts:", ", ".join(f"{t!r} ({n})" for t, n in sorted(wrong, key=lambda x: -x[1])))

    def estimate(source, mode, weights=None):
        """weights: None = the source's own N_h; otherwise {cell: share} shared by both sources."""
        parts = []
        cw, cp, cn = census_part(source, mode)
        cells = [st.split(" | ", 1)[1] for st in strata if st.startswith(source + " |")]
        if weights is None:
            if cn:
                parts.append((cw, cp, 0.0, cn))
        elif cn:
            parts.append((weights[CENSUS], cp, 0.0, cn))
        for c in cells:
            st = f"{source} | {c}"
            k, n = cell_stats(st, mode)
            if not n:
                continue
            p = k / n
            fpc = max(0.0, 1 - n / N[st]) if N[st] else 0.0
            w = N[st] if weights is None else weights[c]
            parts.append((w, p, fpc * p * (1 - p) / n, n))
        return combine(parts)

    def show(title, res):
        if res is None:
            print(f"  {title:30} (no judgements)")
        else:
            p, lo, hi, n_eff = res
            print(f"  {title:30} {p:5.1%} [{lo:5.1%} - {hi:5.1%}]   n_eff {n_eff:5.1f}")

    # pooled cell shares for the mix-adjusted comparison
    cells = list(dict.fromkeys(st.split(" | ", 1)[1] for st in strata))   # cascade order
    pooled = {c: sum(N.get(f"{s} | {c}", 0) for s in SOURCES) for c in cells}
    pooled[CENSUS] = sum(census_occ.values())

    for mode in ("strict", "lenient"):
        print(f"\nOverall precision, {mode} ({'correct' if mode == 'strict' else 'correct + partial'}):")
        own = {s: estimate(s, mode) for s in SOURCES}
        for s in SOURCES:
            show(f"{s} (own mix)", own[s])
        if all(own.values()):
            d = newcombe(own["EU"], own["NL"])
            print(f"  {'EU - NL difference':30} {d[0]:+5.1%} [{d[1]:+5.1%} - {d[2]:+5.1%}]")
        adj = {s: estimate(s, mode, pooled) for s in SOURCES}
        for s in SOURCES:
            show(f"{s} (mix-adjusted)", adj[s])
        if all(adj.values()):
            d = newcombe(adj["EU"], adj["NL"])
            print(f"  {'EU - NL, mix-adjusted':30} {d[0]:+5.1%} [{d[1]:+5.1%} - {d[2]:+5.1%}]")
            print("  (own mix = what each body of law experiences; mix-adjusted = same cascade-level "
                  "mix for both, so a gap there is the mapper, not the mix)")

    print("\nPer cascade level, both sources pooled (strict, weighted by N_h):")
    for c in cells:
        parts = []
        for s in SOURCES:
            st = f"{s} | {c}"
            if st not in N:
                continue
            k, n = cell_stats(st, "strict")
            if n:
                p = k / n
                parts.append((N[st], p, max(0.0, 1 - n / N[st]) * p * (1 - p) / n, n))
        show(c, combine(parts))

    checks = [CHECK_UNMAPPED, CHECK_DROPPED]
    if any(k["stratum"] in checks for k in key.values()):
        print("\nChecks (share judged correct = rightly unmapped / rightly dropped):")
        for st in checks:
            k_, n = cell_stats(st, "strict")
            if n:
                lo, hi = wilson(k_ / n, n)
                print(f"  {st:30} {k_}/{n} = {k_/n:.0%} [{lo:.0%} - {hi:.0%}]")


# --------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample", help="Write the labelling workbook.")
    s.add_argument("--dir", type=Path, default=Path("data/conflict_all/object/eurovoc"),
                   help="folder with norm_object_mapping.jsonl (and mapping_config.json)")
    s.add_argument("--records", type=Path, required=True, help="norm records, for the context text")
    s.add_argument("--id-field", default="norm_id")
    s.add_argument("--text-field", default="text.paragraph")
    s.add_argument("--source-field", default="hierarchy.instrument_id",
                   help="record field with the act id; BWBR... = NL, else EU (falls back to the norm_id prefix)")
    s.add_argument("--fallback-percentile", type=float, default=None,
                   help="override mapping_config.json's fallback_percentile")
    s.add_argument("--census-size", type=int, default=20)
    s.add_argument("--census-contexts", type=int, default=3)
    s.add_argument("--per-source", type=int, default=50)
    s.add_argument("--floor", type=int, default=8)
    s.add_argument("--check-unmapped", type=int, default=0)
    s.add_argument("--check-dropped", type=int, default=0)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--excerpt-chars", type=int, default=150)
    s.add_argument("--out", type=Path, default=None)
    r = sub.add_parser("review", help="Report precision from a filled-in workbook.")
    r.add_argument("--xlsx", type=Path, default=Path("data/conflict_all/object/eurovoc/mapping_review.xlsx"))
    args = p.parse_args()
    if args.cmd == "sample":
        sample(args)
    else:
        review(args)


if __name__ == "__main__":
    main()