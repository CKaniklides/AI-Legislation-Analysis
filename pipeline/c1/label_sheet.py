# -*- coding: utf-8 -*-
"""
Turns data/gold/label_sample_blind.json into a page a reviewer can fill in without any tooling, and
merges the result back.

    python label_sheet.py export                       # writes data/gold/label_sheet.html
    python label_sheet.py import path/to/labels.json   # validates and merges into label_sample_blind.json

The page holds only the two provision texts -- no model output -- so labels stay blind. The reviewer
opens it in a browser, picks a label per pair, and downloads labels.json.
"""
import json
import sys
from pathlib import Path
_HERE = Path(__file__).resolve().parent
_PIPELINE = _HERE.parent
for _d in (_PIPELINE, _PIPELINE / "c1", _PIPELINE / "preprocessing", _PIPELINE / "c2"):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
GOLD = Path(__file__).resolve().parent.parent.parent / "data" / "evaluation"
SAMPLE = GOLD / "label_sample_blind.json"

INSTRUMENT_NAMES = {
    "32016R0679": "GDPR", "32024R1689": "AI Act", "32022L2555": "NIS2 Directive", "32022R2554": "DORA",
    "BWBR0052872": "Cyberbeveiligingswet (Cbw)", "BWBR0040940": "UAVG", "BWBR0048156": "Wet digitale overheid (Wdo)",
    "BWBR0009950": "Telecommunicatiewet", "BWBR0051796": "Uitvoeringswet dataverordening", "BWBR0049497": "Bijlage 35",
}
LABEL_HELP = [
    ("DIRECT_CONFLICT", "Someone subject to both cannot obey both (must vs. must-not, incompatible deadlines or content)."),
    ("PERMISSION_CONFLICT", "One allows or requires what the other forbids, unless an exception applies, and the exception that decides it is not established by the texts shown."),
    ("DIVERGENT_STANDARD", "The same duty measured against different benchmarks, so it is genuinely unclear which governs or whether meeting one meets the other."),
    ("GOAL_TENSION", "Each can be obeyed, but obeying one works against the evident purpose of the other."),
    ("COMPATIBLE", "Both can be satisfied and the texts leave no real open question (e.g. the stricter rule simply satisfies the looser one)."),
    ("UNRELATED", "They only share vocabulary or topic."),
    ("CANNOT_TELL", "You cannot decide from these texts. Use this freely; a wrong confident label is worse than this."),
]

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Provision pair labelling</title>
<style>
body{font:15px/1.5 system-ui,sans-serif;max-width:900px;margin:0 auto;padding:16px;color:#1c1c1c;background:#fff}
h1{font-size:20px} .help{background:#f3f4f6;padding:10px 14px;border-radius:6px;margin:12px 0}
.help dt{font-weight:600;margin-top:6px} .help dd{margin:0 0 0 12px}
.card{border:1px solid #d0d0d0;border-radius:8px;padding:12px 14px;margin:16px 0}
.card.done{border-color:#3a8f4b}
.prov{background:#fafafa;border-left:4px solid #999;padding:8px 10px;margin:8px 0;white-space:pre-wrap}
.prov b{display:block;margin-bottom:4px} label{display:block;margin:2px 0;cursor:pointer}
textarea{width:100%;box-sizing:border-box;margin-top:6px;min-height:44px}
#bar{position:sticky;top:0;background:#fff;padding:8px 0;border-bottom:1px solid #ddd;z-index:2}
button{font-size:15px;padding:6px 14px;margin-left:8px} input[type=text]{font-size:15px;padding:4px}
@media (prefers-color-scheme:dark){body{background:#181818;color:#e6e6e6}.help{background:#252525}.prov{background:#202020}
.card{border-color:#444}#bar{background:#181818;border-color:#444}}
</style></head><body>
<h1>Which relation holds between these two provisions?</h1>
<p>For each pair choose <b>one</b> label, judging only from the two texts and your own knowledge. You have not been shown any computer output. Add a short reason when you choose a tension label (what realistic situation would cause the problem).</p>
<dl class="help" id="help"></dl>
<div id="bar">Your name: <input type="text" id="who" placeholder="reviewer name">
<span id="prog"></span><button id="dl">Download labels.json</button></div>
<div id="cards"></div>
<script>
const DATA=__DATA__;
const store={get(k){try{return localStorage.getItem(k)}catch(e){return null}},set(k,v){try{localStorage.setItem(k,v)}catch(e){}}};
const state=JSON.parse(store.get("labels")||"{}");
const help=document.getElementById("help");
DATA.help.forEach(([k,v])=>{const dt=document.createElement("dt");dt.textContent=k;const dd=document.createElement("dd");dd.textContent=v;help.append(dt,dd)});
const who=document.getElementById("who");who.value=store.get("who")||"";who.oninput=()=>store.set("who",who.value);
function prov(tag,p){const d=document.createElement("div");d.className="prov";const b=document.createElement("b");
 b.textContent=tag+": "+p.name+", art. "+p.article+(p.paragraph?"("+p.paragraph+")":"")+(p.heading?" \\u2014 "+p.heading:"");
 d.append(b,document.createTextNode(p.text));return d}
function refresh(){const n=Object.values(state).filter(s=>s.label).length;
 document.getElementById("prog").textContent="  "+n+" / "+DATA.items.length+" labelled";
 document.querySelectorAll(".card").forEach(c=>c.classList.toggle("done",!!(state[c.dataset.id]||{}).label))}
DATA.items.forEach(it=>{const c=document.createElement("div");c.className="card";c.dataset.id=it.sample_id;
 const h=document.createElement("b");h.textContent=it.sample_id;c.append(h,prov("A",it.a),prov("B",it.b));
 DATA.help.forEach(([k])=>{const l=document.createElement("label");const r=document.createElement("input");r.type="radio";r.name=it.sample_id;r.value=k;
  r.checked=(state[it.sample_id]||{}).label===k;r.onchange=()=>{(state[it.sample_id]=state[it.sample_id]||{}).label=k;store.set("labels",JSON.stringify(state));refresh()};
  l.append(r," "+k);c.append(l)});
 const t=document.createElement("textarea");t.placeholder="comment / reason (optional except for tension labels)";t.value=(state[it.sample_id]||{}).comment||"";
 t.oninput=()=>{(state[it.sample_id]=state[it.sample_id]||{}).comment=t.value;store.set("labels",JSON.stringify(state))};c.append(t);
 document.getElementById("cards").append(c)});
document.getElementById("dl").onclick=()=>{const out={labeler:who.value,labels:DATA.items.map(i=>({sample_id:i.sample_id,label:(state[i.sample_id]||{}).label||null,comment:(state[i.sample_id]||{}).comment||null}))};
 const a=document.createElement("a");a.href=URL.createObjectURL(new Blob([JSON.stringify(out,null,1)],{type:"application/json"}));a.download="labels.json";a.click()};
refresh();
</script></body></html>"""


def _prov(p):
    return {"name": INSTRUMENT_NAMES.get(p["instrument_id"], p["instrument_id"]), "article": p["article"],
            "paragraph": p.get("paragraph"), "heading": p.get("heading"), "text": p["text"]}


def export():
    items = json.loads(SAMPLE.read_text(encoding="utf-8"))["items"]
    data = {"help": LABEL_HELP, "items": [{"sample_id": i["sample_id"], "a": _prov(i["provision_a"]), "b": _prov(i["provision_b"])} for i in items]}
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    (GOLD / "label_sheet.html").write_text(PAGE.replace("__DATA__", blob), encoding="utf-8")
    print(f"wrote {GOLD / 'label_sheet.html'} ({len(items)} pairs, no model output included)")


def import_labels(path):
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))
    valid = {k for k, _ in LABEL_HELP}
    got = json.loads(Path(path).read_text(encoding="utf-8"))
    by_id = {i["sample_id"]: i for i in sample["items"]}
    n = 0
    for row in got["labels"]:
        if row["sample_id"] not in by_id:
            sys.exit(f"unknown sample_id {row['sample_id']}")
        if row["label"] is None:
            continue
        if row["label"] not in valid:
            sys.exit(f"invalid label {row['label']!r} for {row['sample_id']}")
        it = by_id[row["sample_id"]]
        it["label"], it["comment"], it["labeler"] = row["label"], row.get("comment"), got.get("labeler") or None
        n += 1
    SAMPLE.write_text(json.dumps(sample, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"merged {n} labels from {path}; run: python evaluate_c1_gold.py --set sample")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "export":
        export()
    elif len(sys.argv) == 3 and sys.argv[1] == "import":
        import_labels(sys.argv[2])
    else:
        sys.exit(__doc__)
