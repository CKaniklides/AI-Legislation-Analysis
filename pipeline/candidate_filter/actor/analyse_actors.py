#!/usr/bin/env python3
"""
Exploratory analysis of norm addressees (actors).

Answers the questions from the "Checking the data first" paragraph:
  * how many distinct actors are there after cleaning, and how concentrated are they
    (top-k coverage of norms)?
  * which actors correspond to a term defined in the definitions article of the
    instrument (Art. 3 AI Act, Art. 4 GDPR)?
  * how many norms have no explicit actor (passive constructions etc.) or only a
    pronoun as actor?

Usage
-----
    python analyse_actors.py data/original_norms.json \
        --definitions "data/definitions/*_definitions.json" \
        --out out/actor_analysis --top 40

Outputs (in --out):
    actors_raw.csv        one row per raw actor string
    actors_canonical.csv  one row per canonical role (cleaned head / defined term)
    actors_missing.csv    norms without an explicit actor or with a pronoun actor
    summary.txt           the console report
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Filename stem -> CELEX. Extend when you add instruments.
INSTRUMENT_ALIASES = {
    "ai_act": "32024R1689", "aiact": "32024R1689", "ai-act": "32024R1689",
    "aia": "32024R1689", "ai": "32024R1689",
    "gdpr": "32016R0679", "avg": "32016R0679",
}

# Leading words removed during cleaning (articles, quantifiers, demonstratives,
# possessives). "betrokken" is deliberately NOT here: in the GDPR
# "betrokken toezichthoudende autoriteit" is itself a defined term.
DETERMINERS = {
    "de", "het", "een", "elke", "elk", "ieder", "iedere", "alle", "al",
    "deze", "die", "dit", "dat", "dergelijke", "zo'n", "zo’n",
    "hun", "zijn", "haar", "diens", "wiens", "welke",
}

# Actors that are only a pronoun: resolvable only from context.
PRONOUNS = {"zij", "ze", "hij", "het", "men", "u", "wij", "deze", "die",
            "dit", "dat", "zich", "laatstgenoemde", "eerstgenoemde"}

# Words that start a post-modifier; the head noun is the last word before them.
POSTMOD_MARKERS = {
    "van", "voor", "met", "die", "dat", "in", "op", "uit", "bij", "onder",
    "als", "zoals", "welke", "waarop", "waarvan", "aan", "tot", "binnen",
    "krachtens", "overeenkomstig", "inzake", "namens", "door", "om",
}
COORDINATORS = {"en", "of", "en/of"}

# Heuristic hint: does the head noun look like an agent / institution?
# Used only to flag likely extraction errors (e.g. actor = "De datasets").
AGENTIVE_HEADS = {
    "autoriteit", "instantie", "lidstaat", "commissie", "comité", "bureau",
    "ai-bureau", "board", "ai-board", "raad", "forum", "adviesforum", "panel",
    "secretariaat", "functionaris", "toezichthouder", "persoon", "organisatie",
    "instelling", "orgaan", "dienst", "deskundige", "vertegenwoordiger",
    "voorzitter", "covoorzitter", "partij", "derde", "betrokkene", "kmo",
    "start-up", "onderneming", "dochteronderneming", "groep", "personeel",
    "personeelslid", "subgroep", "deelnemer", "belanghebbende", "lid",
    "parlement", "ombudsman", "hof", "rechter", "gerecht", "overheid",
    "overheidsinstantie", "operator", "gemachtigde", "vertegenwoordiging",
    "vereniging", "fabrikant", "werkgever", "werknemer",
}

# Irregular Dutch plurals the rules get wrong.
IRREGULAR_PLURALS = {"leden": "lid", "personeelsleden": "personeelslid",
                     "steden": "stad", "schepen": "schip"}

# Seed of curated multi-word roles that are NOT in a definitions article but should
# not be cut at their preposition. Treated like defined terms with instrument
# "curated". This is the start of the curated actor list.
CURATED_ROLES = [
    "functionaris voor gegevensbescherming",
    "europese toezichthouder voor gegevensbescherming",
    "leidende toezichthoudende autoriteit",
]
AGENTIVE_SUFFIXES = ("er", "eur", "aar", "ent", "ant", "ige", "lijke", "ist",
                     "or", "iteit", "stantie", "staat", "teur", "ier")
NON_AGENT_SUFFIXES = ("register", "filter", "parameter", "nummer", "systeem", "systemen", "model", "verklaring",
                      "handeling", "gegevens", "informatie", "code", "plan")

PASSIVE_RE = re.compile(r"\b(wordt|worden|werd|werden)\b", re.I)


# --------------------------------------------------------------------------- #
# Definitions loading
# --------------------------------------------------------------------------- #

TERM_KEYS = ("term", "defined_term", "term_nl", "lemma", "name", "label", "definiendum")
INSTR_KEYS = ("instrument_id", "celex", "instrument", "source_instrument")


def _instrument_from_path(path: Path) -> str:
    stem = path.stem.lower().replace("_definitions", "").replace("-definitions", "")
    m = re.search(r"3\d{4}[A-Z]\d{4}", path.stem, re.I)
    if m:
        return m.group(0).upper()
    return INSTRUMENT_ALIASES.get(stem, stem.upper())


def _extract_terms(obj) -> list[tuple[str, str | None]]:
    """Return [(term, instrument_or_None)] from any reasonable JSON layout."""
    out: list[tuple[str, str | None]] = []
    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, str):
                out.append((item, None))
            elif isinstance(item, dict):
                term = next((item[k] for k in TERM_KEYS if item.get(k)), None)
                instr = next((item[k] for k in INSTR_KEYS if item.get(k)), None)
                if term:
                    out.append((str(term), instr))
                    # synonyms / abbreviations, if the parser stores them
                    for k in ("aliases", "synonyms", "abbreviation", "short_form"):
                        v = item.get(k)
                        for a in (v if isinstance(v, list) else [v] if v else []):
                            out.append((str(a), instr))
                else:  # nested container
                    out.extend(_extract_terms(list(item.values())))
    elif isinstance(obj, dict):
        for k in ("definitions", "terms", "items", "data"):
            if k in obj:
                instr = next((obj[x] for x in INSTR_KEYS if obj.get(x)), None)
                return [(t, i or instr) for t, i in _extract_terms(obj[k])]
        # plain mapping term -> definition
        if all(isinstance(v, (str, dict)) for v in obj.values()):
            out.extend((k, None) for k in obj.keys())
    return out


def load_definitions(pattern: str) -> dict[str, dict[str, str]]:
    """{instrument_id: {normalised_term: original_term}}"""
    defs: dict[str, dict[str, str]] = defaultdict(dict)
    files = sorted(glob.glob(pattern))
    if not files:
        print(f"WARNING: no definition files match {pattern!r}", file=sys.stderr)
    for f in files:
        path = Path(f)
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        file_instr = _instrument_from_path(path)
        n = 0
        for term, instr in _extract_terms(data):
            norm = normalise_term(term)
            if norm:
                defs[instr or file_instr][norm] = term
                n += 1
        print(f"Loaded {n:4d} defined terms from {path.name} -> {file_instr}",
              file=sys.stderr)
    return dict(defs)


# --------------------------------------------------------------------------- #
# Cleaning and normalisation
# --------------------------------------------------------------------------- #

_PUNCT_EDGES = " \t\n\"'“”‘’«»„:;.,()[]"


def basic_clean(s: str) -> str:
    s = s.replace("\u00a0", " ").lower()
    s = re.sub(r"\([^)]*\)", " ", s)                 # drop bracketed references
    s = re.sub(r"\s+", " ", s).strip(_PUNCT_EDGES)
    return s


def strip_determiners(s: str) -> str:
    words = s.split()
    while len(words) > 1 and words[0] in DETERMINERS:
        words = words[1:]
    return " ".join(words)


def normalise_term(term: str) -> str:
    return strip_determiners(basic_clean(term))


class Singulariser:
    """Vocabulary-aware Dutch singularisation.

    Candidate singulars are generated by simple rules; a candidate that occurs in the
    vocabulary of defined terms wins. Otherwise a conservative default is used.
    """

    def __init__(self, vocab: set[str]):
        self.vocab = vocab

    @staticmethod
    def candidates(w: str) -> list[str]:
        c = [w]
        if re.search(r"['’]s$", w):                       # kmo’s
            c.append(w[:-2])
        if w.endswith("s") and len(w) > 3:                # aanbieders, instanties
            c.append(w[:-1])
        if w.endswith("en") and len(w) > 4:
            stem = w[:-2]
            c.append(w[:-1])                              # verantwoordelijken -> -e
            # vowel doubling: lidstaten -> lidstaat, organen -> orgaan
            if len(stem) >= 3 and stem[-2] in "aeou" and stem[-3] not in "aeiou":
                c.append(stem[:-1] + stem[-2] + stem[-1])
            c.append(stem)                                # autoriteiten -> autoriteit
            if len(stem) >= 2 and stem[-1] == stem[-2]:   # bedrijven-ish degemination
                c.append(stem[:-1])
            if stem.endswith("v"):
                c.append(stem[:-1] + "f")
            if stem.endswith("z"):
                c.append(stem[:-1] + "s")
        return c

    def __call__(self, w: str, default_strip: bool = True) -> str:
        if w in IRREGULAR_PLURALS:
            return IRREGULAR_PLURALS[w]
        cands = self.candidates(w)
        for c in cands:
            if c in self.vocab:
                return c
        if not default_strip or len(cands) == 1:
            return w
        # conservative defaults for heads not in the vocabulary
        if re.search(r"['’]s$", w):
            return w[:-2]
        if w.endswith(("ers", "eurs", "ies", "ors", "aars", "ants", "ents")):
            return w[:-1]
        if w.endswith("lijken") or w.endswith("igen"):
            return w[:-1]
        if w.endswith("en") and len(w) > 5:
            stem = w[:-2]
            if stem[-2] in "aeou" and stem[-3] not in "aeiou":
                return stem[:-1] + stem[-2] + stem[-1]
            return stem
        return w


def split_conjuncts(s: str) -> list[str]:
    """Split coordinated heads, distributing a shared post-modifier.

    'aanbieders en gebruiksverantwoordelijken van ai-systemen'
        -> ['aanbieders van ai-systemen', 'gebruiksverantwoordelijken van ai-systemen']
    Coordination *inside* the post-modifier is left alone.
    """
    s = re.sub(r",?\s+in voorkomend geval,?\s+", " ", s)  # "of, in voorkomend geval, de"
    words = s.replace(",", " , ").split()
    cut = next((i for i, w in enumerate(words) if i > 0 and w in POSTMOD_MARKERS), len(words))
    pre, post = words[:cut], words[cut:]
    parts, cur = [], []
    for w in pre:
        if w in COORDINATORS or w == ",":
            if cur:
                parts.append(cur)
            cur = []
        else:
            cur.append(w)
    if cur:
        parts.append(cur)
    post_s = " ".join(post).replace(" , ", ", ")
    out = []
    for p in parts:
        txt = " ".join(p + ([post_s] if post_s else []))
        txt = strip_determiners(txt.strip(_PUNCT_EDGES))
        if txt:
            out.append(txt)
    return out or [s]


def head_and_premod(np: str) -> tuple[str, list[str]]:
    words = np.split()
    cut = next((i for i, w in enumerate(words) if i > 0 and w in POSTMOD_MARKERS), len(words))
    pre = words[:cut]
    return pre[-1], pre[:-1]


def looks_agentive(head: str) -> bool:
    if head in AGENTIVE_HEADS:
        return True
    if any(head.endswith(s) for s in NON_AGENT_SUFFIXES):
        return False
    return head.endswith(AGENTIVE_SUFFIXES) or any(head.endswith(h) for h in AGENTIVE_HEADS)


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #

@dataclass
class Component:
    cleaned: str                 # cleaned conjunct (singularised pre-modifier part)
    head: str                    # singularised head noun
    canonical: str               # defined term if matched, else head
    match: str                   # exact | prefix | suffix | head | none
    defined_in: list[str] = field(default_factory=list)
    agentive: bool = True


@dataclass
class ActorAnalysis:
    raw: str | None
    kind: str                    # explicit | missing | pronoun
    components: list[Component] = field(default_factory=list)


class Matcher:
    def __init__(self, defs: dict[str, dict[str, str]]):
        self.defs = defs
        self.term_to_instr: dict[str, set[str]] = defaultdict(set)
        vocab: set[str] = set()
        for instr, terms in defs.items():
            for t in terms:
                self.term_to_instr[t].add(instr)
                vocab.update(t.split())
                vocab.add(t)
        self.sing = Singulariser(vocab)
        # longest first so that "betrokken toezichthoudende autoriteit" beats
        # "toezichthoudende autoriteit"
        self.terms_by_len = sorted(self.term_to_instr, key=lambda t: -len(t))

    def _norm_np(self, np: str) -> str:
        words = np.split()
        cut = next((i for i, w in enumerate(words) if i > 0 and w in POSTMOD_MARKERS),
                   len(words))
        pre = [self.sing(w, default_strip=(i == cut - 1)) for i, w in enumerate(words[:cut])]
        return " ".join(pre + words[cut:])

    def match_component(self, conj: str) -> Component:
        np = self._norm_np(conj)
        head, premod = head_and_premod(np)
        pre_part = " ".join(premod + [head])

        # 1. whole cleaned conjunct is a defined term
        if np in self.term_to_instr:
            return Component(np, head, np, "exact", sorted(self.term_to_instr[np]),
                             looks_agentive(head))
        # 2. conjunct starts with a defined term (term + post-modifier)
        for t in self.terms_by_len:
            if np.startswith(t + " ") and len(t.split()) >= len(pre_part.split()):
                return Component(np, head, t, "prefix", sorted(self.term_to_instr[t]),
                                 looks_agentive(head))
        # 3. pre-modified head ends with a defined term ("leidende toezichthoudende autoriteit")
        for t in self.terms_by_len:
            if pre_part == t or pre_part.endswith(" " + t):
                return Component(np, head, t, "suffix", sorted(self.term_to_instr[t]),
                                 looks_agentive(head))
        # 4. fallback: head noun
        return Component(np, head, head, "head", [], looks_agentive(head))

    def analyse(self, raw: str | None) -> ActorAnalysis:
        if raw is None or not str(raw).strip():
            return ActorAnalysis(raw, "missing")
        cleaned = basic_clean(str(raw))
        if cleaned in PRONOUNS or strip_determiners(cleaned) in PRONOUNS:
            return ActorAnalysis(raw, "pronoun")
        cleaned = strip_determiners(cleaned)
        comps = [self.match_component(c) for c in split_conjuncts(cleaned)]
        return ActorAnalysis(raw, "explicit", comps)


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

def coverage(counter: Counter, total: int, ks=(5, 10, 20, 30, 50)) -> dict[int, float]:
    ranked = [c for _, c in counter.most_common()]
    return {k: (sum(ranked[:k]) / total * 100 if total else 0.0) for k in ks}


def norm_level_coverage(norm_roles: list[set[str]], counter: Counter, ks=(5, 10, 20, 30, 50)):
    """% of norms whose roles are ALL within the top-k roles."""
    ranked = [r for r, _ in counter.most_common()]
    res = {}
    for k in ks:
        top = set(ranked[:k])
        res[k] = (sum(1 for rs in norm_roles if rs <= top) / len(norm_roles) * 100
                  if norm_roles else 0.0)
    return res


def defined_status(comp: Component, instr: str) -> str:
    if not comp.defined_in:
        return "not defined"
    if instr in comp.defined_in:
        return "defined (own instrument)"
    if comp.defined_in == ["curated"]:
        return "curated (not in definitions)"
    return "defined (other instrument: " + ",".join(comp.defined_in) + ")"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("norms", nargs="?", default="data/new_norms/original_norms.json")   # where the file lives since 18 Oct 2026
    ap.add_argument("--definitions", default="data/definitions/*_definitions.json")
    ap.add_argument("--out", default="out/actor_analysis")
    ap.add_argument("--top", type=int, default=40, help="rows to print per table")
    ap.add_argument("--no-curated", action="store_true",
                    help="do not add the CURATED_ROLES seed list")
    args = ap.parse_args(argv)

    with open(args.norms, encoding="utf-8") as fh:
        norms = json.load(fh)
    defs = load_definitions(args.definitions)
    if not args.no_curated:
        defs["curated"] = {normalise_term(t): t for t in CURATED_ROLES}
    matcher = Matcher(defs)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    p = lines.append

    raw_counter: Counter = Counter()
    raw_info: dict[str, dict] = {}
    canon_counter: Counter = Counter()          # counted per norm (not per component)
    canon_info: dict[tuple[str, str], dict] = {}
    kind_counter: Counter = Counter()
    norm_roles: list[set[str]] = []
    missing_rows = []
    per_instr_kind: dict[str, Counter] = defaultdict(Counter)

    for rec in norms:
        n = rec.get("norm", rec)
        raw = n.get("actor")
        instr = rec.get("hierarchy", {}).get("instrument_id", "?")
        a = matcher.analyse(raw)
        kind_counter[a.kind] += 1
        per_instr_kind[instr][a.kind] += 1

        if a.kind != "explicit":
            text = rec.get("text", {}).get("paragraph", "") or ""
            missing_rows.append({
                "norm_id": rec.get("norm_id"), "instrument": instr,
                "article": rec.get("hierarchy", {}).get("article"),
                "kind": a.kind, "actor_raw": raw or "",
                "deontic": n.get("deontic"),
                "passive_marker": bool(PASSIVE_RE.search(text)),
                "text": text[:300],
            })
            continue

        key = raw.strip()
        raw_counter[key] += 1
        if key not in raw_info:
            raw_info[key] = {"instr": Counter(), "analysis": a}
        raw_info[key]["instr"][instr] += 1

        roles = set()
        for c in a.components:
            roles.add(c.canonical)
            info = canon_info.setdefault(c.canonical, {
                "defined_in": set(c.defined_in), "matches": Counter(),
                "variants": Counter(), "instr": Counter(), "agentive": c.agentive})
            info["matches"][c.match] += 1
            info["variants"][key] += 1
            info["instr"][instr] += 1
        for r in roles:
            canon_counter[r] += 1
        norm_roles.append(roles)

    total = len(norms)
    explicit = kind_counter["explicit"]

    # ---------------- summary
    p("=" * 78)
    p("ACTOR ANALYSIS")
    p("=" * 78)
    p(f"Norms total:                      {total}")
    p(f"  explicit actor:                 {explicit} ({explicit/total*100:.1f}%)")
    p(f"  no actor (null):                {kind_counter['missing']} "
      f"({kind_counter['missing']/total*100:.1f}%)")
    p(f"  pronoun only (zij/hij/het/...): {kind_counter['pronoun']} "
      f"({kind_counter['pronoun']/total*100:.1f}%)")
    p("")
    for instr, kc in sorted(per_instr_kind.items()):
        t = sum(kc.values())
        p(f"  {instr}: {t} norms | explicit {kc['explicit']} | null {kc['missing']} "
          f"| pronoun {kc['pronoun']}")
    p("")
    p(f"Distinct raw actor strings:       {len(raw_counter)}")
    p(f"Distinct canonical roles:         {len(canon_counter)}")
    n_multi = sum(1 for r in norm_roles if len(r) > 1)
    p(f"Norms with coordinated actors:    {n_multi} (e.g. 'X en Y', 'X of Y')")
    defined_roles = [r for r in canon_counter if canon_info[r]["defined_in"]]
    p(f"Canonical roles that are defined terms: {len(defined_roles)} "
      f"covering {sum(canon_counter[r] for r in defined_roles)} norm-role occurrences")
    nonagent = [r for r in canon_counter if not canon_info[r]["agentive"]]
    p(f"Roles whose head does NOT look agentive (likely object/subject confusion): "
      f"{len(nonagent)} roles, {sum(canon_counter[r] for r in nonagent)} norms")

    # ---------------- coverage
    p("")
    p("-" * 78)
    p("CONCENTRATION (share of norms with an explicit actor)")
    p("-" * 78)
    cov_raw = coverage(raw_counter, explicit)
    cov_can = norm_level_coverage(norm_roles, canon_counter)
    p(f"{'top-k':>6} | {'raw strings':>12} | {'canonical roles (all roles of norm in top-k)':>45}")
    for k in cov_raw:
        p(f"{k:>6} | {cov_raw[k]:>11.1f}% | {cov_can[k]:>44.1f}%")
    k90 = next((k for k in range(1, len(canon_counter) + 1)
                if norm_level_coverage(norm_roles, canon_counter, ks=(k,))[k] >= 90), None)
    p(f"Canonical roles needed for 90% coverage: {k90}")

    # ---------------- canonical table
    p("")
    p("-" * 78)
    p(f"TOP {args.top} CANONICAL ROLES")
    p("-" * 78)
    p(f"{'#':>3} {'norms':>5}  {'role':<42} {'defined in':<22} {'match':<16} agent? variants")
    canon_rows = []
    for i, (role, cnt) in enumerate(canon_counter.most_common(), 1):
        info = canon_info[role]
        dfi = ",".join(sorted(info["defined_in"])) or "-"
        m = ",".join(f"{k}:{v}" for k, v in info["matches"].most_common())
        row = {
            "rank": i, "role": role, "norms": cnt,
            "pct_explicit": round(cnt / explicit * 100, 2),
            "defined_in": dfi,
            "norm_instruments": ";".join(f"{k}:{v}" for k, v in info["instr"].items()),
            "match_types": m,
            "head_looks_agentive": info["agentive"],
            "n_variants": len(info["variants"]),
            "variants": " | ".join(v for v, _ in info["variants"].most_common(10)),
        }
        canon_rows.append(row)
        if i <= args.top:
            p(f"{i:>3} {cnt:>5}  {role[:42]:<42} {dfi[:22]:<22} {m[:16]:<16} "
              f"{'yes' if info['agentive'] else 'NO ':<6} {len(info['variants'])}")

    # ---------------- raw table
    p("")
    p("-" * 78)
    p(f"TOP {args.top} RAW ACTOR STRINGS")
    p("-" * 78)
    p(f"{'norms':>5}  {'raw actor':<45} -> canonical [definition status]")
    raw_rows = []
    for i, (raw, cnt) in enumerate(raw_counter.most_common(), 1):
        info = raw_info[raw]
        instr = info["instr"].most_common(1)[0][0]
        comps = info["analysis"].components
        status = [defined_status(c, instr) for c in comps]
        raw_rows.append({
            "actor_raw": raw, "norms": cnt,
            "instruments": ";".join(f"{k}:{v}" for k, v in info["instr"].items()),
            "cleaned": " + ".join(c.cleaned for c in comps),
            "canonical": " + ".join(c.canonical for c in comps),
            "match_types": " + ".join(c.match for c in comps),
            "definition_status": " + ".join(status),
            "head_looks_agentive": all(c.agentive for c in comps),
        })
        if i <= args.top:
            canon = " + ".join(c.canonical for c in comps)
            short = "; ".join(s.replace("defined ", "") for s in status)
            p(f"{cnt:>5}  {raw[:45]:<45} -> {canon} [{short}]")

    # ---------------- defined terms never used as actor (per instrument)
    p("")
    p("-" * 78)
    p("DEFINED TERMS USED AS ACTOR, PER INSTRUMENT")
    p("-" * 78)
    for instr, terms in sorted(defs.items()):
        used = [t for t in terms if t in canon_counter]
        p(f"{instr}: {len(used)}/{len(terms)} defined terms occur as actor role: "
          + ", ".join(sorted(used, key=lambda t: -canon_counter[t])))

    # ---------------- missing actors
    p("")
    p("-" * 78)
    p("NORMS WITHOUT EXPLICIT ACTOR")
    p("-" * 78)
    miss = [r for r in missing_rows if r["kind"] == "missing"]
    pron = [r for r in missing_rows if r["kind"] == "pronoun"]
    p(f"null actor: {len(miss)}, of which with passive marker (wordt/worden): "
      f"{sum(r['passive_marker'] for r in miss)}")
    p("  by deontic: " + ", ".join(f"{k}:{v}" for k, v in
                                   Counter(r["deontic"] for r in miss).most_common()))
    p(f"pronoun actor: {len(pron)}  ("
      + ", ".join(f"{k}:{v}" for k, v in Counter(r["actor_raw"].lower() for r in pron)
                  .most_common()) + ")")
    p("  examples (null):")
    for r in miss[:5]:
        p(f"   - [{r['norm_id']}] {r['text'][:110]}...")

    report = "\n".join(lines)
    print(report)
    (out / "summary.txt").write_text(report, encoding="utf-8")
    _write_csv(out / "actors_canonical.csv", canon_rows)
    _write_csv(out / "actors_raw.csv", raw_rows)
    _write_csv(out / "actors_missing.csv", missing_rows)
    print(f"\nCSV files written to {out}/", file=sys.stderr)


def _write_csv(path: Path, rows: list[dict]):
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    main()