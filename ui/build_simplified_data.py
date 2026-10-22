# -*- coding: utf-8 -*-
"""
Builds the data the review interface needs for the plain-language laws.

    python pipeline/c4/write_simplified.py 32016R0679 --all   # first: write and check (paid)
    python ui/build_simplified_data.py                        # then this, no model

Writes:
  ui/data/simplified.json          the list of laws: which are written, how many lines each
                                   has, and the key of every line (so the sidebar can count
                                   reviewed lines without loading every law)
  ui/data/simplified_<law>.json    one file per written law: the one-page sheet, the
                                   stakeholder map, the chapter summaries, the article cards,
                                   and the original Dutch text of every article

Nothing here calls a model. It adds three computed things to what the writer produced:
  - the length of each article card next to its target and its source article;
  - which articles are "key" articles (the top 20% from build_fact_sheet.importance);
  - for each chapter, how much of it the public document shows by default. A chapter that
    puts at least two duties on organisations or people is shown in full; any other chapter
    (scope and definitions, instructions to authorities, final provisions) is shown as one
    sentence. A reviewing lawyer can change this per chapter in the interface.
"""
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
UI = Path(__file__).resolve().parent
ROOT = UI.parent
for _d in (UI, ROOT / "pipeline", ROOT / "pipeline" / "c1", ROOT / "pipeline" / "c4", ROOT / "pipeline" / "preprocessing"):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

import build_review_data as b          # noqa: E402  (bilingual law names)
import build_fact_sheet as fs          # noqa: E402
import detect_c1_contradiction as c1   # noqa: E402
import write_simplified as ws          # noqa: E402
from graph import _LAW_FOLDERS         # noqa: E402

OUT = UI / "data"
FULL_NAMES = {"32016R0679": b.bi("Algemene verordening gegevensbescherming (AVG)", "General Data Protection Regulation (GDPR)")}
PUBLIC_WHO = {"REGULATED_ENTITY", "DATA_SUBJECT"}
PUBLIC_MIN_DUTIES = 2


def source_url(iid: str) -> str:
    if iid.startswith("BWBR"):
        return f"https://wetten.overheid.nl/{iid}"
    return f"https://eur-lex.europa.eu/legal-content/NL/TXT/?uri=CELEX:{iid}"


def audience(chapter: dict, duties: list[dict]) -> str:
    arts = set(chapter["articles"])
    n = sum(1 for d in duties if d["article"] in arts and d["who_type"] in PUBLIC_WHO)
    return "public" if n >= PUBLIC_MIN_DUTIES else "background"


def line_view(ln: dict) -> dict:
    m = ln.get("mechanical") or {}
    return {"id": ln["id"], "nl": ln["nl"], "en": ln["en"], "articles": ln["articles"],
            "verdict": ln["check"]["verdict"], "note": ln["check"]["note"], "suggestion": ln["check"].get("suggestion"),
            "figures_not_in_source": m.get("figures_not_in_source") or []}


def _after(text: str, sep: str) -> "str | None":
    """The part after the first `sep`, or None when the text does not have that shape."""
    head, found, tail = text.partition(sep)
    return tail.strip() if found and tail.strip() else None


# Where the condition ends when a repaired if/then line has no ", then" / ", dan": in English
# at ", you must" (or another modal), in Dutch at the inverted main clause (", meld je").
_CONSEQUENCE = {
    "en": re.compile(r",\s+(?=(?:you|they|it|the \w+)\s+(?:must|may|can|cannot|have to|need to|should|shall|are|will|do not)\b)", re.I),
    "nl": re.compile(r",\s+(?=\w+\s+(?:je|u|jij|zij|ze|het|hij)\b)", re.I),
}


def split_if_then(text: str, lang: str) -> "tuple[str, str] | None":
    """("condition", "consequence") of one repaired if/then line, or None if it does not split."""
    head, sep, tail = text.partition(", dan " if lang == "nl" else ", then ")
    if not sep:
        m = _CONSEQUENCE[lang].search(text)
        if not m:
            return None
        head, tail = text[:m.start()], text[m.end():]
    return (head, tail.strip()) if ws.clean_if(head) and ws.clean_then(tail) else None


def apply_corrections(kind: str, content: dict, lines: list) -> None:
    """
    Where the check found a gap and proposed a repaired line, that repair becomes the line.

    Every line still goes to a lawyer for approval, so the reader should see the best wording the
    system has, not a gap plus a separate fix. The original AI wording stays in `original`, and the
    verdict becomes "corrected" (the repaired text itself has not been re-checked). A repair whose
    shape cannot be fitted back into its card (for example an if/then that no longer splits into
    "if" and "then") is left as a suggestion for the lawyer instead.
    """
    by_card = {c["article"]: c for c in content.get("cards", [])} if kind == "articles" else {}
    for ln in lines:
        sug = ln.get("suggestion")
        if ln["verdict"] not in ("partly_supported", "not_supported") or not sug or not sug.get("nl") or not sug.get("en"):
            continue
        lid, done = ln["id"], False
        if kind == "chapter":
            if lid == "about":
                content["about"].update(nl=sug["nl"], en=sug["en"]); done = True
            elif lid.startswith("line"):
                content["lines"][int(lid[4:])].update(nl=sug["nl"], en=sug["en"]); done = True
        elif kind == "articles":
            art, _, i = lid[3:].rpartition("_")
            if art in by_card:
                by_card[art]["points"][int(i)].update(nl=sug["nl"], en=sug["en"]); done = True
        elif kind == "map" and lid.startswith("rel"):
            # the repair is usually "who -> whom: what", sometimes only the "what" itself
            what = {lang: _after(sug[lang], ": ") or sug[lang].strip() for lang in ("nl", "en")}
            if all(what.values()):
                content["relations"][int(lid[3:])]["what"] = what; done = True
        elif kind == "law":
            for key in ("in_short", "most_consequential", "exceptions"):
                if lid.startswith(key) and lid[len(key):].isdigit():
                    content[key][int(lid[len(key):])].update(nl=sug["nl"], en=sug["en"]); done = True
            if lid == "purpose":
                content["purpose"].update(nl=sug["nl"], en=sug["en"]); done = True
            elif lid.startswith("if_then"):
                parts = {lang: split_if_then(sug[lang], lang) for lang in ("nl", "en")}
                if all(parts.values()):
                    t = content["if_then"][int(lid[7:])]
                    t["if_"] = {lang: ws.clean_if(p[0]) for lang, p in parts.items()}
                    t["then"] = {lang: ws.clean_then(p[1]) for lang, p in parts.items()}
                    done = True
            elif lid.startswith("core_concepts"):
                plain = {lang: _after(sug[lang], ": ") for lang in ("nl", "en")}
                if all(plain.values()):
                    content["core_concepts"][int(lid[13:])]["plain"] = plain; done = True
            elif lid.startswith("foundations"):
                plain = {lang: _after(sug[lang], ": ") for lang in ("nl", "en")}
                if all(plain.values()):
                    content["foundations"][int(lid[11:])]["plain"] = plain; done = True
            elif lid.startswith("stakeholders"):
                owes = {lang: _after(sug[lang].partition("->")[2], ": ") for lang in ("nl", "en")}
                if all(owes.values()):
                    content["stakeholders"][int(lid[12:])]["owes"] = owes; done = True
        if done:
            ln["original"] = {"nl": ln["nl"], "en": ln["en"]}
            ln["nl"], ln["en"], ln["verdict"] = sug["nl"], sug["en"], "corrected"


def law_data(iid: str) -> "dict | None":
    folder = _LAW_FOLDERS[iid]
    path = fs.OUT / f"simplified_{folder}.json"
    if not path.exists():
        return None
    res = json.loads(path.read_text(encoding="utf-8"))
    if "law" not in res["units"]:
        return None
    sheet = fs.build(iid)
    rows = json.loads(fs.provisions_path(iid).read_text(encoding="utf-8"))
    rows = rows if isinstance(rows, list) else rows["provisions"]
    key = {a["article"] for a in sheet["articles_by_importance"] if a["key_article"]}
    chapter_of = {a: i + 1 for i, c in enumerate(sheet["chapters"]) for a in c["articles"]}

    units = {}
    for name, u in res["units"].items():
        content = u["content"]
        if u["kind"] == "law":                 # shown as two cells, so the condition must not carry its own "If"
            for t in content["if_then"]:
                t["if_"] = {k: ws.clean_if(v) for k, v in t["if_"].items()}
                t["then"] = {k: ws.clean_then(v) for k, v in t["then"].items()}
        if u["kind"] == "articles":
            for card in content["cards"]:
                src = next((fs.words(r.get("text")) for r in rows if r["article"] == card["article"]), 0)
                card["source_words"] = src
                card["target_words"] = ws.article_target(src)
        lines = [line_view(ln) for ln in u["lines"]]
        apply_corrections(u["kind"], content, lines)
        for ln in lines:                       # the writer's own "then", sometimes copied into the repair
            for text in (ln, ln.get("original") or {}) if ln["id"].startswith("if_then") else ():
                if "nl" in text:
                    text["nl"] = text["nl"].replace(", dan dan ", ", dan ")
                    text["en"] = text["en"].replace(", then then ", ", then ")
        for card in content.get("cards", []) if u["kind"] == "articles" else []:
            card["n_words"] = {lang: sum(len(pt[lang].split()) for pt in card["points"]) for lang in ("nl", "en")}
        units[name] = {"kind": u["kind"], "content": content, "lines": lines,
                       "n_words": {lang: sum(len(ln[lang].split()) for ln in lines) for lang in ("nl", "en")},
                       "target_words": u["target_words"]}

    return {
        "id": folder, "instrument_id": iid, "name": FULL_NAMES.get(iid, b.LAWS[iid]), "short": b.LAWS[iid],
        "source_url": source_url(iid), "n_words": sheet["n_words"], "n_articles": sheet["n_articles"],
        "model": res.get("model"), "generated_on": date.today().isoformat(),
        "chapters": [{"number": i + 1, "legal_title": c["title"], "articles": c["articles"], "n_words": c["n_words"],
                      "audience": audience(c, sheet["duties"]),
                      "has_summary": f"chapter{i + 1}" in units, "has_cards": f"articles{i + 1}" in units}
                     for i, c in enumerate(sheet["chapters"])],
        "units": units,
        "articles": {r["article"]: {"heading": r.get("heading") or f"Artikel {r['article']}", "text": r.get("text") or "",
                                    "n_words": fs.words(r.get("text")), "key": r["article"] in key,
                                    "chapter": chapter_of.get(r["article"]),
                                    "url": b.official_url(iid, r["article"], r)} for r in rows}
                    # The Charter, the Treaties and the Grondwet: the official document (no article anchor checked there).
                    | {k: {"heading": v["heading"], "text": v["text"], "n_words": fs.words(v["text"]), "key": False,
                           "chapter": None, "official_source": True, "url": source_url(v["celex"])}
                       for k, v in res.get("sources", {}).items()},
    }


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    index = []
    for iid, folder in _LAW_FOLDERS.items():
        law = law_data(iid)
        entry = {"id": folder, "instrument_id": iid, "name": FULL_NAMES.get(iid, b.LAWS[iid]), "short": b.LAWS[iid],
                 "ready": bool(law)}
        if law:
            c1.atomic_write_json(OUT / f"simplified_{folder}.json", law, indent=None)
            keys = [f"{iid}::{name}::{ln['id']}" for name, u in law["units"].items() for ln in u["lines"]]
            confirmed = sum(1 for u in law["units"].values() for ln in u["lines"] if ln["verdict"] == "supported")
            entry.update(n_words=law["n_words"], n_articles=law["n_articles"], n_chapters=len(law["chapters"]),
                         n_chapters_written=sum(1 for c in law["chapters"] if c["has_summary"]),
                         n_confirmed=confirmed, keys=keys)
            print(f"{folder:22} {len(keys)} lines, {confirmed} confirmed by the check, "
                  f"{entry['n_chapters_written']}/{entry['n_chapters']} chapters written")
        index.append(entry)
    c1.atomic_write_json(OUT / "simplified.json", {"generated_on": date.today().isoformat(), "laws": index}, indent=None)
    print(f"-> {OUT / 'simplified.json'}")


if __name__ == "__main__":
    main()
