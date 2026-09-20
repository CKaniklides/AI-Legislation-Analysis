"""
Stage 2 — Parse to Provisions (L1 -> L2), Dutch side.

Walks a BWB toestand XML and emits one Provision per <artikel>, matching the
schema in architecture_and_implementation_strategy.md Part 3.1: article-level
rows, lid-level text kept as sub-structure in `leden`. Norm extraction
(Stage 6) is a separate, later pass — this stage produces no `norms`.

Usage:
    python parse.py BWBR0040940 2026-09-01
"""
import argparse
import json
import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

# Stage 5's fixed conditionality lexicon (architecture doc Part 4, Stage 5) — also the
# input to Stage 6's norm.deference extraction, so it does double duty. Kept here, not in
# features.py, because both stages need the identical list and this is the one place it's
# defined.
CONDITIONAL_OPERATORS = ["tenzij", "behoudens", "voor zover", "mits", "indien",
                          "niet van toepassing indien", "onverminderd", "in afwijking van"]


def _max_lijst_depth(el, current: int = 0) -> int:
    """Max nesting depth of <lijst> elements anywhere under el (0 if none). ElementTree
    has no parent pointers, so this walks down tracking depth explicitly rather than
    walking up from each <lijst> found via findall(".//lijst")."""
    deepest = current
    for child in el:
        d = current + 1 if child.tag == "lijst" else current
        deepest = max(deepest, _max_lijst_depth(child, d))
    return deepest


def _conditional_counts(text: str) -> dict:
    counts = {}
    low = text.lower()
    for op in CONDITIONAL_OPERATORS:
        counts[op] = len(re.findall(r"\b" + re.escape(op) + r"\b", low))
    return counts

RAW_ROOT = Path(__file__).resolve().parent.parent / "raw" / "nl"
DATA_ROOT = Path(__file__).resolve().parent.parent / "data"


SKIP_TAGS = {"kop", "meta-data"}  # heading is captured separately; meta-data is provenance/jci, not article text


def _entry_text(entry) -> str:
    """Flatten a single CALS <entry>'s content (usually one <al>, possibly with <nadruk>)."""
    return re.sub(r"\s+", " ", "".join(entry.itertext())).strip()


def _table_rows(table) -> tuple[dict[str, str], list[dict[str, str]]] | None:
    """Parse a CALS table into (headers, rows) — headers keyed by colname, each row a dict
    of {header_label: value}. Returns None if the table doesn't have the expected CALS shape.

    Two real complexities, both found by diffing against hand-built datasets that got them
    wrong or gave up on them:
    - A cell that repeats across consecutive rows (e.g. Cbw art. 15: one minister covering
      several sectors) is NOT repeated in the XML — it appears once, with morerows="N"
      saying how many further rows it still applies to.
    - A header can span multiple rows (a grouping label over sub-labels) and multiple
      columns at once (Bijlage 35's enforcement table: "Bestuurlijke boete" spans two data
      columns) — both need combining into one label per actual column.
    """
    tgroup = table.find("tgroup")
    if tgroup is None:
        return None

    colnames = [cs.get("colname") for cs in tgroup.findall("colspec")]

    def _span(entry) -> tuple[int, int]:
        """Column index range an <entry> covers — just its own colname normally, but a
        wider range when it's a colspan (namest/nameend), e.g. Bijlage 35's enforcement
        table, where one header cell ('Bestuurlijke boete') spans two data columns."""
        c1 = entry.get("namest") or entry.get("colname")
        c2 = entry.get("nameend") or entry.get("colname")
        try:
            return colnames.index(c1), colnames.index(c2)
        except ValueError:
            i = colnames.index(entry.get("colname"))
            return i, i

    # Headers can span multiple rows (a grouping label over one row, sub-labels under it),
    # not just multiple columns — collect each row's contribution per column, then join
    # top-to-bottom, collapsing a value that repeats unchanged from the row above it.
    header_parts: dict[str, list[str]] = {c: [] for c in colnames}
    thead = tgroup.find("thead")
    if thead is not None:
        for row in thead.findall("row"):
            row_text = {c: None for c in colnames}
            for entry in row.findall("entry"):
                i1, i2 = _span(entry)
                txt = _entry_text(entry)
                for idx in range(i1, i2 + 1):
                    row_text[colnames[idx]] = txt
            for c in colnames:
                if row_text[c] and (not header_parts[c] or header_parts[c][-1] != row_text[c]):
                    header_parts[c].append(row_text[c])
    headers = {c: " – ".join(header_parts[c]) if header_parts[c] else c for c in colnames}
    current = {c: None for c in colnames}   # current value per column
    remaining = {c: 0 for c in colnames}    # how many more rows that value still covers

    tbody = tgroup.find("tbody")
    if tbody is None:
        return None

    rows = []
    for row in tbody.findall("row"):
        present = {e.get("colname"): e for e in row.findall("entry")}
        for c in colnames:
            if c in present:
                current[c] = _entry_text(present[c])
                remaining[c] = int(present[c].get("morerows", "0") or "0")
            elif remaining[c] > 0:
                remaining[c] -= 1
            # else: no value for this column at all (e.g. col3 blank on a row that
            # only sets col1/col2) — current[c] keeps whatever it last was, matching
            # how the source renders an inherited-but-unstated cell.
        rows.append({headers.get(c, c): (current[c] or "(geen)") for c in colnames})

    return headers, rows


def _table_text(table) -> str:
    """Render a CALS table as one line per row: '{header1}: {val1} | {header2}: {val2} | ...'."""
    parsed = _table_rows(table)
    if parsed is None:
        return _flatten_generic(table)
    _headers, rows = parsed
    return "\n\n".join(" | ".join(f"{k}: {v}" for k, v in row.items()) for row in rows)


def _flatten_generic(el) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def _text(el, include_lidnr: bool = True) -> str:
    """Flatten an element's text content, preserving list markers and paragraph breaks.
    Skips <kop> (the heading, captured separately) and <meta-data> (provenance/jci,
    not article text) wherever they appear in the tree, not just at the top level.

    include_lidnr=False drops the leading lid number ("1", "2", ...) that the source
    embeds as a <lidnr> child of <lid> — wanted when rendering the whole article's `text`
    (matches the existing corpus convention: "1 ... 2 ..." so a reader can tell the
    paragraphs apart in one combined block), redundant when rendering a single lid in
    isolation for `paragraphs[]`, which already carries its own explicit "number" field."""
    parts = []

    def walk(node):
        if node.tag in SKIP_TAGS:
            return
        if node.tag == "lidnr" and not include_lidnr:
            return
        if node.tag == "table":
            parts.append("\n\n" + _table_text(node) + "\n\n")
            return
        if node.tag == "li.nr":
            parts.append((node.text or "") + " ")
        elif node.tag == "redactie":
            # an editorial note (e.g. "this article amends another law's text"), not
            # operative statutory language — wetten.overheid.nl displays these in square
            # brackets, and the existing corpus follows that convention; match it rather
            # than let it read as if it were ordinary article text.
            parts.append(f"[Red: {(node.text or '').strip()}]")
            return
        elif node.text:
            parts.append(node.text)
        for child in node:
            walk(child)
            if child.tail:
                parts.append(child.tail)
        if node.tag in ("al", "li", "lid"):
            parts.append("\n\n")

    walk(el)
    txt = "".join(parts)
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r"[ \t]*\n[ \t]*", "\n", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt.strip()


def _jci_for(el, onderdeel_prefix: str) -> str | None:
    """Find the <jci versie="1.3" .../> element under el whose onderdeel matches exactly."""
    meta = el.find("meta-data")
    if meta is None:
        return None
    for jci in meta.iter("jci"):
        if jci.get("versie") == "1.3" and jci.get("onderdeel") == onderdeel_prefix:
            return jci.get("verwijzing")
    return None


def _heading_of(el, label_word: str) -> str | None:
    """Build '{label_word} {nr}. {titel}' for a <hoofdstuk>/<paragraaf>/<artikel> element,
    e.g. 'Hoofdstuk 8. Significante incidenten, ...' or 'Paragraaf 8.1. Meldplicht'."""
    kop = el.find("kop")
    if kop is None:
        return None
    nr = kop.findtext("nr")
    titel_el = kop.find("titel")
    titel = _text(titel_el) if titel_el is not None else None
    if nr is None:
        return None
    return f"{label_word} {nr}" + (f". {titel}" if titel else "")


def parse_artikel(art, bwb_id: str, effective_date: str, chapter: str | None, paragraph: str | None) -> dict:
    kop = art.find("kop")
    # kop/nr, not the article's own @label attribute — found by diffing the Wdo: an
    # <artikel> with status="nogniet" (not yet in force) sometimes carries no @label at
    # all (e.g. Wdo articles 9, 11, 14), while kop/nr is always present regardless of
    # commencement status. Relying on @label silently produced an empty article number
    # for those three, which then collided into one duplicate uid — caught by the
    # validation gate, not by inspection.
    nr = kop.findtext("nr") if kop is not None else art.get("label", "").replace("Artikel", "").strip()
    number = nr
    # NOT kop.findtext("titel") — a titel can carry a nested <extref> (e.g. Cbw art. 7
    # and 57 cite DORA inline), and findtext() silently truncates at the first nested
    # element instead of raising, which is exactly the kind of gap that stays invisible
    # unless the output is diffed against something. Flatten with _text() instead.
    titel_el = kop.find("titel") if kop is not None else None
    titel = _text(titel_el) if titel_el is not None else None
    heading = f"Artikel {nr}" + (f". {titel}" if titel else "")

    paragraphs = []
    for lid in art.findall("lid"):
        lidnr = lid.findtext("lidnr")
        points = [li.findtext("li.nr", default="").strip(".").strip()
                  for li in lid.iter("li") if li.find("li.nr") is not None]
        paragraphs.append({
            "number": lidnr,
            "text": _text(lid, include_lidnr=False),
            "points": points,
            "jci": _jci_for(lid, f"lid={lidnr}"),  # look within this <lid>, not the whole article
        })

    # Article-level extref/intref. Rewritten from an earlier version that only captured
    # @bwb-id and branched on "does this article have any <lid>" to decide where to look --
    # two real gaps, found only once the graph-building step actually needed precise
    # citation targets:
    #   1. @bwb-id is null for every EU-instrument citation (they use @reeks="Celex" and a
    #      CELEX id in @doc instead, e.g. doc="31976L0769"), so every EU citation was being
    #      recorded with no usable identifier at all -- confirmed by checking the Cbw, whose
    #      47 extrefs are *all* EU citations and therefore all had bwb_id=None.
    #   2. @doc itself, even for a Dutch bwb-id citation, carries more than the plain id --
    #      "jci1.3:c:BWBR0001840&artikel=10" targets article 10 specifically, not the whole
    #      law -- and only capturing bwb-id threw that away, making a citation to one
    #      specific article indistinguishable from a citation to the whole instrument.
    #   The "if not art.findall('lid'): ..." branch was also a gap in its own right: an
    #   article WITH paragraphs but with an extref sitting outside all of them (rare, but
    #   possible in an intro/closing sentence) would have silently lost that reference,
    #   since the fallback only ran when there were no paragraphs at all. Walking the whole
    #   article once with art.iter(), and separately recording which paragraph (if any)
    #   contains each reference by checking ancestry, fixes both branches at once instead
    #   of two.
    ARTIKEL_RE = re.compile(r"artikel=([^&]+)")

    def _containing_paragraph(ref, art):
        for lid in art.findall("lid"):
            if ref in lid.iter():
                return lid.findtext("lidnr")
        return None

    def _ref_record(ref, art):
        doc = ref.get("doc")
        return {
            "doc": doc,
            "bwb_id": ref.get("bwb-id"),
            "identifier_scheme": ref.get("reeks"),  # "Celex" for an EU instrument citation, absent for BWB
            "target_article": ARTIKEL_RE.search(doc).group(1) if doc and ARTIKEL_RE.search(doc) else None,
            "label": ref.get("label"),
            "text": (ref.text or "").strip(),
            "paragraph_number": _containing_paragraph(ref, art),
        }

    extref = [_ref_record(ref, art) for ref in art.iter("extref")]
    intref = [_ref_record(ref, art) for ref in art.iter("intref")]

    full_text = _text(art)

    brondata = art.find("meta-data/brondata/oorspronkelijk/publicatie")
    dossier = None
    bron = art.get("bron")
    if brondata is not None:
        dref = brondata.find("dossierref")
        if dref is not None:
            dossier = dref.get("dossier")

    article_jci = _jci_for(art, f"artikel={number}")

    return {
        "uid": f"nl:{bwb_id}:art{number}@{effective_date}",
        "jci": article_jci,
        "eli": None,
        "celex": None,
        "instrument_id": bwb_id,
        "jurisdiction": "NL",
        "language": "nl",
        "article": number,
        "heading": heading,
        "chapter": chapter,
        "section": paragraph,  # a "Paragraaf N.M" chapter subdivision -- NOT the lid concept,
                               # which is called "paragraph" below (leden -> paragraphs),
                               # matching standard English legislative translation for both
                               # ("Paragraaf" -> Section, "lid" -> paragraph) rather than
                               # letting both collapse onto the same English word
        "text": full_text,
        "paragraphs": paragraphs,
        "text_fidelity": "official_xml",
        "source_ref": f"raw/nl/{bwb_id}/{effective_date}/toestand.xml#versie-id={art.get('versie-id')}",
        "in_force_status": "in_force" if art.get("inwerking") else "not_yet_in_force",
        "in_force_from": art.get("inwerking"),
        "label_id": art.get("label-id"),  # the key the WTI's own citation records reference an
                                          # article by -- not element_id or jci, checked directly
                                          # against the WTI's <regelingelement label-id="..."> blocks
        "element_id": art.get("stam-id"),
        "version_id": art.get("versie-id"),
        "source_publication": bron,
        "effect": art.get("effect"),
        "dossier_number": dossier,
        "extref": extref,
        "intref": intref,
        "norms": [],  # populated by Stage 6, not this script
        "features": {
            "n_words": len(full_text.split()),
            "xref_intra_instrument": len(intref),
            "xref_inter_instrument": len([r for r in extref if r.get("identifier_scheme") != "Celex"]),
            "xref_cross_jurisdiction": len([r for r in extref if r.get("identifier_scheme") == "Celex"]),
            "conditional_operators": _conditional_counts(full_text),
            "max_condition_depth": _max_lijst_depth(art),
            # undefined_terms_per_100w and delegation_layers need corpus-wide context
            # (the instrument's own definitions list; the WTI-derived graph) that a
            # single article's own XML doesn't have -- computed separately in
            # pipeline/features.py, not here.
        },
    }


def _walk_structure(el, bwb_id, effective_date, chapter, paragraph, out):
    """Recurse the document tree tracking the current Hoofdstuk/Paragraaf title, so each
    <artikel> gets the chapter/paragraph it actually sits under (not every law has both —
    e.g. a paragraaf-less chapter, or a law with no paragraaf level at all)."""
    if el.tag == "hoofdstuk":
        chapter = _heading_of(el, "Hoofdstuk") or chapter
    elif el.tag == "paragraaf":
        paragraph = _heading_of(el, "Paragraaf") or paragraph
    elif el.tag == "artikel":
        out.append(parse_artikel(el, bwb_id, effective_date, chapter, paragraph))
        return  # articles don't contain nested hoofdstuk/paragraaf/artikel
    for child in el:
        _walk_structure(child, bwb_id, effective_date, chapter, paragraph, out)


def parse(bwb_id: str, effective_date: str) -> list[dict]:
    xml_path = RAW_ROOT / bwb_id / effective_date / "toestand.xml"
    if not xml_path.exists():
        raise FileNotFoundError(f"{xml_path} not found — run acquire.py {bwb_id} first")
    root = ET.parse(xml_path).getroot()
    provisions: list[dict] = []
    _walk_structure(root, bwb_id, effective_date, None, None, provisions)
    return provisions


def parse_bijlage(bwb_id: str, effective_date: str, bijlage_number: str) -> dict:
    """Parse one numbered <bijlage> (an annex, not a standalone law — Bijlage 35 of the
    Besluit EU-verordeningen Wft, DORA's competent-authority designation, is the motivating
    case). Structurally different from an <artikel>-based law: a <bijlage> holds one
    unnumbered wrapper <divisie> (just the EU-instrument citation as a title), and inside
    that, numbered <divisie> children — these are the addressable sub-provisions, playing
    the role <artikel> plays in a normal law, but with no <lid> level under them here."""
    xml_path = RAW_ROOT / bwb_id / effective_date / "toestand.xml"
    if not xml_path.exists():
        raise FileNotFoundError(f"{xml_path} not found — run acquire.py {bwb_id} first")
    root = ET.parse(xml_path).getroot()

    bijlage_el = None
    for b in root.iter("bijlage"):
        kop = b.find("kop")
        if kop is not None and kop.findtext("nr") == str(bijlage_number):
            bijlage_el = b
            break
    if bijlage_el is None:
        raise ValueError(f"Bijlage {bijlage_number} not found in {bwb_id}")

    kop = bijlage_el.find("kop")
    titel_el = kop.find("titel") if kop is not None else None
    bijlage_titel = _text(titel_el) if titel_el is not None else None

    provisions = []
    for divisie in bijlage_el.iter("divisie"):
        d_kop = divisie.find("kop")
        nr = d_kop.findtext("nr") if d_kop is not None else None
        if nr is None:
            continue  # the unnumbered wrapper divisie — a container, not its own provision
        titel_el = d_kop.find("titel")
        titel = _text(titel_el) if titel_el is not None else None
        heading = f"{nr}. {titel}" if titel else nr
        provisions.append({
            "provision_id": f"{bwb_id}-bijlage{bijlage_number}-p{nr}",
            "number": int(nr) if nr.isdigit() else nr,
            "heading": heading,
            "text": _text(divisie),
            "text_fidelity": "official_xml",
            "source_ref": f"raw/nl/{bwb_id}/{effective_date}/toestand.xml#versie-id={divisie.get('versie-id')}",
        })

    return {"annex_number": bijlage_number, "annex_heading": bijlage_titel, "provisions": provisions}


def validate(provisions: list[dict], bwb_id: str) -> list[str]:
    """Cheap gates from Stage 2's own spec: no empty text, unique jci, unique uid."""
    problems = []
    uids = [p["uid"] for p in provisions]
    if len(uids) != len(set(uids)):
        problems.append("duplicate uid found")
    jcis = [p["jci"] for p in provisions if p["jci"]]
    if len(jcis) != len(set(jcis)):
        problems.append("duplicate article-level jci found")
    for p in provisions:
        if not p["text"]:
            problems.append(f"empty text: {p['uid']}")
        if p["jci"] is None:
            problems.append(f"missing article-level jci: {p['uid']}")
    return problems


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("bwb_id")
    ap.add_argument("effective_date")
    args = ap.parse_args()

    provisions = parse(args.bwb_id, args.effective_date)
    problems = validate(provisions, args.bwb_id)

    out_path = DATA_ROOT / f"{args.bwb_id}_{args.effective_date}_provisions.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(provisions, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"[{args.bwb_id}] parsed {len(provisions)} provisions -> {out_path}")
    if problems:
        print(f"[{args.bwb_id}] VALIDATION PROBLEMS ({len(problems)}):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"[{args.bwb_id}] validation clean: no duplicate/missing ids, no empty text")
