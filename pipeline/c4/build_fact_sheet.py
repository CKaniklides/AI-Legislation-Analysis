# -*- coding: utf-8 -*-
"""
Builds the fact sheet for one instrument: everything the simplification feature can know
about a law WITHOUT a language model.

    python pipeline/c4/build_fact_sheet.py 32016R0679
    python pipeline/c4/build_fact_sheet.py --all

Why this exists (2026-10-06). The simplification feature writes a short plain-language
sheet per law, following the mentor's nine-step process (core concepts, if/then triggers,
stakeholder matrix, exceptions, the consequential 20%, ...). Most of those steps ask for
facts the corpus already holds as structured data: the extracted norms carry who must do
what, when, and for whom. Computing them here means the model that writes the sheet is
handed the facts and asked to word them, instead of being asked to find them -- and every
line it writes can be checked back against this file.

Three levels, matching the reading tree:
  law      -- one sheet, target 1-3% of the law's words
  chapter  -- one summary per chapter (the lawmaker's own division), 5-10% in total
  article  -- only the "key" articles (top 20% by the importance rank below) get a card

Nothing in this file is a legal judgement. The importance rank is a transparent sum of
counts, not a claim about which articles matter in court: the corpus holds no case law.
"""
import argparse
import json
import math
import re
import sys
from collections import Counter, OrderedDict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
_HERE = Path(__file__).resolve().parent
_PIPELINE = _HERE.parent
for _d in (_PIPELINE, _PIPELINE / "c1", _PIPELINE / "preprocessing"):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

import detect_c1_contradiction as c1
from graph import _LAW_FOLDERS

DATA = c1.ROOT / "data"
OUT = DATA / "results" / "c4"

LAW_TARGET = (0.01, 0.03)       # the mentor's own target for the essence
TREE_TARGET = (0.05, 0.10)      # law sheet + all chapter summaries together
KEY_ARTICLE_SHARE = 0.20        # the Pareto step, made literal

DUTY = {"OBLIGATION", "PROHIBITION"}
_EXCEPTION_OPS = ("tenzij", "behoudens", "niet van toepassing indien", "in afwijking van")
_SANCTION = re.compile(r"\b(geldboete|bestuurlijke boete|last onder dwangsom|sanctie)", re.I)
# A reference to an article of ANOTHER instrument must not count as an internal citation.
_FOREIGN = re.compile(r"\b(VWEU|VEU|Handvest|Richtlijn|Verordening \((?:EU|EG)\)|van de (?!onderhavige)\w*(?:wet|verordening \())", re.I)
_ARTREF = re.compile(r"\bartikel(?:en)?\s+(\d+[a-z]?(?:(?:,?\s*(?:lid|leden)\s*[\d, en]+)?\s*(?:,|en|of|tot en met|en met)\s*\d+[a-z]?)*)", re.I)


def provisions_path(instrument_id: str) -> Path:
    folder = DATA / "laws" / _LAW_FOLDERS[instrument_id]
    files = [p for p in sorted(folder.glob(f"{instrument_id}_*provisions.json"))
             if "complete" not in p.name]          # ADR-0004: no Digital Omnibus content
    return files[0]


def words(text: str) -> int:
    return len((text or "").split())


def chapters(rows: list[dict]) -> list[dict]:
    out = OrderedDict()
    for r in rows:
        c = out.setdefault(r.get("chapter") or "(geen hoofdstuk)",
                           {"title": r.get("chapter") or "(geen hoofdstuk)", "articles": [], "n_words": 0})
        c["articles"].append(r["article"])
        c["n_words"] += words(r.get("text"))
    return list(out.values())


def defined_terms(rows: list[dict]) -> list[dict]:
    """Terms the law defines itself, ranked by how often the rest of the law uses them."""
    terms = {}
    for r in rows:
        if not any(n.get("deontic") == "DEFINITION" for n in r.get("norms") or []):
            continue
        text = r.get("text") or ""
        found = re.findall(r"[„\"“]([^”\"“„\n]{2,80})[”\"“]\s*:", text)                  # EU: „term”: ...
        found += re.findall(r"(?:^|\n)(?:[–-]\s*\n+\s*)?([a-zA-Zà-ÿ][^:;\n]{2,80}):\s", text)  # NL: term: ...
        for t in found:
            t = t.strip()
            if t and t.lower() not in terms and not t.lower().startswith(("in deze", "voor de toepassing")):
                terms[t.lower()] = {"term": t, "defined_in": r["article"]}
    for key, t in terms.items():
        pat = re.compile(r"\b" + re.escape(key) + r"\w{0,2}\b", re.I)
        t["uses"] = sum(len(pat.findall(r.get("text") or "")) for r in rows if r["article"] != t["defined_in"])
        t["used_in_articles"] = sum(1 for r in rows if r["article"] != t["defined_in"] and pat.search(r.get("text") or ""))
    return sorted(terms.values(), key=lambda t: -t["uses"])


def cited_articles(r: dict, known: set) -> set:
    """Articles of the same law that this article points at (text pattern, approximate)."""
    targets = set()
    text = r.get("text") or ""
    for m in _ARTREF.finditer(text):
        if _FOREIGN.search(text[m.end():m.end() + 45]):
            continue
        span = re.sub(r"(?:lid|leden)\s*[\d, en]+", " ", m.group(1))
        for lo, hi in re.findall(r"(\d+) tot en met (\d+)", span):
            targets.update(str(i) for i in range(int(lo), int(hi) + 1))
        targets.update(re.findall(r"\d+[a-z]?", span))
    return {t for t in targets if t in known and t != r["article"]}


def norm_view(r: dict, n: dict) -> dict:
    return {"article": r["article"], "paragraph": n.get("number"), "kind": n.get("deontic"),
            "who": n.get("addressee"), "who_type": n.get("addressee_type"),
            "when": n.get("trigger_event"), "must": n.get("action"),
            "to_whom": n.get("recipient_body") or [],
            "deadline": (n.get("deadline") or {}).get("raw"),
            "conditions": n.get("conditions") or []}


def duty_norms(rows: list[dict]) -> list[dict]:
    return [norm_view(r, n) for r in rows for n in r.get("norms") or [] if n.get("deontic") in DUTY]


def stakeholder_matrix(rows: list[dict]) -> list[dict]:
    """Who owes something to whom. One row per (who, to whom) with the articles behind it."""
    cells = {}
    for d in duty_norms(rows):
        if not d["who"]:
            continue
        for to in d["to_whom"] or ["(niemand genoemd)"]:
            c = cells.setdefault((d["who"].lower(), to.lower()),
                                 {"who": d["who"], "who_type": d["who_type"], "to_whom": to,
                                  "n_duties": 0, "articles": [], "example": d["must"]})
            c["n_duties"] += 1
            if d["article"] not in c["articles"]:
                c["articles"].append(d["article"])
    return sorted(cells.values(), key=lambda c: -c["n_duties"])


def exceptions(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        for n in r.get("norms") or []:
            conds = [c for c in n.get("conditions") or [] if any(op in c.lower() for op in _EXCEPTION_OPS)]
            if conds or n.get("deference"):
                out.append({"article": r["article"], "paragraph": n.get("number"), "rule": n.get("action"),
                            "exception": conds, "yields_to": n.get("deference")})
    return out


def importance(rows: list[dict]) -> list[dict]:
    """
    Points per article, every component reported so a reader can see why an article is on
    the list:  1 per other article citing it, 2 per duty on a regulated party, 5 if a
    penalty article names it (breaking it can be fined). This stands in for the mentor's
    "most used in court" check, which the corpus cannot answer.
    """
    known = {r["article"] for r in rows}
    cited, fined = Counter(), set()
    for r in rows:
        targets = cited_articles(r, known)
        cited.update(targets)
        if _SANCTION.search(r.get("text") or ""):
            fined |= targets
    stats = []
    for r in rows:
        norms = r.get("norms") or []
        s = {"article": r["article"], "heading": r.get("heading"),
             "cited_by_articles": cited[r["article"]],
             "n_duties_on_regulated": sum(1 for n in norms if n.get("deontic") in DUTY
                                          and n.get("addressee_type") == "REGULATED_ENTITY"),
             "fine_backed": r["article"] in fined,
             "n_words": words(r.get("text"))}
        s["points"] = s["cited_by_articles"] + 2 * s["n_duties_on_regulated"] + (5 if s["fine_backed"] else 0)
        stats.append(s)
    stats.sort(key=lambda s: -s["points"])
    n_key = max(1, math.ceil(len(stats) * KEY_ARTICLE_SHARE))
    for i, s in enumerate(stats):
        s["key_article"] = i < n_key
    return stats


def build(instrument_id: str) -> dict:
    path = provisions_path(instrument_id)
    rows = json.loads(path.read_text(encoding="utf-8"))
    rows = rows if isinstance(rows, list) else rows["provisions"]
    total = sum(words(r.get("text")) for r in rows)
    duties = duty_norms(rows)
    sheet = {
        "instrument_id": instrument_id,
        "source_file": path.name,
        "n_articles": len(rows),
        "n_words": total,
        "length_targets_words": {"law_sheet": [round(total * LAW_TARGET[0]), round(total * LAW_TARGET[1])],
                                 "whole_tree": [round(total * TREE_TARGET[0]), round(total * TREE_TARGET[1])]},
        "chapters": chapters(rows),
        "defined_terms": defined_terms(rows),
        "duties": duties,
        "stakeholder_matrix": stakeholder_matrix(rows),
        "exceptions": exceptions(rows),
        "articles_by_importance": importance(rows),
        "not_in_corpus": ["recitals (the law's stated purpose)", "constitution and EU Charter", "case law"],
    }
    for c in sheet["chapters"]:
        share = c["n_words"] / total if total else 0
        c["target_words"] = [round(total * TREE_TARGET[0] * share), round(total * TREE_TARGET[1] * share)]
    return sheet


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("instrument_id", nargs="?")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    ids = list(_LAW_FOLDERS) if args.all else [args.instrument_id]
    OUT.mkdir(parents=True, exist_ok=True)
    for iid in ids:
        sheet = build(iid)
        c1.atomic_write_json(OUT / f"fact_sheet_{_LAW_FOLDERS[iid]}.json", sheet)
        key = [a["article"] for a in sheet["articles_by_importance"] if a["key_article"]]
        print(f"{_LAW_FOLDERS[iid]:22} {sheet['n_articles']:4} articles {sheet['n_words']:6} words | "
              f"{len(sheet['chapters'])} chapters, {len(sheet['defined_terms'])} defined terms, "
              f"{len(sheet['duties'])} duties, {len(sheet['exceptions'])} exceptions, {len(key)} key articles")


if __name__ == "__main__":
    main()
