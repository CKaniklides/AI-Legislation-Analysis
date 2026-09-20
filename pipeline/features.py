# -*- coding: utf-8 -*-
"""
Stage 5 (continued) — the two features that need corpus-wide context, not just a single
article's own XML: linguistic load (needs the instrument's own defined-terms list) and
delegation layers (needs the citation graph). Interdependence, conditionality and nesting
depth are computed per-article inside parse.py/parse_eu.py instead, at parse time, since
they only need that one article's own structure.

Linguistic load, verified in two separate steps rather than assumed:
  1. Defined-or-not: Dutch instruments mark a defined term with <nadruk type="cur"> in the
     definitions article specifically (confirmed on Cbw art. 1); EU instruments use plain
     enumerated points with the term in Dutch guillemets, „term": (confirmed on GDPR art. 4)
     — genuinely different markup, extracted separately per side.
  2. Specialist-or-not: a word absent (or very rare) in general Dutch is a specialist-term
     candidate. Uses the `wordfreq` package's Dutch data (Zipf scale) rather than a
     hand-picked heuristic — verified directly against real terms first: "verwerkings-
     verantwoordelijke" (GDPR's own term for "data controller") scores 0.00, "kan"/"geven"
     score 6+, so the signal separates real jargon from ordinary words as expected.

A word counts toward undefined_terms_per_100w only if it is BOTH rare (zipf < RARITY_CUTOFF)
AND not itself a defined term of this instrument (or an EU instrument it implements) --
neither signal alone is what section 6.4 asks for.
"""
import json
import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

from wordfreq import zipf_frequency

DATA = Path(__file__).resolve().parent.parent / "data"
RAW_NL = Path(__file__).resolve().parent.parent / "raw" / "nl"

RARITY_CUTOFF = 3.0  # zipf score below this = candidate specialist term; calibrated
                      # loosely against the spot-checks above, not against Stage 9's
                      # labelled sample yet -- treat as a placeholder like every other
                      # Stage 5/C4 threshold until that calibration happens.

DUTCH_DEFINITIONS_ARTICLE = {
    "BWBR0040940": "1",     # UAVG
    "BWBR0052872": "1",     # Cbw
    "BWBR0051796": "1",     # Uitvoeringswet dataverordening
    "BWBR0048156": "1",     # Wdo
    "BWBR0009950": "1.1",   # Telecommunicatiewet
}
DUTCH_DATE = {
    "BWBR0040940": "2026-09-01", "BWBR0052872": "2026-08-15", "BWBR0051796": "2025-11-21",
    "BWBR0048156": "2025-11-11", "BWBR0009950": "2026-08-15",
}
EU_DEFINITIONS_ARTICLE = {
    "32016R0679": "4",  # GDPR
    "32022L2555": "6",  # NIS2
    "32022R2554": "3",  # DORA
    "32024R1689": "3",  # AI Act
}

# Dutch instrument -> EU instrument it implements (for pulling in the EU parent's own
# defined terms too, since a thin transposition like UAVG relies on the AVG's definitions
# without repeating all of them) -- same mapping as graph.py's EU_NUMBER_TO_CELEX, kept
# separate rather than imported since this file only needs the two entries that matter.
IMPLEMENTS = {"BWBR0040940": "32016R0679", "BWBR0052872": "32022L2555"}

WORD_RE = re.compile(r"[a-zA-ZÀ-ÿ][a-zA-ZÀ-ÿ\-]{2,}")
STOPWORDS_KEEP = {"niet", "geen", "tenzij", "onverminderd"}  # never "undefined jargon"


def dutch_defined_terms(bwb_id: str) -> set[str]:
    date = DUTCH_DATE[bwb_id]
    art_nr = DUTCH_DEFINITIONS_ARTICLE[bwb_id]
    xml_path = RAW_NL / bwb_id / date / "toestand.xml"
    root = ET.parse(xml_path).getroot()
    for art in root.iter("artikel"):
        kop = art.find("kop")
        if kop is not None and kop.findtext("nr") == art_nr:
            terms = set()
            for nadruk in art.iter("nadruk"):
                if nadruk.get("type") == "cur" and nadruk.text:
                    term = nadruk.text.strip().rstrip(":").strip()
                    if term:
                        terms.add(term.lower())
            return terms
    return set()


def eu_defined_terms(celex: str) -> set[str]:
    path = DATA / f"{celex}_original_provisions.json"
    provs = json.loads(path.read_text(encoding="utf-8"))
    art_nr = EU_DEFINITIONS_ARTICLE[celex]
    art = next((p for p in provs if p["article"] == art_nr), None)
    if art is None:
        return set()
    # Confirmed by inspection, not assumed: different EU laws use different Dutch
    # typographic quote conventions for the defined term -- GDPR uses low-high guillemets
    # („term"), DORA uses plain curly double quotes ("term") -- both close with the same
    # right-double-quote character, so only the opening character actually varies. Missing
    # this entirely zeroed out DORA/NIS2/AI Act's defined-terms extraction with no error.
    terms = set()
    for m in re.finditer(r"[„“]([^“”„”]+)[””]", art["text"]):
        terms.add(m.group(1).strip().lower())
    return terms


def specialist_undefined_score(text: str, defined_terms: set[str]) -> dict:
    """Returns per-100-word count of words that are both rare (wordfreq zipf < cutoff)
    and not part of any defined multi-word term for this instrument."""
    words = WORD_RE.findall(text)
    if not words:
        return {"undefined_terms_per_100w": 0.0, "n_candidate_words": 0, "n_words_checked": 0}
    defined_words = set()
    for term in defined_terms:
        defined_words.update(term.split())
    flagged = 0
    for w in words:
        lw = w.lower()
        if lw in STOPWORDS_KEEP or lw in defined_words:
            continue
        if zipf_frequency(lw, "nl") < RARITY_CUTOFF:
            flagged += 1
    return {
        "undefined_terms_per_100w": round(100 * flagged / len(words), 2),
        "n_candidate_words": flagged,
        "n_words_checked": len(words),
    }


def delegation_layers(graph, uid: str) -> dict:
    """How many distinct out-of-corpus instruments this provision is the (legal_basis_for)
    legal basis for. Honest limit, stated plainly rather than faked: this corpus doesn't
    contain the Cyberbeveiligingsbesluit or any sectoral regeling as their own parsed
    instruments, so a chain deeper than 1 (e.g. Cbw art. 25 -> Besluit -> Regeling, three
    real layers) cannot currently be followed past the first link. This returns the count
    of direct legal_basis_for targets, not a verified multi-level depth."""
    targets = set()
    for _, v, d in graph.out_edges(uid, data=True):
        if d.get("kind") == "legal_basis_for":
            targets.add(v)
    return {"delegation_layers_direct": len(targets),
            "note": "direct legal_basis_for targets only -- see delegation_layers() docstring"}


def compute_all():
    import networkx as nx
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from graph import build
    g = build()

    dutch_terms = {bwb: dutch_defined_terms(bwb) for bwb in DUTCH_DEFINITIONS_ARTICLE}
    eu_terms = {celex: eu_defined_terms(celex) for celex in EU_DEFINITIONS_ARTICLE}
    print("defined terms found:")
    for bwb, terms in dutch_terms.items():
        print(f"  {bwb}: {len(terms)}")
    for celex, terms in eu_terms.items():
        print(f"  {celex}: {len(terms)}")

    results = []
    for bwb, date in DUTCH_DATE.items():
        provs = json.loads((DATA / f"{bwb}_{date}_provisions.json").read_text(encoding="utf-8"))
        own_terms = dutch_terms[bwb]
        parent_terms = eu_terms.get(IMPLEMENTS.get(bwb), set())
        for p in provs:
            ling = specialist_undefined_score(p["text"], own_terms | parent_terms)
            deleg = delegation_layers(g, p["uid"])
            p["features"] = {**p.get("features", {}), **ling, **deleg}
            results.append(p)
    for celex in EU_DEFINITIONS_ARTICLE:
        provs = json.loads((DATA / f"{celex}_original_provisions.json").read_text(encoding="utf-8"))
        own_terms = eu_terms[celex]
        for p in provs:
            ling = specialist_undefined_score(p["text"], own_terms)
            p["features"] = {**p.get("features", {}), **ling}
            results.append(p)

    # Percentile ranks, not raw thresholds -- architecture doc Part 4/Stage 5 is explicit
    # that nothing here should be capable of producing a flag until Stage 9's calibration
    # sample exists. A percentile rank is safe to report now; a threshold isn't.
    PERCENTILE_METRICS = ["xref_intra_instrument", "xref_inter_instrument",
                          "xref_cross_jurisdiction", "max_condition_depth",
                          "undefined_terms_per_100w"]
    for metric in PERCENTILE_METRICS:
        values = sorted(p["features"][metric] for p in results if metric in p["features"])
        if not values:
            continue
        for p in results:
            if metric not in p["features"]:
                continue
            v = p["features"][metric]
            rank = sum(1 for x in values if x <= v) / len(values)
            p["features"][f"{metric}_percentile"] = round(rank * 100, 1)

    out_path = DATA / "features_by_provision.json"
    out_path.write_text(json.dumps(
        [{"uid": p["uid"], "instrument_id": p["instrument_id"], "article": p["article"],
          "features": p["features"]} for p in results],
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nwrote {len(results)} provisions' features -> {out_path}")
    return results


if __name__ == "__main__":
    compute_all()
