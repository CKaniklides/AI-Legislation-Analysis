#!/usr/bin/env python3
"""
Steps 1-2 of the DAPRECO validation pipeline.

Reads the structured DAPRECO file produced by dapreco_to_norms.py and builds
the reference set your extractions are compared against:

  Step 1  keep obligation/permission formulas, resolve their bearer,
          group them by GDPR provision, record coverage and
          "constitutive-only" provisions (negatives).
  Step 2  extract comparable slots from every reference norm:
          deontic, actor, content (actions / relations / properties with
          agent, participants, recipients, deadline), conditions,
          trigger candidates, exceptions, thresholds, plus quality flags.

Usage:
    python build_dapreco_reference.py
    python build_dapreco_reference.py -i data/validation_norms/dapreco_structured.json \
        -o data/validation_norms/dapreco_reference.json \
        -c data/validation_norms/config/dapreco_slots_config.json

The config file is created with defaults on first run (an older config is
backed up and replaced). Edit it to tune predicate lists.
Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from collections import Counter, defaultdict

DEFAULT_INPUT = "data/validation_norms/dapreco_structured.json"
DEFAULT_OUTPUT = "data/validation_norms/dapreco_reference.json"
DEFAULT_CONFIG = "data/validation_norms/config/dapreco_slots_config.json"
CONFIG_VERSION = 2

# All predicate names are LOCAL names (no prefix): DAPRECO uses the same
# predicate under several prefixes (prOnto:/rioOnto:/dapreco:Communicate).
DEFAULT_CONFIG_CONTENT = {
    "config_version": CONFIG_VERSION,
    # Context repeated in almost every antecedent; never reported as a condition.
    "scaffold_predicates": [
        "RexistAtTime", "and", "or", "not", "imply",
        "DataSubject", "PersonalData", "Controller", "Processor",
        "nominates", "PersonalDataProcessing",
    ],
    "connectives": ["and", "or", "not", "imply"],
    "deontic_predicates": ["Obliged", "Permitted"],
    "temporal_predicates": ["Before", "After"],
    "undue_delay_predicates": ["nonDelayed"],
    # States/relations between things: they have a subject, not an agent.
    "relational_predicates": [
        "AdequateWith", "RelevantTo", "LimitedTo", "CompatibleWith", "Contain",
        "Describe", "RelatedTo", "ControlledBy", "AuthorizedBy", "NecessaryFor",
        "ResponsibleFor", "Safeguard", "TakenToAddress", "ProposedToAddress",
        "PartyOf", "ConflictOfInterest", "isRepresentedBy", "isBasedOn", "cause",
        "partOf", "CategoryOf", "timeOf", "Feasible", "LinkBetween",
        "ReachableFrom", "IsInTheInterestOf", "StructureOf", "Recipient",
        "CompensationFor", "ViolationOf", "Indicates", "WorkIn", "mitigate",
        "Override", "ExpertIn", "AccreditedBy", "SourceOf", "ContactPointFor",
        "LetterReasonFor", "ReasonFor", "HasNumberOfEmployees", "Involve",
        "Risk", "EasyAs", "AwareOf", "ComplyWith",
    ],
    # Lower-case predicates that are nevertheless actions with an agent.
    "lowercase_actions": ["designates", "nominates"],
    # Argument index (0 = eventuality) that holds the recipient, per action.
    "recipient_positions": {
        "Communicate": 2, "Transmit": 3, "Lodge": 3, "AssistFor": 2,
        "Charge": 2, "CooperateWith": 2, "GiveCompensationTo": 3,
        "AdviseOn": 2, "Penalise": 2,
    },
    "comparison_predicates": [
        "lessThan", "lessThanOrEqual", "greaterThan", "greaterThanOrEqual",
        "equal", "notEqual",
    ],
    "qualitative_threshold_predicates": [
        "largeScale", "occasional", "concernsLimitedNumberOfDS",
        "HasNumberOfEmployees",
    ],
    "actor_classes": [
        "Controller", "Processor", "DataSubject", "DPO", "Representative",
        "JointDataController", "SupervisoryAuthority", "LeadSupervisoryAuthority",
        "Board", "Commission", "MemberState", "BodyACM", "BodyCC",
        "CertificationBody", "MonitoringBody", "ThirdParty", "Recipient",
        "InternationalOrganization", "Person", "Legal_Person", "Child",
        "HolderOfParentalResponsibility", "Court", "OfficialAuthority",
        "Public_Body", "NotForProfitBody", "ThePublic",
    ],
    "time_units": {"h": "hour", "d": "day", "w": "week", "M": "month", "y": "year"},
}

TIME_CONSTANT = re.compile(r"^(\d+)([hdwMy])$")
NUMBER = re.compile(r"^-?\d+(\.\d+)?$")


# --------------------------------------------------------------------------
# Helpers over the formula tree produced by dapreco_to_norms.py
# --------------------------------------------------------------------------

def local_name(pred: str | None) -> str:
    return (pred or "").split(":", 1)[-1]


def is_exception(pred: str | None) -> bool:
    return local_name(pred).startswith("exception")


def iter_atoms(node, negated=False):
    """Yield (atom, negated) for every atom, honouring Naf nesting."""
    if not node:
        return
    op = node.get("op")
    if op == "atom":
        yield node, negated
    elif op == "and":
        for a in node["args"]:
            yield from iter_atoms(a, negated)
    elif op == "naf":
        yield from iter_atoms(node.get("arg"), not negated)
    elif op == "exists":
        yield from iter_atoms(node["body"], negated)


def render_term(t: dict) -> str:
    if "var" in t:
        return t["var"]
    if "ind" in t:
        return t["ind"]
    if "const" in t:
        return t["const"]
    if "fun" in t:
        return f"{t['fun']}({' '.join(render_term(a) for a in t.get('args', []))})"
    if "atom" in t:
        return render_atom(t["atom"]) if t["atom"].get("op") == "atom" else "<formula>"
    return "?"


def render_atom(atom: dict, negated: bool = False) -> str:
    prime = "'" if atom.get("reified") else ""
    body = "(" + " ".join([f"{atom['pred']}{prime}"] + [render_term(a) for a in atom["args"]]) + ")"
    return f"Naf{body}" if negated else body


def var_of(term) -> str | None:
    return term.get("var") if isinstance(term, dict) else None


def iter_subterms(term: dict):
    yield term
    for a in term.get("args", []):
        yield from iter_subterms(a)


def provision_keys(ref: dict) -> tuple[str | None, str | None]:
    if ref.get("article") is None:
        return None, None
    para_key = f"art{ref['article']}"
    if ref.get("paragraph") is not None:
        para_key += f".{ref['paragraph']}"
    full_key = para_key + (f".{ref['point']}" if ref.get("point") else "")
    return full_key, para_key


# --------------------------------------------------------------------------
# Per-norm analysis
# --------------------------------------------------------------------------

class NormAnalyzer:
    def __init__(self, norm: dict, cfg: dict):
        self.norm = norm
        self.cfg = cfg
        self.sets = {k: set(v) for k, v in cfg.items() if isinstance(v, list)}
        self.atoms = {s: list(iter_atoms(norm[s])) for s in ("if", "then")}
        self.var_classes = self._var_classes()
        self.content_atom_ids: set = set()

    # ---- predicate kinds --------------------------------------------------
    def kind_of(self, name: str) -> str:
        if name in self.sets["relational_predicates"]:
            return "relation"
        if name in self.sets["lowercase_actions"] or name[:1].isupper():
            return "action"
        return "property"

    def is_structural(self, name: str) -> bool:
        """Predicates that are logical/temporal machinery, never content."""
        return (name in self.sets["connectives"] or name in self.sets["deontic_predicates"]
                or name in self.sets["undue_delay_predicates"]
                or name in self.sets["temporal_predicates"] or name == "RexistAtTime")

    # ---- variable typing (fix 3) ------------------------------------------
    def _var_classes(self) -> dict:
        """var -> class names, from positive class atoms like (prOnto:Controller :y :z).
        Structural and relational predicates are not classes."""
        classes = defaultdict(list)
        for side in ("if", "then"):
            for atom, neg in self.atoms[side]:
                if neg or not atom["args"] or atom.get("builtin"):
                    continue
                name = local_name(atom["pred"])
                if (not name[:1].isupper() or self.is_structural(name)
                        or name in self.sets["relational_predicates"] or is_exception(atom["pred"])):
                    continue
                v = var_of(atom["args"][0])
                if v and name not in classes[v]:
                    classes[v].append(name)
        return classes

    def classes_of_term(self, term: dict) -> list:
        if "var" in term:
            return list(self.var_classes.get(term["var"], []))
        if "fun" in term:
            return [local_name(term["fun"])]   # SupervisoryAuthority(MemberState(:y))
        if "const" in term:
            return [local_name(term["const"])]
        return []

    def describe_term(self, term: dict) -> dict:
        classes = self.classes_of_term(term)
        return {
            "term": render_term(term),
            "classes": classes,
            "actor_classes": [c for c in classes if c in self.sets["actor_classes"]],
        }

    # ---- reachability ----------------------------------------------------------
    def _index(self, side: str) -> dict:
        """eventuality var -> [(atom, negated)] on one side (exceptions excluded)."""
        idx = defaultdict(list)
        for atom, neg in self.atoms[side]:
            if not atom["args"] or atom.get("builtin") or is_exception(atom["pred"]):
                continue
            ev = var_of(atom["args"][0])
            if ev and local_name(atom["pred"]) != "RexistAtTime":
                idx[ev].append((atom, neg))
        return idx

    def _existing(self, side: str) -> dict:
        """eventualities asserted by (RexistAtTime e t) on a side -> time term."""
        out = {}
        for atom, neg in self.atoms[side]:
            if not neg and local_name(atom["pred"]) == "RexistAtTime" and len(atom["args"]) > 1:
                ev = var_of(atom["args"][0])
                if ev:
                    out[ev] = atom["args"][1]
        return out

    def _connective_children(self, entries) -> list:
        """[(child_var, name)] for connective atoms among an eventuality's entries."""
        out = []
        for atom, _ in entries:
            name = local_name(atom["pred"])
            if name in self.sets["connectives"]:
                out += [(var_of(c), name) for c in atom["args"][1:] if var_of(c)]
        return out

    def _durations_by_time(self) -> dict:
        """time var -> durations, from (Before :t swrlb:add(:t1 1M)) in <then>."""
        by_time = defaultdict(list)
        for atom, neg in self.atoms["then"]:
            if neg or local_name(atom["pred"]) != "Before" or len(atom["args"]) < 2:
                continue
            t_var = var_of(atom["args"][0])
            for sub in iter_subterms(atom["args"][1]):
                if "ind" in sub:
                    d = self.parse_duration(sub["ind"])
                    if d and t_var:
                        by_time[t_var].append(d)
        return by_time

    def parse_duration(self, token: str) -> dict | None:
        m = TIME_CONSTANT.match(token)
        if not m:
            return None
        return {"value": int(m.group(1)), "unit": self.cfg["time_units"].get(m.group(2)), "raw": token}

    # ---- step 2a: deontic content (fixes 2, 4, 5, 6, 7) -----------------------
    def content(self) -> list:
        idx_then, idx_if = self._index("then"), self._index("if")
        exists_then = self._existing("then")
        durations_by_time = self._durations_by_time()
        undue = {var_of(a["args"][0]) for side in ("then", "if") for a, n in self.atoms[side]
                 if not n and a["args"] and local_name(a["pred"]) in self.sets["undue_delay_predicates"]}

        def has_content(entries):
            return any(not self.is_structural(local_name(a["pred"])) for a, _ in entries)

        # Walk connectives side by side: follow <then> connectives; only for an
        # eventuality with no <then> description at all fall back to <if> (fix 6).
        reached = {}
        stack = []
        for ev, t in exists_then.items():
            own = list(durations_by_time.get(var_of(t), []))
            stack.append((ev, False, False, own, ev in undue))
        while stack:
            ev, negd, alt, durs, ud = stack.pop()
            if ev in reached:
                continue
            ud = ud or ev in undue
            then_entries = idx_then.get(ev, [])
            if has_content(then_entries) or self._connective_children(then_entries):
                side, entries = "then", then_entries
            else:
                # <then> only re-asserts the eventuality (e.g. adds nonDelayed);
                # its description lives in <if>
                side, entries = "if", idx_if.get(ev, [])
            reached[ev] = {"negated": negd, "alternative": alt, "durations": durs,
                           "undue": ud, "side": side, "entries": entries}
            for child, name in self._connective_children(entries):
                # children inherit timing from the connective node (fix 5)
                stack.append((child, negd ^ (name == "not"), alt or name == "or", durs, ud))

        result = []
        for ev, info in reached.items():
            for atom, neg in info["entries"]:
                name = local_name(atom["pred"])
                if self.is_structural(name):
                    continue
                self.content_atom_ids.add(id(atom))
                kind = self.kind_of(name)
                args = atom["args"][1:]
                item = {
                    "predicate": atom["pred"], "local": name, "kind": kind,
                    "eventuality": ev, "described_in": info["side"],
                    "negated": info["negated"] ^ neg, "alternative": info["alternative"],
                    "agent": None, "subject": None, "participants": [],
                    "recipients": [],
                    "deadline": {"durations": info["durations"],
                                 "without_undue_delay": info["undue"]},
                    "rendered": render_atom(atom, neg),
                }
                if kind == "action":
                    item["agent"] = self.describe_term(args[0]) if args else None
                    item["participants"] = [self.describe_term(a) for a in args[1:]]
                    pos = self.cfg["recipient_positions"].get(name)
                    if pos is not None and len(atom["args"]) > pos:
                        rec = self.describe_term(atom["args"][pos])
                        if not item["agent"] or rec["term"] != item["agent"]["term"]:
                            item["recipients"] = [rec]
                elif kind == "relation":
                    item["subject"] = self.describe_term(args[0]) if args else None
                    item["participants"] = [self.describe_term(a) for a in args[1:]]
                else:
                    item["participants"] = [self.describe_term(a) for a in args]
                result.append(item)
        order = {"action": 0, "relation": 1, "property": 2}
        result.sort(key=lambda a: (order[a["kind"]], a["local"], a["eventuality"]))
        return result

    # ---- step 2b: conditions and triggers (fix 8) --------------------------------
    def conditions_and_triggers(self) -> tuple[list, list]:
        """Call after content(). Atoms used as deontic content are not conditions."""
        idx_if = self._index("if")
        events = set()
        stack = list(self._existing("if"))
        while stack:
            ev = stack.pop()
            if ev in events:
                continue
            events.add(ev)
            stack += [c for c, _ in self._connective_children(idx_if.get(ev, []))]

        conditions, triggers, seen = [], [], set()
        for atom, neg in self.atoms["if"]:
            name = local_name(atom["pred"])
            if (id(atom) in self.content_atom_ids or name in self.sets["scaffold_predicates"]
                    or is_exception(atom["pred"])):
                continue
            rendered = render_atom(atom, neg)
            if rendered in seen:
                continue
            seen.add(rendered)
            entry = {
                "predicate": atom["pred"], "local": name, "negated": neg,
                "temporal": bool(atom.get("builtin")) or name in self.sets["temporal_predicates"],
                "rendered": rendered,
            }
            ev = var_of(atom["args"][0]) if atom["args"] else None
            # A trigger is a positive, reified, agentive event asserted in the antecedent.
            if (not neg and atom.get("reified") and ev in events and not entry["temporal"]
                    and self.kind_of(name) == "action" and name not in self.sets["actor_classes"]
                    and not self.is_structural(name)):
                triggers.append(entry)
            else:
                conditions.append(entry)
        return conditions, triggers

    # ---- step 2c: exceptions ----------------------------------------------------
    def exceptions(self) -> list:
        out, seen = [], set()
        for side in ("if", "then"):
            for atom, neg in self.atoms[side]:
                if not is_exception(atom["pred"]):
                    continue
                key = (atom["pred"], tuple(render_term(a) for a in atom["args"]))
                if key in seen:
                    continue
                seen.add(key)
                hint = self._exception_source(atom["pred"])
                src = None
                if "article" in hint:
                    src = f"art{hint['article']}" + (f".{hint['paragraph']}" if "paragraph" in hint else "")
                out.append({"predicate": atom["pred"], "args": [render_term(a) for a in atom["args"]],
                            "source_hint": hint, "source_key": src,
                            "asserted_positively": not neg})
        return out

    @staticmethod
    def _exception_source(pred: str) -> dict:
        hint = {}
        for key, val in re.findall(r"(Cha|Sec|Art|Par|Point)(\d+)", local_name(pred)):
            k = {"Cha": "chapter", "Sec": "section", "Art": "article",
                 "Par": "paragraph", "Point": "point"}[key]
            hint.setdefault(k, int(val))
        return hint

    # ---- step 2d: thresholds ------------------------------------------------------
    def thresholds(self) -> list:
        out = []
        for side in ("if", "then"):
            for atom, neg in self.atoms[side]:
                name = local_name(atom["pred"])
                numbers = [s["ind"] for a in atom["args"] for s in iter_subterms(a)
                           if "ind" in s and NUMBER.match(s["ind"])]
                if name in self.sets["comparison_predicates"]:
                    out.append({"type": "numeric" if numbers else "comparison",
                                "predicate": atom["pred"],
                                "values": [float(n) if "." in n else int(n) for n in numbers],
                                "side": side, "negated": neg, "rendered": render_atom(atom, neg)})
                elif name in self.sets["qualitative_threshold_predicates"]:
                    out.append({"type": "qualitative", "predicate": atom["pred"],
                                "side": side, "negated": neg, "rendered": render_atom(atom, neg)})
        return out

    # ---- step 1: bearer (fix 1) ----------------------------------------------------
    def actor(self, content: list) -> dict:
        bearer = self.norm["derived"].get("bearer")
        if bearer and bearer.get("var"):
            var, source = bearer["var"], "deontic_twin"
        else:
            var, source = None, "unresolved"
            # prefer agents of actions that carry an actor class, then relation subjects
            for field, kinds in (("agent", ("action",)), ("subject", ("relation",))):
                for item in content:
                    t = item.get(field)
                    if item["kind"] in kinds and t and t["actor_classes"]:
                        var = t["term"]
                        source = "agent_of_main_action" if field == "agent" else "subject_of_relation"
                        break
                if var:
                    break
        if var is None:
            return {"var": None, "classes": [], "actor_classes": [], "source": source}
        classes = self.var_classes.get(var, [])
        actor_classes = [c for c in classes if c in self.sets["actor_classes"]]
        if not actor_classes and source == "deontic_twin":
            source = "deontic_twin_untyped"
        return {"var": var, "classes": classes, "actor_classes": actor_classes, "source": source}


# --------------------------------------------------------------------------
# Deontic labelling and quality flags
# --------------------------------------------------------------------------

def effective_deontic(deontic: str, content: list) -> str:
    core = [c for c in content if c["kind"] == "action"] or content
    if not core:
        return deontic
    neg = [c["negated"] for c in core]
    if all(neg):
        return "prohibition" if deontic == "obligation" else "permission_not"
    if any(neg):
        return "mixed"
    return deontic


def quality_flags(ref: dict, warnings: list) -> list:
    flags = []
    actor = ref["actor"]
    agents = [c["agent"] for c in ref["content"]
              if c["kind"] == "action" and c["agent"] and c["agent"]["actor_classes"]]
    if actor["source"] == "deontic_twin" and agents and actor["var"] not in {a["term"] for a in agents}:
        agent_classes = {c for a in agents for c in a["actor_classes"]}
        detail = (f"bearer {actor['var']} {actor['actor_classes']} is not the agent of any action; "
                  f"agents: {sorted({a['term'] + ' ' + '/'.join(a['actor_classes']) for a in agents})}")
        # DAPRECO idiom: the controller is the bearer of actions modelled as done by the
        # processor (processing, transfers), the data subject (rectification, erasure,
        # restriction requested by them) or the DPO. Informational, not an error.
        if (set(actor["actor_classes"]) & {"Controller", "JointDataController"}
                and agent_classes <= {"Processor", "DataSubject", "DPO"}):
            flags.append({"flag": "bearer_ensures_other_party", "detail": detail})
        else:
            flags.append({"flag": "bearer_agent_mismatch", "detail": detail})
    if actor["source"] == "deontic_twin_untyped":
        flags.append({"flag": "bearer_untyped",
                      "detail": f"bearer {actor['var']} has no actor class in this formula"})
    if actor["source"] == "unresolved":
        flags.append({"flag": "bearer_unresolved", "detail": "no twin and no typed agent"})
    for e in ref["exceptions"]:
        if e["asserted_positively"]:
            flags.append({"flag": "exception_asserted_positively",
                          "detail": f"{e['predicate']} appears without Naf"})
    for c in ref["conditions"]:
        if c["temporal"] and "swrlb:add" in c["rendered"]:
            flags.append({"flag": "deadline_in_antecedent",
                          "detail": f"{c['rendered']} conditions the norm; check polarity against the text"})
    if not any(c["kind"] == "action" for c in ref["content"]):
        flags.append({"flag": "no_action_only_state", "detail": "content has only relations/properties"})
    if warnings:
        flags.append({"flag": "converter_warnings", "detail": f"{len(warnings)} warning(s); check 'text'"})
    return flags


# --------------------------------------------------------------------------
# Building the reference set
# --------------------------------------------------------------------------

def load_config(path: str) -> dict:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        if cfg.get("config_version", 1) < CONFIG_VERSION:
            backup = path + f".v{cfg.get('config_version', 1)}.bak"
            shutil.copy(path, backup)
            print(f"Config at {path} is outdated; backed up to {backup} and replaced", file=sys.stderr)
            cfg = None
    else:
        cfg = None
    if cfg is None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG_CONTENT, f, indent=2)
        cfg = json.loads(json.dumps(DEFAULT_CONFIG_CONTENT))
    for k, v in DEFAULT_CONFIG_CONTENT.items():
        cfg.setdefault(k, v)
    return cfg


def build_reference(structured: dict, cfg: dict) -> dict:
    norms = structured["norms"]
    by_block = defaultdict(list)
    for n in norms:
        by_block[n["block"]].append(n)

    references = []
    for n in norms:
        if n["statement_type"] not in ("obligation", "permission"):
            continue
        an = NormAnalyzer(n, cfg)
        content = an.content()
        conditions, triggers = an.conditions_and_triggers()

        provisions = []
        for ref in n["legal_references"]:
            key, para_key = provision_keys(ref)
            provisions.append({"article": ref["article"], "paragraph": ref["paragraph"],
                               "point": ref["point"], "key": key, "paragraph_key": para_key,
                               "ref_id": ref["ref_id"]})

        ref_norm = {
            "ref_norm_id": n["id"],
            "block": n["block"],
            "provisions": provisions,
            "deontic": n["statement_type"],
            "deontic_effective": effective_deontic(n["statement_type"], content),
            "deontic_twin": n["derived"].get("deontic_twin"),
            "actor": an.actor(content),
            "content": content,
            "conditions": conditions,
            "trigger_candidates": triggers,
            "exceptions": an.exceptions(),
            "thresholds": an.thresholds(),
            "text": n["text"],
            "converter_warnings": n.get("warnings", []),
        }
        ref_norm["flags"] = quality_flags(ref_norm, ref_norm["converter_warnings"])
        references.append(ref_norm)

    # Provision index: coverage, negatives, block-level facts
    provisions = {}
    for block, block_norms in by_block.items():
        refs = block_norms[0]["legal_references"]
        if not refs:
            continue  # background axioms
        deontic_ids = [x["id"] for x in block_norms if x["statement_type"] in ("obligation", "permission")]
        constitutive_ids = list(dict.fromkeys(x["id"] for x in block_norms if x["statement_type"] == "constitutive"))
        block_thresholds, seen = [], set()
        for x in block_norms:
            for t in NormAnalyzer(x, cfg).thresholds():
                if t["rendered"] not in seen:
                    seen.add(t["rendered"])
                    block_thresholds.append(dict(t, formula=x["id"]))
        for ref in refs:
            key, para_key = provision_keys(ref)
            entry = provisions.setdefault(key, {
                "key": key, "paragraph_key": para_key, "blocks": [],
                "reference_norm_ids": [], "constitutive_ids": [], "thresholds_in_block": [],
            })
            entry["blocks"].append(block)
            entry["reference_norm_ids"] += deontic_ids
            entry["constitutive_ids"] += constitutive_ids
            entry["thresholds_in_block"] += block_thresholds
    for entry in provisions.values():
        entry["constitutive_only"] = not entry["reference_norm_ids"]

    def has_deadline(r):
        return any(c["deadline"]["durations"] or c["deadline"]["without_undue_delay"] for c in r["content"])

    summary = {
        "reference_norms": len(references),
        "by_deontic_effective": dict(Counter(r["deontic_effective"] for r in references)),
        "actor_source": dict(Counter(r["actor"]["source"] for r in references)),
        "actor_classes": dict(Counter("+".join(r["actor"]["actor_classes"]) or "<none>" for r in references)),
        "content_kinds": dict(Counter(c["kind"] for r in references for c in r["content"])),
        "norms_with_action": sum(1 for r in references if any(c["kind"] == "action" for c in r["content"])),
        "norms_with_recipient": sum(1 for r in references if any(c["recipients"] for c in r["content"])),
        "norms_with_deadline": sum(1 for r in references if has_deadline(r)),
        "norms_with_trigger": sum(1 for r in references if r["trigger_candidates"]),
        "norms_with_exceptions": sum(1 for r in references if r["exceptions"]),
        "norms_with_thresholds": sum(1 for r in references if r["thresholds"]),
        "flags": dict(Counter(f["flag"] for r in references for f in r["flags"])),
        "covered_provisions": len(provisions),
        "constitutive_only_provisions": sum(1 for p in provisions.values() if p["constitutive_only"]),
    }

    return {"source": structured.get("source"), "config": cfg, "summary": summary,
            "provisions": provisions, "reference_norms": references}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-i", "--input", default=DEFAULT_INPUT, help="output of dapreco_to_norms.py")
    ap.add_argument("-o", "--output", default=DEFAULT_OUTPUT)
    ap.add_argument("-c", "--config", default=DEFAULT_CONFIG)
    args = ap.parse_args(argv)

    if not os.path.exists(args.input):
        sys.exit(f"Input not found: {args.input}\nRun dapreco_to_norms.py first.")

    cfg = load_config(args.config)
    with open(args.input, encoding="utf-8") as f:
        structured = json.load(f)

    result = build_reference(structured, cfg)

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(json.dumps(result["summary"], indent=2), file=sys.stderr)
    print(f"Wrote {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()