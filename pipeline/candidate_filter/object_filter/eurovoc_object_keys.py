"""
Object filter, Step 5: give every object unit a set of keys.

The object filter compares two norms by the keys their objects share. This
script computes the keys of every cleaned object unit (from eurovoc_clean.py):

  head     the head noun: what kind of thing the object is
           ('administratieve geldboeten' -> geldboet)
  tail     the last part of a compound head, if that part is itself the head
           of some object in the corpus ('eindverslag' -> verslag). Links a
           specific object to its general type.
  nouns    the other nouns in the phrase: what the object is about
           ('informatie over geldboeten' -> geldboet)
  concept  the EuroVoc concept from eurovoc_maps.py, if the unit was mapped.
           Links different words for the same thing ('boete' ~ 'sanctie').

Keys are word stems, so singular and plural get the same key (geldboete and
geldboeten -> geldboet). A stem is made from the spaCy lemma, except when the
lemma is clearly wrong (it is not the start of the word, as in verslagen ->
verslaan); then the word itself is stemmed. Accents and hyphens are removed
first ('categorieën' -> categorieen, 'AI-systeem' -> aisysteem).

Inputs (in --dir)
  norm_object_units.jsonl  per norm: its object units   (eurovoc_clean.py)
  object_mapping.jsonl     per unit: EuroVoc candidates (eurovoc_maps.py)

Outputs (in --dir)
  unit_keys.jsonl          per unit: head, tail, nouns, concept, plus readable forms
  norm_keys.jsonl          per norm: norm_id, object, and the keys of each unit

Usage
  python object_keys.py
  python object_keys.py --dir data/conflict/eurovoc --min-tail-len 4

Requirements
  pip install spacy nltk     (and the Dutch spaCy model used in the other steps)
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import unicodedata
from pathlib import Path

from nltk.stem.snowball import DutchStemmer

from eurovoc_clean import NON_CONTENT

log = logging.getLogger("object.keys")
STEMMER = DutchStemmer()
NOUN_POS = ("NOUN", "PROPN")


# --------------------------------------------------------------------------
# Word -> key
# --------------------------------------------------------------------------

def _plain(word: str) -> str:
    """Lowercase, without accents and hyphens."""
    word = unicodedata.normalize("NFKD", word.lower()).encode("ascii", "ignore").decode()
    return word.replace("-", "")


def word_key(token) -> str:
    """Stem of the token's lemma, or of the word itself if the lemma is not
    the start of the word (a sign that the lemmatizer picked a verb)."""
    lemma, text = _plain(token.lemma_), _plain(token.text)
    return STEMMER.stem(lemma if lemma and text.startswith(lemma) else text)


def prepare(text: str) -> str:
    """Drop the plural 's so that risico's and risico get the same key."""
    return re.sub(r"[’']s\b", "", text)


# --------------------------------------------------------------------------
# Head noun (same rule as the map step)
# --------------------------------------------------------------------------

def head_token(doc):
    """The syntactic root if it is a noun, otherwise the noun closest to the
    root. None if the unit has no noun."""
    nouns = [t for t in doc if t.pos_ in NOUN_POS]
    if not nouns:
        # one-word units are often tagged as something else ('doelstellingen')
        return doc[0] if len(doc) == 1 and doc[0].is_alpha else None
    root = next((t for t in doc if t.dep_ == "ROOT"), None)
    if root is not None and root.pos_ in NOUN_POS:
        return root

    def depth(t):
        d = 0
        while t.head is not t and d < 50:
            t, d = t.head, d + 1
        return d
    return min(nouns, key=lambda t: (depth(t), t.i))


def is_content(token) -> bool:
    """Not a reference word such as 'artikel' or 'lid'."""
    return token.text.lower() not in NON_CONTENT and token.lemma_.lower() not in NON_CONTENT


# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------

def parse_units(units: list[str], nlp) -> dict[str, dict]:
    """Head and noun keys per unit (tails and concepts are added later)."""
    out = {}
    for unit, doc in zip(units, nlp.pipe([prepare(u) for u in units], batch_size=256)):
        head = head_token(doc)
        head_ok = head is not None and is_content(head)
        head_key = word_key(head) if head_ok else None
        nouns = sorted({word_key(t) for t in doc
                        if t.pos_ in NOUN_POS and is_content(t) and t is not head} - {head_key})
        out[unit] = {"unit": unit,
                     "head": head_key,
                     "head_word": head.text.lower() if head_ok else None,
                     "tail": None,
                     "nouns": [n for n in nouns if n],
                     "concept": None, "concept_label": None}
    return out


def compound_tail(head: str, heads: set[str], min_len: int) -> str | None:
    """Longest proper ending of the head that is itself a head in the corpus."""
    for i in range(1, len(head) - min_len + 1):
        if head[i:] in heads:
            return head[i:]
    return None


def load_concepts(path: Path) -> dict[str, tuple[str, str]]:
    """Unit -> (concept, label) of the top EuroVoc candidate, if mapped."""
    concepts = {}
    if not path.is_file():
        log.warning("%s not found: no concept keys.", path.name)
        return concepts
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("level") and r.get("candidates"):
                c = r["candidates"][0]
                concepts[r["object"]] = (c["concept"], c.get("label"))
    return concepts


# --------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dir", type=Path, default=Path("data/conflict/object"))
    p.add_argument("--spacy-model", default="nl_core_news_sm")
    p.add_argument("--min-tail-len", type=int, default=4,
                   help="minimum length of a compound tail key (boete -> 'boet' has 4)")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    with open(args.dir / "norm_object_units.jsonl", encoding="utf-8") as f:
        norms = [json.loads(l) for l in f if l.strip()]
    units = sorted({u for n in norms for u in n["units"] if u})

    import spacy
    nlp = spacy.load(args.spacy_model, disable=["ner"])
    keys = parse_units(units, nlp)

    # compound tails: only parts that are the head of some other unit
    heads = {k["head"] for k in keys.values() if k["head"]}
    for k in keys.values():
        if k["head"]:
            k["tail"] = compound_tail(k["head"], heads, args.min_tail_len)

    for unit, (concept, label) in load_concepts(args.dir / "eurovoc/object_mapping.jsonl").items():
        if unit in keys:
            keys[unit]["concept"], keys[unit]["concept_label"] = concept, label

    with open(args.dir / "unit_keys.jsonl", "w", encoding="utf-8") as f:
        for u in units:
            f.write(json.dumps(keys[u], ensure_ascii=False) + "\n")
    with open(args.dir / "norm_keys.jsonl", "w", encoding="utf-8") as f:
        for n in norms:
            f.write(json.dumps({"norm_id": n["norm_id"], "object": n["object"],
                                "units": [keys[u] for u in n["units"] if u in keys]},
                               ensure_ascii=False) + "\n")

    vals = keys.values()
    log.info("Units: %d, with head: %d, with tail: %d, with nouns: %d, with concept: %d",
             len(units), sum(1 for k in vals if k["head"]), sum(1 for k in vals if k["tail"]),
             sum(1 for k in vals if k["nouns"]), sum(1 for k in vals if k["concept"]))


if __name__ == "__main__":
    main()