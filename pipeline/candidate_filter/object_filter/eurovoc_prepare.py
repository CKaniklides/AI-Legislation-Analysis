"""
EuroVoc preparation: Step 1 (fetch) and Step 2 (normalize labels).

Step 1 downloads from the Publications Office SPARQL endpoint:
  - Dutch preferred and alternative labels for all EuroVoc concepts
  - broader links between concepts
  - microthesaurus membership per concept, and the domain of each microthesaurus
Each fetch is cached separately in <out>/cache, so a failed run resumes where
it stopped. Filtered results are written as raw_*.csv for Step 2.

Step 2 normalizes every label (lowercase, lemmatize, split off parenthetical
qualifiers) and builds a lookup table: normalized label -> concept(s).

IMPORTANT: the function `normalize_texts` must also be used later on the
LLM-extracted objects, so labels and objects are normalized identically.

Usage:
  python eurovoc_prepare.py                 # fetch + normalize
  python eurovoc_prepare.py --test          # check the endpoint with one small query
  python eurovoc_prepare.py --skip-fetch    # normalize from cached raw files
  python eurovoc_prepare.py --refresh       # re-download, ignoring the fetch cache
  python eurovoc_prepare.py --out data/eurovoc --spacy-model nl_core_news_lg

Requirements:
  pip install requests pandas spacy
  python -m spacy download nl_core_news_sm
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from pathlib import Path

import pandas as pd
import requests

ENDPOINT = "https://publications.europa.eu/webapi/rdf/sparql"
EUROVOC_SCHEME = "http://eurovoc.europa.eu/100141"
LANG = "nl"
PAGE_SIZE = 5_000      # lower this if the endpoint still times out
MAX_RETRIES = 4
PAUSE_S = 1.0          # be polite between pages

PREFIXES = """
PREFIX skos:   <http://www.w3.org/2004/02/skos/core#>
PREFIX skosxl: <http://www.w3.org/2008/05/skos-xl#>
PREFIX owl:    <http://www.w3.org/2002/07/owl#>
"""

IN_EUROVOC = f"?c skos:inScheme <{EUROVOC_SCHEME}> ."

log = logging.getLogger("eurovoc")


class SparqlError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Step 1: fetch
#
# The endpoint (Virtuoso) rejects queries it estimates to be expensive, and
# reports this as HTTP 500. All queries below are therefore kept minimal:
# one triple pattern family per query, no UNION, no FILTER NOT EXISTS, no
# DISTINCT. Deduplication and filtering happen in pandas afterwards.
# --------------------------------------------------------------------------

def run_sparql(query: str) -> list[dict]:
    """Run one SPARQL query; on failure, show the server's error message."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(
                ENDPOINT,
                params={"query": query,
                        "format": "application/sparql-results+json"},
                headers={"Accept": "application/sparql-results+json"},
                timeout=300,
            )
        except requests.RequestException as e:
            err = f"network error: {e}"
        else:
            if r.ok:
                rows = r.json()["results"]["bindings"]
                return [{k: v["value"] for k, v in row.items()} for row in rows]
            err = f"HTTP {r.status_code}: {r.text[:800]}"
        if attempt == MAX_RETRIES:
            raise SparqlError(f"{err}\n--- query ---\n{query}")
        wait = 5 * attempt
        log.warning("Query failed (%s), retry %d in %ds", err[:200], attempt, wait)
        time.sleep(wait)
    return []


def run_paged(where: str, select: str, key: str = "c") -> pd.DataFrame:
    """Run a query page by page using keyset pagination.

    The endpoint refuses sorted queries where OFFSET + LIMIT exceeds 10,000,
    so OFFSET is never used. Each page asks for rows whose key (the concept
    URI) sorts at or after the last key seen. Rows of that last key are
    dropped from the full page and re-fetched with the next one, so a
    concept's rows are never split across pages.
    """
    rows, last, page_no = [], None, 0
    while True:
        flt = f'FILTER(STR(?{key}) >= "{last}")' if last else ""
        q = (f"{PREFIXES}\nSELECT {select} WHERE {{ {where} {flt} }}\n"
             f"ORDER BY STR(?{key})\nLIMIT {PAGE_SIZE}")
        page = run_sparql(q)
        page_no += 1
        if len(page) < PAGE_SIZE:
            rows.extend(page)
            (log.debug if page_no == 1 else log.info)(
                "  page %d: %d rows (done)", page_no, len(page))
            break
        last = page[-1][key]
        kept = [r for r in page if r[key] != last]
        if not kept:
            raise SparqlError(f"one key has more than {PAGE_SIZE} rows: {last}")
        rows.extend(kept)
        log.info("  page %d: %d rows", page_no, len(kept))
        time.sleep(PAUSE_S)
    return pd.DataFrame(rows)


def connectivity_test() -> None:
    q = f"{PREFIXES}\nSELECT ?c ?label WHERE {{ {IN_EUROVOC} ?c skos:prefLabel ?label . " \
        f'FILTER(lang(?label) = "{LANG}") }} LIMIT 5'
    for row in run_sparql(q):
        print(row)


def fetch_label_property(prop: str, label_type: str, optional: bool) -> pd.DataFrame:
    """Labels via one property, e.g. 'skos:prefLabel' or
    'skosxl:altLabel/skosxl:literalForm'. SKOS-XL variants are optional:
    if the endpoint rejects them, we continue without them."""
    where = f'{IN_EUROVOC} ?c {prop} ?label . FILTER(lang(?label) = "{LANG}")'
    try:
        df = run_paged(where, "?c ?label")
    except SparqlError as e:
        if not optional:
            raise
        log.warning("Skipping %s: %s", prop, str(e).splitlines()[0][:200])
        return pd.DataFrame(columns=["concept", "label", "label_type"])
    if df.empty:
        return pd.DataFrame(columns=["concept", "label", "label_type"])
    df = df.rename(columns={"c": "concept"})
    df["label_type"] = label_type
    return df


def fetch_labels() -> pd.DataFrame:
    parts = [
        fetch_label_property("skos:prefLabel", "pref", optional=False),
        fetch_label_property("skos:altLabel", "alt", optional=True),
        fetch_label_property("skosxl:prefLabel/skosxl:literalForm", "pref", optional=True),
        fetch_label_property("skosxl:altLabel/skosxl:literalForm", "alt", optional=True),
    ]
    df = pd.concat(parts, ignore_index=True)
    return df.drop_duplicates(["concept", "label", "label_type"])


def fetch_deprecated() -> pd.DataFrame:
    where = f"{IN_EUROVOC} ?c owl:deprecated ?d ."
    try:
        df = run_paged(where, "?c ?d")
    except SparqlError as e:
        log.warning("Could not fetch deprecated flags, keeping all: %s",
                    str(e).splitlines()[0][:200])
        return pd.DataFrame(columns=["concept"])
    if df.empty:
        return pd.DataFrame(columns=["concept"])
    df = df[df["d"].str.lower() == "true"]
    return df[["c"]].rename(columns={"c": "concept"}).drop_duplicates()


def fetch_broader() -> pd.DataFrame:
    where = f"{IN_EUROVOC} ?c skos:broader ?b ."
    df = run_paged(where, "?c ?b")
    return df.rename(columns={"c": "concept", "b": "broader"})


MT_CODE = re.compile(r"^\s*\d{4}\b")


def list_candidate_schemes() -> list[str]:
    """Schemes in the EuroVoc namespace, found in two independent ways (either
    may be unsupported by the store; the union is used)."""
    queries = {
        "typed ConceptScheme":
            ('?mt a skos:ConceptScheme . '
             'FILTER(STRSTARTS(STR(?mt), "http://eurovoc.europa.eu/"))'),
        "has EuroVoc top concepts":
            (f'?mt skos:hasTopConcept ?c . ?c skos:inScheme <{EUROVOC_SCHEME}> . '
             'FILTER(STRSTARTS(STR(?mt), "http://eurovoc.europa.eu/"))'),
    }
    found: set[str] = set()
    for name, where in queries.items():
        try:
            df = run_paged(where, "DISTINCT ?mt", key="mt")
        except SparqlError as e:
            log.warning("Scheme lookup '%s' failed: %s", name, str(e).splitlines()[0][:200])
            continue
        n = 0 if df.empty else df["mt"].nunique()
        log.info("  scheme lookup '%s': %d schemes", name, n)
        if n:
            found |= set(df["mt"])
    found.discard(EUROVOC_SCHEME)
    if not found:
        raise SparqlError("No candidate microthesauri found by any lookup.")
    return sorted(found)


def fetch_scheme_labels(schemes: list[str]) -> pd.DataFrame:
    rows = []
    for i in range(0, len(schemes), 50):
        values = " ".join(f"<{u}>" for u in schemes[i:i + 50])
        q = (f"{PREFIXES}\nSELECT ?mt ?mt_label WHERE {{ VALUES ?mt {{ {values} }} "
             f'?mt skos:prefLabel ?mt_label . FILTER(lang(?mt_label) = "{LANG}") }}')
        rows.extend(run_sparql(q))
    return pd.DataFrame(rows, columns=["mt", "mt_label"]).drop_duplicates("mt")


def fetch_microthesauri() -> pd.DataFrame:
    """Concept -> microthesaurus membership.

    Asking for all schemes of every concept returns memberships from other
    datasets in the store as well (tens of thousands of rows). Instead:
      1. list schemes in the EuroVoc namespace,
      2. keep those whose Dutch label starts with a 4-digit code
         (the microthesaurus signature, e.g. '0406 politiek kader'),
      3. fetch the members of each microthesaurus separately.
    """
    schemes = list_candidate_schemes()
    labels = fetch_scheme_labels(schemes)
    mts = labels[labels["mt_label"].str.match(MT_CODE)]
    log.info("  microthesauri (4-digit code): %d of %d candidate schemes",
             len(mts), len(schemes))
    if not 100 <= len(mts) <= 160:
        log.warning("Expected roughly 127 microthesauri, got %d. "
                    "Inspect raw_microthesauri.csv before continuing.", len(mts))

    parts = []
    for n, (mt, lab) in enumerate(zip(mts["mt"], mts["mt_label"]), 1):
        where = f"VALUES ?mt {{ <{mt}> }} ?c skos:inScheme ?mt ."
        df = run_paged(where, "DISTINCT ?c ?mt")
        log.info("  [%d/%d] %s: %d concepts", n, len(mts), lab, len(df))
        if not df.empty:
            df["mt_label"] = lab
            parts.append(df)
        time.sleep(PAUSE_S)
    df = pd.concat(parts, ignore_index=True) if parts else \
        pd.DataFrame(columns=["c", "mt", "mt_label"])
    return df.rename(columns={"c": "concept", "mt": "microthesaurus"}) \
             .drop_duplicates(["concept", "microthesaurus"])


def derive_domain(mt_label: str | None) -> str | None:
    """'0406 politiek kader' -> '04'. Returns None if no code is present."""
    if not isinstance(mt_label, str):
        return None
    m = re.match(r"\s*(\d{2})\d{2}\b", mt_label)
    return m.group(1) if m else None


def cached(path: Path, fetch, refresh: bool) -> pd.DataFrame:
    """Return the cached CSV if present, otherwise fetch and save it.
    A failure in a later fetch therefore never loses earlier results."""
    if path.exists() and not refresh:
        log.info("Using cached %s", path.name)
        return pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])
    df = fetch()
    df.to_csv(path, index=False)
    return df


def step1_fetch(out: Path, refresh: bool = False) -> None:
    out.mkdir(parents=True, exist_ok=True)
    cache = out / "cache"
    cache.mkdir(exist_ok=True)

    log.info("Labels ...")
    labels = cached(cache / "labels.csv", fetch_labels, refresh)
    log.info("Deprecated flags ...")
    deprecated = set(cached(cache / "deprecated.csv", fetch_deprecated, refresh)["concept"])
    log.info("Broader links ...")
    broader = cached(cache / "broader.csv", fetch_broader, refresh)
    log.info("Microthesauri ...")
    mts = cached(cache / "microthesauri.csv", fetch_microthesauri, refresh)
    mts["domain"] = mts["mt_label"].map(derive_domain)

    # Filter deprecated concepts, and keep only EuroVoc concepts with a label.
    labels = labels[~labels["concept"].isin(deprecated)]
    known = set(labels["concept"])
    broader = broader[broader["concept"].isin(known) & broader["broader"].isin(known)]
    mts = mts[mts["concept"].isin(known)]

    labels.to_csv(out / "raw_labels.csv", index=False)
    broader.to_csv(out / "raw_broader.csv", index=False)
    mts.to_csv(out / "raw_microthesauri.csv", index=False)

    # Sanity checks: these are the things most likely to go wrong silently.
    n_concepts = labels["concept"].nunique()
    n_alt = (labels["label_type"] == "alt").sum()
    per_concept = mts.groupby("concept").size()
    log.info("Deprecated concepts removed: %d", len(deprecated))
    log.info("Concepts with a Dutch label: %d", n_concepts)
    log.info("Alternative labels: %d", n_alt)
    log.info("Broader links: %d", len(broader))
    log.info("Microthesauri: %d", mts["microthesaurus"].nunique())
    log.info("Concepts without microthesaurus: %d", n_concepts - len(per_concept))
    log.info("Microthesauri per concept: max %d, mean %.2f",
             per_concept.max() if len(per_concept) else 0,
             per_concept.mean() if len(per_concept) else 0)
    if n_concepts < 6000:
        log.warning("Fewer concepts than expected (~7,000). Check paging/filters.")
    if n_alt == 0:
        log.warning("No alternative labels found. Synonym matching will be limited.")
    n_no_domain = mts["domain"].isna().sum()
    if n_no_domain:
        log.warning("%d microthesaurus rows without a domain code.", n_no_domain)


# --------------------------------------------------------------------------
# Step 2: normalize
# --------------------------------------------------------------------------

_QUALIFIER = re.compile(r"\s*\(([^)]*)\)\s*")
_SPACES = re.compile(r"\s+")


def split_qualifier(label: str) -> tuple[str, str | None]:
    """'vergunning (administratief)' -> ('vergunning', 'administratief')."""
    quals = _QUALIFIER.findall(label)
    core = _QUALIFIER.sub(" ", label)
    core = _SPACES.sub(" ", core).strip()
    return core, ("; ".join(q.strip() for q in quals) or None)


def load_nlp(model: str):
    import spacy
    return spacy.load(model, disable=["parser", "ner"])


def normalize_texts(texts: list[str], nlp, batch_size: int = 512) -> list[str]:
    """Lowercase + lemmatize, dropping punctuation and whitespace tokens.
    Use this same function for the LLM-extracted objects later on."""
    out = []
    for doc in nlp.pipe((t.lower() for t in texts), batch_size=batch_size):
        lemmas = [t.lemma_.lower() for t in doc if not (t.is_punct or t.is_space)]
        out.append(_SPACES.sub(" ", " ".join(lemmas)).strip())
    return out


def step2_normalize(out: Path, spacy_model: str) -> None:
    labels = pd.read_csv(out / "raw_labels.csv")
    mts = pd.read_csv(out / "raw_microthesauri.csv")

    split = labels["label"].map(split_qualifier)
    labels["label_core"] = [s[0] for s in split]
    labels["qualifier"] = [s[1] for s in split]

    log.info("Lemmatizing %d labels with %s ...", len(labels), spacy_model)
    nlp = load_nlp(spacy_model)
    labels["label_norm"] = normalize_texts(labels["label_core"].tolist(), nlp)
    labels = labels[labels["label_norm"] != ""]

    labels[["concept", "label", "label_type", "label_core", "qualifier",
            "label_norm"]].to_csv(out / "labels_normalized.csv", index=False)

    # Concept table: one row per concept, with its preferred label and
    # microthesauri/domains (a concept can belong to several microthesauri).
    pref = (labels[labels["label_type"] == "pref"]
            .groupby("concept")["label"].first().rename("pref_label"))
    mt_agg = mts.groupby("concept").agg(
        microthesauri=("microthesaurus", lambda s: sorted(set(s))),
        domains=("domain", lambda s: sorted({str(d) for d in s if pd.notna(d)})),
    )
    concepts = pd.concat([pref, mt_agg], axis=1).reset_index()
    concepts = concepts.rename(columns={"index": "concept"})
    concepts.to_json(out / "concepts.jsonl", orient="records",
                     lines=True, force_ascii=False)

    # Lookup: normalized label -> concepts. Ambiguous labels keep all concepts;
    # disambiguation happens later (Step 7).
    lookup = labels.groupby("label_norm")["concept"].apply(
        lambda s: sorted(set(s))).to_dict()
    with open(out / "label_lookup.json", "w", encoding="utf-8") as f:
        json.dump(lookup, f, ensure_ascii=False, indent=0)

    n_ambig = sum(len(v) > 1 for v in lookup.values())
    log.info("Normalized labels: %d unique", len(lookup))
    log.info("Ambiguous normalized labels (>1 concept): %d", n_ambig)


# --------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", type=Path, default=Path("data/eurovoc"))
    p.add_argument("--spacy-model", default="nl_core_news_sm")
    p.add_argument("--skip-fetch", action="store_true",
                   help="reuse raw_*.csv from a previous run")
    p.add_argument("--refresh", action="store_true",
                   help="ignore cached fetches and download everything again")
    p.add_argument("--test", action="store_true",
                   help="run one tiny query to check the endpoint, then stop")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.test:
        connectivity_test()
        return
    if not args.skip_fetch:
        step1_fetch(args.out, refresh=args.refresh)
    step2_normalize(args.out, args.spacy_model)


if __name__ == "__main__":
    main()