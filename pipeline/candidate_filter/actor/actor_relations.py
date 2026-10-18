"""
Actor filter, Step 3a: find relations between actor IDs, and how much they cover.

Relations are found in two ways, both within one document (a relation is only
used for norms of a document in which both actors are valid):

  definitions        the start of an actor's definition, up to the first
                     relative clause or exclusion, names what it is:
                       'aangemelde instantie: een conformiteitsbeoordelings-
                        instantie die ...'            -> subtype of it
                       'nationale bevoegde autoriteit: een aanmeldende
                        autoriteit of een markttoezichtautoriteit; ...'
                                                      -> each listed actor is
                                                         a subtype of it
                       'AI-bureau: de taak van de Commissie waarbij ...'
                                                      -> part of it
  labels in labels   a label that is another label plus words is a subtype of it
                     (labels only, not aliases):
                       'leidende toezichthoudende autoriteit' -> toezichthoudende
                       autoriteit, 'dochteronderneming' -> onderneming,
                       'aanbieder verder in de AI-waardeketen' -> aanbieder
  inventory          part_of fields set by hand in the inventory

From these, two relations are derived:
  subtype2           a subtype of a subtype (two steps)
  sibling            two subtypes of the same actor

Relation types and their direction: 'a subtype b' means a is a kind of b;
'a part_of b' means a belongs to b. Sibling is symmetric.

Coverage (--norms)
  types   the share of pairs of distinct actor IDs that have a relation, among
          all pairs of IDs that occur in the norms and are valid in a common
          document. Every pair counts once, however often it occurs.
  pairs   the share of pairs of norms with a mapped actor whose actors are the
          same or related (the best pair of their actors counts). Shown for
          pairs within a document and across documents.

Inputs
  --inventory  actor_inventory.jsonl (defined entries carry their definition)
  --norms      norm_actor_map.jsonl (optional, for the coverage estimate)
Outputs
  --out        actor_relations.jsonl: a, b, relation, source, documents, evidence

Usage
  python actor_relations.py --dir data/conflict/actor
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import re
from collections import Counter, defaultdict
from pathlib import Path

from actor_lexicon import in_scope, plain
from actor_map import FUNCTION_WORDS, RE_WORD, key, read_unit

log = logging.getLogger("actor.relations")

# Where the genus part of a definition ends: relative clauses, exclusions,
# conditions and the end of the first clause.
RE_GENUS_END = re.compile(
    r"\b(?:die|dat|welke|wie|waarbij|waarin|waarvan|waarmee|waardoor|waaraan|wanneer|indien|"
    r"niet\s+zijnde|noch|al\s+dan\s+niet|met\s+uitzondering\s+van|dan\s+de|dan\s+het|"
    r"als\s+bedoeld|zoals\s+bedoeld|zoals\s+gedefinieerd|in\s+de\s+zin\s+van)\b|[;:]", re.I)
RE_ENUM = re.compile(r"^\s*(?:\(?[a-z0-9]{1,3}\)|[ivx]+\.)\s*", re.I)
LEADING = {"een", "de", "het", "elke", "ieder", "iedere", "alle", "enige"}


# --------------------------------------------------------------------------
# Labels as key sequences
# --------------------------------------------------------------------------

def label_keys(text: str) -> list[str]:
    """Content words of a label as keys ('leidende toezichthoudende autoriteit'
    -> leid toezichthoud autoriteit)."""
    return [key(w) for w in RE_WORD.findall(text) if w.lower() not in FUNCTION_WORDS]


def structure(text: str) -> tuple[tuple[str, ...], str, tuple[str, ...]] | None:
    """(adjective keys, head key, complement keys), as in actor_map.py."""
    pre, head, post = read_unit(text)
    if head is None:
        return None
    return tuple(key(w) for w in pre), key(head), tuple(key(w) for w in post)


def names(e: dict) -> list[str]:
    return [e["label"]] + list(e.get("aliases", []))


def documents_of(entries: list[dict]) -> list[str]:
    docs = set()
    for e in entries:
        docs.update(e.get("documents", []))
        if isinstance(e.get("scope"), list):
            docs.update(e["scope"])
    return sorted(docs)


# --------------------------------------------------------------------------
# Relations from labels inside labels
# --------------------------------------------------------------------------

def contains_label(a: tuple, b: tuple, min_tail: int = 4) -> bool:
    """Label a is label b plus words: b's adjectives end a's adjectives, b's
    complement starts a's complement, and b's head ends a's head (equal, or a
    compound tail of at least min_tail letters)."""
    (pa, ha, qa), (pb, hb, qb) = a, b
    if a == b:
        return False
    if not (ha == hb or (ha.endswith(hb) and len(hb) >= min_tail)):
        return False
    if len(pb) > len(pa) or (pb and pa[len(pa) - len(pb):] != pb):
        return False
    return qa[:len(qb)] == qb


def label_relations(entries: list[dict], docs: list[str]) -> list[dict]:
    out = []
    for doc in docs:
        valid = [e for e in entries if in_scope(e, doc)]
        # labels only: aliases are often full official names ('Europese raad voor
        # artificiële intelligentie'), whose words do not make a subtype ('Raad')
        shapes = {e["id"]: structure(e["label"]) for e in valid}
        for a, b in itertools.permutations(valid, 2):
            sa, sb = shapes[a["id"]], shapes[b["id"]]
            if a["id"] != b["id"] and sa and sb and contains_label(sa, sb):
                out.append({"a": a["id"], "b": b["id"], "relation": "subtype", "source": "label",
                            "documents": [doc],
                            "evidence": f"'{a['label']}' contains '{b['label']}'"})
    return out


# --------------------------------------------------------------------------
# Relations from definitions
# --------------------------------------------------------------------------

def genus(definition: str) -> tuple[str, bool]:
    """The start of a definition up to where it stops naming what the term is,
    and whether it was cut (False: the genus is the whole first clause)."""
    text = RE_ENUM.sub("", definition or "")
    m = RE_GENUS_END.search(text)
    return (text[:m.start()] if m else text).strip(" ,"), bool(m and m.group(0) not in ";:")


def find_labels(words: list[str], valid: list[dict], self_id: str) -> list[tuple[int, int, dict]]:
    """(start, end, entry) of every label of a valid entry found in the words,
    longest first, without overlaps."""
    keys = [key(w) for w in words]
    found = []
    for e in valid:
        if e["id"] == self_id:
            continue
        for n in names(e):
            lk = label_keys(n)
            if not lk:
                continue
            for i in range(len(keys) - len(lk) + 1):
                # the last word may be a compound ending with the label's last word
                if keys[i:i + len(lk) - 1] == lk[:-1] and keys[i + len(lk) - 1] == lk[-1]:
                    found.append((i, i + len(lk), e))
    found.sort(key=lambda f: (-(f[1] - f[0]), f[0]))
    taken, out = set(), []
    for s, t, e in found:
        if not taken & set(range(s, t)) and all(e["id"] != o[2]["id"] for o in out):
            taken |= set(range(s, t))
            out.append((s, t, e))
    return sorted(out, key=lambda f: f[0])


def definition_relations(entries: list[dict]) -> list[dict]:
    out = []
    for d in (e for e in entries if e["kind"] == "defined" and e.get("definition")):
        doc = d["documents"][0]
        valid = [e for e in entries if in_scope(e, doc)]
        seg, cut = genus(d["definition"])
        words = [w for w in RE_WORD.findall(seg)]
        while words and words[0].lower() in LEADING:
            words = words[1:]
        content = [i for i, w in enumerate(words) if w.lower() not in FUNCTION_WORDS]
        hits = find_labels(words, valid, d["id"])
        if not hits:
            continue
        rel = lambda b, r: {"a": d["id"], "b": b, "relation": r, "source": "definition",
                            "documents": [doc], "evidence": seg[:160]}
        first = hits[0]
        pos = content.index(first[0]) if first[0] in content else 99
        before = {w.lower() for w in words[:first[0]]}
        # extensional list: 'een A, B of C' and nothing else -> A, B, C are subtypes of d
        listed = len(hits) >= 2 and not cut and "of" in {w.lower() for w in words} and \
            sum(t - s for s, t, _ in hits) >= 0.6 * len(content)
        of_x = first[0] >= 1 and "van" in [w.lower() for w in words[max(0, first[0] - 2):first[0]]]
        if listed:
            out += [{**rel(e["id"], "subtype"), "a": e["id"], "b": d["id"]} for _, _, e in hits]
        elif len(hits) == 1 and of_x and pos <= 1:
            out.append(rel(first[2]["id"], "part_of"))                       # 'de taak van de X'
        elif pos == 0 or (pos == 1 and not before & {"of", ","}):
            out.append(rel(first[2]["id"], "subtype"))                       # 'een X die ...' 
    return out


def inventory_relations(entries: list[dict]) -> list[dict]:
    return [{"a": e["id"], "b": e["part_of"], "relation": "part_of", "source": "inventory",
             "documents": e["scope"] if isinstance(e.get("scope"), list) else e.get("documents", []),
             "evidence": "part_of in inventory"} for e in entries if e.get("part_of")]


def derive(direct: list[dict]) -> list[dict]:
    """Two-step subtypes and siblings from the direct subtypes."""
    parents = defaultdict(set)
    docs = defaultdict(set)
    for r in direct:
        if r["relation"] == "subtype":
            parents[r["a"]].add(r["b"])
            docs[(r["a"], r["b"])].update(r["documents"])
    have = {(r["a"], r["b"]) for r in direct}
    out = []
    for a, ps in parents.items():
        for p in ps:
            for g in parents.get(p, ()):
                if g != a and (a, g) not in have:
                    have.add((a, g))
                    out.append({"a": a, "b": g, "relation": "subtype2", "source": "derived",
                                "documents": sorted(docs[(a, p)] & docs[(p, g)]),
                                "evidence": f"via {p}"})
    children = defaultdict(set)
    for a, ps in parents.items():
        for p in ps:
            children[p].add(a)
    seen = set()
    for p, cs in children.items():
        for a, b in itertools.combinations(sorted(cs), 2):
            if (a, b) in seen or (a, b) in have or (b, a) in have:
                continue
            seen.add((a, b))
            out.append({"a": a, "b": b, "relation": "sibling", "source": "derived",
                        "documents": sorted(docs[(a, p)] & docs[(b, p)]), "evidence": f"both {p}"})
    return out


# --------------------------------------------------------------------------
# Coverage
# --------------------------------------------------------------------------

def coverage(relations: list[dict], entries: list[dict], norms_path: Path) -> None:
    by_id = {e["id"]: e for e in entries}
    rel = {}
    for r in relations:
        for d in r["documents"] or [None]:
            rel[(r["a"], r["b"], d)] = r["relation"]
            rel[(r["b"], r["a"], d)] = r["relation"]

    def relation(a, b, doc):
        if a == b:
            return "same"
        return rel.get((a, b, doc))

    norms = [json.loads(l) for l in open(norms_path, encoding="utf-8") if l.strip()]
    sig = Counter()
    for n in norms:
        ids = tuple(sorted({a["actor_id"] for a in n.get("actors", []) if a.get("actor_id")}))
        if ids:
            sig[(n["document"], ids)] += 1

    # types: distinct ID pairs valid in a common document
    used = sorted({(doc, i) for (doc, ids) in sig for i in ids})
    docs = sorted({d for d, _ in used})
    type_rel = Counter()
    for doc in docs:
        ids = sorted({i for d, i in used if d == doc})
        for a, b in itertools.combinations(ids, 2):
            type_rel[rel.get((a, b, doc)) or "none"] += 1
    # cross-document type pairs: only shared IDs (EU-wide key actors) can relate
    cross, seen = Counter(), set()
    for d1, d2 in itertools.combinations(docs, 2):
        i1 = {i for d, i in used if d == d1}
        i2 = {i for d, i in used if d == d2}
        for a in i1:
            for b in i2:
                pair = frozenset((a, b))
                if pair not in seen:
                    seen.add(pair)
                    cross["same" if a == b else "none"] += 1

    def show(title, c, extra=""):
        t = sum(c.values())
        print(f"\n{title}: {t} pairs{extra}")
        for k, v in sorted(c.items(), key=lambda x: -x[1]):
            print(f"  {k:<9} {v:>8}  {100 * v / t:5.1f}%")
        cov = t - c.get("none", 0)
        print(f"  covered   {cov:>8}  {100 * cov / t:5.1f}%")

    print(f"\nActor IDs in the norms: {len({i for _, i in used})} "
          f"({', '.join(f'{d} {sum(1 for x, _ in used if x == d)}' for d in docs)})")
    show("Actor ID combinations within a document (distinct IDs, each pair once)", type_rel)
    show("Actor ID combinations across documents (each pair once)", cross)

    # norm pairs, by actor-set signature
    order = ["same", "subtype", "part_of", "subtype2", "sibling"]
    within, across = Counter(), Counter()
    items = list(sig.items())
    for x, ((d1, s1), n1) in enumerate(items):
        for (d2, s2), n2 in items[x:]:
            w = n1 * (n1 - 1) // 2 if (d1, s1) == (d2, s2) else n1 * n2
            if not w:
                continue
            if d1 == d2:
                found = {relation(a, b, d1) for a in s1 for b in s2} - {None}
            else:
                found = {"same"} if set(s1) & set(s2) else set()
            best = next((o for o in order if o in found), "none")
            (within if d1 == d2 else across)[best] += w
    show("Norm pairs within a document (best relation of their actors)", within)
    show("Norm pairs across documents", across)
    both = within + across
    show("All norm pairs with a mapped actor", both)


# --------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dir", type=Path, default=Path("data/conflict/actor"))
    p.add_argument("--inventory", type=Path, default=None)
    p.add_argument("--norms", type=Path, default=None, help="default: <dir>/norm_actor_map.jsonl")
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    inv = args.inventory or args.dir / "actor_inventory.jsonl"
    entries = [json.loads(l) for l in open(inv, encoding="utf-8") if l.strip()]
    docs = documents_of(entries)

    direct = definition_relations(entries) + label_relations(entries, docs) + \
        inventory_relations(entries)
    # one relation per ordered pair and document: definitions before labels
    seen, uniq = set(), []
    for r in direct:
        k = (r["a"], r["b"], tuple(r["documents"]))
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    relations = uniq + derive(uniq)

    out = args.out or args.dir / "actor_relations.jsonl"
    with open(out, "w", encoding="utf-8") as f:
        for r in relations:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    c = Counter((r["relation"], r["source"]) for r in relations)
    log.info("Relations: %s -> %s", ", ".join(f"{a}/{b} {v}" for (a, b), v in c.items()), out)
    for r in relations:
        log.info("  %-9s %-10s %s -> %s   [%s]", r["relation"], r["source"], r["a"], r["b"],
                 r["evidence"][:90])

    norms = args.norms or args.dir / "norm_actor_map.jsonl"
    if norms.is_file():
        coverage(relations, entries, norms)


if __name__ == "__main__":
    main()