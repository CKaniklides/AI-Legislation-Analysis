"""
Actor filter, Step 2b: map every cleaned actor unit to an actor ID.

Input is the output of actor_clean.py (per norm: its actor units) and the
inventory from actor_inventory.py. Each unit gets a record

  role            the unit's words as keys: adjectives, head, then the nouns
                  after the first preposition ('functionaris voor
                  gegevensbescherming' -> functionaris gegevensbescherm)
  role_keys       role and its shorter versions, in the order they are tried
  head            key of the head word
  actor_id        the inventory entry that matched, if any
  match           exact | shortened | fallback | not_actor
  fallback_class  broad class of the head (actor_heads.tsv), only without actor_id

How a unit is read
  The head is the last word before the first preposition, skipping a few
  adverbs that can follow it ('aanbieder verder in de AI-waardeketen' ->
  aanbieder). Words before the head are its adjectives; content words after
  it are its complement. In 'door de markttoezichtautoriteiten verkregen
  informatie' the head is 'informatie', so the unit is not an actor.

How a unit is matched
  Shorter versions drop adjectives from the left and complement words from
  the right; the head always stays. They are tried longest first:
  'bevoegde toezichthoudende autoriteit van de lidstaat' tries
  bevoegd toezichthoud autoriteit lidstat, bevoegd toezichthoud autoriteit,
  toezichthoud autoriteit lidstat, toezichthoud autoriteit, autoriteit lidstat,
  autoriteit. The first version that is the key of an inventory entry valid for
  the norm's document gives the actor_id. If a defined term and a key actor
  share that key, the defined term wins. If no version matches, the head's class
  in actor_heads.tsv is the fallback; a head without a class is not an actor.

Keys are made from the text alone (lowercase, no accents or hyphens, plural
-s after a vowel removed, then the Dutch Snowball stem), for units and
inventory labels alike, so both sides always get the same key. No parser is
needed: units from actor_clean.py are already short noun phrases.

Inputs (in --dir)
  norm_actor_units.jsonl   per norm: norm_id, actor, kind, coord, units
  actor_inventory.jsonl    inventory entries (actor_inventory.py)
  The document of a norm is the part of norm_id before the first ':'
  ('32024R1689:5:2:2' -> 32024R1689), unless --doc-field names a field.
Outputs (in --dir)
  norm_actor_map.jsonl     per norm: norm_id, document, actor, kind, coord,
                           status, actors (one record per unit)
                           status: mapped | fallback | not_actor | pronoun |
                                   missing | no_units
  unit_actor_map.jsonl     per document and unit: the record and how many norms
                           use it, for review

Usage
  python actor_map.py
  python actor_map.py --dir data/conflict/actor
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from collections import Counter, defaultdict
from pathlib import Path

from nltk.stem.snowball import DutchStemmer

from actor_lexicon import head_class, in_scope, plain

log = logging.getLogger("actor.map")
STEMMER = DutchStemmer()

# words that are not content: articles, prepositions, conjunctions, pronouns
FUNCTION_WORDS = {
    "de", "het", "een", "der", "des", "en", "of", "en/of", "die", "dat", "dit", "deze",
    "hun", "zijn", "haar", "van", "voor", "in", "op", "met", "tot", "aan", "bij", "over",
    "onder", "uit", "door", "tegen", "naar", "als", "om", "te", "ten", "ter", "binnen",
}
# words that can follow the head before its complement ('aanbieder verder in ...')
AFTER_HEAD = {"verder", "zelf", "zelve", "alleen", "samen", "gezamenlijk", "daarvan"}
PREPOSITIONS = {"van", "voor", "in", "op", "met", "tot", "aan", "bij", "over", "onder",
                "uit", "door", "tegen", "naar", "als", "om", "binnen", "ten", "ter"}
RE_WORD = re.compile(r"[^\W\d_][\w\-’']*", re.UNICODE)


# --------------------------------------------------------------------------
# Words -> keys
# --------------------------------------------------------------------------

def key(word: str) -> str:
    """Key of one word. The plural -s after a vowel ('instanties', 'comités',
    'kmo’s') and 'leden' are handled first; Snowball leaves those alone."""
    w = plain(re.sub(r"[’']s$", "", word))
    if w.endswith("leden"):
        w = w[:-5] + "lid"
    elif len(w) > 4 and w.endswith("s") and w[-2] in "aeiou":
        w = w[:-1]
    return STEMMER.stem(w)


def read_unit(text: str) -> tuple[list[str], str | None, list[str]]:
    """(adjective words, head word, complement content words) of a unit."""
    words = RE_WORD.findall(text)
    if not words:
        return [], None, []
    cut = next((i for i, w in enumerate(words) if w.lower() in PREPOSITIONS and i > 0), len(words))
    before, after = words[:cut], words[cut:]
    h = len(before) - 1
    while h > 0 and before[h].lower() in AFTER_HEAD:
        h -= 1
    pre = [w for w in before[:h] if w.lower() not in FUNCTION_WORDS]
    post = [w for w in before[h + 1:] + after if w.lower() not in FUNCTION_WORDS]
    return pre, before[h], post


def role_versions(pre: list[str], head: str, post: list[str]) -> list[str]:
    """Role key and its shorter versions, longest first (see the docstring)."""
    pk, hk, qk = [key(w) for w in pre], key(head), [key(w) for w in post]
    out = []
    for i in range(len(pk) + 1):
        for k in range(len(qk), -1, -1):
            v = " ".join(pk[i:] + [hk] + qk[:k])
            if v not in out:
                out.append(v)
    return out


# --------------------------------------------------------------------------
# Inventory index
# --------------------------------------------------------------------------

def index_inventory(entries: list[dict]) -> dict[str, list[dict]]:
    """Full role key of every label and alias -> entries with that key,
    defined terms first."""
    index = defaultdict(list)
    for e in entries:
        for name in [e["label"]] + e.get("aliases", []):
            pre, head, post = read_unit(name)
            if head is None:
                continue
            k = role_versions(pre, head, post)[0]
            if e not in index[k]:
                index[k].append(e)
    for k in index:
        index[k].sort(key=lambda e: e["kind"] != "defined")
    return index


# --------------------------------------------------------------------------
# Mapping
# --------------------------------------------------------------------------

def map_unit(unit: str, document: str, index: dict[str, list[dict]],
             stats: Counter) -> dict:
    pre, head, post = read_unit(unit)
    rec = {"unit": unit, "role": None, "role_keys": [], "head": None,
           "actor_id": None, "label": None, "match": "not_actor", "fallback_class": None}
    if head is None:
        return rec
    versions = role_versions(pre, head, post)
    rec.update(role=versions[0], role_keys=versions, head=key(head))
    for n, v in enumerate(versions):
        hits = [e for e in index.get(v, []) if in_scope(e, document)]
        if hits:
            if len(hits) > 1 and hits[0]["kind"] == hits[1]["kind"]:
                stats["ambiguous"] += 1
                log.debug("ambiguous %r in %s: %s", unit, document, [e["id"] for e in hits])
            rec.update(actor_id=hits[0]["id"], label=hits[0]["label"],
                       match="exact" if n == 0 else "shortened")
            return rec
    cls = head_class(head)
    if cls:
        rec.update(match="fallback", fallback_class=cls)
    return rec


def norm_status(kind: str, actors: list[dict]) -> str:
    if kind != "units":
        return kind
    if any(a["actor_id"] for a in actors):
        return "mapped"
    if any(a["match"] == "fallback" for a in actors):
        return "fallback"
    return "not_actor"


def document_of(norm: dict, field: str | None) -> str:
    if field:
        return str(norm.get(field) or "")
    return str(norm.get("norm_id") or "").split(":", 1)[0]


# --------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dir", type=Path, default=Path("data/conflict/actor"))
    p.add_argument("--inventory", type=Path, default=None,
                   help="default: <dir>/actor_inventory.jsonl")
    p.add_argument("--doc-field", default=None,
                   help="field with the norm's document (default: norm_id before ':')")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    inv_path = args.inventory or args.dir / "actor_inventory.jsonl"
    with open(inv_path, encoding="utf-8") as f:
        entries = [json.loads(l) for l in f if l.strip()]
    with open(args.dir / "norm_actor_units.jsonl", encoding="utf-8") as f:
        norms = [json.loads(l) for l in f if l.strip()]
    index = index_inventory(entries)

    stats, cache, uses = Counter(), {}, Counter()
    status, matches = Counter(), Counter()
    with open(args.dir / "norm_actor_map.jsonl", "w", encoding="utf-8") as f:
        for n in norms:
            doc = document_of(n, args.doc_field)
            actors = []
            for u in n.get("units", []):
                if (doc, u) not in cache:
                    cache[(doc, u)] = map_unit(u, doc, index, stats)
                uses[(doc, u)] += 1
                actors.append(cache[(doc, u)])
                matches[cache[(doc, u)]["match"]] += 1
            # a unit that is not an actor is dropped from the norm's actors
            kept = [a for a in actors if a["match"] != "not_actor"]
            st = norm_status(n.get("kind", "units" if actors else "missing"), actors)
            status[st] += 1
            f.write(json.dumps({"norm_id": n.get("norm_id"), "document": doc,
                                "actor": n.get("actor"), "kind": n.get("kind"),
                                "coord": n.get("coord") if len(kept) > 1 else None,
                                "status": st, "actors": kept,
                                "dropped": [a["unit"] for a in actors if a["match"] == "not_actor"]},
                               ensure_ascii=False) + "\n")

    with open(args.dir / "unit_actor_map.jsonl", "w", encoding="utf-8") as f:
        for (doc, u), rec in sorted(cache.items(), key=lambda x: -uses[x[0]]):
            f.write(json.dumps({"document": doc, "norms": uses[(doc, u)], **rec},
                               ensure_ascii=False) + "\n")

    total = sum(status.values())
    log.info("Norms: %d -> %s", total, ", ".join(
        f"{k} {v} ({100 * v / total:.1f}%)" for k, v in status.most_common()))
    units = sum(matches.values())
    log.info("Unit uses: %d -> %s", units, ", ".join(
        f"{k} {v} ({100 * v / units:.1f}%)" for k, v in matches.most_common()))
    if stats["ambiguous"]:
        log.info("Units with more than one entry of the same kind: %d (first taken; "
                 "run with DEBUG logging to list them)", stats["ambiguous"])


if __name__ == "__main__":
    main()