"""Stage A: cheap exclusion filters on norm pairs.

Input : data/conflicts_data/merged_norms.json   (output of merge_norms.py)
        data/conflicts_data/graph_edges.json    (links between articles, see below)
Output: data/conflicts_data/stageA_pairs.json

Rules (a pair is discarded if either rule fires)
------------------------------------------------
1. Same article. Discard the pair when both norms have the same ``hierarchy.instrument_id``
   AND the same ``hierarchy.article``. The same article number in two different documents
   (e.g. GDPR art. 5 vs AI Act art. 5) is NOT the same article, so that pair stays; so does
   a pair from two different articles of one document. A norm whose instrument_id or
   article is missing is never discarded by this rule (counted and warned about).

2. Unrelated subject. Discard the pair when the cosine similarity of the two norms'
   action + trigger text (see norm_similarity.norm_text) is below SIMILARITY_FLOOR AND the
   graph has no direct link between their articles. A norm without action/trigger text has
   no score, so its pairs are never discarded by this rule.

Graph links
-----------
Links come from the networkx citation graph built by graph.py, whose node ids follow the
same uid convention as ``hierarchy.graph_uid`` (e.g. "eu:32024R1689:art2@original"). The
file is GEXF (.gexf); for flexibility, node-link JSON (.json), GraphML (.graphml), GML
(.gml), a pickled graph (.gpickle / .pkl / .pickle) and a plain JSON list of
[uid_a, uid_b] edges are also accepted, picked by extension. GEXF is read with a small
custom reader because networkx's read_gexf drops an edge's ``type`` attribute (GEXF
reserves it for edge direction), which LINK_EDGE_TYPES may need.

- Direction is ignored: A citing B links them just as B citing A does.
- Only direct edges count; two articles that both cite a third are NOT linked.
- Version suffixes ("@original", "@2025-11-21") are ignored on both sides, so a citation
  to any version of an article counts.
- LINK_EDGE_TYPES optionally restricts which edge types count as a link (None = all).
- A norm without ``hierarchy.graph_uid`` gets one reconstructed the way the adjudication
  pipeline's _graph_uid does for the two hand-built Datasets/ sources (BWBR0051796,
  BWBR0049497). Norms whose uid is not a node in the graph are counted and warned about,
  since that usually means a uid convention mismatch rather than a truly unlinked article.

Running without a graph requires --no-graph, so links are never ignored silently.

Embeddings
----------
Vectors are cached by text in data/conflicts_data/norm_embeddings.npz, so re-running with
another SIMILARITY_FLOOR does not re-encode anything. Changing the model rebuilds the cache.

Output format (compact on purpose: the pair list is millions of entries long)
-----------------------------------------------------------------------------
    {
      "stage": "A", "rule": "...", "input": "...", "graph": "...",
      "model": "...", "similarity_floor": 0.75, "n_norms": 3000,
      "stats": {"total_pairs": ..., "discarded_same_article": ...,
                "discarded_low_similarity": ..., "rescued_by_graph_link": ...,
                "kept": ..., "discarded_by_instrument": {...},
                "norms_without_article_key": 0, "norms_without_text": 0,
                "norms_without_graph_uid": 0, "norms_not_in_graph": 0},
      "norm_ids": ["32024R1689:2:5:6:None", ...],   # position k = norm number k
      "pair_format": ["pair_id", "i", "j"],
      "pairs": [[15, 0, 17], [16, 0, 18], ...]       # i, j index norm_ids, always i < j
    }

"rescued_by_graph_link" counts pairs that scored below the floor but were kept because
their articles are linked. Pairs are listed by i, then j, in input order, and streamed to
disk.

Pair ids
--------
``pair_id`` is the pair's position among ALL n*(n-1)/2 pairs of the input norms (ordered
by i, then j), not its position in the kept list. So the same two norms keep the same id
when SIMILARITY_FLOOR, the graph or the rules change; ids only change when the norm list
itself (its contents or order) changes. Gaps in the ids are the discarded pairs. See
pair_id() for the formula.

Usage
-----
    python stageA_filtering.py
    python stageA_filtering.py --sim-floor 0.8 --graph path/to/edges.json
    python stageA_filtering.py --no-graph
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pickle
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable, Iterator, Optional

import networkx as nx
import numpy as np

from norm_similarity import NormSimilarity, norm_text

log = logging.getLogger(__name__)

# --- Tunable --------------------------------------------------------------------------
# Pairs scoring below this AND without a graph link are discarded. multilingual-e5 scores
# cluster high (unrelated legal text often scores ~0.7), so calibrate on known related pairs.
SIMILARITY_FLOOR = 0.83
MODEL_NAME = "intfloat/multilingual-e5-large"
MODEL_PREFIX = "query: "   # e5 models expect this prefix; use "" for other models
# ---------------------------------------------------------------------------------------

DEFAULT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_INPUT = Path("data/new_norms/original_norms.json")
DEFAULT_GRAPH = Path("data/graph.gexf")   # graph.py output; set to the real path
DEFAULT_EMB_CACHE = Path("data/conflict/stageA/norm_embeddings.npz")
DEFAULT_OUTPUT = Path("data/conflict/stageA_pairs.json")

RULE = ("discard pair if (same hierarchy.instrument_id and same hierarchy.article) "
        "or (similarity < similarity_floor and no direct graph link between the articles)")


# ---------------------------------------------------------------------------
# Loading and grouping
# ---------------------------------------------------------------------------


def load_records(path: Path) -> list[dict]:
    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError(f"{path}: expected a JSON array of records, got {type(records).__name__}")
    for i, rec in enumerate(records):
        if not isinstance(rec, dict) or "norm_id" not in rec or not isinstance(rec.get("hierarchy"), dict):
            raise ValueError(f"{path}: record {i} needs 'norm_id' and a 'hierarchy' object")
    return records


def article_key(rec: dict) -> Optional[tuple[str, str]]:
    """(document, article) of a norm, or None when either part is missing."""
    h = rec["hierarchy"]
    instrument, article = h.get("instrument_id"), h.get("article")
    if instrument is None or article is None:
        return None
    return str(instrument), str(article)


def assign_groups(records: list[dict]) -> tuple[list[int], int]:
    """Give each norm an integer id for its (document, article), so the pair loop compares
    plain ints. A norm without a key gets its own unique negative id and therefore never
    equals any other norm. Returns (group ids, number of norms without a key)."""
    ids: list[int] = []
    known: dict[tuple[str, str], int] = {}
    unknown = 0
    for rec in records:
        key = article_key(rec)
        if key is None:
            unknown += 1
            ids.append(-unknown)
        else:
            ids.append(known.setdefault(key, len(known)))
    return ids, unknown


def same_article_stats(records: list[dict]) -> tuple[int, dict]:
    """Same-article pairs in closed form: a group of g norms holds g*(g-1)/2 of them.
    (The scan double-checks this against what it actually enumerates.)"""
    sizes = Counter(k for k in map(article_key, records) if k is not None)
    by_instrument: Counter = Counter()
    for (instrument, _article), g in sizes.items():
        by_instrument[instrument] += g * (g - 1) // 2
    return sum(by_instrument.values()), dict(sorted(by_instrument.items()))


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------


def _save_cache(path: Path, key: str, cached: dict[str, np.ndarray]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    texts = list(cached)
    with open(tmp, "wb") as fh:
        np.savez(fh, key=np.array(key), texts=np.array(texts),
                 vectors=np.stack([cached[t] for t in texts]))
    os.replace(tmp, path)


def embed_norms(records: list[dict], cache_path: Path, model_name: str,
                prefix: str) -> tuple[np.ndarray, np.ndarray]:
    """Return (unit vectors [n, d], has_text mask [n]). Only texts missing from the cache
    are encoded; the model is not even loaded when everything is cached."""
    texts = [norm_text(r) for r in records]
    cache_key = f"{model_name}|{prefix}"
    cached: dict[str, np.ndarray] = {}
    if cache_path.is_file():
        with np.load(cache_path, allow_pickle=False) as z:
            if str(z["key"]) == cache_key:
                cached = {str(t): v for t, v in zip(z["texts"], z["vectors"])}
            else:
                log.info("Embedding cache %s was built with another model; re-encoding.", cache_path)

    missing = sorted({t for t in texts if t and t not in cached})
    if missing:
        log.info("Encoding %d norm text(s) with %s ...", len(missing), model_name)
        encoded = NormSimilarity(model_name=model_name, prefix=prefix).encode_texts(missing)
        cached.update(zip(missing, encoded))
        _save_cache(cache_path, cache_key, cached)

    has_text = np.array([bool(t) for t in texts], dtype=bool)
    if not cached:
        return np.zeros((len(records), 0), dtype=np.float32), has_text
    dim = len(next(iter(cached.values())))
    vectors = np.zeros((len(records), dim), dtype=np.float32)
    for k, t in enumerate(texts):
        if t:
            vectors[k] = cached[t]
    return vectors, has_text


# ---------------------------------------------------------------------------
# Graph links
# ---------------------------------------------------------------------------

# Edge types that count as a link, or None to count every edge in the graph. If graph.py
# also stores structural edges (e.g. article -> its own chapter or instrument), restrict
# this to the citation types, otherwise every article of one act would be "linked".
LINK_EDGE_TYPES: Optional[set[str]] = None
EDGE_TYPE_ATTRS = ("type", "relation", "kind", "label")   # where an edge's type is looked up


def strip_version(uid: str) -> str:
    """'eu:32024R1689:art2@original' -> 'eu:32024R1689:art2'."""
    return uid.split("@", 1)[0]


def norm_graph_uid(rec: dict) -> Optional[str]:
    """The norm's graph node id. Mirrors the adjudication pipeline's _graph_uid: provisions
    from the standard pipeline carry their own uid; the two hand-built Datasets/ sources
    don't, so theirs is rebuilt with graph.py's convention. The version part is dropped
    later anyway, so it doesn't matter which version is written here."""
    h = rec["hierarchy"]
    if h.get("graph_uid"):
        return str(h["graph_uid"])
    instrument, article = h.get("instrument_id"), h.get("article")
    if article is None:
        return None
    if instrument == "BWBR0051796":
        return f"nl:BWBR0051796:art{article}@2025-11-21"
    if instrument == "BWBR0049497":
        return f"nl:BWBR0049497:bijlage35-p{article}@2026-08-15"
    return None


_GEXF_DIRECTIONS = {"directed", "undirected", "mutual"}


def _local(tag: str) -> str:
    """Tag name without its XML namespace (GEXF 1.1, 1.2 and 1.3 use different ones)."""
    return tag.rsplit("}", 1)[-1]


def _read_gexf(path: Path) -> nx.MultiGraph:
    """GEXF reader that keeps edge types. networkx's read_gexf treats an edge's XML ``type``
    attribute as its direction and silently drops it, so edges written with type="cites"
    come back untyped. Here a ``type`` value other than a direction is kept as the edge
    type, alongside the edge ``label`` and any custom edge attributes (by title). Node ids
    are used as-is: nx.write_gexf writes the node itself (the uid) as its id."""
    root = ET.parse(path).getroot()
    titles: dict[str, str] = {}
    for el in root.iter():
        if _local(el.tag) == "attributes" and el.get("class") == "edge":
            for attr in el:
                if _local(attr.tag) == "attribute":
                    titles[attr.get("id")] = attr.get("title") or attr.get("id")

    graph = nx.MultiGraph()
    for el in root.iter():
        tag = _local(el.tag)
        if tag == "node" and el.get("id") is not None:
            graph.add_node(el.get("id"))
        elif tag == "edge":
            data: dict[str, str] = {}
            for value in el.iter():
                if _local(value.tag) == "attvalue":
                    key = value.get("for") or value.get("id")
                    data[titles.get(key, key)] = value.get("value")
            if el.get("label"):
                data["label"] = el.get("label")
            if el.get("type") and el.get("type") not in _GEXF_DIRECTIONS:
                data["type"] = el.get("type")
            graph.add_edge(el.get("source"), el.get("target"), **data)
    return graph


def _read_graph(path: Path) -> nx.Graph:
    suffix = path.suffix.lower()
    if suffix == ".gexf":
        return _read_gexf(path)
    if suffix in (".gpickle", ".pkl", ".pickle"):
        with open(path, "rb") as fh:
            return pickle.load(fh)
    if suffix == ".graphml":
        return nx.read_graphml(path)
    if suffix == ".gml":
        return nx.read_gml(path)
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):                       # plain [uid_a, uid_b] edge list
            g = nx.Graph()
            for k, edge in enumerate(data):
                if not (isinstance(edge, (list, tuple)) and len(edge) >= 2):
                    raise ValueError(f"{path}: edge {k} is not [uid_a, uid_b]")
                g.add_edge(edge[0], edge[1])
            return g
        if isinstance(data, dict) and "nodes" in data:   # networkx node-link JSON
            key = "edges" if "edges" in data else "links"
            try:
                return nx.node_link_graph(data, edges=key)
            except TypeError:                            # networkx < 3.4
                return nx.node_link_graph(data, link=key)
        raise ValueError(f"{path}: JSON is neither node-link data nor a list of edges")
    raise ValueError(f"{path}: unsupported graph format '{suffix}'")


def _edge_type(data: dict) -> Optional[str]:
    for attr in EDGE_TYPE_ATTRS:
        if attr in data:
            return str(data[attr])
    return None


def load_graph(path: Path) -> nx.Graph:
    """Read the citation graph and collapse it to an undirected graph of version-less
    article uids. Only edges whose type is in LINK_EDGE_TYPES are kept (all, if None)."""
    source = _read_graph(path)
    links = nx.Graph()
    links.add_nodes_from(strip_version(str(n)) for n in source.nodes)
    skipped: Counter = Counter()
    for u, v, data in source.edges(data=True):
        etype = _edge_type(data)
        if LINK_EDGE_TYPES is not None and etype not in LINK_EDGE_TYPES:
            skipped[etype] += 1
            continue
        a, b = strip_version(str(u)), strip_version(str(v))
        if a != b:
            links.add_edge(a, b)
    if skipped:
        log.info("Graph: ignored %d edge(s) not in LINK_EDGE_TYPES: %s",
                 sum(skipped.values()), dict(skipped))
    log.info("Graph: %d article nodes, %d links", links.number_of_nodes(), links.number_of_edges())
    return links


def linked_norms(records: list[dict], graph: Optional[nx.Graph]) -> tuple[list[np.ndarray], int, int]:
    """For each norm i, the sorted indices j of norms whose article is directly linked to
    i's article. Returns (links per norm, norms without a uid, norms whose uid is not a
    node in the graph)."""
    nodes: list[Optional[str]] = []
    by_node: dict[str, list[int]] = defaultdict(list)
    for idx, rec in enumerate(records):
        uid = norm_graph_uid(rec)
        node = strip_version(uid) if uid else None
        nodes.append(node)
        if node:
            by_node[node].append(idx)

    links, not_in_graph = [], 0
    for node in nodes:
        js: set[int] = set()
        if node and graph is not None:
            if node in graph:
                js = {j for nb in graph.adj[node] for j in by_node.get(nb, ())}
            else:
                not_in_graph += 1
        links.append(np.array(sorted(js), dtype=np.int64))
    return links, sum(node is None for node in nodes), not_in_graph


# ---------------------------------------------------------------------------
# The filter
# ---------------------------------------------------------------------------

Scan = Iterator[tuple[int, np.ndarray, dict]]


def pair_id(i, j, n: int):
    """Position of pair (i, j), i < j, among all n*(n-1)/2 pairs ordered by i, then j:
    (0,1) -> 0, (0,2) -> 1, ..., (0,n-1) -> n-2, (1,2) -> n-1, ... Works on ints and on
    numpy arrays of j."""
    return i * n - i * (i + 1) // 2 + (j - i - 1)


def scan_pairs(group_ids: list[int], vectors: np.ndarray, has_text: np.ndarray,
               links: list[np.ndarray], floor: float) -> Scan:
    """For each norm i, yield (i, kept partners j > i, counts of what was dropped)."""
    groups = np.asarray(group_ids)
    n = len(groups)
    for i in range(n - 1):
        rest = slice(i + 1, n)
        other_article = groups[rest] != groups[i]

        if has_text[i]:
            low = has_text[rest] & ((vectors[rest] @ vectors[i]) < floor)
        else:
            low = np.zeros(n - i - 1, dtype=bool)

        linked = np.zeros(n - i - 1, dtype=bool)
        later = links[i][links[i] > i]
        linked[later - i - 1] = True

        drop_low = other_article & low & ~linked
        keep = other_article & ~drop_low
        counts = {
            "discarded_same_article": int((~other_article).sum()),
            "discarded_low_similarity": int(drop_low.sum()),
            "rescued_by_graph_link": int((other_article & low & linked).sum()),
        }
        yield i, np.flatnonzero(keep) + i + 1, counts


def count_pairs(make_scan: Callable[[], Scan]) -> Counter:
    """First pass: count without writing, so the header can carry exact stats."""
    totals: Counter = Counter()
    for _i, partners, counts in make_scan():
        totals.update(counts)
        totals["kept"] += len(partners)
    return totals


def write_pairs(path: Path, header: dict, make_scan: Callable[[], Scan]) -> int:
    """Stream the header and every surviving pair to ``path`` (temp file + fsync +
    os.replace, so a killed run never leaves a half-written file). Returns pairs written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    written = 0
    n = header["n_norms"]
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            head = json.dumps(header, ensure_ascii=False)
            fh.write(head[:-1] + ',"pairs":[')   # reopen the object to append the pair list
            first = True
            for i, partners, _counts in make_scan():
                if not len(partners):
                    continue
                ids = pair_id(i, partners, n).tolist()
                chunk = ",".join(f"[{p},{i},{j}]" for p, j in zip(ids, partners.tolist()))
                fh.write(chunk if first else "," + chunk)
                first = False
                written += len(partners)
            fh.write("]}")
            fh.flush()
            os.fsync(fh.fileno())
        if written != header["stats"]["kept"]:
            raise RuntimeError(f"Enumerated {written} pairs but expected {header['stats']['kept']}; "
                               f"refusing to publish an inconsistent file.")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="project root (default: %(default)s)")
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT,
                    help="merged norms file, relative to --root (default: %(default)s)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUTPUT,
                    help="pairs file, relative to --root (default: %(default)s)")
    ap.add_argument("--graph", type=Path, default=DEFAULT_GRAPH,
                    help="graph edge file, relative to --root (default: %(default)s)")
    ap.add_argument("--no-graph", action="store_true",
                    help="run without graph links (the similarity rule then acts alone)")
    ap.add_argument("--emb-cache", type=Path, default=DEFAULT_EMB_CACHE,
                    help="embedding cache, relative to --root (default: %(default)s)")
    ap.add_argument("--sim-floor", type=float, default=SIMILARITY_FLOOR,
                    help="similarity floor (default: SIMILARITY_FLOOR = %(default)s)")
    ap.add_argument("--model", default=MODEL_NAME, help="embedding model (default: %(default)s)")
    ap.add_argument("--prefix", default=MODEL_PREFIX, help="text prefix for the model (default: %(default)r)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    def resolve(p: Path) -> Path:
        return p if p.is_absolute() else args.root / p

    in_path, out_path = resolve(args.input), resolve(args.out)
    graph_path = None if args.no_graph else resolve(args.graph)

    if not in_path.is_file():
        print(f"ERROR: input not found: {in_path}", file=sys.stderr)
        return 1
    if graph_path is not None and not graph_path.is_file():
        print(f"ERROR: graph not found: {graph_path} (pass --no-graph to run without links)",
              file=sys.stderr)
        return 1
    try:
        records = load_records(in_path)
        graph = load_graph(graph_path) if graph_path else None
    except (ValueError, nx.NetworkXError, ET.ParseError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    norm_ids = [r["norm_id"] for r in records]
    dupes = len(norm_ids) - len(set(norm_ids))
    if dupes:
        log.warning("%d duplicate norm_id values in the input; pairs are still keyed by position, "
                    "but run merge_norms.py to find out why.", dupes)

    group_ids, unknown = assign_groups(records)
    if unknown:
        log.warning("%d norm(s) lack instrument_id or article; the same-article rule never "
                    "discards them.", unknown)

    vectors, has_text = embed_norms(records, resolve(args.emb_cache), args.model, args.prefix)
    no_text = int((~has_text).sum())
    if no_text:
        log.warning("%d norm(s) have no action/trigger text; the similarity rule never discards "
                    "their pairs.", no_text)

    links, no_uid, not_in_graph = linked_norms(records, graph)
    if graph is not None:
        if no_uid:
            log.warning("%d norm(s) have no graph uid (and none could be rebuilt); they can "
                        "have no graph links.", no_uid)
        if not_in_graph:
            log.warning("%d norm(s) have a graph uid that is not a node in the graph; check for "
                        "a uid convention mismatch.", not_in_graph)

    def make_scan() -> Scan:
        return scan_pairs(group_ids, vectors, has_text, links, args.sim_floor)

    totals = count_pairs(make_scan)
    expected_same, by_instrument = same_article_stats(records)
    if totals["discarded_same_article"] != expected_same:
        raise RuntimeError(f"Scan found {totals['discarded_same_article']} same-article pairs, "
                           f"closed form says {expected_same}.")

    n = len(records)
    stats = {
        "total_pairs": n * (n - 1) // 2,
        "discarded_same_article": totals["discarded_same_article"],
        "discarded_low_similarity": totals["discarded_low_similarity"],
        "rescued_by_graph_link": totals["rescued_by_graph_link"],
        "kept": totals["kept"],
        "discarded_by_instrument": by_instrument,
        "norms_without_article_key": unknown,
        "norms_without_text": no_text,
        "norms_without_graph_uid": no_uid,
        "norms_not_in_graph": not_in_graph,
    }
    header = {"stage": "A", "rule": RULE, "input": str(in_path),
              "graph": str(graph_path) if graph_path else None, "model": args.model,
              "similarity_floor": args.sim_floor, "n_norms": n, "stats": stats,
              "norm_ids": norm_ids, "pair_format": ["pair_id", "i", "j"]}
    write_pairs(out_path, header, make_scan)

    print(f"Read {n} norms from {in_path}")
    print(f"  all pairs:                    {stats['total_pairs']:>12,}")
    print(f"  discarded (same doc+article): {stats['discarded_same_article']:>12,}")
    print(f"  discarded (low sim, no link): {stats['discarded_low_similarity']:>12,}"
          f"   floor = {args.sim_floor}")
    print(f"  rescued by graph link:        {stats['rescued_by_graph_link']:>12,}")
    print(f"  kept:                         {stats['kept']:>12,}")
    for inst, k in by_instrument.items():
        print(f"    same-article discards in {inst:<14} {k:>12,}")
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())