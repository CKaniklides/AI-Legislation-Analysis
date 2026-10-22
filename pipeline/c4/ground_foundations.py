# -*- coding: utf-8 -*-
"""
Grounds the "Where it comes from" lines of a plain-language law in the official texts they cite.

    python pipeline/c4/ground_foundations.py 32016R0679     # one check call per law
    python ui/build_simplified_data.py                      # then rebuild the interface data

The writer names the articles of the EU Charter, the EU Treaties or the Dutch Constitution
that a law rests on, from its own knowledge, because those texts are not in the corpus.
This step fetches exactly the cited articles from EUR-Lex (Dutch), stores them next to the
corpus in data/laws/foundations/, and runs the same faithfulness check as every other line:
does the official article say what the line says? After that these lines are grounded like
the rest, and the reader can open the official text from the line.

A source this step cannot recognise or fetch stays "not_checked", as before. The Dutch
Constitution is not fetched yet; no law written so far cites it.
"""
import html
import json
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
_HERE = Path(__file__).resolve().parent
for _d in (_HERE, _HERE.parent, _HERE.parent / "c1", _HERE.parent / "preprocessing"):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

import build_fact_sheet as fs
import detect_c1_contradiction as c1
import write_simplified as ws
from graph import _LAW_FOLDERS

STORE = fs.DATA / "laws" / "foundations"
# English name in the source line -> (CELEX of the consolidated 2016 text, short Dutch label, Dutch title)
SOURCES = {
    "charter": ("12016P", "Handvest", "Handvest van de grondrechten van de Europese Unie"),
    "functioning": ("12016E", "VWEU", "Verdrag betreffende de werking van de Europese Unie"),
    "european union,": ("12016M", "VEU", "Verdrag betreffende de Europese Unie"),
}


def identify(source_en: str):
    """('12016P', 'Handvest', title, '8') for 'Charter of Fundamental Rights ..., Article 8', else None."""
    m = re.search(r"article\s+(\d+[a-z]?)", source_en, re.I)
    if not m:
        return None
    low = source_en.lower()
    for needle, (celex, label, title) in SOURCES.items():
        if needle in low and not (needle == "european union," and ("functioning" in low or "charter" in low)):
            return celex, label, title, m.group(1)
    return None


def parse_articles(raw: str) -> dict:
    """Article number -> heading and text, from an EUR-Lex HTML page."""
    text = html.unescape(re.sub(r"<[^>]+>", "\n", raw))
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    parts = re.split(r"\n(Artikel \d+[a-z]?)\n", text)
    arts = {}
    for i in range(1, len(parts) - 1, 2):
        num = parts[i].split()[1]
        body = parts[i + 1].strip().split("\n")
        if num not in arts:                       # the first occurrence is the article itself, later ones are protocols
            arts[num] = {"heading": body[0].strip(), "text": "\n".join(x.strip() for x in body[1:]).strip()}
    return arts


def articles_of(celex: str, saved_html: "Path | None" = None) -> dict:
    """All articles of one official text, fetched once from EUR-Lex and kept in data/laws/foundations/."""
    path = STORE / f"{celex}_nl.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    url = f"https://eur-lex.europa.eu/legal-content/NL/TXT/HTML/?uri=CELEX:{celex}/TXT"
    if saved_html:                                # a page downloaded earlier, when EUR-Lex rate-limits
        raw = saved_html.read_text(encoding="utf-8", errors="replace")
    else:
        # EUR-Lex does not answer Python's own HTTP client (the read times out), but it does answer curl.
        raw = subprocess.run(["curl", "-sL", "--max-time", "90", "-A", "Mozilla/5.0", url], capture_output=True, check=True).stdout.decode("utf-8", "replace")
    articles = parse_articles(raw)
    if not articles:
        # EUR-Lex can answer an automated request with an empty page (HTTP 202, its bot check). Storing
        # that would make every later run trust an empty text, so nothing is stored (seen 21 October 2026).
        raise RuntimeError(f"no articles in the EUR-Lex page for {celex} (probably its bot check): open {url} "
                           "in a browser, save the page, and pass it as saved_html")
    STORE.mkdir(parents=True, exist_ok=True)
    data = {"celex": celex, "source": url, "articles": articles}
    c1.atomic_write_json(path, data)
    return data


def ground(iid: str) -> None:
    folder = _LAW_FOLDERS[iid]
    path = fs.OUT / f"simplified_{folder}.json"
    res = json.loads(path.read_text(encoding="utf-8"))
    law = res["units"]["law"]
    lines = {ln["id"]: ln for ln in law["lines"]}
    rows, todo = [], []
    res.setdefault("sources", {})
    for i, f in enumerate(law["content"].get("foundations", [])):
        found = identify(f["source"]["en"])
        if not found:
            print(f"  foundations{i}: not a recognised source, stays unchecked ({f['source']['en']})")
            continue
        celex, label, title, num = found
        try:
            art = articles_of(celex)["articles"].get(num)
        except RuntimeError as e:
            print(f"  foundations{i}: stays unchecked, {e}")
            continue
        if not art or not art["text"]:
            print(f"  foundations{i}: {label} article {num} not found in the official text, stays unchecked")
            continue
        key = f"{label} {num}"
        res["sources"][key] = {"heading": f"{title}, artikel {num}. {art['heading']}", "text": art["text"], "celex": celex}
        rows.append({"article": key, "heading": res["sources"][key]["heading"], "text": art["text"]})
        ln = lines[f"foundations{i}"]
        ln["articles"] = [key]
        todo.append(ln)
    if not todo:
        print(f"{folder}: nothing to ground")
        return
    from openai import OpenAI
    cache = json.loads(ws.CACHE.read_text(encoding="utf-8")) if ws.CACHE.exists() else {}
    prompt = ws.check_prompt([{k: ln[k] for k in ("id", "nl", "en", "articles")} for ln in todo], rows)
    checks = {c["id"]: c for c in ws.cached(OpenAI(), cache, f"{iid}::law::ground", prompt, ws.CheckResult)["checks"]}
    for ln in todo:
        if ln["id"] in checks:
            ln["check"] = checks[ln["id"]]
            print(f"  {ln['id']}: {ln['articles'][0]} -> {ln['check']['verdict']}")
    c1.atomic_write_json(path, res)
    print(f"-> {path}")


if __name__ == "__main__":
    for iid in sys.argv[1:]:
        ground(iid)
