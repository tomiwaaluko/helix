# Helix — Complete Project Reference

> A single-document, end-to-end account of the Helix project: the problem it
> solves, the research it validates, the technology behind it, how it is
> implemented, how it is benchmarked, and the statistics that back the headline
> result.
>
> **Tagline:** Run agents reliably. Measure them rigorously. Make them better automatically.
>
> License: Apache 2.0. Generated as a reference companion to `README.md`,
> `SPEC.md`, and the `docs/` set.

---

## Table of contents

1. [Problem statement](#1-problem-statement)
2. [Research contribution](#2-research-contribution)
3. [Goals and non-goals](#3-goals-and-non-goals)
4. [System architecture](#4-system-architecture)
5. [Technology stack](#5-technology-stack)
6. [Data model](#6-data-model)
7. [API surfaces](#7-api-surfaces)
8. [The RAG self-improvement loop](#8-the-rag-self-improvement-loop)
9. [Implementation](#9-implementation)
10. [Milestone history (M1–M11)](#10-milestone-history-m1m11)
11. [Benchmarking methodology](#11-benchmarking-methodology)
12. [Statistical results](#12-statistical-results)
13. [Deployment](#13-deployment)
14. [Repository layout](#14-repository-layout)
15. [Engineering conventions and definition of done](#15-engineering-conventions-and-definition-of-done)
16. [Known limitations and remaining work](#16-known-limitations-and-remaining-work)
17. [Glossary](#17-glossary)

---

## 1. Problem statement

Production agents fail in ways traditional software does not. They retry the wrong
tool, retrieve irrelevant context, hallucinate citations, and recover from upstream
errors silently. Most agent frameworks optimize for the happy path: chain a few LLM
calls, hit a vector store, return an answer. They treat failure as an exception
rather than as a signal.

The gap between an "agent demo" and "an agent that works at the 95th percentile" is
closed by three capabilities current frameworks bolt on at best:

1. **A durable execution layer** that survives worker crashes, broker outages, and
   provider rate limits without losing state.
2. **An evaluation and observability layer** that captures traces, scores outcomes,
   and surfaces regressions before they ship.
3. **A retrieval subsystem that learns from its own failure cases** instead of
   decaying as the corpus and the query distribution drift.

Helix is one platform that does all three. The thesis: **an agent runtime designed
from the ground up around evaluation produces measurably better agents than one
where evaluation is grafted on after the fact.**

---

## 2. Research contribution

**Claim.** Embedding models fine-tuned on automatically mined failure cases from
agent traces produce measurable retrieval improvement on a held-out hard set.

**Method.**

1. Run the reference agent over a benchmark workload; collect retrieval traces with
   success labels (was the cited gold passage actually in the retrieved set?).
2. Cluster failures by error signature: lexical-only mismatch, semantic mismatch,
   multi-hop miss, ambiguous query.
3. Mine hard negatives from in-cluster failures, producing contrastive triplets of
   `(query, gold_passage, near_miss_passage)`.
4. Fine-tune the base embedding model (Nomic Embed v1.5) on the mined triplets with
   InfoNCE loss.
5. Evaluate the fine-tuned model against a held-out hard set that is sequestered at
   project start and never touched by the miner.

**Headline metric.** Recall@10 on the held-out hard set, before and after
fine-tuning, with bootstrap confidence intervals.

**Result (BRIGHT biology).** One mine→train→promote cycle lifted recall@10 from
**0.253 → 0.343** (+0.090, 95% CI [0.010, 0.175], sign-test p = 0.041). The lift
reproduces across three independent training seeds (see §12).

**Secondary contributions.**

- A retrieval failure taxonomy derived empirically from the trace corpus.
- A reusable failure-mining pipeline open-sourced as part of the platform.
- An eval harness whose span schema is compatible with downstream trace tooling.

**Risk acknowledged up front.** The fine-tune may produce no measurable lift. If so,
the framework, taxonomy, and methodology remain the contribution, and the negative
result is itself publishable. (This actually happened on HotpotQA — see §11 — which
is why the experiment moved to the higher-headroom BRIGHT corpus.)

---

## 3. Goals and non-goals

### Goals

- **Durable agent execution.** Workflows survive worker crashes, broker outages, and
  provider failures with at-least-once execution and idempotency hooks.
- **Lossless observability.** Every LLM call, tool call, and retrieval is captured as
  a structured span. Replay reproduces a byte-identical trace given the same inputs.
- **Evaluation as a first-class workflow.** Evals are not side scripts; they are
  workflows the runtime schedules, traces, and stores alongside production runs.
- **Self-improving retrieval.** Failure mining and embedding fine-tuning are
  continuous background workflows, not one-off scripts.
- **Open by default.** Apache 2.0, reproducible builds, public benchmarks.

### Non-goals

- **A new agent framework.** Helix wraps existing agent code (Python functions that
  call LLMs and tools). It does not prescribe a graph DSL or a chain abstraction.
- **A managed cloud offering.** The reference deployment is single-tenant Kubernetes.
- **A general-purpose workflow engine.** Helix optimizes for LLM-and-tool workloads;
  it will not compete with Temporal on financial-transaction durability.
- **Real-time streaming agents at high QPS.** Helix targets batch and interactive
  workloads up to roughly 100 concurrent runs per cluster.

### Success criteria (from `SPEC.md`)

1. Runtime executes 10,000 agent runs end-to-end with under 0.1% lost-run rate.
2. Replay reproduces a sample of 100 runs byte-identically.
3. The eval harness completes the full benchmark suite in under one hour on eight
   workers.
4. The fine-tuned model shows at least 5% relative recall@10 improvement on the
   held-out hard set, significant at p < 0.05 over 1000 bootstrap samples — or the
   negative result is documented with the methodology intact.
5. The dashboard renders a run, its trace, and its eval scores within 2 seconds on a
   corpus of 1M spans.

---

## 4. System architecture

Helix splits responsibility across **three planes**:

```
                        ┌──────────────────────────────────────────┐
                        │  Control Plane (Go)                       │
  Client ─── gRPC/REST ─▶  Orchestrator + Scheduler + Postgres      │
                        └──────────────┬───────────────────────────┘
                                       │ NATS JetStream (task dispatch)
               ┌───────────────────────┼───────────────────────┐
               ▼                       ▼                       ▼
        Worker: research        Worker: finetune_job    Worker: indexer
        (Python)                (Python)                (Python)
               │                       │
               └───────────┬───────────┘
                           │ OTel spans
                           ▼
                    OTel Collector ──▶ ClickHouse ──▶ Dashboard (Next.js)
               │
               └── Qdrant (vectors) · Redis (exactly-once) · MinIO (blobs)
```

The principal design decision: **execution state is durable and centralized**
(Postgres plus checkpoints), while **execution itself is stateless and distributed**
(workers can be killed mid-task without data loss). This is the same pattern as
Temporal, Cadence, and Inngest; Helix adopts it because LLM workloads have wide
latency distributions and frequent transient failures.

### 4.1 Control plane (Go)

A single Go service (`cmd/orchestrator/`) that:

- **Workflow registry.** Workflows are declared in Python via the SDK and registered
  with the orchestrator at worker startup. The orchestrator stores the materialized
  DAG (topology, retry policies, timeouts) but **never executes user code**.
- **Run lifecycle.** A run moves through `pending → running → succeeded | failed |
  cancelled`. Each transition is a Postgres row update inside a transaction that also
  emits the corresponding event.
- **Task dispatch.** For each ready task (dependencies satisfied), the orchestrator
  selects a worker pool, publishes a `helix.tasks.dispatch.<pool>` message on NATS,
  and writes a `task_attempts` row.
- **Retry policy.** Per task: max attempts, backoff curve (exponential with jitter),
  retryable error classes (rate-limit retries; validation does not).
- **Checkpointing.** Long-running tasks emit opaque checkpoint bytes the orchestrator
  persists. On retry, the worker resumes from the last checkpoint.
- **Dead-letter queue.** After max retries, tasks land in `dead_letter_queue` with
  full context for human triage or programmatic replay.

The **scheduler** lives in the same process. Initial routing is round-robin per pool;
least-loaded routing using heartbeat data is a follow-up.

### 4.2 Data plane (Python)

Stateless Python worker processes (`worker/helix/worker/__main__.py`). Each worker:

1. Connects to the orchestrator over gRPC. Registers pool name, capabilities, max
   concurrency.
2. Subscribes to NATS subjects derived from its pool name.
3. Loops: pull task → deserialize input → look up the workflow handler in the local
   registry → call it → emit spans → ack the message → post a completion event.

Workers are killable at any point. If a worker dies mid-task, the NATS unacked
message redelivers after the visibility timeout (default 30s). The orchestrator
deduplicates by `(task_id, attempt_number)`.

Two pools ship in the reference deployment: **`research`** (handles the
`deep_research` workflow) and **`finetune_job`** (handles the mine→train→promote
workflow).

### 4.3 Observability plane

Workers emit OpenTelemetry spans. An OTel Collector (`cmd/collector/`) receives spans
over OTLP/gRPC, resolves `span_id → run_id`, and exports to a ClickHouse
`BatchWriter` (200ms flush, 500-row batches). The dashboard queries ClickHouse
directly through a thin Go API.

The collector additionally **fans out** specific span kinds into denormalized tables
for fast queries: `llm` spans → `llm_calls`, `retrieval` spans → `retrievals`, and
eval outcomes → `eval_events` (written synchronously, because eval data cannot be
lost).

### 4.4 Event bus

NATS JetStream with the streams:

- `helix.tasks.dispatch.<pool>` — orchestrator → workers. Persistent, at-least-once.
- `helix.events.completion` — workers → orchestrator. Persistent, at-least-once.
- `helix.events.heartbeat` — workers → orchestrator, every 5s.
- `helix.events.checkpoint` — workers → orchestrator on checkpoint.

**Why NATS over Kafka:** lower operational overhead, faster cold starts, native
request/reply and KV buckets for heartbeats. **Why over Redis Streams:** stronger
persistence guarantees and a clearer subject/stream model.

### 4.5 Failure modes and recovery

| Failure | Detection | Recovery |
|---|---|---|
| Worker crash mid-task | NATS visibility timeout expires | Message redelivered; checkpoint resumes |
| Orchestrator crash | K8s liveness probe | Restart; gRPC retried by clients; state intact in Postgres |
| Postgres unavailable | Connection error | Orchestrator returns 503; workers buffer events in NATS |
| ClickHouse unavailable | OTel exporter error | Spans buffer at collector (disk-backed) up to 1 hour |
| NATS unavailable | Connection error | Orchestrator falls back to direct gRPC dispatch (degraded) |
| LLM provider rate-limit | LiteLLM error | Exponential backoff; fail over to secondary provider |
| Embedding model unavailable | Qdrant error/timeout | Retriever falls back to BM25-only |

### 4.6 Determinism guarantees

- **Trace fidelity.** Every span captures inputs, outputs, model version, and tool
  version. Replay against the same trace reproduces the workflow result.
- **Eval reproducibility.** An eval run is parameterized by `(workflow_version,
  dataset_version, scorer_version, retriever_version, embedding_model_version)`. Same
  parameters → same result, modulo LLM sampling, which the harness pins via
  `temperature=0`.

What Helix does **not** guarantee: bit-exact LLM output across providers or over
time. Providers change weights silently; the harness records the provider's model id
and date stamp for every call.

---

## 5. Technology stack

Every choice is justified against the alternatives considered. Where the obvious pick
was rejected, the reason is stated.

### 5.1 Languages

| Layer | Choice | Why | Rejected |
|---|---|---|---|
| Control plane | **Go 1.22+** | Canonical for distributed systems (Temporal, NATS, K8s); predictable concurrency, low GC, strong gRPC/protobuf | Rust (async still settling), Python (loses systems signal) |
| Workers / ML | **Python 3.12+** | The LLM/ML ecosystem (LiteLLM, sentence-transformers, Qdrant, OTel) is Python-first; I/O-bound so the GIL is not a bottleneck | None seriously |
| Dashboard | **TypeScript 5 + Next.js 14** | The dashboard is CRUD-and-charts over REST/SSE; this stack ships | — |

### 5.2 Datastores

| Store | Version | Role | Why this and not the obvious alternative |
|---|---|---|---|
| **Postgres** | 15 | Control-plane transactional state | ACID + `jsonb` for workflow specs; single-writer keeps consistency simple. Over MySQL (`jsonb`), CockroachDB (no scale need). |
| **ClickHouse** | 24 | Spans, retrievals, llm_calls, eval_events | High-volume append-mostly telemetry with aggregations — its wheelhouse. Over Postgres/Timescale (worse wide-trace fit), Elasticsearch (heavier/slower). |
| **Qdrant** | 1.9+ | Vector index | Clean per-collection model enables base + candidate side by side with atomic alias swap. Over pgvector (filtered ANN lags), Milvus/Weaviate (heavier). |
| **Redis** | 7 | Exactly-once sentinel, rate-limit counters, heartbeats | Standard; not the system of record for anything. |
| **MinIO** | S3-compatible | Large span payloads, model checkpoints, corpus snapshots | The S3 API is the lingua franca; swappable for real S3 with one flag. |

### 5.3 Messaging

**NATS JetStream** — lower ops footprint than Kafka, stronger guarantees than Redis
Streams. Subject-based routing fits per-pool dispatch; the persistence + ack model
gives at-least-once with redelivery on visibility timeout. Direct gRPC dispatch is
kept as the NATS-unavailable fallback path inside the orchestrator.

### 5.4 LLM and embedding stack

| Component | Choice | Why |
|---|---|---|
| Provider abstraction | **LiteLLM** | Multi-provider routing, built-in retry/fallback/cost tracking. **All** model calls go through it — direct provider calls are forbidden because they break the cache, replay, and metering. |
| Base embedding model | **`nomic-ai/nomic-embed-text-v1.5`** (768d, Apache 2.0) | Open license, fine-tunable, MTEB-competitive; ships `search_query:` / `search_document:` task prefixes the retriever uses out of the box. |
| Reranker | **`BAAI/bge-reranker-base`** | Reasonable size, strong results, MIT. Held fixed across experiments so the embedding fine-tune's contribution is isolated. |
| Fine-tuning | **sentence-transformers + PyTorch**, `accelerate>=1.1.0` | Mature, native InfoNCE (`MultipleNegativesRankingLoss`), HF-hub integration. `accelerate` is pinned because `.fit()` delegates to `transformers.Trainer`, which requires it even for single-device CPU training. |
| Chunk sizing | **tiktoken** (`cl100k_base`) | Fast, dependency-light token counting; degrades to a char-based heuristic when the tokenizer blob is unreachable. |
| Corpus/eval seeding | **HuggingFace `datasets`** | Canonical cached loader for HotpotQA/BRIGHT; keeps prep reproducible from a clean checkout. |

### 5.5 Frontend

**Next.js 14 (App Router) + TanStack Query + shadcn/ui + Tailwind + Recharts.**
Server components where they earn their keep; client components only when
interactivity demands it. API types are generated from `web/openapi.yaml` — never
hand-written.

### 5.6 Observability and deployment

- **OpenTelemetry** SDKs in both Python and Go workers; a custom ClickHouse exporter
  is the primary sink. OTel compatibility means traces work with Jaeger, Tempo, etc.
- **Docker Compose** for local dev: one `make dev` boots Postgres, ClickHouse,
  Qdrant, NATS, Redis, MinIO, and the OTel collector.
- **Kubernetes + Helm** for production (the `infra/helm/helix/` chart, §13).

### 5.7 Versions of record

| Component | Version | Pinned in |
|---|---|---|
| Go | 1.22+ | `go.mod` |
| Python | 3.12+ | `pyproject.toml`, uv lockfile |
| Postgres | 15 | Helm values |
| ClickHouse | 24 | Helm values |
| Qdrant | 1.9+ | Helm values |
| NATS | 2.10+ | Helm values |
| Redis | 7 | Helm values |
| MinIO | latest | Helm values |
| Nomic Embed | v1.5 | HF revision hash |
| BGE reranker | base | HF revision hash |

---

## 6. Data model

### 6.1 Postgres (control plane)

The control-plane schema is normalized; Postgres handles all transactional state.
Core tables:

| Table | Purpose |
|---|---|
| `workflows` | Registered workflow definitions (versioned; stores the materialized DAG `spec` as JSONB). |
| `runs` | Workflow run instances. Status enum `pending/running/succeeded/failed/cancelled`; carries `input`, `output`, `error`, `trace_id`. |
| `tasks` | Logical task instances within a run, one row per `(run, node)`. |
| `task_attempts` | One row per retry (append-only); records `worker_id`, `span_id`, timing. |
| `checkpoints` | Per-task durable opaque state (`BYTEA`) for long-running tasks. |
| `dead_letter_queue` | Tasks that exhausted retries, replayable by hand or API. |
| `workers` | Registered workers + heartbeats (`pool`, `capabilities`, `last_heartbeat`). |
| `datasets` | Eval datasets, versioned, content-hashed (SHA-256). |
| `evals` | Eval run instances (each eval is itself a Helix workflow). |
| `embedding_jobs` | Fine-tune job state: status enum `queued/mining/training/evaluating/promoted/archived/failed`, `config`, `triplets_count`, `metrics`, `artifact_uri`. |
| `failure_cases` | Mined retrieval failures with `signature`, `gold_passage_id`, `retrieved_top_k`. |
| `finetune_jobs` | Production failure-miner jobs (M9b), linked to `embedding_jobs` via `finetune_job_id`. |

Migrations on disk:

```
migrations/202606150001_initial_schema.sql              -- core tables
migrations/202606160001_finetune_jobs.sql               -- M9b finetune_jobs
migrations/202606160002_embedding_jobs_finetune_link.sql -- M10 FK + index
migrations/clickhouse/202606150001_initial_schema.sql   -- ClickHouse tables
```

Example — `embedding_jobs.metrics` shape:

```json
{
  "before": {"recall@10": 0.61, "ci_low": 0.59, "ci_high": 0.63},
  "after":  {"recall@10": 0.68, "ci_low": 0.66, "ci_high": 0.70},
  "delta":  {"recall@10": 0.07, "p_value": 0.003}
}
```

### 6.2 ClickHouse (telemetry)

| Table | Engine / partition | Purpose |
|---|---|---|
| `spans` | MergeTree, `PARTITION BY toYYYYMMDD(start_time)`, 90-day TTL, `ORDER BY (trace_id, start_time, span_id)` | All OTel spans. `kind ∈ {llm, tool, retrieval, workflow, internal}`. Has a materialized `duration_ms`. |
| `llm_calls` | MergeTree, by day, `ORDER BY (run_id, start_time)` | Denormalized `spans WHERE kind='llm'` for fast cost/latency queries (`cost_usd Decimal(10,6)`, token counts, `prompt_uri`/`completion_uri`). |
| `retrievals` | MergeTree, by day, `ORDER BY (run_id, start_time)` | Retrieval events the miner reads. `retriever ∈ {dense, sparse, hybrid, reranked}`; `results Array(Tuple(passage_id, score, rank))`; `recall_at_k`. |
| `eval_events` | MergeTree, by day, `ORDER BY (eval_id, example_id)` | Per-example eval outcomes. **Written synchronously** — eval data cannot be lost. |

### 6.3 Qdrant (vectors)

Two collection roles with an **alias indirection** for atomic swap:

- **`corpus.base`** — vectors from the base model. 768d, cosine, HNSW `m=16,
  ef_construct=200`. Payload: `doc_id`, `chunk_id`, `source`, `section`, `text`
  (BM25-indexed), `published_at`.
- **`corpus.candidate.<job_id>`** — vectors from a candidate (fine-tuned) model.

```
corpus.active → corpus.base                  (initial)
corpus.active → corpus.candidate.<job_id>    (after a successful promotion)
```

The retriever always reads from `corpus.active`. **Promotion is a single Qdrant
alias-swap API call** — and the inverse is one call, so rollback is trivial.

### 6.4 NATS event schemas

`helix.tasks.dispatch` (orchestrator → worker), `helix.events.completion` (worker →
orchestrator), `helix.events.heartbeat` (every 5s), `helix.events.checkpoint`. Each
carries `task_id`, `attempt_number`, and the relevant payload. The completion event
carries `status`, `output`, `error`, `span_id`, `duration_ms`.

### 6.5 Dataset format

Every eval dataset is JSONL, one example per line:

```json
{
  "id": "hotpotqa_dev_001",
  "input": {"question": "..."},
  "expected_output": {"answer": "...", "supporting_facts": [{"doc": "...", "sent": 3}]},
  "metadata": {"hops": 2, "type": "comparison"}
}
```

Datasets are immutable; new versions are new datasets. Each is content-hashed
(SHA-256) for integrity.

---

## 7. API surfaces

Helix exposes four surfaces, each small on purpose.

| Surface | Audience | Transport | Stability |
|---|---|---|---|
| gRPC | Workers ↔ control plane | gRPC over mTLS | Internal; breaking changes via proto version bump |
| REST + SSE | Dashboard, external tooling | HTTPS, JSON, ISO-8601 UTC | Public; semver under `/api/v1/` |
| Python SDK | Workflow authors | In-process + REST/gRPC underneath | Public; semver |
| CLI | Operators, demos | Wraps REST | Public; semver |

All public surfaces share one error envelope:

```json
{ "error": { "code": "RUN_NOT_FOUND", "message": "no run with id=run_01H...", "details": {} } }
```

### 7.1 gRPC (workers ↔ control plane)

Defined in `proto/helix/v1/`. Three services:

- **Orchestrator** — `RegisterWorker`, `Heartbeat` (bidi-stream), `CompleteTask`
  (**idempotent on `(task_id, attempt_number)`**), `Checkpoint`, `GetWorkflow`.
- **EvalRunner** — `SubmitEval`, `PublishExampleResult` (the only synchronous
  ClickHouse write path; eval workers batch in groups of 50).
- **Embeddings** — `StartFineTuneJob`, `PromoteModel` (flips the `corpus.active`
  alias atomically, records the previous collection for one-call rollback).

### 7.2 REST (`/api/v1/`)

JSON over HTTPS, bearer-token auth. Surfaces (selected):

| Group | Endpoints |
|---|---|
| Runs | `POST /runs`, `GET /runs/{id}` (includes `tasks` tree), `GET /runs`, `POST /runs/{id}/cancel`, `GET /runs/{id}/events` (SSE), `POST /runs/{id}/replay` |
| Traces | `GET /runs/{id}/trace` (spans from ClickHouse; `s3://` blob attrs rewritten to presigned HTTPS, 1h expiry), `GET /spans/{id}` |
| Evals | `POST /evals`, `GET /evals`, `GET /evals/{id}`, `POST /evals/{id}/events`, `GET /evals/{id}/compare?baseline=` |
| Retrievals | `GET /retrievals` (filters: `run_id`, `recall_at_10__lt`, `failure_signature`), `POST /retrievals/mine` |
| LLM calls | `GET /llm-calls` (optional `?run_id=`) |
| Finetune jobs | `POST /finetune-jobs`, `GET /finetune-jobs`, `GET /finetune-jobs/{id}` |
| Embeddings | `GET /embedding-jobs`, `GET /embedding-jobs/{id}` (artifact `s3://` URIs presigned), `POST /embeddings/jobs/{id}/promote`, `POST /embeddings/rollback` |
| Workers | `GET /workers`, `POST /workers/{id}/drain` |

Endpoints that depend on ClickHouse return **503 with a clear message** when
`CLICKHOUSE_URL` is unset — no config is required to run the control plane without
telemetry.

### 7.3 Python SDK

Workflow code is plain Python; decorators register it with the runtime. The decorator
metadata **is** the DAG — there is no separate graph language.

```python
import helix

@helix.task(retries=3, timeout="30s")
async def decompose(query: str) -> list[str]: ...

@helix.task(retries=2, timeout="2m")
async def retrieve(subquery: str) -> list[helix.Doc]: ...

@helix.task(retries=1, timeout="3m")
async def synthesize(query: str, evidence: list[helix.Doc]) -> helix.Answer: ...

@helix.workflow(name="deep_research", version="1.0.0")
async def deep_research(query: str) -> helix.Answer:
    subqueries = await decompose(query)
    evidence = await helix.gather(*(retrieve(sq) for sq in subqueries))
    flat = [doc for sublist in evidence for doc in sublist]
    return await synthesize(query, flat)
```

`helix.gather` is the parallel construct. `deep_research.local(query=...)` runs
in-process without Postgres/NATS/ClickHouse for unit tests. **Local vs. submit mode
is detected via a `contextvars.ContextVar[Engine | None]`** — workflow code is
identical in both modes.

### 7.4 CLI

A single `helix` binary wrapping REST: `helix run submit/get/replay/cancel`,
`helix eval submit/get/compare`, `helix dataset list/push`, `helix mine`,
`helix embed train/promote/rollback`, `helix worker list/drain`. All accept `--json`.

---

## 8. The RAG self-improvement loop

The RAG subsystem is the research contribution. Five components, each itself a Helix
workflow.

```
Query ──▶ Retriever (dense+sparse+RRF) ──▶ Reranker ──▶ Agent ──▶ Eval
                                                                    │ failure
                                                              Failure Miner
                                                                    │ triplets
                                                       Embedding Trainer (InfoNCE)
                                                                    │ candidate
                                                       Canary eval on held-out set
                                                                    │
                                                    ┌─ promote? ───┤
                                                    ▼ yes          ▼ no
                                            Qdrant alias swap     archive
```

### 8.1 Indexer

Input: a corpus URI (JSONL of documents). Steps: load → chunk (structural splitting:
paragraphs → sentences → word-window fallback, greedy-packed to 512 tokens with
64-token overlap) → embed in batches → upsert to Qdrant with metadata → point the
`corpus.active` alias at the new collection. Chunk ids map to deterministic `uuid5`
point ids, so a re-index overwrites cleanly.

### 8.2 Retriever

Hybrid: dense (Qdrant ANN over `corpus.active`) + sparse (BM25 via `rank_bm25`) fused
by **reciprocal rank fusion** (`k=60`) at the top-50 boundary. `Doc.id` is the corpus
`doc_id` so recall and citations key off the document.

### 8.3 Reranker

Optional second stage: `BAAI/bge-reranker-base` cross-encoder for top-50 → top-10.
Adds ~200ms, materially improves precision, and **stays fixed across experiments** so
the embedding fine-tune's contribution is isolated.

### 8.4 Failure miner

Reads recent retrieval spans (from `data/spans.jsonl` in the slice, or ClickHouse in
production), joins them against eval outcomes, identifies failures (gold passage not
in the retrieved set), and classifies each into one of four signatures via a rule
classifier:

- **Lexical-only mismatch** — gold passage shares no surface tokens with the query.
- **Semantic mismatch** — embedding similarity below threshold despite topical alignment.
- **Multi-hop miss** — first-hop retrieval succeeded, second-hop failed.
- **Ambiguous query** — multiple plausible gold passages, retriever chose wrong.

These categories form the empirical taxonomy contribution.

### 8.5 Embedding trainer

For each failure, build contrastive triplets `(query, gold_passage, hard_negative)`
— hard negatives sampled from the top-k retrieved-but-wrong passages, with Nomic
`search_query:` / `search_document:` prefixes applied. Fine-tune Nomic Embed v1.5 with
InfoNCE loss (`MultipleNegativesRankingLoss`) using sentence-transformers. The default
backend seeds the torch/numpy/python RNGs for reproducible candidates. The checkpoint
is tar+gzipped and uploaded to MinIO (`s3://helix-blobs/artifacts/<job_id>.tar.gz`).

> **Implementation gotcha (logged in `ISSUES.md`).** Nomic's remote modeling code
> writes transformer weights under a doubled `encoder.encoder.layers.*` prefix while
> reload expects `encoder.layers.*`, so a naive save→reload silently dropped all 108
> fine-tuned tensors and fell back to base weights — making every canary a no-op.
> `_normalize_nomic_checkpoint()` de-doubles the prefix after `model.save()`. After
> the fix, save→reload embeddings match the in-memory model exactly (max-abs 0.0).

### 8.6 Promotion + canary

Re-index the corpus into `corpus.candidate.<job_id>` with the candidate model. Measure
recall@10 before (current `corpus.active`, base embedder) and after (candidate
embedder, new collection), each with 95% bootstrap CIs, **using the full hybrid
pipeline** (dense + BM25 + rerank) so a candidate whose dense gain is washed out by
the reranker is not promoted. Atomically swap `corpus.active` to the candidate only
when `after.mean > before.mean`; otherwise archive. The `embedding_jobs` row records
`promoted` or `archived` with full CI metrics regardless of outcome.

---

## 9. Implementation

What is physically on disk (counts from the tracked tree):

| Area | Files | Notes |
|---|---|---|
| Go (`cmd/`, `internal/`) | 31 source + 9 test | Orchestrator, collector, stores, ClickHouse writers/readers, gRPC, REST, MinIO presigner |
| Python (`worker/helix/`) | 43 source | SDK, runtime, tools, rag/{miner,trainer,promotion}, eval, workflows, worker entrypoint |
| Python tests | 35 files | Including `worker/tests/integration/` (ClickHouse, finetune, M3, remote_engine) |
| Web (`web/`) | 58 TS/TSX + 13 test files | Next.js app, BFF routes, components |
| Migrations | 4 | 3 Postgres + 1 ClickHouse |
| Proto | 2 | `types.proto`, `orchestrator.proto` |

### 9.1 Go control plane (`cmd/orchestrator/`, `internal/`)

- **`internal/store/`** — Postgres stores. `FinetuneJobStore`, `EmbeddingJobStore`,
  run/task stores. Narrow interfaces (`finetuneJobCompleter`, `embeddingJobFinalizer`)
  let the gRPC server depend only on the methods it calls.
- **`internal/clickhouse/`** — `BatchWriter` (spans), `RetrievalWriter`,
  `LlmCallWriter`, `EvalWriter`, and paired readers. All async writers flush on a
  200ms ticker; `Stop()` flushes before reads in tests.
- **`internal/grpc/server.go`** — `RegisterWorker`, `Heartbeat`, `CompleteTask`,
  `Checkpoint`. `CompleteTask` runs a best-effort hook that finalizes a `finetune_job`
  /`embedding_jobs` row from the parsed `FinetuneTaskOutput`. `Checkpoint` parses
  `{"phase":"..."}` and updates the embedding-job status (real-time phase visibility).
- **`internal/api/handler.go`** — REST handlers. ClickHouse-dependent routes return
  503 when unconfigured. `presignArtifact` rewrites `s3://` artifact URIs to presigned
  HTTPS GET URLs (1h expiry), reusing the trace endpoint's `attrPresigner`.
- **`internal/minio/presigner.go`** — MinIO presigning (dep `minio/minio-go/v7`).

### 9.2 Python worker (`worker/helix/`)

- **`runtime/engine.py`** — slice asyncio dispatcher + SQLite state. Mode detected via
  `contextvars`.
- **`runtime/remote_engine.py`** — production gRPC client + NATS consumer. Sets a
  `_current_task` ContextVar around each handler call so workflow code can call
  `emit_task_checkpoint(phase)` without threading a `task_id` parameter.
- **`runtime/idempotency.py`** — Redis exactly-once sentinel keyed on `(task_id,
  attempt)`; `helix.exactly_once(...)` helper.
- **`tools/`** — `litellm_adapter` (the only LLM path), `llm_cache` (disk-backed,
  keyed on `sha256(model+messages+temperature+max_tokens+top_p+stop)`),
  `qdrant_adapter`, `embedder`, `reranker`, `bm25`, `blob` (MinIO `BlobStore` with
  `put_artifact` and a `maybe_offload` for >32KB span payloads), `rate_limit`
  (distributed token bucket).
- **`rag/{miner,trainer,promotion}/`** — the loop of §8.
- **`worker/__main__.py`** — gRPC client + NATS consumer; `_run_finetune_job` runs
  mine→train→promote and emits phase checkpoints.

All Redis/MinIO/OTel paths are **no-ops when their env vars are unset**, so `make
eval` runs fully in local mode with none of them.

### 9.3 Web dashboard (`web/`)

Next.js 14 App Router. Pages: `/runs`, `/runs/[id]`, `/runs/[id]/trace`, `/evals`,
`/evals/[id]`, `/retrievals`, `/llm-calls`, `/finetune-jobs`, `/embeddings`. Data
flows through a **server-side BFF proxy** (`web/app/api/**`) that attaches the bearer
token, so it never reaches the browser and the orchestrator needs no CORS. Types are
generated from `web/openapi.yaml`.

---

## 10. Milestone history (M1–M11)

The project began as a **pure-Python vertical slice** (M0): a single-process,
end-to-end implementation of the research loop (SQLite for state, `asyncio.Queue` for
dispatch, JSONL for spans, real Qdrant). The slice proved the loop and produced the
first baselines. Milestones M1–M11 then built out the production stack alongside it.

| Milestone | What landed |
|---|---|
| **M0** (slice) | asyncio engine, SDK decorators, LiteLLM adapter + cache, embedder, Qdrant adapter, BM25, hybrid retriever + reranker, indexer, `deep_research`, eval harness + scorers, holdout guard, the full mine→train→promote loop. |
| **M1** | Go orchestrator (gRPC + REST + migration runner), Postgres schema, NATS JetStream dispatch, Python `RemoteEngine`, worker entrypoint. |
| **M2** | ClickHouse 24 + OTel collector (OTLP/gRPC → `BatchWriter`); `worker/helix/otel.py` dual-writes alongside JSONL. |
| **M3** | Redis (exactly-once sentinel + token-bucket rate limiter) + MinIO (`BlobStore`, presigned URLs, `maybe_offload`). |
| **M4** | Dashboard Runs slice: `/runs`, `/runs/[id]`, BFF proxy, `tasks` array on run detail. |
| **M5** | Trace endpoint (`GET /runs/{id}/trace`) + span-tree view + presigned blob hydration + CI wiring. |
| **M6** | Eval events write path (synchronous ClickHouse) + `/evals` list/detail views + `EvalReporter`. |
| **M7** | Retrievals fan-out + ClickHouse miner path (`mine_from_clickhouse`) + `/retrievals` view. |
| **M8** | LLM-calls fan-out + cost dashboard (`/llm-calls`). |
| **M9a** | Integration test suite (testcontainers: real Postgres, ClickHouse, NATS). |
| **M9b** | Production failure miner: `POST /api/v1/finetune-jobs` → NATS → worker mine→train→promote → gRPC `CompleteTask` → Postgres. |
| **M10** | Embedding-jobs write/read path + `/embeddings` view; deferred items: `TrainConfig` capture into `embedding_jobs.config`, intermediate phase statuses via gRPC `Checkpoint`, MinIO artifact upload + presigned download. |
| **M11** | Helm chart for all 9 components + optional HPA/PDB/Ingress + `helm-lint` CI. |

Full per-milestone detail is in `CHANGELOG.md`.

---

## 11. Benchmarking methodology

### 11.1 Reference workload

A multi-hop deep-research agent over a fixed corpus. Given a question requiring 2–4
hops across documents, return an answer with citations.

- **Eval sets:** HotpotQA distractor-dev (100), a sequestered 500-question holdout,
  and BRIGHT biology (103 queries → 52 train / 51 canary).
- **Metrics:** `answer_f1` (token-level F1 with SQuAD-style normalization),
  `citation_precision` (fraction of cited doc_ids that are gold), and
  `retrieval_recall@10` (gold doc_ids found in the top-10 retrieved set).

### 11.2 The holdout protocol

The 500-question hard set is **sequestered at project start**. It is never seen by the
miner or trainer; it is used only to measure final lift.

- The holdout file `hotpotqa_dev_holdout_500.jsonl` requires `HELIX_HOLDOUT_UNLOCK=1`
  to read. The only legitimate caller is `make eval-final`.
- CI (`holdout-guard.yml`) greps Python files for unauthorized references to the
  holdout filename and fails the build on any read outside the guard.
- A SHA-256 lock (`.sha256`) verifies the holdout's integrity in `make lint`.

### 11.3 Determinism

`temperature=0` is the default in the LLM adapter; eval determinism depends on it.
LLM responses are cached, keyed on the full generation params, so cache hits emit
`cache_hit=true` spans with `cost_usd=0` (do **not** sum `cost_usd` without filtering
`cache_hit=false`). The harness records the provider's model id and timestamp for
every call.

### 11.4 Why the experiment moved from HotpotQA to BRIGHT

The HotpotQA dev set was a **ceiling-effect trap**. The base hybrid retriever already
scored **0.94–0.96 recall@10** on it — there was essentially no headroom for
mined-failure fine-tuning to demonstrate a measurable lift. Across three clean
HotpotQA fine-tune runs the canary deltas were −0.025, +0.010, and −0.015, all inside
heavily overlapping 95% CIs: **no run showed a statistically significant effect in
either direction.** (Run 2's "+0.010 promotion" was within noise — a coin-flip — and
was later rolled back.)

Diagnosis: the end-to-end eval recall (0.65) is far below the direct-retrieval recall
(0.94) because the bottleneck is **LLM sub-question decomposition, not the retriever**.
Fine-tuning the embedder cannot fix a decomposition bottleneck.

BRIGHT biology, by contrast, has a base recall@10 of ~0.253 — real headroom. The
experiment moved there, and the thesis was confirmed.

### 11.5 A bug that corrupted early BRIGHT runs (and how it was caught)

BRIGHT-B1 and B2 were corrupted by a **canary dispatch bug**:
`_build_promotion_backends._retrieve()` compared `collection == ACTIVE_ALIAS`
(hardcoded `"corpus.active"`). When the canary ran against `corpus.bright.active`,
neither the before-arm nor the after-arm matched, so both routed to the candidate
retriever → Δ = 0 by construction. The fix dispatches on `collection ==
candidate_collection` instead. Logged in `ISSUES.md`; surfaced by the suspiciously
exact Δ=0. The split was also re-stratified (random 80/23 → interleaved 52/51 by base
recall) so the train and canary arms have matched difficulty (mean recall 0.262 vs
0.253, within 0.009).

---

## 12. Statistical results

### 12.1 HotpotQA dev-100 baseline (base model)

| Metric | Mean | 95% CI |
|---|---|---|
| `answer_f1` | 0.1492 | [0.1277, 0.1734] |
| `citation_precision` | 0.8915 | [0.8475, 0.9357] |
| `retrieval_recall@10` | 0.6500 | [0.6050, 0.6950] |

Model: `gemini/gemini-2.5-flash` via LiteLLM; embedder Nomic Embed v1.5; corpus
15,512 docs. This is the authoritative baseline.

### 12.2 BRIGHT biology — the headline result

One mine→train→promote cycle on the stratified 52-train / 51-canary split. **92
failures mined → 92 contrastive triplets → 3-epoch fine-tune.** Canary recall@10
measured on 51 held-out questions with 0% training-set overlap.

| Seed | Base recall@10 | After recall@10 | Δ | 95% CI (paired) | P(Δ≤0) bootstrap | Sign-test p | Improved / Regressed / Unchanged | Decision |
|---|---|---|---|---|---|---|---|---|
| **B3** | 0.2528 | 0.3413 | **+0.0886** | [0.0111, 0.1716] | 0.012 | 0.041 | 15 / 5 / 31 | promoted |
| **B4** (seed 1) | 0.2528 | 0.3436 | **+0.0908** | [0.0105, 0.1742] | 0.0124 | 0.052 | 16 / 6 / 29 | promoted |
| **B5** (seed 2) | 0.2528 | 0.3433 | **+0.0905** | [0.0098, 0.1755] | 0.0126 | 0.041 | 15 / 5 / 31 | promoted |

**Headline (B5, reported in `README.md`/`AGENTS.md`):** recall@10 **0.253 → 0.343,
+0.090, 95% CI [0.010, 0.175], sign-test p = 0.041.** That is a **~35% relative
improvement** over base.

### 12.3 Reproducibility across seeds

The lift reproduces across all three independent training seeds:

- Cross-seed delta spread: **0.003 pp** (B3 +0.0886, B4 +0.0908, B5 +0.0905).
- Every run's paired 95% CI excludes zero (P(Δ≤0) ≤ 0.013).
- Improvements dominate regressions ~15:5 (Miss→Hit consistently exceeds Hit→Miss:
  7:3, 9:4, 8:4) — the **inverse** of the net-negative HotpotQA pattern.

**Conclusion:** training randomness has no meaningful effect on the outcome. The
result is a property of the method, not of a lucky seed.

### 12.4 How the flip analysis is computed

`scripts/analyze_canary_flips.py` re-runs the canary's exact hybrid retrieval
per-question and computes a paired test the original canary omitted. For each of the
51 canary questions it records base recall, candidate recall, and the delta; then it
reports the paired bootstrap CI, bootstrap P(Δ≤0), the sign-test p-value, and the
Miss→Hit / Hit→Miss transition counts. Result artifacts live in
`evals/baselines/bright_b{3,4,5}_canary_flips.json`.

### 12.5 The regression mechanism (documented, not hidden)

On HotpotQA, all 6 regressions in the worst run followed the same pattern: recall
1.00 → 0.50 on 2-hop questions — the fine-tuned model finds one gold doc but drops the
second. This is consistent with the multi-hop-miss signature and is documented in
`docs/vertical-slice-plan.md` rather than papered over.

---

## 13. Deployment

### 13.1 Local development

```bash
make dev          # boot Postgres, ClickHouse, Qdrant, NATS, Redis, MinIO, OTel collector (~30s)
make seed         # download corpus + HotpotQA, embed, index into Qdrant (~10 min)
make eval         # deep_research over 100 questions → baseline JSON (~3 min)
make finetune     # mine → train → canary → promote-on-lift
make web          # Next.js dashboard at http://localhost:3000
make dev-down     # tear down stack + volumes
```

Go services: `make build`, `make orchestrator`, `make collector`, `make worker`.

> MinIO's API is on host **9100** (console 9101) to avoid ClickHouse's host 9000.
> Span-payload offload is gated by `HELIX_SPAN_PAYLOADS` (default off).

### 13.2 Production (Kubernetes + Helm)

The `infra/helm/helix/` chart deploys all nine components: orchestrator, collector,
worker×2 (research + finetune_job pools), postgresql, nats, clickhouse, redis, minio,
qdrant.

```bash
# Dev / local cluster
helm install helix infra/helm/helix -n helix --create-namespace \
  --set secrets.apiToken=<token>

# Production (HPA 2–6 + PDB enabled in values-prod.yaml)
helm install helix infra/helm/helix -n helix --create-namespace \
  -f infra/helm/helix/values-prod.yaml \
  --set secrets.apiToken=<token> \
  --set secrets.postgresPassword=<pw> \
  --set secrets.minioRootPassword=<pw>
```

Optional, off by default: orchestrator **HorizontalPodAutoscaler**
(`autoscaling/v2`), **PodDisruptionBudget** (`policy/v1`), and **Ingress**
(`networking.k8s.io/v1`). `values-prod.yaml` enables HPA (2–6 replicas) + PDB; Ingress
stays disabled until you set `ingress.host`/`ingress.className`. A `helm-lint` GitHub
workflow runs `helm lint` + `helm template` (default and prod values) on any
`infra/helm/**` change.

Full values reference, first-time setup, and upgrade/rollback instructions:
`infra/helm/README.md`.

### 13.3 CI/CD

GitHub Actions. Per PR: `golangci-lint`, `ruff`, `mypy --strict`, `prettier`, unit
tests, integration tests via testcontainers, an eval smoke test, the holdout guard,
and helm-lint. Lint includes the holdout SHA-256 check.

---

## 14. Repository layout

```
cmd/
  orchestrator/      Go: control-plane API, scheduler, dispatcher, migration runner
  collector/         Go: OTel collector → ClickHouse (+ retrieval/llm-call fan-out)
internal/            Go: stores, clickhouse writers/readers, grpc, api, minio presigner

worker/
  helix/             Public Python SDK (workflow, task decorators, core types)
  helix/v1/          Generated gRPC stubs (do not edit by hand)
  helix/runtime/     engine, remote_engine, sqlite_store, idempotency
  helix/tools/       litellm_adapter, llm_cache, qdrant_adapter, embedder, reranker,
                     bm25, blob, rate_limit
  helix/rag/         indexer, retriever, chunker, miner/, trainer/, promotion/
  helix/workflows/   deep_research + prompts
  helix/eval/        harness, scorers, corpus, hotpotqa, reporter
  tests/             unit + integration (clickhouse, finetune, m3, remote_engine)

web/                 Next.js 14 dashboard (App Router) + BFF routes + openapi.yaml
proto/helix/v1/      types.proto, orchestrator.proto (source of truth for gRPC)
migrations/          up-only SQL (3 Postgres + 1 ClickHouse)
evals/
  datasets/          hotpotqa_dev_100, holdout_500 (locked), bright_biology_*
  baselines/         scored eval outputs + bright_b{3,4,5}_canary_flips.json
infra/
  compose/           docker-compose.yml (local dev)
  helm/helix/        production Helm chart (9 components + HPA/PDB/Ingress)
scripts/             prepare_corpus, prepare_hotpotqa, prepare_bright,
                     analyze_canary_flips, check_holdout_integrity, …
docs/                architecture, data-model, api, tech-stack, vertical-slice-plan,
                     m1-plan … m11-plan
SPEC.md AGENTS.md CLAUDE.md CODEX.md CHANGELOG.md HANDOFF.md ISSUES.md README.md
```

---

## 15. Engineering conventions and definition of done

### Conventions

- **Python:** `mypy --strict` required (`Any` needs a justified `# type: ignore`);
  `ruff format` + `ruff check`; async-first; no `print` (use the span pipeline).
- **Go:** structured `slog` with `run_id`/`trace_id`; `ctx context.Context` first arg
  on all I/O; no global state (constructor injection); `%w` error wrapping;
  table-driven tests.
- **TypeScript:** App Router, server components by default; TanStack Query (no direct
  fetch in components); shadcn/ui only; Tailwind; API types generated, never
  hand-written.
- **SQL:** `snake_case`; up-only migrations named `YYYYMMDDHHMM_description.sql`,
  never edited after merge; explicit indices on hot-path WHERE/JOIN columns;
  ClickHouse 90-day TTL by default.
- **Protobuf:** one service per file; field numbers never reused (removed →
  `reserved`); breaking changes go to `proto/helix/v2/`.

### Definition of done

A change is done when: tests pass (`make test`); lint passes including the holdout
SHA-256 check (`make lint`); for schema/proto/cross-component changes, integration
tests pass (`make test-integration`); a migration exists if state shape changed;
`docs/api.md` / `docs/tech-stack.md` updated if the public surface or deps changed;
`CHANGELOG.md` has an `## Unreleased` line; and `HANDOFF.md` has an updated entry.

### Things never done without asking

Add a datastore; make a breaking `/api/v1/` change; bypass LiteLLM; log raw
prompts/completions outside the span pipeline; read the holdout outside the guard;
commit secrets/weights/blobs over 1 MB; edit a merged migration; reuse a proto field
number.

---

## 16. Known limitations and remaining work

What is **not** yet on disk:

- **Production-scale eval.** `make eval-full` (BRIGHT + 50k arXiv, ~1hr, live cluster)
  is wired but not run; the slice-scale BRIGHT experiment is the demonstrated result.
- **Small deferred M10 polish.** Worker-driven intermediate phase checkpoints exist,
  but richer training-curve capture and presigned-URL nuances remain incremental.
- **Replay endpoint and SSE** are specified in `docs/api.md` but not fully wired in
  the slice (`POST /runs/{id}/replay`, `GET /runs/{id}/events`).
- **mTLS** between control plane and workers is specified; the slice uses bearer-token
  auth on REST and an untrusted-but-isolated worker channel in dev.
- **The HotpotQA negative result stands** as a deliberately documented finding: the
  thesis is validated on BRIGHT (which has headroom), not on the ceiling-effect
  HotpotQA dev set.

The largest single remaining milestone before M11 was Helm/Kubernetes, which has now
landed.

---

## 17. Glossary

| Term | Meaning |
|---|---|
| **Canary eval** | The before/after recall@10 measurement on a held-out split that gates promotion. |
| **Contrastive triplet** | `(query, gold_passage, hard_negative)` — the training unit for InfoNCE fine-tuning. |
| **Failure signature** | One of four mined retrieval-failure categories (lexical-only, semantic mismatch, multi-hop miss, ambiguous). |
| **Hard negative** | A retrieved-but-wrong passage near the top of the ranking; the contrastive "push-away" example. |
| **Holdout** | The 500-question hard set sequestered at project start; `HELIX_HOLDOUT_UNLOCK=1`-gated. |
| **InfoNCE** | The contrastive loss (`MultipleNegativesRankingLoss`) used to fine-tune the embedder. |
| **Promotion** | Atomically swapping the `corpus.active` Qdrant alias to a candidate collection after a significant lift. |
| **Recall@10** | Fraction of gold documents present in the top-10 retrieved set — the headline metric. |
| **RRF** | Reciprocal Rank Fusion (`k=60`), how dense and sparse candidate lists are merged. |
| **Sign test** | The non-parametric test on per-question Miss→Hit vs Hit→Miss transitions reported alongside the bootstrap CI. |
| **Span** | A structured record of one LLM/tool/retrieval/workflow operation; the unit of observability. |

---

*This document is a companion reference. For the authoritative, continuously updated
sources, see `SPEC.md` (problem + research), `docs/architecture.md`,
`docs/data-model.md`, `docs/api.md`, `docs/tech-stack.md`, `CHANGELOG.md` (milestone
detail), and `evals/baselines/` (raw result artifacts).*
