#!/usr/bin/env python3
"""
Convert the DAPRECO knowledge base (LegalRuleML / RuleML XML, e.g. rioKB_GDPR.xml)
into structured norm records that mirror DAPRECO's own structure.

Each formula (lrml:ConstitutiveStatement) becomes one norm record containing:

  - identity:      id, block (parent lrml:Statements)
  - classification: statement_type + raw lrml:Context type
  - provenance:    the GDPR provisions linked via lrml:Association
  - logic:         the if/then parts as a typed formula tree
                   (exists / and / naf / atom, with typed terms), the
                   quantifier of every variable, and reification info
                   taken from the rioOnto:reification Association
  - text:          a readable one-string rendering of the formula
  - derived:       convenience fields computed from the logic
                   (deontic twin, bearer, exceptions, temporal constraints,
                   constants). These are interpretations, kept separate
                   from the faithful encoding.

Usage:
    python dapreco_to_norms.py rioKB_GDPR.xml -o dapreco_structured.json
    python dapreco_to_norms.py rioKB_GDPR.xml -o out.json --jsonl out.jsonl

Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

LRML = "{http://docs.oasis-open.org/legalruleml/ns/v1.0/}"
RULEML = "{http://ruleml.org/spec}"

# lrml:Context types -> short statement types
CONTEXT_TYPES = {
    "rioOnto:obligationRule": "obligation",
    "rioOnto:permissionRule": "permission",
    "rioOnto:constitutiveRule": "constitutive",
    "rioOnto:constitutiveRule4MappingWithPrOnto": "constitutive_mapping",
}

# Predicates that express the deontic status inside constitutive "twins"
DEONTIC_PREDICATES = {"obligation": "rioOnto:Obliged", "permission": "rioOnto:Permitted"}
EXISTENCE_PREDICATE = "rioOnto:RexistAtTime"

REF_PATTERN = re.compile(
    r"art_(?P<article>\d+)(?:__para_(?P<paragraph>\d+))?"
    r"(?:.*?__point_(?P<point>[a-z]+))?$"
)
EXCEPTION_PART = re.compile(r"(Cha|Sec|Art|Par|Point)(\d+|[A-Za-z](?=[A-Z]|$))")


def norm_id(ref: str) -> str:
    """Atom ids appear as '#A1' in Associations and ':A1' on atoms."""
    return ref.lstrip("#").lstrip(":")


def local(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


# --------------------------------------------------------------------------
# Document-level metadata
# --------------------------------------------------------------------------

def parse_legal_reference(local_id: str, ref_id: str) -> dict:
    ref = {"local_id": local_id, "ref_id": ref_id,
           "article": None, "paragraph": None, "point": None}
    m = REF_PATTERN.search(ref_id)
    if m:
        ref["article"] = int(m.group("article"))
        if m.group("paragraph"):
            ref["paragraph"] = int(m.group("paragraph"))
        ref["point"] = m.group("point")
    return ref


def read_metadata(root: ET.Element) -> dict:
    prefixes = {p.get("pre"): p.get("refID") for p in root.findall(LRML + "Prefix")}

    legal_refs = {}
    for lr in root.iter(LRML + "LegalReference"):
        legal_refs[lr.get("refersTo")] = parse_legal_reference(lr.get("refersTo"), lr.get("refID"))

    block_refs = defaultdict(list)   # statements block key -> [legal reference]
    reified_atom_ids = set()         # atom keyrefs listed under rioOnto:reification
    for assoc in root.iter(LRML + "Association"):
        targets = [t.get("keyref", "").lstrip("#") for t in assoc.findall(LRML + "toTarget")]
        source = assoc.find(LRML + "appliesSource")
        modality = assoc.find(LRML + "appliesModality")
        if source is not None:
            ref = legal_refs.get(source.get("keyref", "").lstrip("#"))
            for t in targets:
                if ref:
                    block_refs[t].append(ref)
        elif modality is not None and modality.get("iri", "").endswith("rioOnto:reification"):
            reified_atom_ids.update(norm_id(t) for t in targets)

    statement_context = {}           # formula key -> (context key, context type)
    contexts = {}
    for ctx in root.findall(LRML + "Context"):
        contexts[ctx.get("key")] = ctx.get("type")
        for scope in ctx.findall(LRML + "inScope"):
            statement_context[scope.get("keyref", "").lstrip("#")] = (ctx.get("key"), ctx.get("type"))

    return {
        "prefixes": prefixes,
        "legal_references": legal_refs,
        "block_refs": block_refs,
        "reified_atom_ids": reified_atom_ids,
        "contexts": contexts,
        "statement_context": statement_context,
    }


# --------------------------------------------------------------------------
# Formula parsing: RuleML XML -> typed tree
# --------------------------------------------------------------------------

class FormulaParser:
    """Parses one ruleml:Rule. Collects variable declarations and warnings."""

    def __init__(self, reified_atom_ids: set):
        self.reified_atom_ids = reified_atom_ids
        self.existential = {"if": [], "then": []}
        self.all_vars = []
        self.warnings = []
        self._side = None

    def _note_var(self, name: str):
        if name and name not in self.all_vars:
            self.all_vars.append(name)

    # ---- terms -----------------------------------------------------------
    def term(self, el: ET.Element) -> dict:
        tag = local(el.tag)
        if tag == "Var":
            name = el.get("key") or el.get("keyref")
            self._note_var(name)
            return {"var": name}
        if tag == "Ind":
            return {"ind": (el.text or "").strip()}
        if tag == "Const":
            return {"const": el.get("iri") or (el.text or "").strip()}
        if tag == "Expr":
            fun, args = None, []
            for child in el:
                if local(child.tag) == "Fun":
                    fun = child.get("iri")
                    # Irregular nesting: arguments placed inside <Fun> itself
                    if len(child):
                        self.warnings.append(f"Fun {fun}: arguments nested inside <Fun>; treated as function arguments")
                        args.extend(self.term(c) for c in child)
                else:
                    args.append(self.term(child))
            return {"fun": fun, "args": args}
        if tag == "Fun":  # bare function element used as a term
            return {"fun": el.get("iri"), "args": [self.term(c) for c in el]}
        self.warnings.append(f"Unknown term element <{tag}>")
        return {"unknown": tag}

    def arg_terms(self, elements: list) -> list:
        """Atom arguments. A <Rel> in argument position is a flattened function
        application: (P :a Rel :b) means P(:a, Rel(:b)); a trailing <Rel> is a constant."""
        args, i = [], 0
        while i < len(elements):
            el = elements[i]
            if local(el.tag) == "Rel":
                iri = el.get("iri")
                nxt = elements[i + 1] if i + 1 < len(elements) else None
                if nxt is not None and local(nxt.tag) == "Var":
                    self.warnings.append(f"<Rel {iri}> used as a term; read as function {iri}(next argument)")
                    args.append({"fun": iri, "args": [self.term(nxt)]})
                    i += 2
                    continue
                self.warnings.append(f"<Rel {iri}> used as a term; read as constant")
                args.append({"const": iri})
            elif local(el.tag) == "Atom":
                self.warnings.append("Nested <Atom> used as a term; kept as nested formula")
                args.append({"atom": self.atom(el)})
            else:
                args.append(self.term(el))
            i += 1
        return args

    # ---- formulas --------------------------------------------------------
    def atom(self, el: ET.Element) -> dict:
        children = list(el)
        if not children:
            self.warnings.append("Empty <Atom>")
            return {"op": "atom", "pred": None, "args": []}

        first = local(children[0].tag)

        # Temporal ordering written as <Atom><After>t2 t1</After></Atom>
        if first in ("After", "Before") and len(children) == 1:
            return {"op": "atom", "pred": first, "builtin": "temporal",
                    "args": [self.term(c) for c in children[0]],
                    "reified": False, "atom_id": None}

        if first != "Rel":
            self.warnings.append(f"<Atom> without leading <Rel> (starts with <{first}>)")
            return {"op": "atom", "pred": None, "args": [self.term(c) for c in children]}

        pred = children[0].get("iri")
        rest = children[1:]

        # Irregular negation written as <Atom><Rel iri="rioOnto:Naf"/><Atom>..</Atom></Atom>
        if pred == "rioOnto:Naf" and len(rest) == 1 and local(rest[0].tag) == "Atom":
            self.warnings.append("Negation encoded as rioOnto:Naf relation; normalised to naf")
            return {"op": "naf", "arg": self.atom(rest[0]), "source_form": "rioOnto:Naf"}

        # Negation written as <Atom><Rel iri="rioOnto:not"/>[<Var/>]<Atom>..</Atom></Atom>
        # (a nested atom instead of an eventuality argument)
        nested = [c for c in rest if local(c.tag) == "Atom"]
        if pred in ("rioOnto:not", "rioOnto:Naf") and len(nested) == 1:
            self.warnings.append(f"Negation encoded as {pred} over a nested <Atom>; normalised to naf")
            return {"op": "naf", "arg": self.atom(nested[0]), "source_form": pred}

        args = self.arg_terms(rest)
        atom_id = el.get("keyref")
        reified = bool(atom_id) and norm_id(atom_id) in self.reified_atom_ids
        node = {"op": "atom", "pred": pred, "args": args, "reified": reified, "atom_id": atom_id}
        if reified and args:
            node["eventuality"] = args[0]
        return node

    def formula(self, el: ET.Element) -> dict:
        tag = local(el.tag)
        if tag == "Atom":
            return self.atom(el)
        if tag == "And":
            return {"op": "and", "args": [self.formula(c) for c in el]}
        if tag == "Naf":
            inner = list(el)
            return {"op": "naf", "arg": self.formula(inner[0]) if inner else None}
        if tag == "Exists":
            return self._exists(list(el))
        self.warnings.append(f"Unknown formula element <{tag}>")
        return {"op": "unknown", "tag": tag}

    def _exists(self, children: list) -> dict:
        vars_, body = [], []
        for c in children:
            if local(c.tag) == "Var":
                name = c.get("key") or c.get("keyref")
                vars_.append(name)
                self._note_var(name)
                self.existential[self._side].append(name)
            else:
                body.append(self.formula(c))
        if len(body) != 1:
            body_node = {"op": "and", "args": body}
        else:
            body_node = body[0]
        return {"op": "exists", "vars": vars_, "body": body_node}

    def side(self, el: ET.Element, side: str) -> dict:
        self._side = side
        children = list(el)
        # Irregular: bare <Var> declarations directly under <if>/<then>
        if any(local(c.tag) == "Var" for c in children):
            self.warnings.append(f"Bare <Var> under <{side}>; treated as existential declaration")
            return self._exists(children)
        if len(children) == 1:
            return self.formula(children[0])
        return {"op": "and", "args": [self.formula(c) for c in children]}


# --------------------------------------------------------------------------
# Rendering and traversal helpers
# --------------------------------------------------------------------------

def render_term(t: dict) -> str:
    if "var" in t:
        return t["var"]
    if "ind" in t:
        return t["ind"]
    if "const" in t:
        return t["const"]
    if "fun" in t:
        return f"{t['fun']}({' '.join(render_term(a) for a in t['args'])})"
    if "atom" in t:
        return render(t["atom"])
    return "?"


def render(node: dict) -> str:
    op = node["op"]
    if op == "atom":
        prime = "'" if node.get("reified") else ""
        parts = [f"{node['pred']}{prime}"] + [render_term(a) for a in node["args"]]
        return "(" + " ".join(parts) + ")"
    if op == "and":
        return " & ".join(render(a) for a in node["args"])
    if op == "naf":
        return f"Naf{render(node['arg'])}" if node["arg"] else "Naf()"
    if op == "exists":
        return f"Exists[{' '.join(node['vars'])}]({render(node['body'])})"
    return f"<{op}>"


def iter_atoms(node: dict, negated: bool = False):
    """Yield (atom, negated) for every atom in a formula tree."""
    op = node["op"]
    if op == "atom":
        yield node, negated
    elif op == "and":
        for a in node["args"]:
            yield from iter_atoms(a, negated)
    elif op == "naf" and node.get("arg"):
        yield from iter_atoms(node["arg"], not negated)
    elif op == "exists":
        yield from iter_atoms(node["body"], negated)


def iter_terms(term: dict):
    yield term
    for a in term.get("args", []):
        yield from iter_terms(a)


def atom_signature(atom: dict, negated: bool) -> str:
    """Comparable form of an atom that ignores atom ids."""
    return ("~" if negated else "") + render(dict(atom, atom_id=None))


# --------------------------------------------------------------------------
# Derived information
# --------------------------------------------------------------------------

def parse_exception_name(pred: str) -> dict:
    name = pred.split(":", 1)[-1]
    hint = {}
    for key, val in EXCEPTION_PART.findall(name):
        k = {"Cha": "chapter", "Sec": "section", "Art": "article",
             "Par": "paragraph", "Point": "point"}[key]
        if k not in hint:  # first occurrence = provision the exception comes from
            hint[k] = int(val) if val.isdigit() else val
    return hint


def derive(norm: dict) -> dict:
    sides = {s: list(iter_atoms(norm[s])) for s in ("if", "then")}
    all_atoms = sides["if"] + sides["then"]

    exceptions, negated = [], []
    for atom, neg in all_atoms:
        if not neg:
            continue
        entry = {"predicate": atom["pred"], "args": [render_term(a) for a in atom["args"]]}
        negated.append(entry)
        if atom["pred"] and "exception" in atom["pred"].lower():
            exceptions.append(dict(entry, source_hint=parse_exception_name(atom["pred"])))

    temporal = [render(a) for a, _ in all_atoms
                if a.get("builtin") == "temporal" or a["pred"] in ("rioOnto:After", "rioOnto:Before")]

    constants = set()
    for atom, _ in all_atoms:
        for arg in atom["args"]:
            for t in iter_terms(arg):
                if "ind" in t:
                    constants.add(t["ind"])
                elif "const" in t:
                    constants.add(t["const"])

    predicates = {s: sorted({a["pred"] for a, _ in sides[s] if a["pred"]}) for s in sides}

    return {
        "predicates_if": predicates["if"],
        "predicates_then": predicates["then"],
        "negated_atoms": negated,
        "exceptions": exceptions,
        "temporal_constraints": temporal,
        "constants": sorted(constants),
        # filled in by link_deontic_twins()
        "deontic_twin": None,
        "bearer": None,
    }


def roles_of(var: str, norm: dict) -> list:
    """Non-reified predicates whose first argument is `var` (e.g. prOnto:Controller)."""
    roles = []
    for side in ("if", "then"):
        for atom, neg in iter_atoms(norm[side]):
            if neg or atom.get("reified") or atom.get("builtin") or not atom["args"]:
                continue
            if atom["args"][0].get("var") == var and atom["pred"] not in roles:
                roles.append(atom["pred"])
    return roles


def link_deontic_twins(norms: list) -> Counter:
    """
    DAPRECO usually encodes each obligation/permission twice in the same block:
      deontic formula:      if A then (RexistAtTime e t) & B
      constitutive twin:    if A then (Obliged|Permitted e t y) & B
    The twin names the bearer y explicitly. Match them and record the bearer.
    """
    stats = Counter()
    by_block = defaultdict(list)
    for n in norms:
        by_block[n["block"]].append(n)

    for block_norms in by_block.values():
        candidates = [n for n in block_norms if n["statement_type"] == "constitutive"]
        for d in block_norms:
            target = DEONTIC_PREDICATES.get(d["statement_type"])
            if not target:
                continue
            stats["deontic"] += 1
            d_if = sorted(atom_signature(a, ng) for a, ng in iter_atoms(d["if"]))
            d_then = Counter(atom_signature(a, ng) for a, ng in iter_atoms(d["then"]))

            for c in candidates:
                if c["derived"]["deontic_twin"]:
                    continue
                if sorted(atom_signature(a, ng) for a, ng in iter_atoms(c["if"])) != d_if:
                    continue
                c_then, bearer_var = Counter(), None
                for a, ng in iter_atoms(c["then"]):
                    if a["pred"] == target and not ng and len(a["args"]) >= 3 and bearer_var is None:
                        # Obliged(e, t, y) -> compare as RexistAtTime(e, t)
                        bearer_var = a["args"][2].get("var")
                        rewritten = dict(a, pred=EXISTENCE_PREDICATE, args=a["args"][:2])
                        c_then[atom_signature(rewritten, ng)] += 1
                    else:
                        c_then[atom_signature(a, ng)] += 1
                if bearer_var and c_then == d_then:
                    bearer = {"var": bearer_var, "roles": roles_of(bearer_var, c)}
                    d["derived"]["deontic_twin"] = c["id"]
                    d["derived"]["bearer"] = bearer
                    c["derived"]["deontic_twin"] = d["id"]
                    c["derived"]["bearer"] = bearer
                    stats["twin_matched"] += 1
                    break
            else:
                stats["twin_unmatched"] += 1
    return stats


# --------------------------------------------------------------------------
# Main conversion
# --------------------------------------------------------------------------

def convert(xml_path: str) -> dict:
    root = ET.parse(xml_path).getroot()
    meta = read_metadata(root)

    norms = []
    used_keys = {s.get("key") for s in root.iter(LRML + "ConstitutiveStatement")}
    seen_keys = set()
    duplicates = []
    for block in root.findall(LRML + "Statements"):
        block_key = block.get("key")
        for stmt in block.findall(LRML + "ConstitutiveStatement"):
            key = stmt.get("key")
            dup_warning = None
            if key in seen_keys:
                # Duplicate key in the source. Prefer the next formula number that a
                # Context references but no statement carries (a likely typo), else suffix.
                m = re.match(r"(.*Formula)(\d+)$", key)
                new_key = None
                if m:
                    for nxt in range(int(m.group(2)) + 1, int(m.group(2)) + 20):
                        cand = f"{m.group(1)}{nxt}"
                        if cand in meta["statement_context"] and cand not in used_keys and cand not in seen_keys:
                            new_key = cand
                            break
                new_key = new_key or f"{key}_dup{sum(1 for k in seen_keys if k.startswith(key)) + 1}"
                dup_warning = f"Duplicate statement key {key} in source; renamed to {new_key}"
                duplicates.append({"original": key, "renamed_to": new_key})
                key = new_key
            seen_keys.add(key)
            rule = stmt.find(RULEML + "Rule")
            parser = FormulaParser(meta["reified_atom_ids"])
            # Match <if>/<then> by local name: at least one rule uses lrml:then instead of ruleml:then
            parts = {local(c.tag): c for c in rule}
            for part in ("if", "then"):
                if parts.get(part) is not None and parts[part].tag != RULEML + part:
                    parser.warnings.append(f"<{part}> in non-RuleML namespace ({parts[part].tag.split('}')[0][1:]})")
            if dup_warning:
                parser.warnings.append(dup_warning)
            if_tree = parser.side(parts["if"], "if")
            then_tree = parser.side(parts["then"], "then")

            existential = set(parser.existential["if"]) | set(parser.existential["then"])
            ctx_key, ctx_type = meta["statement_context"].get(key, (None, None))
            stype = CONTEXT_TYPES.get(ctx_type, "unknown")

            norm = {
                "id": key,
                "block": block_key,
                "statement_type": stype,
                "context": {"key": ctx_key, "type": ctx_type},
                "legal_references": meta["block_refs"].get(block_key, []),
                "closure": rule.get("closure"),
                "variables": {
                    "universal": [v for v in parser.all_vars if v not in existential],
                    "existential_if": parser.existential["if"],
                    "existential_then": parser.existential["then"],
                },
                "if": if_tree,
                "then": then_tree,
            }
            label = {"obligation": "Obligation", "permission": "Permission"}.get(stype, "Entailment")
            norm["text"] = f"{label}: IF {render(if_tree)} THEN {render(then_tree)}"
            norm["derived"] = derive(norm)
            norm["warnings"] = parser.warnings
            norms.append(norm)

    twin_stats = link_deontic_twins(norms)

    summary = {
        "norms": len(norms),
        "blocks": len({n["block"] for n in norms}),
        "by_statement_type": dict(Counter(n["statement_type"] for n in norms)),
        "with_legal_reference": sum(1 for n in norms if n["legal_references"]),
        "with_exceptions": sum(1 for n in norms if n["derived"]["exceptions"]),
        "deontic_twins_matched": twin_stats["twin_matched"],
        "deontic_twins_unmatched": twin_stats["twin_unmatched"],
        "norms_with_warnings": sum(1 for n in norms if n["warnings"]),
        "duplicate_keys": duplicates,
        "warning_types": dict(Counter(re.sub(r"\S+:\S+", "<pred>", w)
                                      for n in norms for w in n["warnings"])),
    }

    return {
        "source": xml_path,
        "prefixes": meta["prefixes"],
        "contexts": meta["contexts"],
        "legal_references": list(meta["legal_references"].values()),
        "summary": summary,
        "norms": norms,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("xml", help="DAPRECO LegalRuleML file, e.g. rioKB_GDPR.xml")
    ap.add_argument("-o", "--output", default="data/validation_norms/dapreco_structured.json", help="output JSON file")
    ap.add_argument("--jsonl", help="optionally also write one norm per line to this file")
    ap.add_argument("--compact", action="store_true", help="write JSON without indentation")
    args = ap.parse_args(argv)

    result = convert(args.xml)

    for path in (args.output, args.jsonl):
        if path and os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=None if args.compact else 2)
    if args.jsonl:
        with open(args.jsonl, "w", encoding="utf-8") as f:
            for n in result["norms"]:
                f.write(json.dumps(n, ensure_ascii=False) + "\n")

    print(json.dumps(result["summary"], indent=2), file=sys.stderr)


if __name__ == "__main__":
    main()