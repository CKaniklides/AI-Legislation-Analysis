# -*- coding: utf-8 -*-
"""
Writes the plain-language sheet for one law, then checks it.

    python pipeline/c4/build_fact_sheet.py 32016R0679            # first, no model
    python pipeline/c4/write_simplified.py 32016R0679 --dry-run   # how many calls, no spending
    python pipeline/c4/write_simplified.py 32016R0679 --chapters 3,4
    python pipeline/c4/write_simplified.py 32016R0679 --all        # the whole law
    python ui/build_simplified_data.py                             # then rebuild the interface data

    python pipeline/c4/write_simplified.py 32016R0679 --no-law --articles 3,4 --map

Four kinds of unit, matching the reading tree:
  law      -- the mentor's nine-step sheet for the whole law (1-3% of its words)
  chapter  -- a short summary per chapter (all of them together: 5-10%)
  articles -- a plain-language card per article, one call per chapter. Each card is
              10-50% of its article: short articles keep half, long ones a tenth
  map      -- the stakeholder map: the parties and everything they owe each other

Each unit costs two calls: one to write, one to check. The writer gets the legal text plus
the fact sheet (build_fact_sheet.py) and must name the article behind every line. The
checker gets each line next to the text of the articles it names and says whether the text
supports it. Where it does not fully support a line, the checker also proposes a minimal
repair, which a reviewing lawyer can take over with one click. On top of that, three
checks need no model: the named articles exist, every figure in a line appears in the
articles it names, and the length is inside the target.

Boundary. This writes a summary that sits NEXT TO the law. It never replaces the legal
text and never says how the law applies to someone's case. A lawyer accepts or declines a
sheet before it is shared. The "foundations" step (constitution, EU Charter) is answered
from the model's own knowledge because those texts are not in the corpus; those lines carry
the verdict "not_checked" and must be shown that way.
"""
import argparse
import hashlib
import json
import math
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
_HERE = Path(__file__).resolve().parent
_PIPELINE = _HERE.parent
for _d in (_PIPELINE, _PIPELINE / "c1", _PIPELINE / "preprocessing", _HERE):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

import detect_c1_contradiction as c1
import build_fact_sheet as fs
from graph import _LAW_FOLDERS

CACHE = fs.DATA / "cache" / "c4_simplified_cache.json"
MODEL = "gpt-5.6-terra"
EFFORT = "medium"
VERSION = "v1"


class Bi(BaseModel):
    nl: str
    en: str


class Line(BaseModel):
    nl: str
    en: str
    articles: list[str]


class Concept(BaseModel):
    term: str                 # the law's own Dutch term, unchanged
    term_en: str
    plain: Bi
    articles: list[str]


class Trigger(BaseModel):
    if_: Bi
    then: Bi
    articles: list[str]


class Relation(BaseModel):
    who: Bi
    owes: Bi
    to_whom: Bi
    articles: list[str]


class Foundation(BaseModel):
    source: Bi                # e.g. "Handvest van de grondrechten van de EU, artikel 8" / "EU Charter of Fundamental Rights, Article 8"
    plain: Bi


class LawSheet(BaseModel):
    in_short: list[Line]
    core_concepts: list[Concept]
    foundations: list[Foundation]
    if_then: list[Trigger]
    stakeholders: list[Relation]
    purpose: Line
    most_consequential: list[Line]
    exceptions: list[Line]


class ChapterSheet(BaseModel):
    title: Bi
    about: Line               # one sentence; all the public document shows for a chapter about authorities
    lines: list[Line]


class ArticleCard(BaseModel):
    article: str
    title: Bi
    points: list[Bi]


class ArticleCards(BaseModel):
    cards: list[ArticleCard]


class Party(BaseModel):
    id: str                   # short ascii id, e.g. "controller"
    name: Bi
    role: Bi                  # one plain sentence: who this is


class MapRelation(BaseModel):
    from_id: str
    to_id: str
    kind: Literal["must", "may", "must_not"]
    what: Bi
    articles: list[str]


class StakeholderMap(BaseModel):
    parties: list[Party]
    relations: list[MapRelation]


class LineCheck(BaseModel):
    id: str
    verdict: Literal["supported", "partly_supported", "not_supported"]
    note: Bi
    suggestion: Optional[Bi]  # a minimal repair, only when the verdict is not "supported"


class CheckResult(BaseModel):
    checks: list[LineCheck]


STYLE = """HOW TO WRITE
- Write for someone with no legal training. Short sentences. Everyday words. Level B1.
- Say what people must do, may do, or may not do. Use "you" or name the party plainly
  ("an organisation that uses personal data"), not the legal label alone. When a legal
  term cannot be avoided, give the plain meaning first and the term in brackets.
- Every line must name, in `articles`, the article number(s) it rests on, exactly as they
  appear in the text below (e.g. "33"). Name only articles you actually used.
- Use only what the legal text says. Do not add examples of fines, dates, numbers or
  duties that are not in the text. Keep every figure (72 hours, one month, 4%) exact.
- Do not say how the law applies to a particular person's case, and give no advice.
- Write each line in Dutch (nl) and English (en). The two must say the same thing."""


def law_prompt(sheet: dict, rows: list[dict]) -> str:
    lo, hi = sheet["length_targets_words"]["law_sheet"]
    facts = {
        "most_used_defined_terms": sheet["defined_terms"][:12],
        "articles_by_importance": [{k: a[k] for k in ("article", "heading", "points", "cited_by_articles",
                                                       "n_duties_on_regulated", "fine_backed")}
                                   for a in sheet["articles_by_importance"] if a["key_article"]],
        "who_owes_whom": sheet["stakeholder_matrix"][:25],
        "exceptions": sheet["exceptions"][:40],
    }
    law = "\n\n".join(f"### {r.get('heading') or 'Artikel ' + r['article']}  [article {r['article']}]\n{r.get('text') or ''}"
                      for r in rows)
    return f"""You are writing a one-page plain-language sheet about a law, for the general public and for
lawmakers who want to see what the law comes down to. It follows a fixed nine-step process.

{STYLE}

WHAT TO PRODUCE (the whole sheet: between {lo} and {hi} words per language, so be selective)
- in_short: 2 to 4 lines. The essence of the law. What a reader should remember.
- core_concepts: the 3 to 7 legal terms that carry the most weight. `term` is the law's own
  Dutch term. The FACTS list shows which defined terms the law uses most; use it as a guide.
  A central concept the law names without a formal definition (for example in the heading of
  the article that regulates it) can be a key term too.
- foundations: the 2 to 4 articles of the EU Charter of Fundamental Rights, the EU Treaties
  or the Dutch Constitution this law most relates to. These texts are NOT given to you;
  answer from your own knowledge. Be conservative. `source` names the text and the article
  precisely, in Dutch (nl) and in English (en), with the official name in each language.
- if_then: 5 to 8 concrete "if this happens, then you must ..." rules that matter most in
  practice for the people and organisations the law applies to.
- stakeholders: 4 to 7 rows of who owes what to whom. Fill in `to_whom` from the legal
  text; the FACTS matrix often leaves it empty.
- purpose: one line. The core requirement and intention behind the law.
- most_consequential: 3 to 5 lines. The roughly 20% of the law with the most consequences.
  The FACTS ranking counts citations, duties and whether a breach can be fined; it is a
  guide, not the answer. The corpus holds no case law, so do not claim anything about
  court practice.
- exceptions: 3 to 5 lines summarising the main exceptions to the general rules.

FACTS (computed from the law, no model involved)
{json.dumps(facts, ensure_ascii=False)}

THE LAW (Dutch, complete)
{law}"""


def chapter_prompt(sheet: dict, chapter: dict, rows: list[dict]) -> str:
    lo, hi = chapter["target_words"]
    lo, hi = max(lo, 40), max(hi, 80)
    arts = [r for r in rows if r["article"] in set(chapter["articles"])]
    duties = [d for d in sheet["duties"] if d["article"] in set(chapter["articles"])]
    text = "\n\n".join(f"### {r.get('heading') or 'Artikel ' + r['article']}  [article {r['article']}]\n{r.get('text') or ''}"
                       for r in arts)
    return f"""You are writing a short plain-language summary of ONE chapter of a law, for the general
public and for lawmakers. A reader opens it after reading a one-page sheet about the whole law
and wants to know what this chapter adds.

{STYLE}

WHAT TO PRODUCE
- title: a plain title for the chapter (not the legal heading).
- about: ONE sentence that says what this chapter is about and who it concerns. A reader
  who sees only this sentence must know whether the chapter matters to them.
- lines: the rest of the summary as separate lines. Do not repeat `about`. Give the rules
  that matter most, then the main exceptions. Leave out procedure that only matters to
  authorities, unless the chapter is about authorities.
- Length: `about` and `lines` together between {lo} and {hi} words per language. {hi} is
  a hard limit: count your words, and cut the least important line if you are over.

DUTIES EXTRACTED FROM THIS CHAPTER (computed, may be incomplete)
{json.dumps(duties, ensure_ascii=False)}

THE CHAPTER (Dutch, complete): {chapter['title']}
{text}"""


def article_target(n_words: int) -> list[int]:
    """Share of the article to keep: 50% at 100 words or fewer, 10% at 1,000 or more, sliding in between."""
    share = 0.5 if n_words <= 100 else 0.1 if n_words >= 1000 else 0.5 - 0.4 * math.log10(n_words / 100)
    mid = max(15, n_words * share)
    return [round(mid * 0.75), round(mid * 1.25)]


def articles_prompt(chapter: dict, rows: list[dict]) -> str:
    arts = [r for r in rows if r["article"] in set(chapter["articles"])]
    targets = {r["article"]: article_target(fs.words(r.get("text"))) for r in arts}
    text = "\n\n".join(f"### {r.get('heading') or 'Artikel ' + r['article']}  [article {r['article']}]\n{r.get('text') or ''}"
                       for r in arts)
    return f"""You are writing a plain-language card for EACH article of one chapter of a law, for the
general public and for lawmakers. A reader opens a card to understand one article without
reading the legal text, which is always shown next to it.

{STYLE}
(Here each card is about exactly one article, so you do not fill in `articles`.)

WHAT TO PRODUCE
One card per article below, in the same order, none skipped.
- article: the article number exactly as given in [article N].
- title: a plain title saying what the article is about.
- points: the content as separate short points. Each point is one or two sentences and can
  be read on its own. Put the main rule first, then who it applies to, then deadlines and
  figures, then the exceptions that change the outcome for ordinary cases.
- Length per card, in words per language, all points together, as [lowest, highest].
  Shorter articles keep more of their content, long ones less. The highest number is a
  hard limit: if a card is over it, drop the least important point.
  {json.dumps(targets)}

THE CHAPTER (Dutch, complete): {chapter['title']}
{text}"""


def map_prompt(sheet: dict, rows: list[dict]) -> str:
    law = "\n\n".join(f"### {r.get('heading') or 'Artikel ' + r['article']}  [article {r['article']}]\n{r.get('text') or ''}"
                      for r in rows)
    return f"""You are drawing the stakeholder map of a law: who the parties are and what each one owes
to, or may do towards, each other one. Legislation is a web of relationships; a reader
should be able to pick one party and see everything that runs to and from it.

{STYLE}

WHAT TO PRODUCE
- parties: the 6 to 9 parties that matter most, including the people the law protects,
  the organisations it binds, the authorities that supervise, and lawmakers where the law
  gives them tasks. `id` is a short lowercase ascii word. `name` is plain; where the law
  has its own label, put it in brackets. `role` is one plain sentence.
- relations: 16 to 24 arrows. Each runs from one party to another and says what the first
  must do for, may do towards, or must not do to the second (`kind`). One duty per arrow,
  in a short phrase. Cover every party with at least two arrows. Prefer relations with
  practical weight over procedure between authorities.

WHO OWES WHOM, COUNTED FROM THE LAW (computed, often without a recipient)
{json.dumps(sheet["stakeholder_matrix"][:40], ensure_ascii=False)}

THE LAW (Dutch, complete)
{law}"""


def check_prompt(lines: list[dict], rows: list[dict]) -> str:
    used = {a for ln in lines for a in ln["articles"]}
    text = "\n\n".join(f"### [article {r['article']}] {r.get('heading') or ''}\n{r.get('text') or ''}"
                       for r in rows if r["article"] in used)
    return f"""You are checking a plain-language summary of a law against the legal text.

The summary is written for people with no legal training. It is SUPPOSED to be shorter and
simpler than the law. Your job is to catch lines that would mislead such a reader. It is
not to make the summary as complete or as precise as the law.

For each line, read ONLY the articles it names and decide:
- supported: a reader who relies on this line is not misled. What it states follows from
  those articles.
- partly_supported: the main point follows, but the line leaves out or blurs something that
  changes the outcome in ordinary cases: a condition that decides whether the rule applies
  at all, the party it binds, a deadline or figure, or a main exception. A typical reader
  could wrongly conclude that they must act, or need not act.
- not_supported: the line states something those articles do not say, contradicts them,
  or gets a figure wrong.

These are NOT reasons to mark a line down:
- a plain name for a legal role ("an organisation" for the controller, "the regulator" for
  the supervisory authority), as long as the right party is meant;
- not repeating what the whole document is about (for example that the rules concern
  personal data, or natural persons);
- leaving out secondary detail, procedure or rare cases, or shortening a list with "such
  as", "for example" or "among other things";
- words such as "in principle", "normally", "in certain cases" or "where the conditions
  are met": they tell the reader honestly that conditions exist without listing them;
- a line that only says what a chapter or article is about, if that description fits;
- a statement that is correct but could be more precise.
When you hesitate between supported and partly_supported, ask whether a careful lawyer
would call the line wrong or misleading for a typical reader. If not, it is supported.
In a good summary most lines are supported.

Return one check per line, with the same id.
- note: one short plain sentence saying what is missing or wrong, in Dutch (nl) and in
  English (en). For a supported line: "ok" in both.
- suggestion: null for a supported line. Otherwise the same line, changed as little as
  possible so that it is supported, in Dutch and English. Keep the plain wording and the
  plain names; add at most one short clause, or correct the detail.

LINES
{json.dumps([{"id": ln["id"], "text_nl": ln["nl"], "text_en": ln["en"], "articles": ln["articles"]} for ln in lines], ensure_ascii=False, indent=1)}

LEGAL TEXT (Dutch)
{text}"""


_LEAD = re.compile(r"^(als|indien|wanneer|if|when)\s+", re.I)


def clean_if(text: str) -> str:
    """The writer often starts a condition with "If" itself and ends it with a comma."""
    text = _LEAD.sub("", text.strip()).rstrip(" ,.;")
    return text[:1].lower() + text[1:]


_THEN = re.compile(r"^(dan|then)\s+", re.I)


def clean_then(text: str) -> str:
    """The writer sometimes starts the consequence with "then" itself, which gave "then then"."""
    return _THEN.sub("", text.strip())


def flatten(kind: str, out: dict) -> list[dict]:
    """Every checkable line of a unit as {id, nl, en, articles}. Foundations are added in run_unit."""
    lines = []
    def add(i, nl, en, arts):
        lines.append({"id": i, "nl": nl, "en": en, "articles": arts})
    if kind == "chapter":
        add("about", out["about"]["nl"], out["about"]["en"], out["about"]["articles"])
        for i, ln in enumerate(out["lines"]):
            add(f"line{i}", ln["nl"], ln["en"], ln["articles"])
        return lines
    if kind == "articles":
        for c in out["cards"]:
            for i, pt in enumerate(c["points"]):
                add(f"art{c['article']}_{i}", pt["nl"], pt["en"], [c["article"]])
        return lines
    if kind == "map":
        name = {p["id"]: p["name"] for p in out["parties"]}
        verb = {"must": ("moet", "must"), "may": ("mag", "may"), "must_not": ("mag niet", "must not")}
        for i, r in enumerate(out["relations"]):
            a, b = name.get(r["from_id"], {"nl": r["from_id"], "en": r["from_id"]}), name.get(r["to_id"], {"nl": r["to_id"], "en": r["to_id"]})
            add(f"rel{i}", f"{a['nl']} {verb[r['kind']][0]} tegenover {b['nl']}: {r['what']['nl']}",
                f"{a['en']} {verb[r['kind']][1]}, towards {b['en']}: {r['what']['en']}", r["articles"])
        return lines
    for key in ("in_short", "most_consequential", "exceptions"):
        for i, ln in enumerate(out[key]):
            add(f"{key}{i}", ln["nl"], ln["en"], ln["articles"])
    add("purpose", out["purpose"]["nl"], out["purpose"]["en"], out["purpose"]["articles"])
    for i, c in enumerate(out["core_concepts"]):
        add(f"core_concepts{i}", f"{c['term']}: {c['plain']['nl']}", f"{c['term_en']}: {c['plain']['en']}", c["articles"])
    for i, t in enumerate(out["if_then"]):
        add(f"if_then{i}", f"Als {clean_if(t['if_']['nl'])}, dan {clean_then(t['then']['nl'])}",
            f"If {clean_if(t['if_']['en'])}, then {clean_then(t['then']['en'])}", t["articles"])
    for i, s in enumerate(out["stakeholders"]):
        add(f"stakeholders{i}", f"{s['who']['nl']} -> {s['to_whom']['nl']}: {s['owes']['nl']}",
            f"{s['who']['en']} -> {s['to_whom']['en']}: {s['owes']['en']}", s["articles"])
    return lines


_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def mechanical_checks(lines: list[dict], rows: list[dict]) -> dict:
    """No model: named articles exist, and every figure in a line appears in those articles."""
    by_art = {r["article"]: re.sub(r"[\s .]", "", r.get("text") or "") for r in rows}
    out = {}
    for ln in lines:
        missing = [a for a in ln["articles"] if a not in by_art]
        src = "".join(by_art.get(a, "") for a in ln["articles"])
        own_refs = set(ln["articles"])
        figures = [n for n in _NUM.findall(ln["nl"]) if n not in own_refs]
        bad = [n for n in figures if n.replace(".", "") not in src]
        out[ln["id"]] = {"no_article_named": not ln["articles"], "unknown_articles": missing,
                         "figures_not_in_source": bad}
    return out


def n_words(lines: list[dict], lang: str) -> int:
    return sum(len(ln[lang].split()) for ln in lines)


def call(client, prompt: str, schema):
    for attempt in range(3):
        try:
            resp = client.responses.parse(model=MODEL, input=prompt, text_format=schema,
                                          reasoning={"effort": EFFORT})
            return resp.output_parsed.model_dump()
        except Exception as e:  # network or rate hiccup: brief backoff, then give up on this one
            if attempt == 2 or "credit" in str(e).lower():
                raise
            time.sleep(3 * (attempt + 1))


_LOCK = threading.Lock()


def cached(client, cache: dict, key: str, prompt: str, schema):
    k = f"{key}::{VERSION}::{hashlib.sha1(prompt.encode('utf-8')).hexdigest()[:12]}"
    if k in cache:
        print(f"  cached  {key}", flush=True)
        return cache[k]
    out = call(client, prompt, schema)          # outside the lock: units run side by side
    with _LOCK:
        cache[k] = out
        c1.atomic_write_json(CACHE, cache)
    print(f"  called  {key}", flush=True)
    return out


OVER = 1.10      # a draft more than 10% over its limit goes back once to be shortened


def too_long(kind: str, out: dict, target: list[int], limits: dict) -> "dict | None":
    """What is over its limit, or None. A chapter has one limit; article cards have one each."""
    if kind in ("chapter", "law"):
        n = n_words(flatten(kind, out), "nl") + sum(len(f"{f['source']['nl']}: {f['plain']['nl']}".split())
                                                    for f in out.get("foundations", []))
        return {"words_now": n, "limit": target[1]} if n > target[1] * OVER else None
    if kind == "articles":
        over = {}
        for c in out["cards"]:
            n = sum(len(pt["nl"].split()) for pt in c["points"])
            if n > limits.get(c["article"], n) * OVER:
                over[c["article"]] = {"words_now": n, "limit": limits[c["article"]]}
        return over or None
    return None


def shorten_prompt(kind: str, out: dict, over: dict) -> str:
    """The mentor's steps 8 and 9: shorten, then simplify again. Nothing may be added."""
    what = (f"The summary is {over['words_now']} words in Dutch. The limit is {over['limit']} words per language."
            if kind in ("chapter", "law") else
            "These cards are over their limit (words in Dutch now, and the limit per language):\n"
            + json.dumps(over, ensure_ascii=False))
    return f"""This plain-language draft about a law is too long. Shorten it.

{what}

HOW
- Drop the least important line or point first. Then tighten the wording of what is left.
- Keep what an ordinary reader most needs: the main rule, who it applies to, deadlines
  and figures, and the exceptions that change the outcome in ordinary cases.
- Do not add anything that is not in the draft. Keep every figure exact. Keep the article
  numbers of the lines you keep. Dutch and English must still say the same thing.
- Return the complete draft in the same structure, including the parts you did not change.

DRAFT
{json.dumps(out, ensure_ascii=False)}"""


def run_unit(client, cache, iid, kind, name, prompt, schema, rows, target) -> dict:
    out = cached(client, cache, f"{iid}::{name}::write", prompt, schema)
    limits = {r["article"]: article_target(fs.words(r.get("text")))[1] for r in rows}
    over = too_long(kind, out, target, limits)
    if over:
        shorter = cached(client, cache, f"{iid}::{name}::shorten", shorten_prompt(kind, out, over), schema)
        # The model must not lose or invent a card while shortening; if it did, keep the draft.
        if kind != "articles" or [c["article"] for c in shorter["cards"]] == [c["article"] for c in out["cards"]]:
            out = shorter
    lines = flatten(kind, out)
    checks = cached(client, cache, f"{iid}::{name}::check", check_prompt(lines, rows), CheckResult)["checks"]
    by_id = {c["id"]: c for c in checks}
    mech = mechanical_checks(lines, rows)
    unchecked = {"verdict": "not_checked", "suggestion": None,
                 "note": {"nl": "De controle gaf geen oordeel.", "en": "The check returned no verdict."}}
    for ln in lines:
        ln["check"] = by_id.get(ln["id"], unchecked)
        ln["mechanical"] = mech[ln["id"]]
    for i, f in enumerate(out.get("foundations", []) if kind == "law" else []):
        # Not in the corpus, so nothing to check against: the model's own knowledge, shown as such.
        lines.append({"id": f"foundations{i}", "nl": f"{f['source']['nl']}: {f['plain']['nl']}",
                      "en": f"{f['source']['en']}: {f['plain']['en']}", "articles": [],
                      "mechanical": {"no_article_named": False, "unknown_articles": [], "figures_not_in_source": []},
                      "check": {"verdict": "not_checked", "suggestion": None,
                                "note": {"nl": "Eigen kennis van de AI: deze tekst zit niet in de verzameling.",
                                         "en": "The AI's own knowledge: this text is not in the collection."}}})
    words = {"nl": n_words(lines, "nl"), "en": n_words(lines, "en")}
    return {"kind": kind, "name": name, "content": out, "lines": lines, "n_words": words,
            "target_words": target, "within_target": target[0] <= words["nl"] <= target[1],
            "n_supported": sum(1 for ln in lines if ln["check"]["verdict"] == "supported"),
            "n_lines": len(lines)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("instrument_id")
    ap.add_argument("--chapters", default="", help="chapter numbers, 1-based, e.g. 3,4")
    ap.add_argument("--all-chapters", action="store_true")
    ap.add_argument("--articles", default="", help="chapters whose articles get a card each, e.g. 3,4")
    ap.add_argument("--map", action="store_true", help="the stakeholder map")
    ap.add_argument("--no-law", action="store_true")
    ap.add_argument("--all", action="store_true", help="law sheet, map, every chapter and every article")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    iid = args.instrument_id
    sheet = fs.build(iid)
    rows = json.loads(fs.provisions_path(iid).read_text(encoding="utf-8"))
    rows = rows if isinstance(rows, list) else rows["provisions"]
    everything = list(range(len(sheet["chapters"])))
    picked = everything if args.all_chapters or args.all else [int(x) - 1 for x in args.chapters.split(",") if x.strip()]
    carded = everything if args.all else [int(x) - 1 for x in args.articles.split(",") if x.strip()]

    units = [] if args.no_law else [("law", "law", law_prompt(sheet, rows), LawSheet, sheet["length_targets_words"]["law_sheet"])]
    for i in picked:
        ch = sheet["chapters"][i]
        units.append(("chapter", f"chapter{i + 1}", chapter_prompt(sheet, ch, rows), ChapterSheet,
                      [max(ch["target_words"][0], 40), max(ch["target_words"][1], 80)]))
    for i in carded:
        ch = sheet["chapters"][i]
        per = [article_target(fs.words(r.get("text"))) for r in rows if r["article"] in set(ch["articles"])]
        units.append(("articles", f"articles{i + 1}", articles_prompt(ch, rows), ArticleCards,
                      [sum(t[0] for t in per), sum(t[1] for t in per)]))
    if args.map or args.all:
        units.append(("map", "map", map_prompt(sheet, rows), StakeholderMap, [0, 10 ** 6]))
    chars = sum(len(u[2]) for u in units)
    print(f"{_LAW_FOLDERS[iid]}: {len(units)} units, {2 * len(units)} calls at most, "
          f"about {chars // 1000}k characters to write from (model {MODEL}, effort {EFFORT})", flush=True)
    if args.dry_run:
        return

    from openai import OpenAI
    client = OpenAI()  # the key is loaded from .env when detect_c1_contradiction is imported
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    path = fs.OUT / f"simplified_{_LAW_FOLDERS[iid]}.json"
    result = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"units": {}}
    result.update({"instrument_id": iid, "n_words_law": sheet["n_words"], "n_articles": sheet["n_articles"],
                   "model": MODEL, "chapters": [{"number": i + 1, "title": c["title"], "articles": c["articles"],
                                                 "n_words": c["n_words"]} for i, c in enumerate(sheet["chapters"])]})
    failed = []
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = {pool.submit(run_unit, client, cache, iid, kind, name, prompt, schema, rows, target): (name, target)
                   for kind, name, prompt, schema, target in units}
        for fut in as_completed(futures):
            name, target = futures[fut]
            try:
                u = fut.result()
            except Exception as e:      # one unit failing must not lose the others; a re-run only pays for this one
                failed.append(name)
                print(f"  {name:10} FAILED: {str(e)[:160]}", flush=True)
                continue
            result["units"][name] = u
            c1.atomic_write_json(path, result)
            print(f"  {name:10} {u['n_words']['nl']} words nl (target {target[0]}-{target[1]}), "
                  f"{u['n_supported']}/{u['n_lines']} lines supported", flush=True)
    print(f"-> {path}" + (f"   FAILED, run again: {', '.join(failed)}" if failed else ""))


if __name__ == "__main__":
    main()
