# -*- coding: utf-8 -*-
"""
Targeted extraction for articles that exist ONLY in a consolidated EUR-Lex text, not in an
instrument's original OJ-style HTML — the AI Act's 6 Digital Omnibus insertions (4a, 60a,
75a-75d) are the motivating case: the content genuinely doesn't exist anywhere in the
original template's HTML, so there's nothing to patch a string into (unlike NIS2's
corrigenda, which corrected existing text).

This is deliberately NOT a general parser for the consolidated template family: that
template (class="norm" divs, no numbered lid ids) is used across a whole document, but is
needed here only for a handful of specific new articles, so a small targeted extractor is
the right scope -- not a second full parser to maintain in parallel with parse_eu.py.
"""
import re
import lxml.html as LH

LID_NUM_RE = re.compile(r"^\s*(\d+)\.\s*")


def _chapter_of(article_div) -> str | None:
    """Note: this consolidated template splits a chapter's label and title into two
    separate <p class="title-division-1"/"title-division-2"> siblings, not one heading
    paragraph plus a separate eli-title div the way the original template (and this same
    consolidated template's own ARTICLE headings) do -- confirmed by inspection, since
    reusing the original template's class name here silently returned the raw div id
    ('cpt_I') instead of a real heading, with no error."""
    anc = article_div.getparent()
    while anc is not None:
        aid = anc.get("id", "")
        if aid.startswith("cpt_") and "." not in aid:
            label_el = anc.xpath('./p[contains(@class,"title-division-1")]')
            title_el = anc.xpath('./p[contains(@class,"title-division-2")]')
            label = label_el[0].text_content().strip() if label_el else aid
            titel = re.sub(r"\s+", " ", title_el[0].text_content()).strip() if title_el else None
            return f"{label}. {titel}" if titel else label
        anc = anc.getparent()
    return None


def extract_new_article(html_path: str, article_id: str, instrument_id: str, expression_id: str) -> dict:
    root = LH.parse(html_path).getroot()
    art_div = root.xpath(f'//div[@id="{article_id}"]')[0]
    number = article_id.removeprefix("art_")

    heading_el = art_div.xpath('./p[contains(@class,"title-article-norm")]')
    heading_prefix = re.sub(r"\s+", " ", heading_el[0].text_content()).strip() if heading_el else f"Artikel {number}"
    titel_el = art_div.xpath(f'./div[@id="{article_id}.tit_1"]')
    titel = re.sub(r"\s+", " ", titel_el[0].text_content()).strip() if titel_el else None
    heading = f"{heading_prefix}. {titel}" if titel else heading_prefix

    paragraphs = []
    current_lidnr = None
    current_parts: list[str] = []

    def flush():
        if current_parts:
            paragraphs.append({"number": current_lidnr, "text": "\n\n".join(current_parts)})

    for child in art_div:
        cls = child.get("class") or ""
        if "title-article-norm" in cls or child.get("id", "").endswith(".tit_1"):
            continue
        if cls == "norm" or child.tag == "p" and "norm" in cls:
            txt = re.sub(r"\s+", " ", child.text_content()).strip()
            if not txt:
                continue
            m = LID_NUM_RE.match(txt)
            if m:
                flush()
                current_lidnr = m.group(1)
                current_parts = [txt[m.end():].strip()]
            else:
                # a continuation paragraph (no leading number) belongs to the lid in progress
                current_parts.append(txt)
    flush()

    full_text = "\n\n".join(f"{l['number']}\n{l['text']}" if l["number"] else l["text"] for l in paragraphs)

    return {
        "uid": f"eu:{instrument_id}:art{number}@{expression_id}",
        "celex": instrument_id,
        "instrument_id": instrument_id,
        "jurisdiction": "EU",
        "language": "nl",
        "article": number,
        "heading": heading,
        "chapter": _chapter_of(art_div),
        "section": None,
        "text": full_text,
        "paragraphs": paragraphs,
        "text_fidelity": "official_html_consolidated_new_article",
        "source_ref": f"{expression_id}#{article_id}",
        "note": "This article does not exist in the instrument's original (pre-amendment) HTML at "
                "all -- it was inserted by a later amending act (see amendment_history in the "
                "companion file) and is extracted here directly from the consolidated text, which "
                "uses a different HTML template (class=\"norm\" divs, no numbered lid ids) than the "
                "original -- lid boundaries are recovered from the leading number in each norm div's "
                "own text, same convention as the original template, just without a stable per-lid id."
    }


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    html_path = sys.argv[1]
    instrument_id = sys.argv[2]
    expression_id = sys.argv[3]
    article_ids = sys.argv[4:]

    results = [extract_new_article(html_path, aid, instrument_id, expression_id) for aid in article_ids]
    out_path = Path("data") / f"{instrument_id}_{expression_id}_new_articles.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"extracted {len(results)} new articles -> {out_path}")
    for r in results:
        print(f"  art {r['article']}: {len(r['leden'])} leden, {len(r['text'])} chars")
