import json
from rank_pairs import combine

base = r"data\conflicts_data"
deo = {(r["i"], r["j"]): r for r in json.load(open(rf"{base}\stageB_deontic_scores.json", encoding="utf-8"))["scores"]}
act = json.load(open(rf"{base}\stageB_action_scored.json", encoding="utf-8"))["scores"]
recs = json.load(open(rf"{base}\merged_norms.json", encoding="utf-8"))
recs = recs["norms"] if isinstance(recs, dict) else recs
norm = {r["norm_id"]: (r.get("norm") or r) for r in recs}

rows = []
for a in act:
    d = deo.get((a["i"], a["j"]), {})
    if d.get("rule_exception"):
        continue
    c = combine(0.5, d.get("deontic_overlap", .5), d.get("deontic_contradiction", .5),
                a["action_overlap"], a["action_contradiction"])
    rows.append({**a, **c, "da": d.get("deontic_a"), "db": d.get("deontic_b")})

def show(key, route, n=10):
    print(f"\n===== TOP {n} {key.upper()} =====")
    for r in sorted(rows, key=lambda r: -r[key])[:n]:
        na, nb = norm[r["norm_id_a"]], norm[r["norm_id_b"]]
        print(f"\n{r[key]:.3f}  [{r[route]}]  object: {r.get('object_state')} {r.get('object_shared_terms')}")
        print(f"  A {r['norm_id_a']}  {r['da']}  {na.get('addressee')}: {na.get('action')}")
        print(f"  B {r['norm_id_b']}  {r['db']}  {nb.get('addressee')}: {nb.get('action')}")

show("contradiction", "contradiction_route")
show("duplication", "duplication_route")