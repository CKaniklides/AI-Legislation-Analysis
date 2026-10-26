# -*- coding: utf-8 -*-
"""
Makes every finding's explanation available in Dutch AND English, and fills the sections a
detector did not write, so all findings show the same sections in the interface.

    python ui/enrich_explanations.py --dry-run     # how many calls, no spending
    python ui/enrich_explanations.py --limit 10    # a small trial
    python ui/enrich_explanations.py               # everything not yet done
    python ui/build_review_data.py                 # then rebuild the interface data

One call per finding. The detectors wrote their explanations in a mix of Dutch and English
(about two thirds English), and some never wrote an example situation or a counter-argument
(deterministic findings, definitions, all duplication findings). The model is asked to:
  - translate what already exists, faithfully, into both languages;
  - write ONLY the sections that are empty, grounded in the two provision texts shown.
It reports which sections it wrote itself (`written`), so translated detector output and
newly written text can always be told apart.

Legal text is never translated and never sent back: the interface always shows the original.
Results are cached per finding in data/cache/ui_explanations_cache.json (atomic writes), so
an interrupted run loses nothing and a re-run only pays for what is new.
"""
import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ui"))

import build_review_data as b  # noqa: E402  (also puts the pipeline on sys.path)
import detect_c1_contradiction as c1  # noqa: E402

CACHE = b.ENRICH_CACHE
MODEL = c1.DEFAULT_MODEL
FIELDS = ("summary", "scenario", "case_for", "case_against")


class Bi(BaseModel):
    nl: str
    en: str


class Notes(BaseModel):
    practical_consequence: Optional[Bi]
    permitted: Optional[Bi]
    restricted: Optional[Bi]
    open_question: Optional[Bi]


class Enriched(BaseModel):
    summary: Bi
    scenario: Bi
    case_for: Bi
    case_against: Bi
    notes: Notes
    written: list[str]   # names of the sections the model wrote itself (were empty in the input)


def build_prompt(sub_title: str, sub_desc: str, va: dict, vb: dict, raw: dict) -> str:
    existing = {k: raw.get(k) for k in FIELDS}
    existing["notes"] = raw["notes"]
    return (
        "You prepare explanations for a review tool used by Dutch government lawyers. A detector "
        "flagged two legal provisions as a possible issue. Lawyers will read your text next to the "
        "original provisions and then accept or decline the finding.\n\n"
        f"Kind of finding: {sub_title}. {sub_desc}\n\n"
        f"PROVISION A: {va['law']['en']} {va['ref']}\n{va['text']}\n\n"
        f"PROVISION B: {vb['law']['en']} {vb['ref']}\n{vb['text']}\n\n"
        "EXISTING EXPLANATION (JSON; written by the detector, in Dutch or English; null = empty):\n"
        f"{json.dumps(existing, ensure_ascii=False, indent=1)}\n\n"
        "Return every section in BOTH Dutch (nl) and English (en).\n"
        "Rules:\n"
        "1. A section that already has text: translate it faithfully. Keep the meaning, the hedging "
        "and the level of detail. Do not add claims, do not drop claims, do not improve the argument. "
        "The version in the original language should stay as close to the original wording as possible.\n"
        "2. A section that is null: write it yourself, grounded ONLY in the two provision texts and the "
        "existing explanation. List its name in `written`.\n"
        "   - summary: one sentence, at most 25 words, saying what the possible issue is.\n"
        "   - scenario: one concrete, realistic situation (2 to 4 sentences) in which one organisation or "
        "authority runs into both provisions at once.\n"
        "   - case_for: the strongest honest reason this is a real issue.\n"
        "   - case_against: the strongest honest reason it is NOT an issue (for example: the provisions "
        "apply to different situations, one is a transposition of the other, or both can simply be met).\n"
        "3. notes: translate each note that is present; return null for notes that are absent.\n"
        "4. Quotations from the provisions stay in the original Dutch in both languages. Use the usual "
        "names of laws in each language (AVG / GDPR, AI-verordening / AI Act, NIS2-richtlijn / NIS2 Directive).\n"
        "5. Plain, precise language for lawyers. No headings, no bullet points, no dashes used as "
        "punctuation, no legal conclusions stated as certain: this is a lead to be judged.\n"
    )


def jobs():
    """Every finding the interface shows, with the input its explanation is built from."""
    titles = {cid: {s: (t, d) for s, t, d in subs} for cid, subs in b.SUBCATEGORIES.items()}
    for category, sub, _group, members in b.candidate_findings():
        t, d = titles[category][sub]
        for f in members:
            llm = f.get("llm_adjudication") or {}
            raw = b.raw_explanation(f, category)
            yield {
                "key": b.explanation_key(f["finding_id"], raw), "raw": raw,
                "va": b.provision_view(f["provisions"][0], llm.get("evidence_span_a")),
                "vb": b.provision_view(f["provisions"][1], llm.get("evidence_span_b")),
                "sub_title": t["en"], "sub_desc": d["en"],
            }


def call(client, job) -> dict:
    prompt = build_prompt(job["sub_title"], job["sub_desc"], job["va"], job["vb"], job["raw"])
    for attempt in range(3):
        try:
            resp = client.responses.parse(model=MODEL, input=prompt, text_format=Enriched,
                                          reasoning={"effort": "none"}, temperature=0)
            out = resp.output_parsed.model_dump()
            out["notes"] = {k: v for k, v in out["notes"].items() if v and k in job["raw"]["notes"]}
            out["written"] = [w for w in out["written"] if w in FIELDS and not job["raw"].get(w)]
            return out
        except Exception as e:  # network or rate hiccup: brief backoff, then give up on this one
            if attempt == 2 or "credit" in str(e).lower():
                raise
            time.sleep(3 * (attempt + 1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args()

    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    all_jobs = list(jobs())
    todo = [j for j in all_jobs if j["key"] not in cache]
    chars = sum(len(json.dumps(j["raw"], ensure_ascii=False)) + len(j["va"]["text"]) + len(j["vb"]["text"]) for j in todo)
    print(f"{len(all_jobs)} findings; {len(all_jobs) - len(todo)} already done; {len(todo)} to do "
          f"(about {chars // 1000}k characters of input, model {MODEL})", flush=True)
    if args.limit:
        todo = todo[:args.limit]
        print(f"--limit {args.limit}: running {len(todo)}", flush=True)
    if args.dry_run or not todo:
        return

    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from openai import OpenAI
    client = OpenAI(timeout=120.0, max_retries=6)  # bridges short network outages (23 Oct 2026)
    lock = threading.Lock()
    done = failed = 0
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = {ex.submit(call, client, j): j for j in todo}
        for fut in as_completed(futs):
            job = futs[fut]
            try:
                out = fut.result()
            except Exception as e:
                failed += 1
                print(f"  ERROR {job['key']}: {e}", flush=True)
                if "credit" in str(e).lower():
                    print("  API credits exhausted: stopping. Completed results are saved.", flush=True)
                    ex.shutdown(wait=False, cancel_futures=True)
                    break
                continue
            with lock:
                cache[job["key"]] = out
                c1.atomic_write_json(CACHE, cache)
                done += 1
                if done % 25 == 0:
                    print(f"  [{done}/{len(todo)}]", flush=True)
    print(f"done: {done} saved, {failed} failed -> {CACHE.relative_to(ROOT)}", flush=True)
    print("next: python ui/build_review_data.py", flush=True)


if __name__ == "__main__":
    main()
