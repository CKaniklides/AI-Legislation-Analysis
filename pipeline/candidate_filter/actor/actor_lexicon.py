"""
Actor filter, Step 2a: build the actor inventory (the list of actor IDs).

The inventory lists every actor a norm's actor unit can be mapped to. It has
two kinds of entries:

  defined  an actor term defined in a document (from its definitions file).
           Applies only to that document's norms.
  key      a known actor that is not defined in a document, such as 'de
           Commissie' or 'het Comité' (CURATED below). Applies to the
           documents in its scope: 'eu' (all EU acts), 'nl' (all Dutch acts)
           or a list of document ids.

Defined terms come first. A key entry is not used for a document that defines
a term with the same label or alias; the document's own definition wins.

A defined term is an actor term when the last word before the first
preposition ends with an actor ending in actor_heads.tsv ('aangemelde instantie',
'markttoezichtautoriteit', 'AI-bureau'). Terms the rule gets wrong are fixed in
INCLUDE and EXCLUDE.

The inventory stores text only (label and aliases). The mapping step computes
word keys for these texts and for the actor units with the same function, so
the keys always agree.

Inputs
  --definitions  folder with <name>_definitions.json files, each
                 {"document": id, "definitions": [{"term", "definition",
                 "article", ...}]}
Outputs
  --out          actor_inventory.jsonl, one entry per line:
                 id, label, aliases, kind, documents | scope, excluded,
                 (ids: '<document>:<term>' for defined, 'KEY:<scope>:<label>' for key)
                 class, part_of, source, definition
  and a review table of all defined terms (actor or not) in the log

Usage
  python actor_inventory.py
  python actor_inventory.py --definitions data/definitions --out data/conflict/actor/actor_inventory.jsonl
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import unicodedata
from pathlib import Path

log = logging.getLogger("actor.inventory")

# --------------------------------------------------------------------------
# Actor head words -> broad class (also the fallback classes of the mapping)
# --------------------------------------------------------------------------

# The table is a separate file next to this script (actor_heads.tsv), so it
# can be edited without touching the code. The mapping step uses the same file
# for its fallback classes, via head_class().
HEADS_FILE = Path(__file__).with_name("actor_heads.tsv")


def load_heads(path: Path = HEADS_FILE) -> tuple[dict[str, str], set[str]]:
    """Ending -> class, and the words marked '-' (not actors)."""
    heads, not_actor = {}, set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        ending, cls = (x.strip() for x in line.split("\t", 1))
        if cls == "-":
            not_actor.add(plain(ending))
        else:
            heads[plain(ending)] = cls
    return heads, not_actor


PREPOSITIONS = {"van", "voor", "in", "op", "met", "tot", "aan", "bij", "over", "onder",
                "uit", "door", "tegen", "naar", "als", "om"}

# Fixes of the term rule, per document: terms that are actors although the
# rule misses them, and terms the rule takes for actors although they are not.
INCLUDE = {
    "32024R1689": ["aanbieder verder in de AI-waardeketen"],
}
EXCLUDE: dict[str, list[str]] = {
}

# --------------------------------------------------------------------------
# Known actors that are not defined in the documents
# --------------------------------------------------------------------------

GDPR, AIA = "32016R0679", "32024R1689"
CURATED = [
    # EU institutions and actors common to EU acts
    {"label": "Commissie", "aliases": ["Europese Commissie"], "scope": "eu", "class": "institution"},
    {"label": "lidstaat", "scope": "eu", "class": "state"},
    {"label": "Europees Parlement", "scope": "eu", "class": "institution"},
    {"label": "Raad", "aliases": ["Raad van de Europese Unie"], "scope": "eu", "class": "institution"},
    {"label": "Hof van Justitie", "aliases": ["Hof van Justitie van de Europese Unie"],
     "scope": "eu", "class": "institution"},
    {"label": "Europese Toezichthouder voor gegevensbescherming", "scope": "eu",
     "class": "authority"},
    {"label": "gerecht", "aliases": ["bevoegd gerecht", "nationaal gerecht"], "scope": "eu",
     "class": "institution"},
    {"label": "overheidsinstantie", "scope": "eu", "class": "authority"},
    {"label": "nationale accreditatie-instantie", "aliases": ["accreditatie-instantie"],
     "scope": "eu", "class": "body"},
    # GDPR: in the AI Act 'comité' is the comitology committee, not the EDPB
    {"label": "Comité", "aliases": ["Europees Comité voor gegevensbescherming"],
     "scope": [GDPR], "class": "body"},
    {"label": "voorzitter", "aliases": ["voorzitter van het Comité"], "scope": [GDPR],
     "class": "office", "part_of": "Comité"},
    {"label": "secretariaat", "aliases": ["secretariaat van het Comité"], "scope": [GDPR],
     "class": "office", "part_of": "Comité"},
    {"label": "leidende toezichthoudende autoriteit", "scope": [GDPR], "class": "authority"},
    {"label": "functionaris voor gegevensbescherming", "scope": [GDPR], "class": "office"},
    {"label": "certificeringsorgaan", "scope": [GDPR], "class": "body"},
    {"label": "vereniging", "aliases": [
        "verenigingen en andere organen die categorieën van verwerkingsverantwoordelijken "
        "of verwerkers vertegenwoordigen"], "scope": [GDPR], "class": "party"},
    # AI Act
    {"label": "AI-board", "aliases": ["Europese raad voor artificiële intelligentie"],
     "scope": [AIA], "class": "body"},
    {"label": "adviesforum", "scope": [AIA], "class": "body"},
    {"label": "wetenschappelijk panel",
     "aliases": ["wetenschappelijk panel van onafhankelijke deskundigen"],
     "scope": [AIA], "class": "body"},
    {"label": "productfabrikant", "scope": [AIA], "class": "party"},
    {"label": "permanente subgroep voor markttoezicht", "aliases": ["subgroep"],
     "scope": [AIA], "class": "body", "part_of": "AI-board"},
    {"label": "gegevensbeschermingsautoriteit", "aliases": ["nationale gegevensbeschermingsautoriteit"],
     "scope": [AIA], "class": "authority"},
    {"label": "civielebeschermingsautoriteit", "scope": [AIA], "class": "authority"},
    {"label": "kmo", "aliases": ["kmo, met inbegrip van start-ups"], "scope": [AIA], "class": "party"},
]

# Document ids: CELEX for EU acts, BWB for Dutch acts
RE_EU = re.compile(r"^3\d{4}[A-Z]{1,2}\d{4}", re.I)
RE_NL = re.compile(r"^BWBR\d+", re.I)


# --------------------------------------------------------------------------
# Helpers (also used by the mapping step)
# --------------------------------------------------------------------------

def plain(text: str) -> str:
    """Lowercase, without accents and hyphens, single spaces."""
    text = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()
    return " ".join(text.replace("-", "").split())


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", plain(text)).strip("_")


_HEADS: tuple[dict[str, str], list[str], set[str]] | None = None


def head_class(word: str) -> str | None:
    """Broad actor class of a head word, or None if it is not an actor word.
    The word and its forms without -s, -en and -n are tried against the
    endings of actor_heads.tsv, longest ending first."""
    global _HEADS
    if _HEADS is None:
        heads, not_actor = load_heads()
        _HEADS = (heads, sorted(heads, key=len, reverse=True), not_actor)
    heads, endings, not_actor = _HEADS
    w = plain(word).replace("\u2019", "").replace("'", "")
    if not w or w in not_actor:
        return None
    forms = [w] + [w[:-len(s)] for s in ("s", "en", "n") if w.endswith(s) and len(w) > len(s) + 2]
    for f in forms:
        for e in endings:
            if f.endswith(e):
                return heads[e]
    return None


def term_head(term: str) -> str:
    """Last word before the first preposition ('systeem voor ...' -> 'systeem')."""
    words = []
    for w in term.split():
        if w.lower() in PREPOSITIONS and words:
            break
        words.append(w)
    return words[-1] if words else term


def in_scope(entry: dict, document: str) -> bool:
    """Whether an inventory entry may be used for a norm of this document."""
    if entry["kind"] == "defined":
        return document in entry["documents"]
    if document in entry.get("excluded", []):
        return False
    scope = entry["scope"]
    if scope == "eu":
        return bool(RE_EU.match(document))
    if scope == "nl":
        return bool(RE_NL.match(document))
    return document in scope


# --------------------------------------------------------------------------
# Building the inventory
# --------------------------------------------------------------------------

def defined_entries(path: Path) -> tuple[list[dict], list[tuple]]:
    """Actor entries of one definitions file, and a review row per term."""
    data = json.loads(path.read_text(encoding="utf-8"))
    doc = data["document"]
    include = {plain(t) for t in INCLUDE.get(doc, [])}
    exclude = {plain(t) for t in EXCLUDE.get(doc, [])}
    entries, review, seen = [], [], set()
    for d in data.get("definitions", []):
        term = " ".join(d["term"].split())
        cls = head_class(term_head(term))
        actor = (cls is not None or plain(term) in include) and plain(term) not in exclude
        review.append((doc, term, cls, actor))
        if not actor or plain(term) in seen:
            continue
        seen.add(plain(term))
        entries.append({
            "id": f"{doc}:{slug(term)}",
            "label": term,
            "aliases": [],
            "kind": "defined",
            "documents": [doc],
            "class": cls or head_class(term.split()[0]),
            "part_of": None,
            "source": f"{doc} art. {d.get('article')}"
                      + (f"({d['paragraph']})" if d.get("paragraph") else ""),
            "definition": d.get("definition"),
        })
    return entries, review


def curated_entries(defined: list[dict]) -> list[dict]:
    """Key entries, each excluded from documents that define the same label."""
    labels_by_doc: dict[str, set[str]] = {}
    for e in defined:
        for doc in e["documents"]:
            labels_by_doc.setdefault(doc, set()).add(plain(e["label"]))
    out = []
    for c in CURATED:
        names = {plain(c["label"])} | {plain(a) for a in c.get("aliases", [])}
        tag = "EU" if c["scope"] == "eu" else "NL" if c["scope"] == "nl" else c["scope"][0]
        out.append({
            "id": f"KEY:{tag}:{slug(c['label'])}",
            "label": c["label"],
            "aliases": c.get("aliases", []),
            "kind": "key",
            "scope": c["scope"],
            "excluded": sorted(d for d, labels in labels_by_doc.items() if names & labels),
            "class": c["class"],
            "part_of": None,
            "source": "curated",
            "definition": None,
            "_part_of_label": c.get("part_of"),
        })
    # part_of: label -> id of the key entry with that label and an overlapping scope
    by_label = {plain(e["label"]): e for e in out}
    for e in out:
        target = e.pop("_part_of_label")
        if target and plain(target) in by_label:
            e["part_of"] = by_label[plain(target)]["id"]
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--definitions", type=Path, default=Path("data/definitions"))
    p.add_argument("--out", type=Path, default=Path("data/conflict/actor/actor_inventory.jsonl"))
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    files = sorted(args.definitions.glob("*_definitions.json"))
    if not files:
        raise SystemExit(f"No *_definitions.json files in {args.definitions}")
    defined, review = [], []
    for f in files:
        e, r = defined_entries(f)
        defined += e
        review += r
        log.info("%s: %d terms, %d actor terms", f.name, len(r), len(e))
    curated = curated_entries(defined)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        for e in defined + curated:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    log.info("Review of defined terms (actor? class):")
    for doc, term, cls, actor in review:
        log.info("  %s  %-3s %-11s %s", doc, "yes" if actor else "-", cls or "", term)
    for e in curated:
        if e["excluded"]:
            log.info("Key entry %s not used in %s: defined there", e["id"], ", ".join(e["excluded"]))
    log.info("Inventory: %d defined + %d key entries -> %s", len(defined), len(curated), args.out)


if __name__ == "__main__":
    main()