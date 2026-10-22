# AI-Legislation-Analysis

TU Delft Joint Interdisciplinary Project 2026, case 6.3.1, "Simplifying legislation with AI", for the Dutch Ministry of Justice and Security.

The system reads ten Dutch and EU digital laws (GDPR, AI Act, NIS2, DORA and their Dutch counterparts) and prepares material for Ministry lawyers to review. It never gives a legal conclusion and never changes the legal text: every result is a suggestion that a lawyer accepts or declines.

## What it does

| Feature | What it produces | Status |
|---|---|---|
| Contradictions and tensions | Places where two provisions may pull in different directions, such as one incident that must be reported twice on different deadlines | Built: 88 review questions |
| Duplications | Places where two laws ask for the same thing twice | Built: 64 review questions |
| Simplification | Each law explained in plain language: a one-page sheet, a stakeholder map, a summary per chapter and a card per article, checked line by line against the law and reviewed by a lawyer before it is shared | Built for the GDPR; the other nine laws are next |
| Outdated provisions | Provisions that are no longer used or have lost their legal basis | Designed, not built (hidden in the interface) |

## Running the review interface

```
python ui/server.py
```

Then open http://localhost:8765. The server only uses the Python standard library.

- **Sign-in is in test mode**: nobody has to sign in. Each browser automatically becomes its own anonymous reviewer ("Reviewer 1", "Reviewer 2"…), remembered for a year, so reviews stay private per person. Start the server with `REVIEW_DEMO_LOGIN=0` to require real accounts (passwords are stored only as salted hashes in `data/review/users.json`, which git ignores).
- **Fonts**: the interface uses the Rijksoverheid house-style fonts when the files are in `ui/fonts/` (see `ui/fonts/README.txt`). They are licensed and not in the repository; without them the browser falls back to a system font.
- **Languages**: everything switches between Dutch and English at the top right. Legal text always stays in the original Dutch.

Decisions are saved in `data/review/`: `decisions.json` for contradictions and duplications, `sheet_reviews.json` for plain-language lines, each with an append-only log. Reviews are private: the files are keyed by account email, and each reviewer only sees their own decisions.

## Rebuilding the data

The AI steps call OpenAI models (key in `.env`) and cache every answer, so a re-run never pays twice.

| Step | Command |
|---|---|
| Contradictions, from the caches | `python pipeline/c1/rebuild_c1_all.py` |
| Interface data for contradictions and duplications | `python ui/build_review_data.py` |
| Plain-language law: facts (no AI) | `python pipeline/c4/build_fact_sheet.py 32016R0679` |
| Plain-language law: write and check (AI) | `python pipeline/c4/write_simplified.py 32016R0679 --all` |
| Check "where it comes from" against the official EU texts (AI) | `python pipeline/c4/ground_foundations.py 32016R0679` |
| Interface data for the plain-language laws | `python ui/build_simplified_data.py` |

## Where things are

| Path | Contents |
|---|---|
| `pipeline/preprocessing/` | Downloading and parsing the laws, extracting the rules, the citation graph, similarity search |
| `pipeline/c1/` | Contradiction detection, ranking, evaluation |
| `pipeline/c2/` | Duplication detection |
| `pipeline/c4/` | Plain-language laws: fact sheet, writer and checker, grounding in official texts |
| `ui/` | The review interface (`app.js`, `simplify.js`), its server and the data builders |
| `data/laws/` | The parsed laws, one folder each; `foundations/` holds the official EU Charter text |
| `data/results/` | Findings and plain-language results, including sample PDFs of the public document |
| `data/cache/` | Cached AI answers and embeddings |
| `data/evaluation/` | Development set, held-out set and the blind sample with expert labels |
| `CONTEXT.md` | Glossary of the project's terms |
