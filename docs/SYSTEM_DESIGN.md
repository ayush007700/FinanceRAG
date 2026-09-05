# FinanceRAG — System Design

A retrieval system for tax advisory work: multi-agent, grounded, and built so
that a wrong answer is harder to produce than no answer.

Every design decision below is stated with the reason it was chosen **and** the
evidence that forced it. The war stories at the end are the real bugs found
building it, with the numbers.

---

## 1. The shape of the problem

Tax advisory retrieval is not general-purpose Q&A. Three properties change the
design:

1. **A confidently wrong answer is worse than no answer.** "The 2026 §179 limit
   is $2.5M" delivered with a citation is worse than a refusal, because it looks
   auditable and is not.
2. **Every claim must trace to a source.** Not "here are some relevant
   documents" — a specific passage supporting a specific sentence.
3. **Guidance expires.** A rule correct in 2023 may be wrong in 2026, and the
   document that states it does not know that.

Those three drive abstention, citation verification, and effective dating
respectively.

---

## 2. Architecture

```
                          ingest
  PDF / MD / JSON ──▶ layout parsing ──▶ page-furniture stripping
                            │
                            ▼
              hierarchical chunking (parent/child, tables atomic)
                            │
                            ▼
              embeddings (text-embedding-3-large @ 1536)
                            │
                            ▼
  ┌──────────────────── Postgres + pgvector ─────────────────────┐
  │  documents · chunks · entities · chunk_entities              │
  │  HNSW (dense) · GIN tsvector (sparse) · adjacency (graph)    │
  │  query_audit · agent_memories · checkpoints · eval_runs      │
  └──────────────────────────────────────────────────────────────┘
                            │
                        retrieval
        RRF( dense , sparse ) in one SQL statement
                    + entity expansion (weighted 3rd ranker)
                    + Cohere rerank-v3.5 cross-encoder
                            │
                            ▼
  supervisor ─┬─ researcher ─┐
              └─ web_search ─┴─ answerability ─┬─ refuse ─┐
                                               └─ analyst ─ critic ─┬ retry ─▶ researcher
                                                                    └ approve ─┤
                                                                      compliance ─▶ audit
```

### Why one database

Postgres does all three retrieval jobs the previous Neo4j deployment did:

| job | Neo4j | Postgres |
|---|---|---|
| dense | `db.index.vector.queryNodes` | `embedding <=> $1` + HNSW |
| sparse | `db.index.fulltext.queryNodes` | `tsvector` + GIN |
| graph | `(c)-[:MENTIONS]->(e)<-[:MENTIONS]-(r)` | `chunk_entities` self-join |

Plus ACID, real migrations, one connection pool, and no external managed
service. The four memory tiers and the eval history land in the same instance,
so the multi-agent system needs **zero additional infrastructure**.

**Interview answer:** "We removed a graph database because Postgres covered all
three of its jobs, and consolidating removed an external dependency, a second
connection pool, and a whole class of consistency questions."

---

## 3. Retrieval: Reciprocal Rank Fusion

$$\text{RRF}(d) = \sum_{r \in R} \frac{w_r}{k + \text{rank}_r(d)} \qquad k = 60$$

Only **rank position** feeds the fusion. Never scores.

### Worked example

Dense returns `[A, B, C, D]`, sparse returns `[C, A, E, B]`:

| doc | dense | sparse | RRF | final |
|---|---|---|---|---|
| A | 1 → 1/61 | 2 → 1/62 | **.032522** | 🥇 |
| C | 3 → 1/63 | 1 → 1/61 | **.032266** | 🥈 |
| B | 2 → 1/62 | 4 → 1/64 | **.031754** | 🥉 |
| E | — | 3 → 1/63 | .015873 | 4 |
| D | 4 → 1/64 | — | .015625 | 5 |

**A wins without topping either list.** RRF rewards consensus across rankers
over dominance in one. That is the whole idea.

### Why not a weighted score blend

1. **Scores aren't commensurable.** `ts_rank_cd` is unbounded; cosine is
   `[-1, 1]`. Adding them is adding metres to kilograms.
2. **Min-max normalisation is corrupted by the candidate set.** It forces
   top → 1.0 and bottom → 0.0 *by construction*, so a query where everything is
   garbage produces the identical distribution to one where everything is
   perfect.
3. **No tuning.** The old code had `hybrid_alpha = 0.65`, an unjustified magic
   number. RRF has no alpha.

### Why k = 60

`k` controls how steeply rank matters. At `k=0`, rank 1 is worth double rank 2
and one confident ranker dominates. At `k=60` the gap is ~1.6%, so appearing in
*several* rankers outweighs topping one. 60 is Cormack's empirical default and
is now standard across Elasticsearch, Weaviate and Qdrant.

### Its weakness — say this in an interview

**RRF scores are meaningless in absolute terms.** Top-1 is always ≈ 1/(k+1)
whether the corpus was relevant or not. So RRF **cannot drive abstention**, and
raw cosine must be carried alongside as a separate absolute signal.

Most candidates can recite the formula. Knowing what it *cannot* do is the
differentiator.

### In SQL, one round trip

```sql
WITH qry AS (SELECT to_tsquery('english', ...) AS tq),
dense AS (
  SELECT chunk_id, ROW_NUMBER() OVER (ORDER BY embedding <=> :qvec) AS rank
  FROM chunks WHERE level='child' AND org_id = :org_id
  ORDER BY embedding <=> :qvec LIMIT 50
),
sparse AS (
  SELECT chunk_id, ROW_NUMBER() OVER (ORDER BY ts_rank_cd(tsv, qry.tq, 17) DESC) AS rank
  FROM chunks CROSS JOIN qry WHERE tsv @@ qry.tq LIMIT 50
)
SELECT ..., COALESCE(1.0/(60+d.rank),0) + COALESCE(1.0/(60+s.rank),0) AS rrf_score,
       1 - (c.embedding <=> :qvec) AS cosine     -- absolute signal, preserved
FROM dense d FULL OUTER JOIN sparse s USING (chunk_id)
JOIN chunks c USING (chunk_id)
ORDER BY rrf_score DESC LIMIT 12;
```

`FULL OUTER JOIN` so a document found by either ranker survives. Entity
expansion folds in afterwards as a **weighted third ranker** (`w=0.5`) on the
same `1/(k+rank)` scale — not as an arbitrary bonus on an unrelated scale.

---

## 4. Why multiple agents

The honest starting point: the original system was **one LangGraph DAG with five
nodes and one branch**. Calling that "multi-agent" would not survive a reviewer
opening the file. What makes the current design genuinely agentic:

| role | model | job |
|---|---|---|
| **Supervisor** | cheap | classify intent, rewrite the query, choose corpus/web/both |
| **Researcher** | *none* | RRF retrieval — fusion is SQL, so no model call |
| **WebSearch** | *none* | current external facts via Tavily |
| **Analyst** | full | grounded synthesis with inline citations |
| **Critic** | cheap | verify claims against passages; can send retrieval back |
| **Compliance** | *none* | guardrails, PII, audit write |

### The cycle is the point

`Critic → Researcher` is what makes this a graph rather than a pipeline. A
rejected draft goes back for **another retrieval** with a reformulated query,
because the usual cause of an unsupported claim is missing evidence, not bad
phrasing.

It is **bounded** by `critic_max_retries`. An unbounded self-correction loop is
an unbounded bill. Exhausting the budget refuses rather than shipping a draft
the verifier would not sign.

### Adding agents made it cheaper

| | before | after |
|---|---|---|
| query rewrite | full model | folded into Supervisor (cheap) |
| **reranking** | **full model, ~2–3k tokens, every query** | **hosted cross-encoder** |
| generation | full model | full model |
| answerability | — | cheap |
| criticism | — | cheap |
| **net** | **3 × full** | **1 × full + 2 × cheap** |

Deleting the LLM reranker paid for the Supervisor and Critic several times over.

**Interview answer:** "More agents does not mean more cost if you match model
tier to task. Routing and verification are classification problems; only
synthesis needs the reasoning model."

### Two failure policies worth defending

- **Fail open.** Critic and answerability outages allow the request through.
  Downstream deterministic citation checks still run, so an outage loses a
  *layer*, not the service.
- **Fail to the safe route.** A router failure defaults to `corpus`, the only
  path whose sources are curated and citable.

---

## 5. Memory — four tiers, one database

| tier | scope | mechanism |
|---|---|---|
| working | one request | LangGraph `AgentState` |
| short-term | one thread | `PostgresSaver` checkpointer, keyed by `thread_id` |
| long-term | one org/user | `agent_memories` + pgvector, namespaced |
| episodic / audit | forever | `query_audit`, append-only |

The audit table does **double duty**: it is the compliance record *and* the
source of real evaluation data. A hand-written golden set measures regressions;
production traffic measures quality.

Namespacing in long-term memory is the tenancy boundary — one org cannot recall
another's facts.

---

## 6. Guardrails and grounding

### Abstention is a separate stage, not a prompt instruction

Folding "refuse if unsupported" into the generation prompt puts the decision
inside a step whose job is producing prose — it has every incentive to write
something. The answerability gate is a dedicated cheap-model call that runs
**before** generation, so a refusal also skips the expensive call.

**Why not a cosine threshold?** Measured on the golden set:

- answerable questions: cosine **0.591 – 0.799**
- unanswerable questions: cosine **0.554 – 0.735**

Near-total overlap. The best possible threshold catches **2 of 6** unanswerable
while keeping all answerable. Cosine measures *topical similarity*, not whether
a fact is present — "How many UK employees?" retrieves the UK practice note at
0.73 because that note genuinely is about the UK. It just never states a
headcount.

### Citation verification

1. Extract `[chunk_id]` markers (hyphen-aware — see war story #5)
2. Intersect against the retrieved set
3. **Partial fabrication** → flag, keep, raise risk score for the Critic
4. **Wholly fabricated** → block; the answer claims grounding it does not have
5. Footnote markers `[1]` and PII placeholders `[REDACTED_SSN]` are excluded

Citations report **what the answer actually cited**, not all 12 retrieved
candidates — returning everything overstates grounding.

### Related-quantity substitution

The subtlest failure, and specific to finance: a figure that answers a
*different* question. A statutory deduction cap is not a recovery total; a
worked example is not a general maximum; a 2024 limit is not a 2026 limit. The
rubric checks **subject, basis, and period** before accepting a number, because
the resulting answer looks precise and sourced while being about something else.

---

## 7. Evaluation

### Online vs offline — the split that matters

| | reported | why |
|---|---|---|
| **online** (per request) | latency, num_retrieved, top/mean/min cosine, rerank score, citation grounding | no labels exist at request time |
| **offline** (golden set) | hit rate, MRR, nDCG, P@k, R@k, faithfulness, abstention accuracy | labels available |

`hit_rate`, `mrr` and `ndcg` are **`None`** online. Deliberately. Computing them
from the retriever's own scores makes them constants, not measurements.

### Golden set design

39 cases across 8 service lines, including **7 unanswerable cases** that assert
the system *refuses*. Those are the discriminating half — no ranking metric can
catch a confident answer to a question the corpus cannot support.

Labels key on **source basename or corpus doc_id**, never generated `doc_id`:
those are hashes of the absolute path and differ per machine.

### Regression gate

`eval_runs` / `eval_cases` store every run with the git SHA and a config
snapshot — *a metric without its configuration is a number, not a measurement*.
`--diff` reports metric deltas, config changes, and **the individual cases that
flipped**, which is the half that leads to a fix.

---

## 8. War stories

Real bugs, with evidence. This section is the interview material.

### Retrieval

**1. The sparse ranker returned nothing. For its entire existence.**
`plainto_tsquery` ANDs every lexeme. A nine-word question became a nine-term
conjunction: **0 of 291 chunks matched**. "Hybrid" retrieval was dense-only, the
GIN index was dead weight, and RRF was fusing one ranker against nothing.
*Fix:* extract lexemes, rejoin with `|`. *Lesson:* verify each component
contributes, not just that the pipeline returns results.

**2. Fixing it made the benchmark worse — and that was the right outcome.**
`hit_rate` 1.000 → 0.939. Investigation showed page boilerplate outranking real
content. The fix was correct; it *exposed* contamination dense-only had been
accidentally hiding. *Lesson:* a metric drop is a finding, not a verdict.

**3. Min-max normalisation killed the abstention gate.**
It forces top → 1.0 by construction, so a `min_relevance = 0.20` threshold could
never fire. The system would confidently answer anything.

**4. A `service_line` filter made the answer invisible.**
`infer_service_line` returns on *first* dict match, and `"179d"` precedes
`"45l"` — so the §179D/§45L document was tagged **§179D only**. Zero chunks
carried `Energy Efficiency §45L`. A correctly-identified §45L question filtered
on §45L and retrieved **nothing**. *Fix:* demote to advisory metadata. *Lesson:*
never hard-filter on a heuristic label.

### Grounding

**5. Five of fourteen documents were structurally uncitable.**
The citation regex was `[a-zA-Z0-9_]+`, but JSON-corpus doc_ids contain hyphens
(`lifo-001_57f4d6...`). Those citations parsed as **nothing**, so a perfectly
cited answer was reported as having no citations.

**6. Citation validation never validated.**
The set of known chunk ids was computed — then only tested for emptiness. A
fabricated id was never detected. The single most important check in an
advisory product was decorative.

**7. Better retrieval made abstention *harder*.**
Layout parsing surfaced *"maximum section 179 expense deduction is $2,500,000"*.
The gate accepted it for "maximum recoverable through cost segregation" — a real
figure, wrong quantity. **Improving one component degraded another**, because
better content makes more convincing distractors.

### Data quality

**8. 55% of the index was page furniture.**
`"The type and rule above prints on all proofs..."` appeared on all 113 pages of
Pub 946; the `Page N of M Fileid:` footer on 112. Of 291 searchable chunks, 266
came from PDFs and **60.9% of those were boilerplate or dot-leaders**. Curated
content was under 9% of the index.

**9. Depreciation tables were destroyed.**
`pypdf.extract_text` emitted row labels, column headers and values as three
disconnected streams. `20.15` was present but nothing tied it to (Year 3, 5-year
property). *Present and unusable is worse than absent.*

**10. PDFs had no structure at all.**
`split_by_structure` keys on markdown headings; a flat text stream has none. A
113-page publication became **one section, one 4000-char parent**, `section` null
on every child. Hierarchical retrieval was off for exactly the documents needing
it. *After layout parsing:* 223 sections, 99% of children labelled.

**11. Every nested file was ingested twice.**
`rglob("*")` returns sub-directories *and* their contents; recursing into them
re-visited the same files. Invisible in the store (content-derived ids upsert
over themselves) but **every duplicate was embedded again at full cost**.

**12. Web `raw_content` was the same bug one layer out.**
Enabling full page text made things *worse*: the first 4000 characters of an IRS
page are `.gov` banners and nav images. `'179' mentioned: False`. The gate
correctly said the page didn't mention the deduction — it had never been shown
the article.

### Measurement

**13. Three metrics were mathematically pinned to 1.0.**
The reranker returns results sorted descending, so `ideal == actual` and
**nDCG ≡ 1.0**, always. `hit_rate` and `MRR` thresholded the retriever's own
scores — circular. The dashboard reported perfect retrieval quality
unconditionally. *A metric that cannot vary is worse than no metric: it looks
like evidence while carrying none.*

**14. nDCG also inflated.**
The ideal ranking was built from the **truncated** slice, so a relevant document
past `k` was invisible to it. `ndcg([1,0,3], k=2)` returned **1.000** where the
correct value is **0.275**.

**15. Retrieval metrics were contaminated by generation.**
`retrieved_ids` was derived from *citations*. A refusal reports all retrieved
chunks as citations; an answer reports only the cited subset — so **refusal
behaviour moved the retrieval metrics**. Reported `precision_at_k` **0.834**;
true value **0.225**. Caught because a rubric string change moved `hit_rate`,
which is causally impossible.

**16. The eval was not reproducible.** `temperature=0.1` on query rewriting meant
identical runs produced different retrieval, with drift exceeding the 0.05
regression tolerance. Noise was indistinguishable from signal.

**17. A shell pipeline silently swallowed the regression gate.**
`python run_eval.py | grep | tail` reports *`tail`'s* exit code. The gate fired,
returned 1, and the pipeline reported success.

### Agents

**18. A route refused before looking.**
The Supervisor classified *"What values guide Source Advisors client work?"* as
conversational and returned a canned refusal **without retrieving**. Retrieval
scores 0.684 on the chunk stating those values. *Lesson:* the router decides
before evidence, the answerability gate decides after — **the gate is strictly
better informed, so refusals belong there**.

**19. Query rewriting was silently dropped** during the multi-agent rewrite.
Restored by folding it into the Supervisor's existing call — one cheap call now
does routing *and* rewriting.

### Infrastructure

**20. A port collision that looked like bad credentials.**
Native PostgreSQL 18 held `0.0.0.0:5432`, leaving Docker only `[::]:5432`. Both
addresses reached PG18, which has no `finrag` role — and Postgres reports a
missing role as *"password authentication failed"* to prevent user enumeration.
*Lesson:* read the error as the server's *policy*, not its diagnosis.

**21. The checkpointer's connection closed underneath it.**
`PostgresSaver.from_conn_string` returns a context manager; letting it fall out
of scope closes the connection, so multi-turn memory failed on the *second*
turn.

**22. LangGraph would have blocked our dataclasses on upgrade** — deserialised
with a warning today, refused in a later version, silently breaking multi-turn
memory.

**23. Uploads went to ephemeral task disk.** Gone on redeploy, invisible to
other tasks. Retrieval succeeded *intermittently*, which reads as a ranking
problem rather than a storage one.

**24. `async def` calling blocking work.** `ask_multipart` was a coroutine
invoking the synchronous agent directly — stalling the event loop for every
concurrent request. Ironically *worse* than a plain `def`, which FastAPI
offloads to a threadpool.

**25. Alarms with no subscriber.** Three CloudWatch alarms existed with no SNS
topic. They changed state and told nobody.

**26. A hardcoded database password** committed in `docker-compose.yml`.

---

## 9. Interview questions this design answers

**Retrieval**
- Why RRF over weighted score fusion? What does `k` control? What can RRF *not* do?
- How do you combine BM25 and vector search when their scores aren't comparable?
- Why is `precision@k` low (0.225) while `hit_rate` is high (0.906)?

**Agents**
- What makes a system agentic rather than a pipeline? *(Cycles and tool choice.)*
- How do you stop a self-correction loop from running forever?
- How do you add agents without multiplying cost?
- Where does a refusal decision belong, and why not in the router?

**Evaluation**
- How do you evaluate RAG without labels at request time?
- Your nDCG is 1.0 on every request. Why should that worry you?
- How do you know an improvement is real and not noise?
- How do you catch a metric that measures the wrong thing?

**Grounding**
- How do you detect a hallucinated citation?
- Why can't cosine similarity decide whether a question is answerable?
- What's the most dangerous kind of wrong answer in finance? *(Related-quantity
  substitution — precise, sourced, and about something else.)*

**Data**
- Why does PDF parsing matter more than model choice here?
- How do you chunk a table? *(Atomically. A split table strands rows without a
  header and misattributes values.)*
- How do you detect boilerplate generically rather than by keyword list?

---

## 10. Related

- [`AWS_AND_TERRAFORM.md`](AWS_AND_TERRAFORM.md) — infrastructure, cost engineering, IaC questions
- [`../README.md`](../README.md) — running it locally
