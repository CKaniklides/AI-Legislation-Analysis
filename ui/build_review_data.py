# -*- coding: utf-8 -*-
"""
Builds ui/data/review_data.json: everything the review interface shows, in one file.

    python ui/build_review_data.py

Reads the grouped findings (C1 contradiction, C2 duplication), attaches the legal text of
each provision, and rewrites each detector's own output into ONE common shape, so every
finding has the same four sections: provisions, side by side, example situation, reasoning.

Legal text is always the original Dutch. Everything else is stored in Dutch and English
({"nl": ..., "en": ...}). Explanations come from ui/enrich_explanations.py when it has been
run; until then the original text is used for both languages and missing sections stay
empty. Only status == "candidate" findings are included.
"""
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
for _d in ("pipeline", "pipeline/c1", "pipeline/preprocessing", "pipeline/c2"):
    sys.path.insert(0, str(ROOT / _d))

import detect_c1_contradiction as c1  # noqa: E402

DATA = ROOT / "data"
OUT = ROOT / "ui" / "data" / "review_data.json"
ENRICH_CACHE = DATA / "cache" / "ui_explanations_cache.json"
NBSP = " "


def bi(nl, en):
    return {"nl": nl, "en": en}


LAWS = {
    "32016R0679": bi("AVG", "GDPR"),
    "32024R1689": bi("AI-verordening", "AI Act"),
    "32022L2555": bi("NIS2-richtlijn", "NIS2 Directive"),
    "32022R2554": bi("DORA", "DORA"),
    "BWBR0052872": bi("Cyberbeveiligingswet", "Cyberbeveiligingswet"),
    "BWBR0040940": bi("UAVG", "UAVG"),
    "BWBR0048156": bi("Wet digitale overheid", "Wet digitale overheid"),
    "BWBR0009950": bi("Telecommunicatiewet", "Telecommunicatiewet"),
    "BWBR0051796": bi("Uitvoeringswet dataverordening", "Uitvoeringswet dataverordening"),
    "BWBR0049497": bi("Besluit EU-verordeningen Wft, bijlage 35", "Besluit EU-verordeningen Wft, bijlage 35"),
}

SUBCATEGORIES = {
    "contradiction": [
        ("dual_reporting_tension", bi("Dubbele melding", "Double reporting"),
         bi("Eén incident moet bij twee autoriteiten of binnen twee verschillende termijnen worden gemeld.",
            "One incident must be reported to two authorities, or on two different deadlines.")),
        ("definitional_mismatch", bi("Verschillende definities", "Different definitions"),
         bi("Twee wetten definiëren hetzelfde begrip verschillend, waardoor dezelfde organisatie of gebeurtenis onder de ene wel en onder de andere niet valt.",
            "Two laws define the same term differently, so the same organisation or event can fall inside one and outside the other.")),
        ("sensitive_data_tension", bi("Gevoelige persoonsgegevens", "Sensitive personal data"),
         bi("De ene wet beperkt de verwerking van bijzondere categorieën gegevens en de andere staat die toe of vereist die.",
            "One law restricts processing of special-category data and another permits or requires it.")),
        ("permission_prohibition_conflict", bi("Toestemming tegenover verbod", "Permission against prohibition"),
         bi("Een rechtsgrond in de ene wet lijkt iets toe te staan dat een andere wet verbiedt.",
            "A legal basis in one law appears to allow something another law prohibits.")),
        ("competence_competition", bi("Overlappende bevoegdheden", "Overlapping powers"),
         bi("Twee autoriteiten krijgen dezelfde of een sterk vergelijkbare bevoegdheid.",
            "Two authorities are given the same or a very similar power.")),
        ("standard_collision", bi("Verschillende termijnen", "Different deadlines"),
         bi("Dezelfde plicht kent in twee bepalingen een andere termijn.",
            "The same duty carries a different deadline in two provisions.")),
        ("classification_mismatch", bi("Verschillende risico-indeling", "Different risk classification"),
         bi("Twee wetten gebruiken verschillende toetsen om te bepalen wanneer extra waarborgen gelden.",
            "Two laws use different tests to decide when extra safeguards apply.")),
        ("legal_tension", bi("Overige juridische spanningen", "Other legal tensions"),
         bi("Beide bepalingen kunnen worden nageleefd, maar hun samenloop laat een juridische vraag open.",
            "Both provisions can be obeyed, but their interaction leaves an open legal question.")),
    ],
    "duplication": [
        ("duplicate", bi("Dezelfde eis twee keer", "Same requirement twice"),
         bi("Twee wetten stellen in wezen dezelfde eis aan hetzelfde soort organisatie.",
            "Two laws impose substantially the same requirement on the same kind of organisation.")),
        ("related", bi("Nauw verwante eisen", "Closely related requirements"),
         bi("Twee wetten stellen vergelijkbare eisen die verschillen in ontvanger, termijn of inhoud.",
            "Two laws impose similar requirements that differ in recipient, deadline or content.")),
    ],
}

PRIORITY_LABELS = {
    1: bi("Berekend uit de tekst, zonder AI-oordeel", "Calculated from the text, no AI judgement"),
    2: bi("Twee onafhankelijke AI-controles waren het eens", "Two independent AI checks agreed"),
    3: bi("Eén AI-controle", "One AI check"),
    4: bi("De AI-controles waren het oneens", "The AI checks disagreed"),
    5: bi("Berust op tekst die inmiddels is gewijzigd", "Rests on text that has since been amended"),
}

NOT_STATED = bi("niet vermeld", "not stated")
NOT_APPLICABLE = bi("niet van toepassing", "not applicable")
ROW = {
    "law": bi("Wet en bepaling", "Law and provision"),
    "type": bi("Soort regel", "Type of rule"),
    "who": bi("Geldt voor", "Applies to"),
    "recipient": bi("Wie moet worden ingelicht", "Who must be told"),
    "deadline": bi("Termijn", "Deadline"),
}
DEONTIC = {
    "OBLIGATION": bi("Verplichting", "Obligation"), "PROHIBITION": bi("Verbod", "Prohibition"),
    "COMPETENCE": bi("Bevoegdheid", "Power"), "PERMISSION": bi("Toestemming", "Permission"),
    "DEFINITION": bi("Definitie", "Definition"),
}
ADDRESSEE = {
    "REGULATED_ENTITY": bi("Gereguleerde organisatie", "Regulated organisation"),
    "COMPETENT_AUTHORITY": bi("Bevoegde autoriteit", "Competent authority"),
    "MEMBER_STATE": bi("Lidstaat", "Member State"), "DATA_SUBJECT": bi("Betrokkene", "Data subject"),
    "EU_INSTITUTION": bi("EU-instelling", "EU institution"),
}
NOTE_LABEL = {
    "practical_consequence": bi("Praktisch gevolg", "Practical consequence"),
    "permitted": bi("Wat de rechtsgrond toestaat", "What the legal basis permits"),
    "restricted": bi("Wat de andere wet beperkt", "What the other law restricts"),
    "open_question": bi("Wat de vraag zou beslechten", "What would settle it"),
    "recipient_relation": bi("Ontvanger", "Recipient"),
    "deadline_relation": bi("Termijn", "Deadline"),
    "content_overlap": bi("Overlap in inhoud", "Overlap in content"),
}
ENUM = {
    "same": bi("gelijk", "same"), "different": bi("verschillend", "different"),
    "same_authority": bi("dezelfde autoriteit", "same authority"),
    "different_authority_same_role": bi("andere autoriteit, zelfde rol", "different authority, same role"),
    "different_authority_different_role": bi("andere autoriteit, andere rol", "different authority, different role"),
    "different_role": bi("andere rol", "different role"), "unclear": bi("onduidelijk", "unclear"),
    "not_applicable": NOT_APPLICABLE, "none": bi("geen", "none"),
    "low": bi("laag", "low"), "medium": bi("middel", "medium"), "high": bi("hoog", "high"),
    "a_stricter": bi("A is strenger", "A is stricter"), "b_stricter": bi("B is strenger", "B is stricter"),
    # The four duplication values that had no Dutch yet (the English stays as it was shown).
    "same_recipient": bi("dezelfde ontvanger", "same recipient"),
    "customer_or_data_subject_vs_authority": bi("klant of betrokkene tegenover autoriteit",
                                                "customer or data subject vs authority"),
    "unrelated": bi("geen verband", "unrelated"),
    "only_one_specified": bi("slechts in één bepaling genoemd", "only one specified"),
}


def _load_law_texts():
    """(instrument, article) -> provision dict, plus norm records per paragraph."""
    by_article, by_para = {}, {}
    # Mirrors load_all_norm_records(): some sources are not plain lists and some
    # provisions carry no instrument_id of their own.
    for rel, get_provisions in c1.SOURCES:
        path = ROOT / rel
        if not path.exists():
            continue
        root = json.loads(path.read_text(encoding="utf-8"))
        for p in get_provisions(root):
            if "instrument_id" in p:
                inst = p["instrument_id"]
            elif "uitvoeringswet" in rel.lower():
                inst = "BWBR0051796"
            elif "bijlage35" in rel.lower():
                inst = "BWBR0049497"
                # The items of Bijlage 35 are parts of one annex, not articles with an address of their
                # own: they link to the annex, at the version analysed (the dataset's own permalink).
                p = {**p, "url": root["bijlage_35"].get("permalink_current_version")}
            else:
                continue
            by_article[(inst, str(p.get("article") or p.get("number")))] = p
    for r in c1.load_all_norm_records():
        by_para.setdefault((r.instrument_id, str(r.article), str(r.norm.get("number"))), []).append(r)
    return by_article, by_para


BY_ARTICLE, BY_PARA = _load_law_texts()
DEFS = c1.load_definitions_by_instrument()


def _norm_for(p: dict):
    """The extracted norm behind a provision reference, or None."""
    recs = BY_PARA.get((p["instrument_id"], str(p["article"]), str(p.get("paragraph_number")))) or []
    for r in recs:
        if (r.norm.get("paragraph_index") == p.get("paragraph_index")
                and r.norm.get("norm_index") == p.get("norm_index")):
            return r.norm
    return recs[0].norm if recs else None


def official_url(inst: str, article: str, prov: dict) -> "str | None":
    """Where a reader opens the official text of an article, at the version the system analysed:
    wetten.overheid.nl through the article's JCI reference, or EUR-Lex at the article's anchor
    (#art_33). A provision that carries its own address (the items of Bijlage 35) uses that."""
    if prov.get("url"):
        return prov["url"]
    if prov.get("jci"):
        return f"https://wetten.overheid.nl/{prov['jci']}"
    if not inst.startswith("BWBR"):
        return f"https://eur-lex.europa.eu/legal-content/NL/TXT/HTML/?uri=CELEX:{inst}#art_{article}"
    return None


def provision_view(p: dict, evidence):
    inst, art = p["instrument_id"], str(p["article"])
    num, idx = p.get("paragraph_number"), p.get("paragraph_index")
    prov = BY_ARTICLE.get((inst, art)) or {}
    heading = prov.get("heading")
    text, kind = None, "paragraph"

    if p.get("defined_term"):
        kind = "definition"
        text = (DEFS.get(inst) or {}).get(p["defined_term"].lower()) or evidence
    elif p.get("legal_basis"):
        kind = "legal_basis"
        text = evidence
    else:
        paras = prov.get("paragraphs") or []
        if idx is not None and 0 <= idx < len(paras) and str(paras[idx].get("number")) in (str(num), "None"):
            text = paras[idx].get("text")
        if text is None and num is not None:
            text = next((x.get("text") for x in paras if str(x.get("number")) == str(num)), None)
        if text is None:
            recs = BY_PARA.get((inst, art, str(num)))
            if recs:
                text, heading = recs[0].text, heading or recs[0].heading
        if text is None:
            text = evidence or prov.get("text")

    ref = f"art.{NBSP}{art}"
    if p.get("legal_basis"):
        ref += f"(1)({p['legal_basis']})"
    elif num not in (None, "None"):
        ref += f"({num})"
    return {
        "law": LAWS.get(inst, bi(inst, inst)), "law_id": inst, "article": art, "ref": ref,
        "heading": heading, "kind": kind, "term": p.get("defined_term"),
        "text": (text or "").strip(), "key_passage": (evidence or "").strip() or None,
        "url": official_url(inst, art, prov),
    }


def side_by_side(f: dict, va: dict, vb: dict) -> list:
    """The same five rows for every finding, whatever detector produced it."""
    det = f.get("deterministic_result") or {}
    rd = f.get("reporting_divergence") or {}
    rows = {k: [None, None] for k in ("type", "who", "recipient", "deadline")}
    for i, (p, v) in enumerate(((f["provisions"][0], va), (f["provisions"][1], vb))):
        if v["kind"] == "definition":
            rows["type"][i] = DEONTIC["DEFINITION"]
            rows["who"][i] = rows["recipient"][i] = rows["deadline"][i] = NOT_APPLICABLE
            continue
        if v["kind"] == "legal_basis":
            rows["type"][i] = bi("Rechtsgrond (toestemming)", "Legal basis (permission)")
            rows["who"][i] = bi("Verwerkingsverantwoordelijke", "Controller")
            rows["recipient"][i] = rows["deadline"][i] = NOT_APPLICABLE
            continue
        n = _norm_for(p) or {}
        rows["type"][i] = DEONTIC.get(n.get("deontic"), NOT_STATED)
        rows["who"][i] = n.get("addressee") or ADDRESSEE.get(n.get("addressee_type"), NOT_STATED)
        rows["recipient"][i] = "; ".join(n.get("recipient_body") or []) or NOT_STATED
        rows["deadline"][i] = (n.get("deadline") or {}).get("raw") or NOT_STATED
    # A detector's own measured values win over the generic norm fields.
    for i, key in enumerate(("recipient_a", "recipient_b")):
        if det.get(key):
            rows["recipient"][i] = "; ".join(det[key])
    for i, key in enumerate(("deadline_a_hours", "deadline_b_hours")):
        if det.get(key) is not None:
            rows["deadline"][i] = bi(f"{det[key]} uur", f"{det[key]} hours")
    for i in (0, 1):
        if (rd.get("recipients") or [[], []])[i]:
            rows["recipient"][i] = "; ".join(rd["recipients"][i])
        if (rd.get("deadlines") or [None, None])[i]:
            rows["deadline"][i] = rd["deadlines"][i]

    def law(v):
        return {k: f"{v['law'][k]} {v['ref']}" for k in ("nl", "en")}

    out = [{"label": ROW["law"], "a": law(va), "b": law(vb), "compare": False}]
    out += [{"label": ROW[k], "a": rows[k][0], "b": rows[k][1], "compare": True}
            for k in ("type", "who", "recipient", "deadline")]
    return out


def raw_explanation(f: dict, category: str) -> dict:
    """The detector's own explanation, in whatever language it was written, in one shape.
    Free-text fields only: this is what ui/enrich_explanations.py translates and completes."""
    llm = f.get("llm_adjudication") or {}
    det = f.get("deterministic_result") or {}
    pa, pb = f["provisions"][0], f["provisions"][1]
    st = f.get("subtype")
    x = {"summary": None, "scenario": llm.get("concrete_scenario"), "case_for": None, "case_against": None, "notes": {}}
    if category == "duplication":
        x["summary"] = llm.get("shared_compliance_action")
        x["case_for"] = llm.get("criterion_fired") or llm.get("shared_compliance_action")
        x["case_against"] = llm.get("distinguishing_factor")
    elif st == "definitional_mismatch":
        x["summary"] = f"‘{pa.get('defined_term')}’ / ‘{pb.get('defined_term')}’"
        x["case_for"] = llm.get("mismatch_description")
        x["notes"]["practical_consequence"] = llm.get("practical_consequence")
    elif st == "permission_prohibition_conflict":
        x["summary"] = llm.get("restricted_act")
        x["case_for"] = llm.get("strongest_case_for_conflict")
        x["case_against"] = llm.get("strongest_case_for_no_conflict")
        x["notes"]["permitted"] = llm.get("permitted_processing")
        x["notes"]["restricted"] = llm.get("restricted_act")
    elif st == "competence_competition":
        x["summary"] = "Two provisions give a similar power to different authorities."
        x["case_for"] = ("Both provisions grant a similar power, but to differently named authorities: "
                         f"‘{'; '.join(det.get('recipient_a') or [])}’ and "
                         f"‘{'; '.join(det.get('recipient_b') or [])}’.")
    elif st == "standard_collision":
        x["summary"] = "The same duty carries two different deadlines."
        x["case_for"] = (f"The same duty has a deadline of {det.get('deadline_a_hours')} hours in provision A "
                         f"and {det.get('deadline_b_hours')} hours in provision B.")
        x["case_against"] = det.get("compliance_note")
    else:
        x["summary"] = llm.get("shared_subject")
        x["case_for"] = llm.get("strongest_case_for_tension")
        x["case_against"] = llm.get("strongest_case_for_compatibility")
        x["notes"]["open_question"] = llm.get("decisive_exception")
    x["notes"] = {k: v for k, v in x["notes"].items() if v}
    return x


# The fields raw_explanation() writes itself, from a fixed English sentence (keep in step with it).
# They are filed as English without guessing: the guess counted the Dutch authority names these
# sentences quote and filed three of them as Dutch, so the Dutch view showed English.
_FIXED_ENGLISH = {
    "competence_competition": {"summary", "case_for"},
    "standard_collision": {"summary", "case_for", "case_against"},
}


def explanation_key(finding_id: str, raw: dict) -> str:
    h = hashlib.sha1(json.dumps(raw, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    return f"{finding_id}::{h}"


# Only words that exist in ONE of the two languages. "in", "is" and "of" are common in both
# Dutch and English, and counting them as English filed about 100 Dutch originals as English.
_NL_WORDS = re.compile(r"\b(de|het|een|van|niet|voor|zijn|wordt|bij|ook|naar|dat|deze|dit|maar|kan|aan|op|en|als)\b", re.I)
_EN_WORDS = re.compile(r"\b(the|and|that|not|for|with|are|which|this|from|by|be|can|but|it|an|as|or|both)\b", re.I)


def _language(text: str) -> str:
    """'nl' or 'en' for a detector-written explanation, by counting common function words."""
    return "nl" if len(_NL_WORDS.findall(text)) > len(_EN_WORDS.findall(text)) else "en"


_ENRICHED = json.loads(ENRICH_CACHE.read_text(encoding="utf-8")) if ENRICH_CACHE.exists() else {}


def instance_view(f: dict, category: str) -> dict:
    llm = f.get("llm_adjudication") or {}
    va = provision_view(f["provisions"][0], llm.get("evidence_span_a"))
    vb = provision_view(f["provisions"][1], llm.get("evidence_span_b"))
    raw = raw_explanation(f, category)
    e = _ENRICHED.get(explanation_key(f["finding_id"], raw))
    fixed_english = set() if category == "duplication" else _FIXED_ENGLISH.get(f.get("subtype"), set())

    def keep_original(translated, original, language=None):
        # The detector's own wording is kept verbatim in the language it was written in;
        # the model's text is used only for the OTHER language. A translation pass must
        # not be able to quietly reword the original.
        if not original:
            return translated
        out = dict(translated)
        out[language or _language(original)] = original
        return out

    def both(field):
        if e and e.get(field):
            return keep_original(e[field], raw.get(field), "en" if field in fixed_english else None)
        return bi(raw[field], raw[field]) if raw.get(field) else None

    notes = []
    for key, value in raw["notes"].items():
        tv = ((e or {}).get("notes") or {}).get(key)
        tv = keep_original(tv, value) if tv else bi(value, value)
        notes.append({"label": NOTE_LABEL[key], "value": tv})
    if category == "duplication":
        for key in ("recipient_relation", "deadline_relation", "content_overlap"):
            if llm.get(key):
                plain = str(llm[key]).replace("_", " ")
                notes.append({"label": NOTE_LABEL[key], "value": ENUM.get(str(llm[key]), bi(plain, plain))})
    return {
        "id": f["finding_id"], "a": va, "b": vb,
        "summary": both("summary"), "scenario": both("scenario"),
        "case_for": both("case_for"), "case_against": both("case_against"),
        "facts": side_by_side(f, va, vb), "notes": notes,
        "translated": bool(e),
        "priority": f.get("review_priority", 3),
        "currency_warning": bool(f.get("currency_warning")),
    }


def question_view(group: dict, category: str, subcategory: str, members: list) -> dict:
    ref_a, ref_b = group["article_pair"]
    (ia, aa), (ib, ab) = ref_a.split(":", 1), ref_b.split(":", 1)
    qid = hashlib.sha1(f"{category}|{subcategory}|{ref_a}|{ref_b}".encode()).hexdigest()[:10]
    instances = sorted((instance_view(m, category) for m in members), key=lambda i: i["priority"])
    priority = min(i["priority"] for i in instances)
    la, lb = LAWS.get(ia, bi(ia, ia)), LAWS.get(ib, bi(ib, ib))
    return {
        "id": f"{'c1' if category == 'contradiction' else 'c2'}-{qid}",
        "category": category, "subcategory": subcategory,
        "title": bi(f"{la['nl']} art.{NBSP}{aa} en {lb['nl']} art.{NBSP}{ab}",
                    f"{la['en']} art.{NBSP}{aa} and {lb['en']} art.{NBSP}{ab}"),
        "summary": instances[0].get("summary"),
        "priority": priority, "priority_label": PRIORITY_LABELS.get(priority),
        "amended": any(i["currency_warning"] for i in instances),
        "instances": instances,
    }


def candidate_findings():
    """Yields (category, subcategory, group, members) for everything the interface shows."""
    g1 = json.loads((DATA / "results" / "c1" / "findings_c1_by_article.json").read_text(encoding="utf-8"))["article_pairs"]
    for g in g1:
        yield "contradiction", g["subtype"], g, g["findings"]
    g2 = json.loads((DATA / "results" / "c2" / "findings_c2_by_article.json").read_text(encoding="utf-8"))["article_pairs"]
    for g in g2:
        for sub, verdict in (("duplicate", "DUPLICATE_BURDEN"), ("related", "RELATED_BURDEN")):
            members = [m for m in g["findings"] if m.get("status") == "candidate"
                       and (m.get("llm_adjudication") or {}).get("duplicate_burden_verdict") == verdict]
            if members:
                yield "duplication", sub, g, members


def build() -> dict:
    qs = {"contradiction": [], "duplication": []}
    for category, sub, g, members in candidate_findings():
        qs[category].append(question_view(g, category, sub, members))

    def cat(cid, title, summary):
        questions = qs[cid]
        order = [s[0] for s in SUBCATEGORIES[cid]]
        questions.sort(key=lambda q: (order.index(q["subcategory"]) if q["subcategory"] in order else 99,
                                      q["priority"], -len(q["instances"])))
        subs = [{"id": s, "title": t, "description": d, "count": sum(1 for q in questions if q["subcategory"] == s)}
                for s, t, d in SUBCATEGORIES[cid]]
        return {"id": cid, "title": title, "summary": summary, "live": True,
                "subcategories": [s for s in subs if s["count"]], "questions": questions}

    cats = [
        cat("contradiction", bi("Tegenstrijdigheden en spanningen", "Contradictions and tensions"),
            bi("Plaatsen waar twee bepalingen een andere kant op wijzen.", "Places where two provisions pull in different directions.")),
        cat("duplication", bi("Dubbelingen", "Duplications"),
            bi("Plaatsen waar twee wetten twee keer hetzelfde vragen.", "Places where two laws ask for the same thing twice.")),
        {"id": "outdatedness", "live": False, "subcategories": [], "questions": [],
         "title": bi("Verouderde bepalingen", "Outdated provisions"),
         "summary": bi("Bepalingen die niet meer worden gebruikt of hun rechtsgrond hebben verloren.",
                       "Provisions that are no longer used or have lost their legal basis.")},
        {"id": "simplification", "live": False, "subcategories": [], "questions": [],
         "title": bi("Vereenvoudiging", "Simplification"),
         "summary": bi("Bepalingen die moeilijker te lezen zijn dan nodig.", "Provisions that are harder to read than they need to be.")},
    ]
    return {"generated_on": date.today().isoformat(), "categories": cats}


if __name__ == "__main__":
    out = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    c1.atomic_write_json(OUT, out)
    for c in out["categories"]:
        inst = [i for q in c["questions"] for i in q["instances"]]
        if not inst:
            continue
        tr = sum(1 for i in inst if i["translated"])
        gaps = sum(1 for i in inst for k in ("scenario", "case_for", "case_against") if not i[k])
        print(f"{c['id']:14s} questions={len(c['questions']):3d} findings={len(inst):3d} translated={tr:3d} empty_sections={gaps}")
    print(f"wrote {OUT.relative_to(ROOT)}")
