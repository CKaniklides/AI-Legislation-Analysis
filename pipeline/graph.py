# -*- coding: utf-8 -*-
"""
Stage 4 — Build the graph (architecture_and_implementation_strategy.md Part 3.2 / Part 4).

Assembles a networkx MultiDiGraph from everything the earlier stages produced:
  - Provision nodes: every parsed article across all 10 in-corpus instruments (6 Dutch,
    4 EU — the AI Act contributes only its 113 pre-Digital-Omnibus articles, per
    docs/adr/0004-exclude-digital-omnibus-content.md).
  - Instrument nodes: both in-corpus instruments and every out-of-corpus instrument
    referenced by a citation, kept as a real (if data-sparse) node rather than a string,
    since "how much does this instrument cite outside the corpus" is itself signal.
  - Edges: cites/cites_internal (from each Dutch provision's own extref/intref),
    cited_by/legal_basis_for/statutory_authority_for (from the WTI, both the
    instrument-level and per-article layers), implements (from each Dutch dataset's own
    eu_regulation_implemented/eu_directive_implemented field).

Deliberately NOT in this pass (scoped out, not forgotten): the EU companion files'
amends/corrected_by/completed_by/etc. relations (four different schemas per file — a
distinct piece of work) and LiDO/SPARQL reconciliation (needs live queries, not just the
already-fetched data this script reads). Every edge added here carries a `source` field
naming exactly which file/mechanism produced it, so it's always possible to tell what a
given edge in the graph is and isn't backed by.
"""
import json
import re
from pathlib import Path

import networkx as nx

DATA = Path(__file__).resolve().parent.parent / "data"
DATASETS = Path(__file__).resolve().parent.parent / "Datasets" / "Dutch Laws"

# Dutch instrument -> which toestand/expression file to load. Bijlage 35 is loaded
# separately below (parse_bijlage() output, not a plain parse.py run).
DUTCH_INSTRUMENTS = [
    ("BWBR0040940", "2026-09-01"),  # UAVG
    ("BWBR0052872", "2026-08-15"),  # Cbw
    ("BWBR0051796", "2025-11-21"),  # Uitvoeringswet dataverordening
    ("BWBR0048156", "2025-11-11"),  # Wdo
    ("BWBR0009950", "2026-08-15"),  # Telecommunicatiewet
]
# BWBR0049497 (Besluit EU-verordeningen Wft) is loaded too, but only its Bijlage 35 matters
# for this corpus per context.md's own scoping -- see load_nodes().

EU_INSTRUMENTS = [
    ("32016R0679", "original"),  # GDPR
    ("32022L2555", "original"),  # NIS2
    ("32022R2554", "original"),  # DORA
    ("32024R1689", "original"),  # AI Act -- ORIGINAL only, not "complete" (ADR-0004)
]

# For resolving each Dutch dataset's own eu_regulation_implemented/eu_directive_implemented
# "number" field (e.g. "(EU) 2016/679") to the CELEX id used as this instrument's node id.
EU_NUMBER_TO_CELEX = {
    "(EU) 2016/679": "32016R0679",
    "(EU) 2022/2555": "32022L2555",
    "(EU) 2022/2554": "32022R2554",
    "(EU) 2023/2854": None,  # Data Act -- not in this corpus as its own instrument
}


def _instrument_node(g: nx.MultiDiGraph, instrument_id: str, jurisdiction: str):
    if not g.has_node(("instrument", instrument_id)):
        g.add_node(("instrument", instrument_id), kind="Instrument",
                   instrument_id=instrument_id, jurisdiction=jurisdiction,
                   in_corpus=False)  # flipped True below for instruments we actually loaded


def load_nodes(g: nx.MultiDiGraph) -> dict:
    """Returns {(jurisdiction, instrument_id, article): uid} for resolving citations later."""
    by_instrument_article: dict[tuple, str] = {}
    by_label_id: dict[tuple, str] = {}  # (instrument_id, label_id) -> uid, for WTI resolution

    def add_provisions(provisions: list[dict], instrument_id: str, jurisdiction: str):
        _instrument_node(g, instrument_id, jurisdiction)
        g.nodes[("instrument", instrument_id)]["in_corpus"] = True
        for p in provisions:
            uid = p["uid"]
            g.add_node(uid, kind="Provision", jurisdiction=jurisdiction,
                       instrument_id=instrument_id, article=p["article"],
                       heading=p.get("heading"), text_fidelity=p.get("text_fidelity"))
            by_instrument_article[(jurisdiction, instrument_id, str(p["article"]))] = uid
            if p.get("label_id"):
                by_label_id[(instrument_id, p["label_id"])] = uid

    for bwb_id, date in DUTCH_INSTRUMENTS:
        provs = json.loads((DATA / f"{bwb_id}_{date}_provisions.json").read_text(encoding="utf-8"))
        add_provisions(provs, bwb_id, "NL")

    for celex, expr in EU_INSTRUMENTS:
        provs = json.loads((DATA / f"{celex}_{expr}_provisions.json").read_text(encoding="utf-8"))
        add_provisions(provs, celex, "EU")

    # Bijlage 35 -- structurally its own thing (an annex, not a normal law), loaded via
    # parse_bijlage() directly since it was never saved as a standalone provisions file.
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from parse import parse_bijlage
    bijlage = parse_bijlage("BWBR0049497", "2026-08-15", "35")
    for p in bijlage["provisions"]:
        uid = f"nl:BWBR0049497:bijlage35-p{p['number']}@2026-08-15"
        g.add_node(uid, kind="Provision", jurisdiction="NL", instrument_id="BWBR0049497",
                   article=f"bijlage35-{p['number']}", heading=p["heading"],
                   text_fidelity=p.get("text_fidelity"))
        by_instrument_article[("NL", "BWBR0049497", f"bijlage35-{p['number']}")] = uid
    _instrument_node(g, "BWBR0049497", "NL")
    g.nodes[("instrument", "BWBR0049497")]["in_corpus"] = True
    g.nodes[("instrument", "BWBR0049497")]["note"] = (
        "Only Bijlage 35 is in this corpus's scope, not the Besluit EU-verordeningen Wft's "
        "own 13 articles -- see context.md's digital-law-platform scoping test."
    )

    return by_instrument_article, by_label_id


def add_cites_edges(g: nx.MultiDiGraph, by_instrument_article: dict):
    """cites / cites_internal, from each Dutch provision's own extref/intref (parse.py)."""
    n_added = 0
    for bwb_id, date in DUTCH_INSTRUMENTS:
        provs = json.loads((DATA / f"{bwb_id}_{date}_provisions.json").read_text(encoding="utf-8"))
        for p in provs:
            source_uid = p["uid"]
            for kind, refs in (("cites", p["extref"]), ("cites_internal", p["intref"])):
                for ref in refs:
                    if ref.get("identifier_scheme") == "Celex" and ref.get("doc"):
                        target_celex = ref["doc"]
                        # only resolve to a corpus EU instrument if it's actually one of ours
                        if any(target_celex == c for c, _ in EU_INSTRUMENTS):
                            _instrument_node(g, target_celex, "EU")
                            g.add_edge(source_uid, ("instrument", target_celex), kind=kind,
                                       source="toestand_extref", paragraph_number=ref.get("paragraph_number"),
                                       raw_text=ref.get("text"))
                        else:
                            _instrument_node(g, target_celex, "EU")
                            g.add_edge(source_uid, ("instrument", target_celex), kind=kind,
                                       source="toestand_extref", paragraph_number=ref.get("paragraph_number"),
                                       raw_text=ref.get("text"), out_of_corpus=True)
                        n_added += 1
                    elif ref.get("bwb_id"):
                        target_bwb = ref["bwb_id"]
                        target_art = ref.get("target_article")
                        key = ("NL", target_bwb, str(target_art)) if target_art else None
                        if key and key in by_instrument_article:
                            g.add_edge(source_uid, by_instrument_article[key], kind=kind,
                                       source="toestand_extref", paragraph_number=ref.get("paragraph_number"),
                                       raw_text=ref.get("text"))
                        else:
                            _instrument_node(g, target_bwb, "NL")
                            g.add_edge(source_uid, ("instrument", target_bwb), kind=kind,
                                       source="toestand_extref", paragraph_number=ref.get("paragraph_number"),
                                       target_article=target_art, raw_text=ref.get("text"),
                                       out_of_corpus=(target_bwb, date) not in [(b, d) for b, d in DUTCH_INSTRUMENTS])
                        n_added += 1
    return n_added


def add_wti_edges(g: nx.MultiDiGraph, by_instrument_article: dict, by_label_id: dict):
    """cited_by, legal_basis_for, statutory_authority_for -- from parse_wti.py output."""
    n_added = 0
    for bwb_id, date in DUTCH_INSTRUMENTS + [("BWBR0049497", "2026-08-15")]:
        wti_path = DATA / f"{bwb_id}_{date}_wti.json"
        if not wti_path.exists():
            continue
        wti = json.loads(wti_path.read_text(encoding="utf-8"))

        for label_id, refs in wti["incoming_references_by_article_label_id"].items():
            target_uid = by_label_id.get((bwb_id, label_id))
            if target_uid is None:
                continue
            for ref in refs:
                citing_bwb = ref.get("bwb_id")
                unit = ref.get("unit")
                key = ("NL", citing_bwb, str(unit["value"])) if (citing_bwb and unit) else None
                source_node = by_instrument_article.get(key) if key else None
                if source_node is None:
                    if citing_bwb:
                        _instrument_node(g, citing_bwb, "NL")
                        source_node = ("instrument", citing_bwb)
                    else:
                        continue
                g.add_edge(source_node, target_uid, kind="cited_by", source="wti_incoming_citation",
                           valid_from=ref.get("valid_from"), valid_until=ref.get("valid_until"),
                           raw_text=ref.get("text"))
                n_added += 1

        for edge_kind, field in [("legal_basis_for", "legal_basis_for_by_article_label_id"),
                                  ("statutory_authority_for", "statutory_authority_for_by_article_label_id")]:
            for label_id, refs in wti[field].items():
                source_uid = by_label_id.get((bwb_id, label_id))
                if source_uid is None:
                    continue
                for ref in refs:
                    target_bwb = ref.get("bwb_id")
                    if not target_bwb:
                        continue
                    _instrument_node(g, target_bwb, "NL")
                    g.add_edge(source_uid, ("instrument", target_bwb), kind=edge_kind,
                               source="wti", valid_from=ref.get("valid_from"),
                               valid_until=ref.get("valid_until"), raw_text=ref.get("text"))
                    n_added += 1
    return n_added


def add_implements_edges(g: nx.MultiDiGraph):
    """instrument -> EU instrument it implements, from each Dutch dataset's own metadata."""
    n_added = 0
    for f in DATASETS.glob("*.json"):
        d = json.loads(f.read_text(encoding="utf-8"))
        reg = d.get("regulation", {})
        bwb_id = reg.get("bwb_id")
        if bwb_id:
            impl = reg.get("eu_regulation_implemented") or reg.get("eu_directive_implemented")
            if impl:
                celex = EU_NUMBER_TO_CELEX.get(impl.get("number"))
                if celex:
                    kind = "implements_regulation" if "eu_regulation_implemented" in reg else "implements_directive"
                    _instrument_node(g, bwb_id, "NL")
                    _instrument_node(g, celex, "EU")
                    g.add_edge(("instrument", bwb_id), ("instrument", celex), kind=kind,
                               source=f.name, short_name=impl.get("short_name"))
                    n_added += 1
        # Bijlage 35 uses a different schema (bijlage_35.eu_regulation, not
        # regulation.eu_regulation_implemented) since it's an annex within a larger
        # regulation, not a standalone Dutch law implementing one -- checked separately.
        bijlage = d.get("bijlage_35")
        if bijlage and bijlage.get("eu_regulation"):
            celex = EU_NUMBER_TO_CELEX.get(bijlage["eu_regulation"].get("number"))
            if celex:
                _instrument_node(g, "BWBR0049497", "NL")
                _instrument_node(g, celex, "EU")
                g.add_edge(("instrument", "BWBR0049497"), ("instrument", celex),
                           kind="implements_regulation", source=f.name,
                           short_name=bijlage["eu_regulation"].get("short_name"),
                           note="Bijlage 35 specifically, not the parent Besluit's other 13 articles")
                n_added += 1
    return n_added


def build() -> nx.MultiDiGraph:
    g = nx.MultiDiGraph()
    by_instrument_article, by_label_id = load_nodes(g)
    n_cites = add_cites_edges(g, by_instrument_article)
    n_wti = add_wti_edges(g, by_instrument_article, by_label_id)
    n_impl = add_implements_edges(g)
    print(f"nodes: {g.number_of_nodes()} | edges: {g.number_of_edges()} "
          f"(cites/cites_internal: {n_cites}, WTI-derived: {n_wti}, implements: {n_impl})")
    return g


if __name__ == "__main__":
    g = build()
    out_path = DATA / "graph.gexf"
    # gexf needs plain-hashable node ids and scalar attrs -- stringify tuple node ids
    g2 = nx.relabel_nodes(g, {n: (f"instrument:{n[1]}" if isinstance(n, tuple) else n) for n in g.nodes})
    for _, attrs in g2.nodes(data=True):
        for k, v in list(attrs.items()):
            if v is None:
                attrs[k] = ""
    for _, _, attrs in g2.edges(data=True):
        for k, v in list(attrs.items()):
            if v is None:
                attrs[k] = ""
    nx.write_gexf(g2, out_path)
    print(f"wrote {out_path}")
