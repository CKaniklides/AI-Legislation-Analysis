"""
Stage 2 — Parse to Provisions (L1 -> L2), EU side.

The EU legal-text HTML (the "original" OJ-style rendering used by the AI Act, GDPR, DORA
and NIS2's own legal_text_nl.html — NOT the differently-templated "consolidated" rendering
fetched from legal-content/.../TXT/HTML, which drops the numbered lid-div structure this
parser depends on) has a genuinely different shape from the Dutch BWB XML, so this is a
separate parser, not a reuse of parse.py — but it targets the same output schema (Part 3.1):
article-level Provisions with a `leden` array.

Structure, confirmed by inspection (NIS2):
    body > div.eli-container > div.eli-subdivision#enc_1 (the enacting terms)
         > div#cpt_{roman}         (chapter, HOOFDSTUK)
             > div.eli-subdivision#art_{n}   (article)
                 > p.oj-ti-art                (heading, "Artikel N")
                 > div.eli-title#art_{n}.tit_1  (title, may be absent)
                 > div#{art:3}.{lid:3}        (one per lid, oj-normal paragraph text;
                                                the lid NUMBER is embedded as literal text
                                                at the start of the paragraph, not a
                                                separate element like Dutch's <lidnr>)
                     > table (0+)             (one table per lettered point -- a), b), ...
                                                -- not a single multi-row table the way
                                                Dutch CALS tables are)

Usage:
    python parse_eu.py Datasets/EU_DigitalLaw/NIS2/legal_text_nl.html BWBR-equivalent-id-or-celex
"""
import argparse
import json
import re
import sys
from pathlib import Path

import lxml.html as LH

sys.path.insert(0, str(Path(__file__).resolve().parent))
from parse import CONDITIONAL_OPERATORS, _conditional_counts  # noqa: E402 -- shared lexicon,
# defined once in parse.py so the Dutch and EU sides can't silently drift apart on it; both
# feed the same Stage 5 features and the same Stage 6 norm.deference extraction.


EU_INTRA_ARTIKEL_RE = re.compile(r"\bartikel\s+\d+[a-z]*\b", re.IGNORECASE)
EU_INTER_INSTRUMENT_RE = re.compile(
    r"\b(Verordening|Richtlijn)\s*\(?(EU|EG)?\)?\s*(nr\.?\s*)?\d{2,4}[/.]\d{1,5}", re.IGNORECASE)


def _eu_interdependence(text: str) -> dict:
    """Lower-confidence than the Dutch side's extref/intref counts, and said so explicitly
    rather than presented as equivalent: the EU HTML carries no cross-reference markup at
    all (checked directly -- zero <a> tags anywhere in a full GDPR article), so this is a
    text-pattern heuristic over bare prose, not a markup-grounded extraction. "artikel N"
    mentions are counted as intra-instrument candidates; a good fraction of those actually
    belong to a *different* instrument (the common phrasing is "artikel N van Richtlijn
    X"), which this heuristic cannot reliably separate out. xref_cross_jurisdiction is left
    at 0 for every EU provision: the Dutch corpus's "cites an EU instrument" signal doesn't
    have a natural EU-side counterpart (an EU regulation citing another EU regulation isn't
    a jurisdiction crossing), so forcing a value into that field would be more misleading
    than an honest zero.
    """
    return {
        "xref_intra_instrument_approx": len(EU_INTRA_ARTIKEL_RE.findall(text)),
        "xref_inter_instrument_approx": len(EU_INTER_INSTRUMENT_RE.findall(text)),
        "xref_cross_jurisdiction": 0,
        "method": "text_pattern_heuristic_no_markup_available",
    }


def _max_table_depth(el, current: int = 0) -> int:
    """EU analogue of parse.py's _max_lijst_depth: nesting depth of <table> elements,
    since a sub-list under a lettered point is a <table> nested in that point's own
    content cell here (confirmed while fixing the NIS2 nested-sub-point bug), not a
    <lijst> the way Dutch XML represents it."""
    deepest = current
    for child in el:
        d = current + 1 if child.tag == "table" else current
        deepest = max(deepest, _max_table_depth(child, d))
    return deepest

LID_NUM_RE = re.compile(r"^\s*(\d+)\.\s*")


def _table_points(table) -> list[str]:
    """Render one EU 'lettered point' table as a flat list of 'marker content' strings,
    recursing into sub-points.

    Real complexity, found by diffing NIS2 art. 2 against itself and noticing a known
    corrigendum-corrected phrase was simply absent from the parsed output: a point with
    its own sub-list (e.g. 'a) de diensten verleend worden door: i) ... ii) ... iii) ...')
    is NOT one table with several rows — the sub-points (i, ii, iii) are each a separate,
    single-row <table> nested INSIDE point a)'s own content <td>. A row-count check
    (assuming one row = one point) silently dropped every point that had a sub-list at
    all, sub-points included, with no error and no visibly wrong output — exactly the
    kind of gap that only a content diff catches, not a structural read of the parser.
    """
    out = []
    trs = table.xpath("./tbody/tr") or table.xpath("./tr")
    for tr in trs:
        tds = tr.xpath("./td")
        if len(tds) != 2:
            continue
        marker = tds[0].text_content().strip()
        content_td = tds[1]
        nested_tables = content_td.xpath("./table")
        full_text = re.sub(r"\s+", " ", content_td.text_content()).strip()
        for nt in nested_tables:
            nt_text = re.sub(r"\s+", " ", nt.text_content()).strip()
            full_text = full_text.replace(nt_text, "").strip()
        out.append(f"{marker} {full_text}".strip() if marker else full_text)
        for nt in nested_tables:
            out.extend(_table_points(nt))
    return out


def _lid_text(lid_div) -> tuple[str | None, str]:
    """Return (lid_number, text) for one numbered lid <div>. The number is embedded as
    literal text at the very start of the first paragraph (e.g. '1.   Elke lidstaat...'),
    not a separate element — extracted here and stripped from the returned text so it
    isn't duplicated, matching the Dutch parser's `leden[].text` convention."""
    parts = []
    lidnr = None
    first = True
    for child in lid_div.iterchildren():
        if child.tag == "table":
            parts.extend(_table_points(child))
            continue
        txt = re.sub(r"\s+", " ", child.text_content()).strip()
        if not txt:
            continue
        if first:
            m = LID_NUM_RE.match(txt)
            if m:
                lidnr = m.group(1)
                txt = txt[m.end():].strip()
            first = False
        parts.append(txt)
    return lidnr, "\n\n".join(parts)


def _article_number(art_div) -> str:
    return art_div.get("id", "").removeprefix("art_")


def parse_artikel(art_div, instrument_id: str, expression_id: str, chapter: str | None, paragraph: str | None = None) -> dict:
    number = _article_number(art_div)

    heading_el = art_div.xpath('.//p[contains(@class,"oj-ti-art")]')
    heading_prefix = heading_el[0].text_content().strip() if heading_el else f"Artikel {number}"
    titel_el = art_div.xpath(f'.//div[@id="art_{number}.tit_1"]')
    titel = re.sub(r"\s+", " ", titel_el[0].text_content()).strip() if titel_el else None
    heading = f"{heading_prefix}. {titel}" if titel else heading_prefix

    lid_divs = art_div.xpath(f'./div[starts-with(@id,"{number.zfill(3) if number.isdigit() else number}.")]')
    # ids are zero-padded to 3 digits on the article side (e.g. "023.001") regardless of
    # how the article number itself displays, so pad purely-numeric article numbers only
    paragraphs = []
    for lid_div in lid_divs:
        lidnr, text = _lid_text(lid_div)
        paragraphs.append({"number": lidnr, "text": text, "id": lid_div.get("id")})

    if paragraphs:
        full_text = "\n\n".join(f"{l['number']}\n{l['text']}" if l["number"] else l["text"] for l in paragraphs)
    else:
        # An article with no lid subdivisions at all (found on 10 of NIS2's 46 articles --
        # short closing/definitional articles, not a rare edge case). Skip the heading
        # paragraph (oj-ti-art) and title div (eli-title) by element, not by string-prefix
        # stripping — string-stripping "Artikel N" left the separate title text
        # ("Minimumharmonisatie" etc.) stuck onto the front of the real body text for
        # every one of these 10 articles, caught by inspecting them directly rather than
        # trusting a clean-looking heading count.
        parts = []
        for child in art_div.iterchildren():
            cls = child.get("class") or ""
            if "oj-ti-art" in cls or child.get("id", "").endswith(".tit_1"):
                continue
            txt = re.sub(r"\s+", " ", child.text_content()).strip()
            if txt:
                parts.append(txt)
        full_text = "\n\n".join(parts)

    return {
        "uid": f"eu:{instrument_id}:art{number}@{expression_id}",
        "eli": None,
        "celex": instrument_id,
        "instrument_id": instrument_id,
        "jurisdiction": "EU",
        "language": "nl",
        "article": number,
        "heading": heading,
        "chapter": chapter,
        "section": paragraph,  # an "Afdeling N" chapter subdivision -- see parse.py's
                               # equivalent field for why this isn't called "paragraph"
        "text": full_text,
        "paragraphs": paragraphs,
        "text_fidelity": "official_html",
        "source_ref": f"{expression_id}#art_{number}",
        "features": {
            "n_words": len(full_text.split()),
            **_eu_interdependence(full_text),
            "conditional_operators": _conditional_counts(full_text),
            "max_condition_depth": _max_table_depth(art_div),
        },
    }


def _heading_of(div, own_id: str) -> str | None:
    """'{LABEL}. {title}' for a chapter/section div, e.g. 'HOOFDSTUK III. Rechten van de
    betrokkene' or 'Afdeling 1. Transparantie en nadere regels'."""
    heading_el = div.xpath('./p[contains(@class,"oj-ti-section-1")]')
    label = heading_el[0].text_content().strip() if heading_el else own_id
    titel_el = div.xpath(f'./div[@id="{own_id}.tit_1"]')
    titel = re.sub(r"\s+", " ", titel_el[0].text_content()).strip() if titel_el else None
    return f"{label}. {titel}" if titel else label


def _walk_structure(div, instrument_id, expression_id, chapter, paragraph, out):
    """Recurse chapter -> (section)* -> article. Real complexity, found by GDPR coming back
    with 41 of its 99 articles: a chapter's articles are not always its direct children —
    four of GDPR's eleven chapters (III, IV, VI, VII) nest their articles one level deeper,
    inside numbered sections. A section's id is not a bare prefix like the chapter's own
    "cpt_III" — it's compound, "cpt_III.sct_1" — which a naive '"." in id means title-div,
    skip it' check (needed to skip real title sub-divs like "cpt_III.tit_1") also matches
    and incorrectly skips, orphaning every article under it with no error at all. Distinguish
    a title div (id ends ".tit_1") from a section div (id contains ".sct_") explicitly,
    rather than keying off "any dot" as a single signal for two different things.
    """
    own_id = div.get("id", "")
    if own_id.endswith(".tit_1"):
        return  # a title sub-div, not a container to walk into
    if own_id.startswith("cpt_") and ".sct_" not in own_id:
        chapter = _heading_of(div, own_id)
    elif ".sct_" in own_id:
        paragraph = _heading_of(div, own_id)
    elif own_id.startswith("art_"):
        out.append(parse_artikel(div, instrument_id, expression_id, chapter, paragraph))
        return  # articles don't contain nested chapters/sections/articles
    for child in div:
        if child.tag == "div":
            _walk_structure(child, instrument_id, expression_id, chapter, paragraph, out)


def parse(html_path: str, instrument_id: str, expression_id: str) -> list[dict]:
    tree = LH.parse(html_path)
    root = tree.getroot()
    provisions: list[dict] = []
    for chapter_div in root.xpath('//div[starts-with(@id,"cpt_")]'):
        if "." in chapter_div.get("id", ""):
            continue  # only start from a real top-level chapter; _walk_structure recurses in
        _walk_structure(chapter_div, instrument_id, expression_id, None, None, provisions)
    return provisions


def validate(provisions: list[dict]) -> list[str]:
    problems = []
    uids = [p["uid"] for p in provisions]
    if len(uids) != len(set(uids)):
        problems.append("duplicate uid found")
    for p in provisions:
        if not p["text"]:
            problems.append(f"empty text: {p['uid']}")
    return problems


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("html_path")
    ap.add_argument("instrument_id", help="CELEX or short instrument id, e.g. 32022L2555")
    ap.add_argument("expression_id", help="which expression this HTML is, e.g. the file name or a date tag")
    args = ap.parse_args()

    provisions = parse(args.html_path, args.instrument_id, args.expression_id)
    problems = validate(provisions)

    out_path = Path("data") / f"{args.instrument_id}_{args.expression_id}_provisions.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(provisions, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"[{args.instrument_id}] parsed {len(provisions)} provisions -> {out_path}")
    if problems:
        print(f"[{args.instrument_id}] VALIDATION PROBLEMS ({len(problems)}):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"[{args.instrument_id}] validation clean")
