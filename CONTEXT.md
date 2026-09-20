# AI Legislation Analysis

A detection system over Dutch and EU digital law (GDPR, NIS2, DORA, AI Act and their Dutch transpositions) that flags candidate contradictions, duplications, outdated provisions and overly complex drafting for expert review. It never issues a legal conclusion and never rewrites legal text. See `context.md` (the project decisions log) for the full history and `architecture_and_implementation_strategy.md` for the technical design.

## Language

**Provision**:
One **article** of a Dutch or EU instrument — the canonical unit the corpus is built around. A provision's `text` may contain multiple leden (paragraphs); lid-level content (a deadline, a deference clause) is captured as an entry in the provision's `norms[]` array, not as a separate corpus row. See `docs/adr/0002-provision-granularity-article-level.md`.
_Avoid_: Provision Unit, lid-as-row, article-fragment. An earlier architecture draft proposed re-chunking to one row per lid; this was rejected to avoid re-splitting six already-verified datasets, and because lid-level facts are adequately captured as `norms[]` entries on the article-level Provision instead.

**Instrument**:
A single piece of legislation in the corpus — a Dutch wet/besluit/regeling (identified by BWB-id) or an EU regulation/directive (identified by CELEX). An instrument is made of provisions.
_Avoid_: law, act, regulation (used loosely elsewhere; "instrument" is the corpus-neutral term covering both Dutch and EU sources).

**Toestand**:
One dated, consolidated version of a Dutch instrument's full text, as published by KOOP. A single instrument has many toestanden over its lifetime (e.g. the Telecommunicatiewet has 95); each provision's identity (`stam-id`) is stable across toestanden, while its content version (`versie-id`) changes only when that specific provision is amended.
_Avoid_: version, revision (used for this concept but "toestand" is the source system's own term and should be preferred once the BWB pipeline is in place).

**WTI**:
The Wetstechnische Informatie file KOOP publishes per instrument — the technical-relations record (incoming citations, legal-basis chains) as distinct from the toestand (the text itself). Adopted as the primary source for legislation-to-legislation citation currency; see `docs/adr/0003-wti-primary-citation-source.md`.
_Avoid_: using "LiDO" as if it were the only or default citation source — LiDO remains the source for case law and EU links, which the WTI does not carry.

**Deference**:
A field on a provision's extracted `norm` recording that the provision explicitly yields to another rule (e.g. Cbw art. 31's "niet van toepassing indien" pattern). A resolved deference downgrades what would otherwise be a contradiction or deduplication finding to `managed_conflict` / excluded, per `context.md` §6.1.
_Avoid_: precedence, override (deference is the specific, extracted field name used in the `norm` schema; keep it distinct from "precedence_relation," which lives at the graph level, not the provision level).

**Finding**:
A structured, machine-emitted candidate record (category, subtype, provisions, evidence, criteria fired, data currency) for one of the four detection categories. A Finding is never itself a conclusion — it becomes `report_eligible` only after human expert review.
_Avoid_: result, flag, output, conclusion.
