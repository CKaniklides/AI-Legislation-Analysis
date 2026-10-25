#!/usr/bin/env bash
# Runs the candidate filter on all ten laws (second-generation norms, 23 Oct 2026) and writes
# the review queue that C1 adjudicates. No language-model calls: every step is local (spaCy,
# sentence-transformers embeddings on the GPU when available). Output: data/conflict_all/.
#
#   bash pipeline/candidate_filter/run_all_laws.sh            # all steps
#   bash pipeline/candidate_filter/run_all_laws.sh actor      # resume from a step
#
# Steps: records, stagea, actor, object, combine, cards, select. Earlier runs (data/conflict/,
# data/conflict_nl/) are left untouched; the EuroVoc thesaurus files are copied from
# data/conflict_nl/object/eurovoc so nothing is downloaded again.
set -euo pipefail
cd "$(dirname "$0")/../.."
export PYTHONIOENCODING=utf-8
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"   # action_verbs imports pipeline.candidate_filter...
OUT=data/conflict_all
REC=$OUT/norms_all.json
CF=pipeline/candidate_filter
START=${1:-records}
mkdir -p "$OUT/stageA" "$OUT/actor" "$OUT/object/eurovoc" "$OUT/logs"

steps=(records stagea actor object combine cards select)
run=0
for s in "${steps[@]}"; do [ "$s" = "$START" ] && run=1; [ $run = 1 ] && todo+=("$s"); done

step() { echo; echo "=== $1 ($(date +%H:%M:%S))"; }

for s in "${todo[@]}"; do
  case $s in
  records)
    step "norm records (all ten laws)"
    python pipeline/load_norms/load_all_norms.py --out "$REC" 2>&1 | tail -2 ;;
  stagea)
    step "Stage A: same-article and similarity floor (embeddings)"
    python $CF/stageA_filtering.py --input "$REC" --out $OUT/stageA_pairs.json \
      --emb-cache $OUT/stageA/norm_embeddings.npz 2>&1 | tail -4 ;;
  actor)
    step "actors: clean, inventory (EU + Dutch definitions), map, relations, scores"
    python $CF/actor/actor_clean.py --dir $OUT/actor --records "$REC" 2>&1 | tail -2
    python $CF/actor/actor_lexicon.py --definitions data/definitions --out $OUT/actor/actor_inventory.jsonl 2>&1 | tail -2
    python $CF/actor/actor_map.py --dir $OUT/actor 2>&1 | tail -2
    python $CF/actor/actor_relations.py --dir $OUT/actor 2>&1 | tail -2
    python $CF/actor/actor_scores.py --data $OUT/actor --stage-a $OUT/stageA_pairs.json 2>&1 | tail -2 ;;
  object)
    step "objects: EuroVoc (thesaurus reused), clean, embed, map, keys, scores"
    cp -rn data/conflict_nl/object/eurovoc/. $OUT/object/eurovoc/
    python $CF/object/NL_eurovoc_prepare.py --out $OUT/object/eurovoc --skip-fetch 2>&1 | tail -2
    python $CF/object/NL_eurovoc_clean.py --dir $OUT/object --records "$REC" 2>&1 | tail -2
    python $CF/object/NL_eurovoc_embeds.py --dir $OUT/object/eurovoc --units $OUT/object/norm_object_units.jsonl 2>&1 | tail -2
    python $CF/object/NL_eurovoc_maps.py --dir $OUT/object/eurovoc --units $OUT/object/norm_object_units.jsonl 2>&1 | tail -2
    # The mapping step writes into eurovoc/, the keys step reads from object/.
    cp $OUT/object/eurovoc/object_mapping.jsonl $OUT/object/eurovoc/norm_object_mapping.jsonl $OUT/object/
    python $CF/object/eurovoc_object_keys.py --dir $OUT/object 2>&1 | tail -2
    python $CF/object/object_scores.py --data $OUT/object --stage-a $OUT/stageA_pairs.json 2>&1 | tail -2 ;;
  combine)
    step "combine action, object and actor scores"
    python $CF/combine_scores.py --objects $OUT/object/object_pair_scores.jsonl --norms "$REC" \
      --actors $OUT/actor/actor_pair_scores.jsonl --stage-a $OUT/stageA_pairs.json \
      --out $OUT/combined_pair_scores.jsonl 2>&1 | tail -6 ;;
  cards)
    step "norm cards"
    python $CF/create_LLM_norms.py --norms "$REC" -d data/definitions/*_definitions.json \
      --out $OUT/LLM_norm_cards.jsonl 2>&1 | tail -2 ;;
  select)
    step "select the review queue (budget 2,000 + calibration sample; no model calls)"
    python $CF/select_LLM_candidates.py --scores $OUT/combined_pair_scores.jsonl \
      --cards $OUT/LLM_norm_cards.jsonl --out $OUT/review_queue.jsonl 2>&1 | tail -8 ;;
  esac
done
echo; echo "done ($(date +%H:%M:%S))"
