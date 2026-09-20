"""
Stage 4 input — parse a Dutch instrument's WTI (Wetstechnische Informatie) file into
citation and legal-basis edges. Distinct from parse.py (the toestand/text parser) because
the WTI is a different file with a different structure, not an alternate rendering of the
same content — see docs/adr/0003-wti-primary-citation-source.md for why this file, not
LiDO, is the corpus's primary source for these relationships.

Two genuinely different granularities, confirmed by inspection, not assumed from the
architecture doc's own edge table (which described grondslag_voor as provision-level; the
actual XML has it at instrument level only — corrected here, and worth fixing upstream too):

    <regeling>
      <regelingelement label-id="X" groep="artikel" label="Artikel N">
        <verwijzing-door>            <!-- PER-ARTICLE: who cites article N specifically -->
          <gerelateerd-regelingelement bwb-id="..." geldig-van="..." geldig-tot="...">
            <extref doc="jci1.3:c:{bwb-id}&{unit}={n}" bwb-id="..." label="...">text</extref>
          </gerelateerd-regelingelement>
        </verwijzing-door>
      </regelingelement>
      <grondslag-voor>               <!-- INSTRUMENT-level: no article association at all -->
        <gerelateerde-regeling bwb-id="..." geldig-van="..." geldig-tot="...">
          <extref doc="jci1.3:c:{bwb-id}" bwb-id="...">title</extref>
        </gerelateerde-regeling>
      </grondslag-voor>
      <wettelijke-bevoegdheid-voor>  <!-- same instrument-level shape as grondslag-voor -->
        ...
      </wettelijke-bevoegdheid-voor>
    </regeling>

The citing instrument's own numbering unit varies (a citation from "Aanwijzingen voor de
regelgeving" uses doc="...&aanwijzing=2.46", not "&artikel="), so the target-unit regex
here is generic (captures whatever follows the first "&key=", not hardcoded to "artikel="),
unlike parse.py's extref extraction, which is fine hardcoding "artikel=" since that's
always the citing document's own convention there.

Usage:
    python parse_wti.py BWBR0009950 2026-08-15
"""
import argparse
import json
import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

RAW_ROOT = Path(__file__).resolve().parent.parent / "raw" / "nl"
DATA_ROOT = Path(__file__).resolve().parent.parent / "data"

UNIT_RE = re.compile(r"&(\w+)=([^&]+)")


def _ref_info(extref) -> dict:
    doc = extref.get("doc")
    unit = None
    if doc:
        m = UNIT_RE.search(doc)
        if m:
            unit = {"kind": m.group(1), "value": m.group(2)}
    return {
        "bwb_id": extref.get("bwb-id"),
        "doc": doc,
        "unit": unit,
        "label": extref.get("label"),
        "text": (extref.text or "").strip(),
    }


def _refs_under(container, group_tag: str) -> list[dict]:
    records = []
    for block in container.findall(f"./{group_tag}"):
        for g in list(block):  # gerelateerde-regeling or gerelateerd-regelingelement
            for ref in g.findall("extref"):
                rec = _ref_info(ref)
                rec["valid_from"] = g.get("geldig-van")
                rec["valid_until"] = g.get("geldig-tot")
                records.append(rec)
    return records


def parse(bwb_id: str, effective_date: str) -> dict:
    """The WTI has two structurally distinct sections, confirmed by inspection after an
    initial version of this parser silently undercounted Cbw's incoming citations (104 vs.
    an earlier ad-hoc count of 140) by only looking in one of them:

        <wetstechnische-informatie>
          <gerelateerde-regelgeving>
            <regeling>                     -- INSTRUMENT-level facts about this law as a
              <grondslag-voor>...             whole: its own legal-basis relations, and
              <wettelijke-bevoegdheid-voor>   citations to the law generally (not any one
              <verwijzing-door>...            article specifically)
            <regelingelementen>
              <regelingelement groep="artikel" label-id="X">
                <verwijzing-door>...          -- PER-ARTICLE incoming citations

    Both are real and distinct signal: an instrument-level citation ("cites this law") is
    not the same fact as an article-level one ("cites this specific article"), and
    conflating them (or, as the first version of this parser did, only capturing one)
    understates the graph.
    """
    wti_path = RAW_ROOT / bwb_id / effective_date / "wti.xml"
    if not wti_path.exists():
        raise FileNotFoundError(f"{wti_path} not found — run acquire.py {bwb_id} first")
    root = ET.parse(wti_path).getroot()
    gr = root.find("gerelateerde-regelgeving")
    regeling = gr.find("regeling") if gr is not None else None
    regelingelementen = gr.find("regelingelementen") if gr is not None else None

    incoming_instrument_level = _refs_under(regeling, "verwijzing-door") if regeling is not None else []
    legal_basis_for_instrument_level = _refs_under(regeling, "grondslag-voor") if regeling is not None else []
    statutory_authority_for_instrument_level = _refs_under(regeling, "wettelijke-bevoegdheid-voor") if regeling is not None else []

    # grondslag-voor and wettelijke-bevoegdheid-voor turn out to have the SAME two-level
    # shape as verwijzing-door -- confirmed by inspection after an initial version of this
    # parser, reading only the instrument-level block, undercounted UAVG's grondslag-voor
    # 57-to-128 against an earlier ad-hoc count: 13 of UAVG's 14 grondslag-voor blocks are
    # per-article (a specific provision, not the law generally, is another regulation's
    # legal basis), and only 1 is instrument-level. This is the more useful signal of the
    # two for the project's loss-of-legal-basis check (context.md 6.3) -- knowing WHICH
    # provision authorises a delegated regulation, not just that the law as a whole does.
    legal_basis_for_by_article: dict[str, list[dict]] = {}
    statutory_authority_for_by_article: dict[str, list[dict]] = {}
    incoming_by_article: dict[str, list[dict]] = {}
    if regelingelementen is not None:
        for regelingelement in regelingelementen.iter("regelingelement"):
            if regelingelement.get("groep") != "artikel":
                continue
            label_id = regelingelement.get("label-id")
            vd_records = _refs_under(regelingelement, "verwijzing-door")
            if vd_records:
                incoming_by_article[label_id] = vd_records
            gv_records = _refs_under(regelingelement, "grondslag-voor")
            if gv_records:
                legal_basis_for_by_article[label_id] = gv_records
            wb_records = _refs_under(regelingelement, "wettelijke-bevoegdheid-voor")
            if wb_records:
                statutory_authority_for_by_article[label_id] = wb_records

    return {
        "instrument_id": bwb_id,
        "incoming_references_instrument_level": incoming_instrument_level,
        "incoming_references_by_article_label_id": incoming_by_article,
        "legal_basis_for_instrument_level": legal_basis_for_instrument_level,
        "legal_basis_for_by_article_label_id": legal_basis_for_by_article,
        "statutory_authority_for_instrument_level": statutory_authority_for_instrument_level,
        "statutory_authority_for_by_article_label_id": statutory_authority_for_by_article,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("bwb_id")
    ap.add_argument("effective_date")
    args = ap.parse_args()

    result = parse(args.bwb_id, args.effective_date)
    out_path = DATA_ROOT / f"{args.bwb_id}_{args.effective_date}_wti.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    n_articles = len(result["incoming_references_by_article_label_id"])
    n_incoming = sum(len(v) for v in result["incoming_references_by_article_label_id"].values())
    n_gv = sum(len(v) for v in result["legal_basis_for_by_article_label_id"].values())
    n_wb = sum(len(v) for v in result["statutory_authority_for_by_article_label_id"].values())
    print(f"[{args.bwb_id}] {n_articles} articles w/ incoming citations ({n_incoming}) "
          f"+ {len(result['incoming_references_instrument_level'])} instrument-level; "
          f"legal_basis_for: {n_gv} per-article + {len(result['legal_basis_for_instrument_level'])} instrument-level; "
          f"statutory_authority_for: {n_wb} per-article + {len(result['statutory_authority_for_instrument_level'])} instrument-level "
          f"-> {out_path}")
