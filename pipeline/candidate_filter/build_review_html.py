#!/usr/bin/env python3
"""build_review_html.py - HTML page (Dutch) for human review of LLM findings.

Selects from review_findings.json (llm_review.py output):
  - every DUPLICATION finding
  - one RELATED_NO_ISSUE and one UNRELATED finding (highest combined score, ties by
    pair_id), as reference points for the reviewer
and writes one self-contained HTML file. No LLM calls.

How the page avoids the A/B confusion
-------------------------------------
  The LLM saw the norms as A and B in a random order (shown_as); the scores refer to
  norm_a/norm_b (sorted IDs). The page uses neither. Each finding shows "Norm 1" and
  "Norm 2" in the order of `provisions`, labelled by citation (e.g. art. 13, lid 1 AVG).
  Every A/B field is mapped through shown_as, "norm A" / "norm B" in the reasoning is
  replaced by the citation, and duplication_scope / object_direction are spelled out
  ("art. 47, lid 1 AVG is enger dan art. 57, lid 1 AVG").

Review
------
  Each finding has a verdict (eens / oneens / twijfel) and a notes field. They are
  kept in the browser (localStorage) and can be downloaded as JSON with the button
  at the top. Nothing is sent anywhere.

Usage
-----
  python build_review_html.py
  python build_review_html.py --findings data/conflict/LLM_review/review_findings.json \
      --cards data/new_norms/LLM_norm_cards.jsonl --out data/conflict/LLM_review/review.html
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path
from typing import Optional

DEFAULT_FINDINGS = Path("data/conflict/LLM_review/review_findings.json")
DEFAULT_CARDS = Path("data/new_norms/LLM_norm_cards.jsonl")
DEFAULT_OUT = Path("data/conflict/LLM_review/review_duplications.html")

# Short Dutch names per instrument. Unknown ids are shown as the id.
INSTRUMENTS = {
    "32016R0679": "AVG",
    "32024R1689": "AI-verordening",
    "32022L2555": "NIS2-richtlijn",
    "32022R2554": "DORA",
    "32023R2854": "Dataverordening",
}

RELATION_NL = {
    "DUPLICATION": "Duplicatie", "CONTRADICTION": "Tegenstrijdigheid", "TENSION": "Spanning",
    "RULE_EXCEPTION": "Regel / uitzondering", "RELATED_NO_ISSUE": "Verwant, geen probleem",
    "UNRELATED": "Niet verwant", "INSUFFICIENT_EVIDENCE": "Onvoldoende informatie",
}
VALUE_NL = {
    "yes": "ja", "no": "nee", "partial": "gedeeltelijk", "unclear": "onduidelijk",
    "possible": "mogelijk", "possible_with_burden": "mogelijk, met extra last",
    "impossible": "onmogelijk", "not_applicable": "n.v.t.",
    "explicit_reference": "uitdrukkelijke verwijzing", "lex_superior": "lex superior",
    "lex_specialis": "lex specialis", "lex_posterior": "lex posterior", "none": "geen",
    "high": "hoog", "medium": "middel", "low": "laag",
    "identical": "identiek", "partial_overlap": "gedeeltelijke overlap", "same": "zelfde reikwijdte",
    "internal_coherence": "binnen één instrument", "cross_law": "tussen instrumenten",
    "candidate": "kandidaat", "no_issue": "geen probleem", "needs_recheck": "opnieuw controleren",
    "unverifiable": "niet te beoordelen", "not_reviewed": "niet beoordeeld",
    True: "ja", False: "nee",
}


def nl(value) -> str:
    if value is None or value == "":
        return "-"
    return VALUE_NL.get(value, str(value))


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------

def load_findings(path: Path) -> tuple[list[dict], dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return (list(data.get("findings", [])) + list(data.get("calibration", [])),
            data.get("meta", {}))


def load_cards(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return {str(c["norm_id"]): c for c in (json.loads(l) for l in f if l.strip())}


def select(findings: list[dict]) -> list[dict]:
    def order(f):
        return (-(f.get("scores", {}).get("combined") or 0), f["pair_id"])
    picked = sorted((f for f in findings if f.get("relation") == "DUPLICATION"), key=order)
    for rel in ("RELATED_NO_ISSUE", "UNRELATED"):
        same = sorted((f for f in findings if f.get("relation") == rel), key=order)
        if same:
            picked.append(same[0])
        else:
            print(f"Note: no {rel} finding in the file.", file=sys.stderr)
    return picked


# ---------------------------------------------------------------------------
# Citations and A/B mapping
# ---------------------------------------------------------------------------

_TEXT_RE = re.compile(r"<text[^>]*>(.*?)</text>", re.S)


def card_text(card: Optional[dict]) -> Optional[str]:
    if not card:
        return None
    m = _TEXT_RE.search(card.get("body", ""))
    return m.group(1).strip() if m else None


def article_paragraph(norm_id: str, card: Optional[dict]) -> tuple[str, Optional[str]]:
    """(article, paragraph) from the card's location line, else from the norm_id
    (instrument:article:paragraph:...)."""
    if card:
        loc = re.search(r"^location: (.*)$", card.get("body", ""), re.M)
        if loc:
            art = re.search(r"art\. ([^ |]+)", loc.group(1))
            par = re.search(r"par\. ([^ |]+)", loc.group(1))
            if art:
                p = par.group(1) if par else None
                return art.group(1), (None if p in (None, "whole", "None") else p)
    parts = norm_id.split(":")
    art = parts[1] if len(parts) > 1 else "?"
    par = parts[2] if len(parts) > 2 and parts[2] not in ("whole", "None", "") else None
    return art, par


def citation(norm_id: str, instrument_id: str, card: Optional[dict]) -> str:
    art, par = article_paragraph(norm_id, card)
    name = INSTRUMENTS.get(instrument_id, instrument_id)
    return f"art. {art}" + (f", lid {par}" if par else "") + f" {name}"


_NORM_REF = re.compile(r"\b[Nn]orm ([AB])\b")


def replace_refs(text: Optional[str], ab_to_cite: dict[str, str]) -> Optional[str]:
    """'norm A' / 'Norm B' -> citation; 'Art.' at the start of a sentence."""
    if not text:
        return text
    text = _NORM_REF.sub(lambda m: ab_to_cite.get(m.group(1), m.group(0)), text)
    return re.sub(r"(^|[.!?]\s+)art\.", r"\1Art.", text)


# ---------------------------------------------------------------------------
# One finding -> view model
# ---------------------------------------------------------------------------

PAIR_TEXT_FIELDS = ("reasoning_summary", "shared_situation", "resolution_basis",
                    "outcome_changing_fact")


def build_view(f: dict, cards: dict[str, dict]) -> dict:
    adj = f.get("llm_adjudication") or {}
    provs = f["provisions"]                               # display order = norm_a, norm_b
    cites = [citation(p["norm_id"], p["instrument_id"], cards.get(p["norm_id"])) for p in provs]
    ab_to_pos = {p["shown_as"]: i for i, p in enumerate(provs) if p.get("shown_as")}
    ab_to_cite = {ab: cites[i] for ab, i in ab_to_pos.items()}

    norms = []
    for i, p in enumerate(provs):
        s = (p.get("shown_as") or "").lower()
        norms.append({
            "norm_id": p["norm_id"], "cite": cites[i],
            "addressee": replace_refs(adj.get(f"addressee_{s}"), ab_to_cite) if s else None,
            "requirement": replace_refs(adj.get(f"requirement_{s}"), ab_to_cite) if s else None,
            "evidence": adj.get(f"evidence_{s}") if s else None,      # verbatim, untouched
            "text": card_text(cards.get(p["norm_id"])),
        })

    def narrower(value, pos):            # a_narrower / b_narrower -> spelled out
        if value in ("a_narrower", "b_narrower") and pos is not None:
            return f"{cites[pos]} is enger dan {cites[1 - pos]}"
        return nl(value)

    dup = adj.get("duplication_scope")
    direction = (f.get("scores") or {}).get("object_direction")
    sc = f.get("scores", {})
    return {
        "finding_id": f["finding_id"], "relation": f.get("relation"),
        "relation_nl": RELATION_NL.get(f.get("relation"), f.get("relation")),
        "status": f.get("status"), "scope": f.get("scope"),
        "norms": norms,
        "pair": {k: replace_refs(adj.get(k), ab_to_cite) for k in PAIR_TEXT_FIELDS},
        "facts": [
            ("Zelfde adressaat", nl(adj.get("same_addressee"))),
            ("Situaties overlappen", nl(adj.get("situations_overlap"))),
            ("Gezamenlijke naleving", nl(adj.get("joint_compliance"))),
            ("Reikwijdte duplicatie",
             narrower(dup, ab_to_pos.get(dup[0].upper()) if dup in ("a_narrower", "b_narrower") else None)),
            ("Opzettelijk", nl(adj.get("intentional"))),
            ("Oplossing", nl(adj.get("resolution"))),
            ("Zekerheid", nl(adj.get("certainty"))),
        ],
        "scores": [
            ("gecombineerd", sc.get("combined")), ("handeling", sc.get("action_relation")),
            ("objectsleutel", sc.get("object_key")), ("objectrelatie", sc.get("object_relation")),
            ("objectrichting", narrower(direction, {"a_narrower": 0, "b_narrower": 1}.get(direction))),
        ],
        "needs_recheck_reason": f.get("needs_recheck_reason"),
    }


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def esc(x) -> str:
    return html.escape("" if x is None else str(x))


def cell(text: Optional[str], quote: bool = False) -> str:
    if not text:
        return '<span class="muted">-</span>'
    return f'<div class="{"txt quote" if quote else "txt"}">{esc(text)}</div>'


def render_finding(v: dict) -> str:
    n1, n2 = v["norms"]
    rows = "".join(
        f"<tr><th>{label}</th><td>{cell(n1[k], k == 'evidence')}</td>"
        f"<td>{cell(n2[k], k == 'evidence')}</td></tr>"
        for label, k in (("Adressaat", "addressee"), ("Vereiste", "requirement"),
                         ("Geciteerd bewijs", "evidence")))
    if n1["text"] or n2["text"]:
        rows += (f'<tr><th>Tekst bepaling</th>'
                 f'<td><details><summary>Toon</summary>{cell(n1["text"])}</details></td>'
                 f'<td><details><summary>Toon</summary>{cell(n2["text"])}</details></td></tr>')
    facts = "".join(f"<div><dt>{esc(k)}</dt><dd>{esc(x)}</dd></div>" for k, x in v["facts"])
    pair = "".join(
        f"<h4>{label}</h4>{cell(v['pair'][k])}"
        for label, k in (("Redenering", "reasoning_summary"),
                         ("Gedeelde situatie", "shared_situation"),
                         ("Grondslag oplossing", "resolution_basis"),
                         ("Beslissend ontbrekend feit", "outcome_changing_fact"))
        if v["pair"][k])
    scores = " · ".join(f"{esc(k)}: {esc(x)}" for k, x in v["scores"] if x not in (None, "", "-"))
    recheck = (f'<p class="warn">Opnieuw controleren: {esc(v["needs_recheck_reason"])}</p>'
               if v["needs_recheck_reason"] else "")
    fid = esc(v["finding_id"])
    return f"""
<article class="finding rel-{esc(v['relation'])}" id="{fid}" data-id="{fid}">
  <header>
    <span class="badge">{esc(v['relation_nl'])}</span>
    <h3>{esc(n1['cite'])} &harr; {esc(n2['cite'])}</h3>
    <span class="muted mono">{fid} · {esc(nl(v['scope']))} · {esc(nl(v['status']))}</span>
  </header>
  {recheck}
  <div class="scroll"><table>
    <thead><tr><th></th>
      <th>Norm 1: {esc(n1['cite'])}<div class="muted mono">{esc(n1['norm_id'])}</div></th>
      <th>Norm 2: {esc(n2['cite'])}<div class="muted mono">{esc(n2['norm_id'])}</div></th></tr></thead>
    <tbody>{rows}</tbody>
  </table></div>
  <dl class="facts">{facts}</dl>
  {pair}
  <p class="muted small">Scores: {scores}</p>
  <fieldset class="review">
    <legend>Uw beoordeling</legend>
    <label><input type="radio" name="v-{fid}" value="eens"> Eens</label>
    <label><input type="radio" name="v-{fid}" value="oneens"> Oneens</label>
    <label><input type="radio" name="v-{fid}" value="twijfel"> Twijfel</label>
    <textarea placeholder="Opmerkingen (bijv. juiste relatie, waarom)" rows="2"></textarea>
  </fieldset>
</article>"""


CSS = """
:root{--bg:#fafaf8;--card:#fff;--fg:#1d1d1b;--muted:#6b6b66;--line:#e2e1dc;--accent:#2f5d8a;
--dup:#b35c00;--rel:#2e7d4f;--unrel:#777;--quote:#fff8e6;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--fg:#ecebe6;--muted:#a3a29b;
--line:#353531;--accent:#8db7e0;--dup:#f0a050;--rel:#6fc893;--unrel:#aaa;--quote:#2b2618;
color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1200px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:1.5rem;margin:0 0 4px}h3{margin:4px 0;font-size:1.1rem}h4{margin:16px 0 4px;font-size:.95rem}
.toolbar{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin:12px 0 24px}
button{font:inherit;padding:6px 12px;border:1px solid var(--line);border-radius:6px;background:var(--card);
color:var(--fg);cursor:pointer}nav a{color:var(--accent);margin-right:12px}
.finding{background:var(--card);border:1px solid var(--line);border-left:5px solid var(--unrel);
border-radius:8px;padding:16px;margin:0 0 24px}
.rel-DUPLICATION{border-left-color:var(--dup)}.rel-RELATED_NO_ISSUE{border-left-color:var(--rel)}
.badge{display:inline-block;font-size:.8rem;font-weight:600;padding:2px 8px;border-radius:99px;
border:1px solid currentColor}.rel-DUPLICATION .badge{color:var(--dup)}
.rel-RELATED_NO_ISSUE .badge{color:var(--rel)}.rel-UNRELATED .badge{color:var(--unrel)}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;min-width:640px;margin-top:12px}
th,td{border-top:1px solid var(--line);padding:8px;vertical-align:top;text-align:left}
tbody th{width:140px;color:var(--muted);font-weight:600}thead th{width:calc(50% - 70px)}
.txt{padding:6px 8px;border-radius:4px;white-space:pre-wrap}.quote{background:var(--quote);font-style:italic}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:8px;margin:16px 0}
.facts div{border:1px solid var(--line);border-radius:6px;padding:6px 8px}
dt{font-size:.75rem;color:var(--muted)}dd{margin:0;font-weight:600}
.muted{color:var(--muted)}.mono{font-family:ui-monospace,Consolas,monospace;font-size:.8rem}
.small{font-size:.8rem}.warn{color:#b00020;font-weight:600}
.review{border:1px solid var(--line);border-radius:6px;margin-top:12px;display:flex;gap:16px;
flex-wrap:wrap;align-items:center}.review textarea{flex:1 1 100%;font:inherit;padding:6px;
background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:4px}
details summary{cursor:pointer;color:var(--accent)}
"""

JS = """
const KEY = "norm-review:" + document.body.dataset.source;
let state = {};
try { state = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) { state = {}; }
function save() { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {} }
document.querySelectorAll(".finding").forEach(card => {
  const id = card.dataset.id, s = state[id] || {};
  card.querySelectorAll("input[type=radio]").forEach(r => {
    r.checked = s.verdict === r.value;
    r.addEventListener("change", () => { state[id] = {...state[id], verdict: r.value}; save(); count(); });
  });
  const t = card.querySelector("textarea");
  t.value = s.notes || "";
  t.addEventListener("input", () => { state[id] = {...state[id], notes: t.value}; save(); });
});
function count() {
  const n = Object.values(state).filter(s => s.verdict).length;
  document.getElementById("progress").textContent =
    n + " van " + document.querySelectorAll(".finding").length + " beoordeeld";
}
count();
document.getElementById("download").addEventListener("click", () => {
  const out = {source: document.body.dataset.source, exported: new Date().toISOString(), reviews: state};
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([JSON.stringify(out, null, 1)], {type: "application/json"}));
  a.download = "menselijke_beoordeling.json"; a.click();
});
"""

SECTIONS = (("DUPLICATION", "Duplicaties"),
            ("RELATED_NO_ISSUE", "Referentie: verwant, geen probleem"),
            ("UNRELATED", "Referentie: niet verwant"))


def render_page(views: list[dict], source: str, meta: dict) -> str:
    body, nav = [], []
    for rel, title in SECTIONS:
        items = [v for v in views if v["relation"] == rel]
        if items:
            body.append(f'<h2 id="sec-{rel}">{title} ({len(items)})</h2>'
                        + "".join(render_finding(v) for v in items))
            nav.append(f'<a href="#sec-{rel}">{title}</a>')
    return f"""<!doctype html>
<html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Beoordeling normparen</title><style>{CSS}</style></head>
<body data-source="{esc(source)}"><main>
<h1>Beoordeling normparen</h1>
<p class="muted">Bron: {esc(source)} · model {esc(meta.get('model'))} ·
prompt {esc(meta.get('prompt_version'))} · aangemaakt {esc(meta.get('created'))}</p>
<nav>{' '.join(nav)}</nav>
<div class="toolbar"><span id="progress"></span>
<button id="download">Beoordeling downloaden (JSON)</button></div>
{''.join(body)}
</main><script>{JS}</script></body></html>"""


# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="Dutch HTML review page for LLM findings.")
    p.add_argument("--findings", type=Path, default=DEFAULT_FINDINGS)
    p.add_argument("--cards", type=Path, default=DEFAULT_CARDS,
                   help="norm cards, for citations and provision text (optional)")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = p.parse_args()

    findings, meta = load_findings(args.findings)
    cards = load_cards(args.cards)
    if not cards:
        print(f"Note: no cards at {args.cards}; citations come from the norm_id and the "
              "provision text is left out.", file=sys.stderr)
    views = [build_view(f, cards) for f in select(findings)]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_page(views, args.findings.name, meta), encoding="utf-8")
    n_dup = sum(v["relation"] == "DUPLICATION" for v in views)
    print(f"{len(views)} findings ({n_dup} duplications) -> {args.out}")


if __name__ == "__main__":
    main()