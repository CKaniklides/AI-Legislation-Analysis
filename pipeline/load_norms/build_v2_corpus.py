# -*- coding: utf-8 -*-
"""
Builds the second-generation corpus: the ten corpus files with their norms replaced by the
norms of the new extractor (pipeline/preprocessing/extract_norms.py, data/new_norms/), written
in the first-generation norm format that C1, C2, C4 and the interface read.

    python pipeline/load_norms/build_v2_corpus.py            # writes data/laws_v2/...
    python pipeline/load_norms/build_v2_corpus.py --check    # also compares with the old norms

Decided with the user on 23 October 2026: the whole project moves to the new norms. The
original corpus under data/laws/ is never written; every output goes to data/laws_v2/ under
the same relative path (Datasets/... for Bijlage 35), so switching back is one setting.

Two first-generation fields have no counterpart in the new extractor and are derived here,
each marked with its source so a reader can tell them from extracted values:
  addressee_type  from the actor's head word, via the candidate filter's head-word table
                  (actor_heads.tsv): party -> REGULATED_ENTITY, authority -> COMPETENT_AUTHORITY,
                  state -> MEMBER_STATE (EU) or COMPETENT_AUTHORITY (Dutch minister, gemeente),
                  institution -> EU_INSTITUTION (EU) or OTHER (Dutch court), person ->
                  DATA_SUBJECT for the people the law protects, a few named words by hand.
                  Unknown stays None, so C1's own sibling inference still applies.
  deference       the clause of the paragraph that makes the norm yield to or rest on another
                  provision ("in afwijking van", "onverminderd", "niet van toepassing indien",
                  "doet geen afbreuk aan", "overeenkomstig artikel", "op grond van artikel").
                  The new extractor deliberately leaves these out of every field.
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "pipeline" / "candidate_filter" / "actor"))
from actor_lexicon import head_class, term_head  # noqa: E402

# The merged copies that keep the extractor's bookkeeping (merge_norms.py --keep-bookkeeping):
# duplicates folded exactly as in the slim files the candidate filter reads, so a norm's
# position in its article, and therefore its candidate-filter id, is the same in both.
NEW = ROOT / "data" / "new_norms" / "merged_full"
V2 = ROOT / "data" / "laws_v2"

# corpus file (relative to ROOT) -> the new extractor's raw output for it
LAWS = {
    "data/laws/nis2_nl/BWBR0052872_2026-08-15_provisions.json": "new_Cbw (full)_merged_full.json",
    "data/laws/uitv_avg/BWBR0040940_2026-09-01_provisions.json": "new_UAVG (full)_merged_full.json",
    "data/laws/wdo/BWBR0048156_2025-11-11_provisions.json": "new_Wdo (full)_merged_full.json",
    "data/laws/telecom/BWBR0009950_2026-08-15_provisions.json": "new_Telecommunicatiewet (full)_merged_full.json",
    "data/laws/uitv_dataverordening/BWBR0051796_2025-11-21_provisions.json": "new_Uitvoeringswet dataverordening (full)_merged_full.json",
    "data/laws/gdpr/32016R0679_original_provisions.json": "new_GDPR (full)_merged_full.json",
    "data/laws/nis2/32022L2555_original_provisions.json": "new_NIS2 (full)_merged_full.json",
    "data/laws/dora/32022R2554_original_provisions.json": "new_DORA (full)_merged_full.json",
    "data/laws/ai_act/32024R1689_original_provisions.json": "new_AI Act (full, pre-Digital-Omnibus per ADR-0004)_merged_full.json",
    "Datasets/Dutch Laws/bijlage35_dataset.json": "new_Bijlage 35 (DORA competent-authority designation)_merged_full.json",
}

# Head words whose class depends on more than the table says.
_DATA_SUBJECT_HEADS = {"betrokkene", "betrokkenen", "consument", "consumenten", "burger", "burgers",
                       "eenieder", "iedereen", "belanghebbende", "belanghebbenden", "abonnee",
                       "abonnees", "eindgebruiker", "eindgebruikers", "werknemer", "werknemers"}
_EU_BODY = re.compile(r"\b(ai-bureau|comit[eé]|board|enisa|europe[se]|unie|commissie|edpb|cg)\b", re.I)
_STATE_EU = re.compile(r"\blidsta", re.I)
_INSTELLING = re.compile(r"instelling", re.I)      # kredietinstelling, betalingsinstelling: regulated
_NOTIFIED = re.compile(r"\baangemelde instantie", re.I)
_REPRESENTATIVE = re.compile(r"wettelijke? vertegenwoordiger", re.I)
_COURT = re.compile(r"\b(rechtbank|rechter|afdeling bestuursrechtspraak|raad van state|college van beroep)\b", re.I)
_AUTHORITY_WORD = re.compile(r"autoriteit|toezichthouder|csirt\b|csirt's|csirt’s|centraal contactpunt|centrale contactpunt", re.I)
_EU_NETWORK = re.compile(r"(netwerk|samenwerkingsgroep|groep)$", re.I)   # CSIRT-netwerk, samenwerkingsgroep

_DEFERENCE = re.compile(
    r"[^.;:]*\b(in afwijking van|onverminderd|niet van toepassing (?:indien|op|voor)|doet geen afbreuk aan"
    r"|overeenkomstig artikel|op grond van artikel|met inachtneming van)\b[^.;:]*", re.I)


# Laws whose duties bind government bodies as the regulated party: in the Wet digitale overheid
# a "bestuursorgaan" or "aangewezen organisatie" is the one that must secure its services and
# report breaches, not a supervisor. Typed as authorities, their duties never reached C1's
# dual-reporting check, which compares regulated parties (23 Oct 2026: 16 earlier questions on
# Wdo art. 18 were missing).
_GOVERNMENT_IS_REGULATED = {"BWBR0048156"}
_GOVERNMENT_BODY = re.compile(r"bestuursorgaan|bestuursorganen|aangewezen organisatie|overheidsorganisatie", re.I)


def addressee_type(actor: "str | None", instrument_id: str) -> "str | None":
    if not actor:
        return None
    if instrument_id in _GOVERNMENT_IS_REGULATED and _GOVERNMENT_BODY.search(actor):
        return "REGULATED_ENTITY"
    eu = not instrument_id.startswith("BWBR")
    head = term_head(actor.replace(",", " ")).lower().strip(".")
    words = [w.lower().strip(".,") for w in actor.split()]
    if _REPRESENTATIVE.search(actor):      # a person's legal representative, not a company
        return "OTHER"
    # Notified bodies are certified companies, not EU bodies (AI Act ch. III, sect. 4).
    if _NOTIFIED.search(actor):
        return "REGULATED_ENTITY"
    # A named authority anywhere in the phrase wins over the last word, and over "consument"
    # inside a name: "Autoriteit Consument en Markt", "de markttoezichtautoriteit van de
    # betrokken lidstaat".
    if any(head_class(w) == "authority" for w in words) or _AUTHORITY_WORD.search(actor):
        return "COMPETENT_AUTHORITY"
    if head in _DATA_SUBJECT_HEADS or any(w in _DATA_SUBJECT_HEADS for w in words[:4]):
        return "DATA_SUBJECT"
    if not eu and _COURT.search(actor):    # Dutch courts decide on supervision and appeals
        return "COMPETENT_AUTHORITY"
    if eu and _STATE_EU.search(head):
        return "MEMBER_STATE"
    if eu and (_EU_BODY.search(actor) or _EU_NETWORK.search(head)) and not _INSTELLING.search(head):
        return "EU_INSTITUTION"
    if eu and _STATE_EU.search(actor):
        return "MEMBER_STATE"
    cls = head_class(head)
    if cls is None:   # the head is not an actor word; try the other words, right to left
        for w in reversed(words):
            cls = head_class(w)
            if cls:
                break
    if cls == "party":
        return "REGULATED_ENTITY"
    if cls == "authority":
        return "COMPETENT_AUTHORITY"
    if cls == "state":
        return "MEMBER_STATE" if eu else "COMPETENT_AUTHORITY"
    if cls == "institution":
        return "EU_INSTITUTION" if eu else "OTHER"
    if cls == "body":
        return "REGULATED_ENTITY" if _INSTELLING.search(head) else ("EU_INSTITUTION" if eu else "COMPETENT_AUTHORITY")
    if cls == "person":
        return "DATA_SUBJECT" if head in _DATA_SUBJECT_HEADS else "REGULATED_ENTITY"
    if cls == "office":
        return "OTHER"
    return None


def deference(paragraph_text: str) -> "str | None":
    m = _DEFERENCE.search(paragraph_text or "")
    return m.group(0).strip() if m else None


def paragraph_text(p: dict, number: "str | None", index: "int | None") -> str:
    paras = p.get("paragraphs") or p.get("leden") or []
    if index is not None and 0 <= index < len(paras):
        return paras[index].get("text") or ""
    for q in paras:
        if str(q.get("number")) == str(number):
            return q.get("text") or ""
    return p.get("text") or ""


def verbatim_action(n: dict, text: str) -> "str | None":
    """The stretch of the paragraph from the first verb fragment to the end of the object (or the
    last verb fragment), as it literally stands. The first generation's `action` was such a quote,
    and the C1/C2 models quote `action` as evidence; a phrase assembled from the fragments
    ("tonen ... de overeenstemming aan") failed the verbatim check (23 Oct 2026: most of C2's
    needs_recheck findings)."""
    low = (text or "").lower()
    parts = [x for x in (n.get("action_span") or []) + ([n["object"]] if n.get("object") else []) if x]
    if not parts:
        return None
    start = low.find(parts[0].lower())
    if start < 0:
        return None
    end, pos = start + len(parts[0]), start
    for x in parts[1:]:
        i = low.find(x.lower(), pos)
        if i >= 0:
            end, pos = max(end, i + len(x)), i
    return text[start:end] if end - start <= 400 else None


def convert(n: dict, p: dict, instrument_id: str, article: str, ordinal: int) -> dict:
    action = " ".join(x for x in (" ".join(n.get("action_span") or []), n.get("object") or "") if x).strip()
    at = addressee_type(n.get("actor"), instrument_id)
    text = paragraph_text(p, n.get("number"), n.get("paragraph_index"))
    action = verbatim_action(n, text) or action
    out = {
        "number": n.get("number"), "paragraph_index": n.get("paragraph_index"),
        # norm_index: the k of the extractor's own id "33(1).k", minus one (first norm = 0)
        "norm_index": int(m.group(1)) - 1 if (m := re.search(r"\.(\d+)$", n.get("norm_id") or "")) else None,
        "norm_id": n.get("norm_id"),
        # the id the candidate filter gives this norm (load_all_norms.make_norm_id)
        "filter_norm_id": f"{instrument_id}:{article}:{n.get('number') if n.get('number') is not None else 'whole'}:{ordinal}",
        "deontic": n.get("deontic"),
        "addressee": n.get("actor"), "addressee_type": at,
        "addressee_type_source": "derived_from_actor" if at else None,
        "trigger_event": n.get("trigger_event"),
        "action": action or n.get("action_lemma"),
        "action_lemma": n.get("action_lemma"), "object": n.get("object"),
        "recipient_body": n.get("recipient") or [],
        "deadline": n.get("deadline"),
        "thresholds": n.get("thresholds") or [], "conditions": n.get("conditions") or [],
        "deference": deference(text), "deference_source": "derived_from_paragraph_text",
        "extraction_model": n.get("extraction_model"), "extraction_generation": 2,
        "human_verified": False,
    }
    for k in ("extraction_uncertain", "uncertainty_note", "alternative_reading", "deontic_alternative"):
        if n.get(k) is not None:
            out[k] = n[k]
    return out


def provisions_of(root):
    return root["bijlage_35"]["substantive_text"]["provisions"] if isinstance(root, dict) else root


def build(check: bool) -> None:
    for rel, new_name in LAWS.items():
        src = json.loads((ROOT / rel).read_text(encoding="utf-8"))
        new = json.loads((NEW / new_name).read_text(encoding="utf-8"))
        by_key = {(str(p.get("article") or p.get("number"))): p for p in provisions_of(new)}
        inst_default = "BWBR0049497" if "bijlage35" in rel else ("BWBR0051796" if "uitv_dataverordening" in rel else None)
        types, n_norms, n_def = Counter(), 0, 0
        for p in provisions_of(src):
            q = by_key.get(str(p.get("article") or p.get("number")))
            inst = p.get("instrument_id") or inst_default
            art = str(p.get("article") or p.get("number"))
            # The new extractor returns no norm for a definition (an empty list is its only way to
            # say "no norm"), but C1 (definitions given to the model as context, the definitional-
            # mismatch detector), the C4 fact sheet and the interface find a definitions article
            # by a DEFINITION norm. Those markers are carried over from the first generation;
            # they are not eligible for any comparison (not in ELIGIBLE_DEONTICS).
            markers = [{**n, "definition_marker": True, "extraction_generation": 1}
                       for n in p.get("norms") or [] if n.get("deontic") == "DEFINITION"]
            p["norms"] = ([convert(n, q, inst, art, i) for i, n in enumerate(q.get("norms") or [], start=1)]
                          if q else []) + markers
            n_norms += len(p["norms"])
            n_def += sum(1 for n in p["norms"] if n["deference"])
            types.update(n["addressee_type"] for n in p["norms"])
        out = V2 / Path(rel).relative_to("data/laws") if rel.startswith("data/laws/") else V2 / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".tmp")
        tmp.write_text(json.dumps(src, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(out)
        print(f"{rel.split('/')[-1][:44]:46} {n_norms:5} norms, {n_def:4} with deference, types {dict(types)}")
        if check:
            compare(rel, provisions_of(src))


def compare(rel: str, new_provs: list) -> None:
    """How often the derived addressee_type matches the first-generation one on the same paragraph."""
    try:
        old = provisions_of(json.loads((ROOT / rel).read_text(encoding="utf-8")))
    except Exception:
        return
    old_by = {}
    for p in old:
        for n in p.get("norms") or []:
            old_by.setdefault((str(p.get("article") or p.get("number")), str(n.get("number"))), set()).add(n.get("addressee_type"))
    same = total = 0
    for p in new_provs:
        for n in p["norms"]:
            ref = old_by.get((str(p.get("article") or p.get("number")), str(n.get("number"))))
            if ref and n["addressee_type"]:
                total += 1
                same += n["addressee_type"] in ref
    if total:
        print(f"    addressee_type agrees with the old extraction on {same}/{total} paragraphs ({100 * same // total}%)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="compare the derived addressee type with the old norms")
    build(ap.parse_args().check)
