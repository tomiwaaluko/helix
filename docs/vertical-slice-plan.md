# Vertical Slice Plan

> Pure-Python, single-process implementation of the research loop, end-to-end.
> Target: ~2 weeks. Each task is independently sign-off-able.

## Goal

A single command that:

1. Indexes a 1–2k document corpus into Qdrant.
2. Runs the `deep_research` workflow over 100 HotpotQA distractor-dev questions.
3. Produces an eval report with `answer_f1`, `citation_precision`, and `retrieval_recall@10`.

## What we use

| Production component | Vertical slice replacement |
|---|---|
| Go orchestrator | Python asyncio loop, in-process |
| Postgres | SQLite via `aiosqlite` |
| NATS JetStream | `asyncio.Queue` |
| ClickHouse | Skipped — spans logged to structured JSON file |
| Redis | Skipped — no distributed locks needed |
| MinIO | Local filesystem (`data/` directory) |
| Qdrant | Qdrant (real — runs in Docker, single node) |
| OTel collector | Skipped — spans written directly to JSON log |

Everything else (LiteLLM, Nomic Embed, BGE reranker, sentence-transformers) is used as specified in `tech-stack.md`.

## What we defer

- gRPC, protobuf, stub generation.
- Multi-worker / multi-process execution.
- Checkpointing, dead-letter queue, retry with backoff.
- Dashboard (web/).
- ~~Failure mining and embedding fine-tuning (the loop's consumer side). This slice produces the traces and eval results that the miner will later consume.~~ **Built as a slice extension** (post-M0): the full mine → train → promote loop now lives in `worker/helix/rag/{miner,trainer,promotion}/` and is orchestrated end-to-end by `python -m helix.cli finetune` / `make finetune`. See "Fine-tune loop (slice extension)" below.
- Replay.

## Directory structure

```
Makefile                        # make seed, eval, eval-final, test, lint, fmt, dev
worker/
  pyproject.toml                # single package for the slice
  helix/
    __init__.py                 # public SDK surface
    types.py                    # Doc, Answer, ScoreResult
    decorators.py               # @workflow, @task, gather
    runtime/
      engine.py                 # asyncio dispatcher + SQLite state
      sqlite_store.py           # state store
    tools/
      litellm_adapter.py        # LLM calls via LiteLLM
      llm_cache.py              # disk-backed LLM response cache
      qdrant_adapter.py         # vector search
      embedder.py               # Nomic Embed v1.5 wrapper
      reranker.py               # BGE cross-encoder
      bm25.py                   # sparse retrieval (rank_bm25)
    rag/
      indexer.py                # chunk + embed + upsert
      retriever.py              # hybrid dense+sparse+RRF → rerank
      chunker.py                # semantic + structural splitting
    workflows/
      deep_research.py          # reference agent
      prompts/                  # system prompt text files
    eval/
      harness.py                # dataset loading + eval runner + holdout guard
      scorers.py                # answer_f1, citation_precision, retrieval_recall@10
    logging.py                  # structured JSON span logger
    cli.py                      # entry points

evals/
  datasets/
    hotpotqa_dev_100.jsonl              # 100 distractor-dev questions
    hotpotqa_dev_holdout_500.jsonl      # 500 sequestered holdout questions
    hotpotqa_dev_holdout_500.sha256     # hash-lock for integrity verification
  baselines/                            # baseline results land here

scripts/
  prepare_corpus.py             # download + format 1–2k doc subset
  prepare_hotpotqa.py           # download + format 100 + 500 holdout questions
  check_holdout_integrity.py    # SHA-256 verification of holdout file

.github/
  workflows/
    holdout-guard.yml           # CI: reject holdout reads outside the guard

data/                           # gitignored; corpora, indices, model cache, llm_cache
```

## Tasks

### Task 1 — Project scaffolding

**Scope:** Create `worker/pyproject.toml`, directory tree, empty `__init__.py` files, `.gitignore` entries for `data/`, `*.db`. Pin dependencies.

**Dependencies:**

```toml
[project]
requires-python = ">=3.12"
dependencies = [
  "litellm>=1.40",
  "qdrant-client>=1.9",
  "sentence-transformers>=3.0",
  "aiosqlite>=0.20",
  "rank-bm25>=0.2",
  "httpx>=0.27",
  "click>=8.1",
  "pydantic>=2.7",
]

[project.optional-dependencies]
dev = ["ruff>=0.4", "mypy>=1.10", "pytest>=8", "pytest-asyncio>=0.23"]
```

**Makefile:** Create a root `Makefile` that wraps CLI commands to match the interface in CLAUDE.md. This is the canonical entry point for all operations in the slice.

```makefile
.PHONY: seed eval eval-full eval-final test lint fmt dev dev-down

# Boot Qdrant (the only external dependency for the slice)
dev:
	docker compose -f infra/compose/docker-compose.yml up -d qdrant

dev-down:
	docker compose -f infra/compose/docker-compose.yml down -v

# Download corpus + datasets, embed, index into Qdrant
seed: data/corpus.jsonl evals/datasets/hotpotqa_dev_100.jsonl
	python -m helix.cli index --corpus data/corpus.jsonl --collection corpus.base

data/corpus.jsonl:
	python scripts/prepare_corpus.py

evals/datasets/hotpotqa_dev_100.jsonl:
	python scripts/prepare_hotpotqa.py

# Eval: 100-question smoke test (the deliverable command)
eval:
	python -m helix.cli eval \
	  --workflow deep_research \
	  --dataset evals/datasets/hotpotqa_dev_100.jsonl \
	  --scorers answer_f1,citation_precision,retrieval_recall@10 \
	  --concurrency 4 \
	  --output evals/baselines/hotpotqa_dev_100_baseline.json

# Alias for CLAUDE.md compatibility
test-eval-smoke: eval

# Full eval (placeholder — same as eval in the slice)
eval-full: eval

# Held-out hard set — the ONLY command allowed to read the holdout file
eval-final:
	HELIX_HOLDOUT_UNLOCK=1 python -m helix.cli eval \
	  --workflow deep_research \
	  --dataset evals/datasets/hotpotqa_dev_holdout_500.jsonl \
	  --scorers answer_f1,citation_precision,retrieval_recall@10 \
	  --concurrency 4 \
	  --output evals/baselines/hotpotqa_holdout_500_result.json

test:
	cd worker && python -m pytest tests/ -x -q

lint:
	cd worker && ruff check . && mypy --strict helix/

fmt:
	cd worker && ruff format . && ruff check --fix .
```

**Done when:** `cd worker && pip install -e '.[dev]' && python -c 'import helix'` succeeds. `make lint` and `make test` pass on the empty package.

**Estimated effort:** 2–3 hours.

---

### Task 2 — SDK decorators (local execution)

**Scope:** Implement `@helix.workflow`, `@helix.task`, and `helix.gather` for **in-process local execution only** (the `.local()` path described in `api.md`). No runtime, no SQLite, no queue. Tasks are just `await`ed directly. This gives us a working programming model before wiring in the engine.

**Files:** `helix/decorators.py`, `helix/types.py`, `helix/__init__.py`.

**`types.py`:**

```python
@dataclass
class Doc:
    id: str
    text: str
    source: str
    score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class Answer:
    text: str
    citations: list[Citation]
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class Citation:
    doc_id: str
    passage: str
    relevance: float = 0.0
```

**Behavior:**

- `@helix.task(retries=N, timeout="Xs")` wraps an `async def`. In local mode, retries and timeout are ignored; the function is called directly.
- `@helix.workflow(name=..., version=...)` wraps an `async def`. Adds a `.local(**kwargs)` classmethod that calls the function directly.
- `helix.gather(*awaitables)` is `asyncio.gather`.

**Done when:** A toy workflow with two tasks can be defined and run via `.local()`. Unit tests pass.

**Estimated effort:** 2–3 hours.

---

### Task 3 — Structured span logger

**Scope:** A minimal span context manager that writes JSON lines to a file. Not OTel, but same shape: `trace_id`, `span_id`, `parent_span_id`, `name`, `kind`, `start_time`, `end_time`, `attributes`. This is the observability surface the eval harness and future miner will read.

**Files:** `helix/logging.py`.

**Behavior:**

- `SpanLogger` class with a configurable output path (default `data/spans.jsonl`).
- `@contextmanager span(name, kind, attributes)` that writes a JSON line on `__exit__`.
- Thread-local / context-var span stack so nested spans get correct `parent_span_id`.
- `trace_id` is set per-run, propagated via `contextvars`.

**Done when:** Running a toy workflow with the logger produces a valid JSONL file with nested spans. Unit tests verify parent-child relationships.

**Estimated effort:** 2–3 hours.

---

### Task 4 — SQLite state store

**Scope:** Implement the subset of the Postgres schema needed for the vertical slice, in SQLite.

**Files:** `helix/runtime/sqlite_store.py`.

**Tables (simplified):**

```sql
CREATE TABLE runs (
  id TEXT PRIMARY KEY,
  workflow_name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  input TEXT NOT NULL,      -- JSON
  output TEXT,              -- JSON
  error TEXT,               -- JSON
  created_at TEXT NOT NULL,
  finished_at TEXT
);

CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  node_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  input TEXT NOT NULL,
  output TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  UNIQUE(run_id, node_id)
);

CREATE TABLE eval_results (
  id TEXT PRIMARY KEY,
  eval_id TEXT NOT NULL,
  example_id TEXT NOT NULL,
  scorer TEXT NOT NULL,
  score REAL NOT NULL,
  details TEXT,             -- JSON
  created_at TEXT NOT NULL
);

CREATE TABLE datasets (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  version INTEGER NOT NULL,
  path TEXT NOT NULL,
  size INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  UNIQUE(name, version)
);
```

**API:** Async methods: `create_run`, `update_run`, `create_task`, `update_task`, `store_eval_result`, `get_eval_results`, `register_dataset`.

**Done when:** Unit tests exercise all CRUD paths. `mypy --strict` passes.

**Estimated effort:** 3–4 hours.

---

### Task 5 — Asyncio engine

**Scope:** A single-process "orchestrator" that resolves task dependencies and dispatches via `asyncio.Queue`. This replaces the Go orchestrator + NATS for the vertical slice.

**Files:** `helix/runtime/engine.py`.

**Behavior:**

1. `Engine.submit(workflow_fn, input)` creates a `Run` row, materializes tasks from the decorated functions, and enqueues ready tasks.
2. A worker loop pulls tasks from the queue, calls the handler, writes results back to SQLite, and enqueues newly-ready downstream tasks.
3. When all tasks for a run complete, the run is marked `succeeded`. If any task fails after its retry budget (1 in the slice), the run is marked `failed`.
4. `Engine.run_until_complete(run_id)` blocks until the run finishes.

**Concurrency:** Configurable max-concurrent-tasks (default 4) via `asyncio.Semaphore`.

**Integration with logging:** The engine sets `trace_id` in the context var before dispatching each task.

**Engine contract — `.local()` vs `Engine.submit()`:**

The decorators `@helix.task` and `helix.gather` behave differently depending on execution mode. This section is the contract.

| Aspect | `.local()` mode | `Engine.submit()` mode |
|---|---|---|
| `@helix.task` invocation | Direct `await` of the underlying `async def`. No wrapping. | Engine enqueues the task. The caller receives a `Future` that resolves when the engine completes the task and writes the result. |
| `helix.gather` | Delegates to `asyncio.gather`. | Enqueues all tasks concurrently, returns a `Future` that resolves when all complete. Each parallel branch gets its own task row. |
| SQLite writes | None. | **On enqueue:** `tasks` row inserted with `status='ready'`, `input` populated. **On dispatch:** `status='running'`, `attempts` incremented. **On success:** `status='succeeded'`, `output` written. **On failure:** `status='failed'`, `error` written. Run-level: `runs` row created on `submit()` with `status='running'`; updated to `succeeded`/`failed` when all tasks resolve. |
| Span emission | None (unless the task internally uses the logger). | Engine wraps each task dispatch in a span (`kind="task"`). Tool adapters emit their own child spans as in both modes. |
| Retries | None. Exception propagates immediately. | Single retry (budget=1 for the slice). On failure, task is re-enqueued once. After exhaustion, task and run are marked `failed`. |
| Context vars | `trace_id` is unset. | `trace_id` and `span_id` are set before dispatch, visible to tool adapters for span parenting. |

The key invariant: **workflow code is identical in both modes.** The `@helix.task` decorator detects which mode is active via a `contextvars.ContextVar[Engine | None]` (default `None` → local mode). When an engine is active, calling a `@helix.task`-decorated function returns a `Future` backed by the engine's queue rather than directly awaiting the function.

**Done when:** The toy workflow from Task 2 runs through the engine, SQLite rows are correct at each lifecycle point (verified by test assertions between steps), spans are emitted. Integration test passes.

**Estimated effort:** 4–6 hours.

---

### Task 6 — LiteLLM tool adapter

**Scope:** Thin async wrapper around LiteLLM's `acompletion` that emits spans, with a disk-backed response cache for deterministic reruns and cost control.

**Files:** `helix/tools/litellm_adapter.py`, `helix/tools/llm_cache.py`.

**Behavior:**

- `async def llm_call(messages, model, temperature, max_tokens) -> LLMResponse`.
- Default `temperature=0`. This is the default for all eval and research workloads; callers may override but the adapter defaults to deterministic output. The `decompose` and `synthesize` tasks in the deep_research workflow (Task 15) use this default.
- Emits a span with `kind="llm"`, attributes: `model`, `prompt_tokens`, `completion_tokens`, `cost_usd`, `cache_hit` (bool), `replayed` (bool). On cache hits, `cost_usd=0` and `replayed=true`.
- Reads model from env `HELIX_DEFAULT_MODEL` (default `claude-sonnet-4-20250514`).
- No retry logic in the adapter; LiteLLM handles provider retries.

**Response cache:**

- Disk-backed, stored in `data/llm_cache/`. Each entry is a JSON file named by its cache key.
- Cache key: `sha256(json_canonicalize({"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens, "top_p": top_p, "stop": stop}))`. Uses `json.dumps(obj, sort_keys=True, separators=(',', ':'))` for canonicalization. All generation parameters that affect output are included; omitted/None values are excluded from the dict before hashing.
- On cache hit: return the stored response immediately. Span is emitted with `cache_hit=true`, `replayed=true`, and `cost_usd=0` (not the original cost — prevents over-counting when summing across reruns). Token counts are replayed from the cached response for informational purposes only.
- On cache miss: call LiteLLM, write the response to cache, return it.
- **On by default** when `HELIX_ENV != "production"` (i.e., always in dev/eval). Disabled explicitly via `--no-cache` CLI flag (sets env `HELIX_LLM_CACHE=0`).
- Cache is gitignored (`data/llm_cache/` in `.gitignore`).
- No TTL or eviction. Cache is append-only; delete `data/llm_cache/` to clear.

**Why:** Re-running the eval over 100 questions costs $5–15 per run. Caching makes iteration on scorers, prompt formatting, and the harness itself free after the first run. It also makes eval results deterministic across reruns with the same prompts.

**Done when:** A call to Claude Sonnet via the adapter returns a response and produces a span. A second call with the same arguments returns the cached response without hitting LiteLLM. `--no-cache` bypasses the cache. Unit test with a mock LiteLLM client verifies both paths.

**Estimated effort:** 3–4 hours.

---

### Task 7 — Embedding client

**Scope:** Wrapper around `sentence-transformers` for Nomic Embed v1.5. Handles task-prefix tokens and batched encoding.

**Files:** `helix/tools/embedder.py`.

**Behavior:**

- `Embedder(model_name="nomic-ai/nomic-embed-text-v1.5")`.
- `embed_queries(texts: list[str]) -> list[list[float]]` — prepends `search_query:`.
- `embed_documents(texts: list[str]) -> list[list[float]]` — prepends `search_document:`.
- Batches internally (default batch size 64).
- Emits spans with `kind="internal"`, attributes: `model`, `count`, `dimension`.

**Done when:** Embedding a batch of 10 texts returns 768-dim vectors. Unit test with a tiny model stub passes.

**Estimated effort:** 2 hours.

---

### Task 8 — Qdrant adapter

**Scope:** Async Qdrant client wrapper for upsert and search, with alias-aware collection resolution.

**Files:** `helix/tools/qdrant_adapter.py`.

**Behavior:**

- `QdrantAdapter(url, collection_alias="corpus.active")`.
- `upsert(points: list[Point])` — batched upsert (batch size 100).
- `search(query_vector, top_k, filters) -> list[ScoredPoint]`.
- `create_collection(name, vector_size, distance)`.
- `set_alias(alias, collection)`.
- Emits spans with `kind="retrieval"`.

**Done when:** Integration test against a local Qdrant (Docker) creates a collection, upserts 100 points, and searches. Span emitted.

**Estimated effort:** 2–3 hours.

---

### Task 9 — Chunker

**Scope:** Document chunking for the indexer. Structural splitting at section/paragraph boundaries with a target chunk size.

**Files:** `helix/rag/chunker.py`.

**Behavior:**

- `chunk_document(doc_id, text, source, max_tokens=512, overlap_tokens=64) -> list[Chunk]`.
- Splits on double newlines (paragraph boundaries) first, then sentence boundaries if a paragraph exceeds `max_tokens`.
- Each `Chunk` has: `chunk_id` (deterministic: `{doc_id}#chunk{N}`), `doc_id`, `text`, `source`, `token_count`.
- Token counting via `tiktoken` with `cl100k_base` (good enough for sizing; not model-specific).

**Done when:** Chunking a 2000-word document produces chunks within the target size. Edge cases (tiny docs, single-paragraph docs) tested.

**Estimated effort:** 2–3 hours.

---

### Task 10 — Indexer workflow

**Scope:** End-to-end corpus ingestion: load documents, chunk, embed, upsert to Qdrant.

**Files:** `helix/rag/indexer.py`.

**Input:** Path to a JSONL file where each line is `{"id": "...", "text": "...", "source": "..."}`.

**Behavior:**

1. Load documents from JSONL.
2. Chunk all documents.
3. Embed all chunks in batches (using the embedder from Task 7).
4. Upsert to Qdrant (using the adapter from Task 8) with payload: `doc_id`, `chunk_id`, `source`, `text`.
5. Create the `corpus.active` alias pointing to the collection.
6. Log summary: total docs, total chunks, time elapsed.

**CLI entry point:** `python -m helix.cli index --corpus data/corpus.jsonl --collection corpus.base`.

**Done when:** Indexing a 100-document test corpus into a local Qdrant succeeds. Collection is searchable. Alias resolves.

**Estimated effort:** 3–4 hours.

---

### Task 11 — BM25 sparse retrieval

**Scope:** In-memory BM25 index over the same corpus, for hybrid retrieval.

**Files:** `helix/tools/bm25.py`.

**Behavior:**

- `BM25Index.build(chunks: list[Chunk])` — tokenize and build the index using `rank_bm25.BM25Okapi`.
- `BM25Index.search(query: str, top_k: int) -> list[ScoredChunk]`.
- `BM25Index.save(path)` / `BM25Index.load(path)` — pickle to `data/bm25_index.pkl` for reuse.
- Tokenization: simple whitespace + lowercase. Adequate for the vertical slice; the production system would use tantivy.

**Done when:** BM25 search over a small corpus returns ranked results. Unit test verifies ranking sanity.

**Estimated effort:** 2 hours.

---

### Task 12 — Hybrid retriever + reranker

**Scope:** The full retrieval pipeline: dense (Qdrant) + sparse (BM25) → RRF fusion → BGE reranker → top-10.

**Files:** `helix/rag/retriever.py`, `helix/tools/reranker.py`.

**Retriever behavior:**

1. Embed the query (dense).
2. Search Qdrant for top-50.
3. Search BM25 for top-50.
4. Reciprocal Rank Fusion at `k=60` to produce a fused top-50.
5. Rerank the fused top-50 with BGE cross-encoder → return top-10.
6. Emit a span with `kind="retrieval"`, attributes: `query`, `retriever="hybrid+reranked"`, `top_k`, `results` (passage IDs and scores).

**Reranker behavior:**

- `Reranker(model_name="BAAI/bge-reranker-base")`.
- `rerank(query: str, passages: list[str], top_k: int) -> list[RankedPassage]`.
- Uses `sentence_transformers.CrossEncoder`.

**Done when:** End-to-end retrieval over the indexed test corpus returns 10 passages with scores. Integration test against Qdrant passes.

**Estimated effort:** 4–5 hours.

---

### Task 13 — Corpus preparation script

**Scope:** Download and format a 1–2k document subset suitable for multi-hop questions.

**Files:** `scripts/prepare_corpus.py`.

**Strategy:** Use HotpotQA's Wikipedia paragraphs. The HotpotQA distractor-dev set includes 10 context paragraphs per question (2 gold + 8 distractors). Across 100 questions, this yields ~600–800 unique documents after dedup. To reach 1–2k, we include all unique paragraphs from the first 500 distractor-dev questions but only evaluate on the first 100.

**Output:** `data/corpus.jsonl` — one line per document: `{"id": "wiki_<title_hash>", "text": "<paragraph>", "source": "hotpotqa_wiki", "title": "<title>"}`.

**Done when:** Running the script produces a JSONL corpus of 1–2k documents. Documents are deduped by title.

**Estimated effort:** 2–3 hours.

---

### Task 14 — HotpotQA dataset preparation

**Scope:** Download HotpotQA distractor-dev, extract 100 questions, format as Helix eval dataset.

**Files:** `scripts/prepare_hotpotqa.py`.

**Output:** `evals/datasets/hotpotqa_dev_100.jsonl` with the schema from `data-model.md`:

```json
{
  "id": "hotpotqa_dev_001",
  "input": {"question": "..."},
  "expected_output": {
    "answer": "...",
    "supporting_facts": [{"doc_id": "wiki_<hash>", "sent": 3}]
  },
  "metadata": {"hops": 2, "type": "comparison"}
}
```

**Done when:** 100-line JSONL file exists. `doc_id` values in `supporting_facts` match IDs in the corpus from Task 13. A validation script confirms referential integrity.

**Estimated effort:** 2 hours.

---

### Task 14b — Holdout set sequestration

**Scope:** Sequester 500 additional HotpotQA distractor-dev questions (questions 101–600, disjoint from the 100 in Task 14) as the held-out hard set for measuring the fine-tuning lift. This file must not be read by any code path except `make eval-final`.

**Files:** `scripts/prepare_hotpotqa.py` (extended), `evals/datasets/hotpotqa_dev_holdout_500.jsonl`, `scripts/check_holdout_integrity.py`.

**Output:** `evals/datasets/hotpotqa_dev_holdout_500.jsonl` — same schema as Task 14, 500 lines.

**Hash-lock:**

- On generation, compute `SHA-256` of the file and write it to `evals/datasets/hotpotqa_dev_holdout_500.sha256`.
- The `.sha256` file is checked into git. The `.jsonl` file is also checked in (it's small — 500 lines of metadata, no embeddings).
- `scripts/check_holdout_integrity.py` verifies the hash matches. Run as part of `make lint`.

**Access guard:**

- The eval harness (`helix/eval/harness.py`) checks for the substring `holdout` in the dataset path. If found, it requires the environment variable `HELIX_HOLDOUT_UNLOCK=1` to proceed; otherwise it raises `HoldoutAccessError` with a message explaining that holdout reads are restricted to `make eval-final`.
- `make eval-final` (defined in Task 1's Makefile) is the only target that sets this variable.
- A CI check (GitHub Actions workflow `.github/workflows/holdout-guard.yml`) greps all Python files for reads of `hotpotqa_dev_holdout_500.jsonl` outside of the harness guard. Specifically: it fails if any file other than `helix/eval/harness.py` and `scripts/prepare_hotpotqa.py` contains the holdout filename string. This catches accidental hardcoded reads in notebooks, scripts, or test fixtures.

**Done when:**
1. `make eval-final` succeeds (with the unlock var set).
2. `python -m helix.cli eval --dataset evals/datasets/hotpotqa_dev_holdout_500.jsonl` fails with `HoldoutAccessError`.
3. `make lint` verifies the SHA-256 hash.
4. The CI workflow catches a test file that references the holdout filename.

**Estimated effort:** 2–3 hours.

---

### Task 15 — Deep research workflow

**Scope:** The reference multi-hop research agent, implemented as a Helix workflow.

**Files:** `helix/workflows/deep_research.py`.

**Workflow DAG:**

```
decompose(question) → [subquery₁, subquery₂, ...]
    ↓ (parallel)
retrieve(subqueryᵢ) → [docs]
    ↓ (gather)
synthesize(question, all_docs) → Answer
```

**Task implementations:**

1. **`decompose`** — LLM call via `llm_call()` (Task 6) at default `temperature=0`. Prompt: given a complex question, break it into 2–4 atomic subqueries. Returns `list[str]`.
2. **`retrieve`** — Calls the hybrid retriever from Task 12. Returns `list[Doc]`.
3. **`synthesize`** — LLM call via `llm_call()` (Task 6) at default `temperature=0`. Prompt: given the question and retrieved evidence, produce an answer with citations. Each citation references a `doc_id` from the evidence. Returns `Answer`.

**Retrieved doc IDs for the recall scorer:** The workflow **must** attach the union of all retrieved doc IDs (from all subquery retrievals, before synthesis filtering) to the returned `Answer.metadata["retrieved_doc_ids"]` as a `list[str]`. This is how the `retrieval_recall_at_k` scorer (Task 17) finds the retrieval results without needing access to intermediate span data. Concretely:

```python
all_retrieved_ids: list[str] = []
for subquery_docs in evidence_per_subquery:
    all_retrieved_ids.extend(doc.id for doc in subquery_docs)
# deduplicate preserving order
seen: set[str] = set()
unique_ids = [x for x in all_retrieved_ids if not (x in seen or seen.add(x))]
answer.metadata["retrieved_doc_ids"] = unique_ids
```

**Prompts:** System prompts live in `helix/workflows/prompts/` as plain text files. Decompose prompt instructs the model to produce a JSON array. Synthesize prompt instructs the model to cite evidence by `doc_id` and to say "I don't know" when evidence is insufficient. Both LLM calls use `temperature=0` (the adapter default from Task 6) for deterministic output across eval runs.

**Done when:** Running `deep_research.local(question="...")` against the indexed corpus returns an `Answer` with citations and `answer.metadata["retrieved_doc_ids"]` populated. Manual spot-check of 3 questions shows reasonable decomposition and citation.

**Estimated effort:** 4–5 hours.

---

### Task 16 — Eval harness core

**Scope:** Dataset loading, scorer registration, and the eval runner loop.

**Files:** `helix/eval/harness.py`.

**Behavior:**

1. `load_dataset(path) -> list[Example]` — read JSONL, validate schema.
2. `evaluate(workflow_fn, dataset, scorers, concurrency) -> EvalReport`.
   - For each example, submit a run of the workflow with `example.input`.
   - Collect the output.
   - Run each scorer against `(example, output)`.
   - Store per-example results in SQLite (`eval_results` table).
   - Aggregate: mean, std, 95% bootstrap CI (1000 samples) per scorer.
3. `EvalReport` is a dataclass with `.metrics` (dict of aggregates), `.per_example` (list of per-example dicts), `.to_json(path)` for serialization.

**Concurrency model:** Uses `asyncio.Semaphore(concurrency)` to limit parallel workflow runs.

**Done when:** Running the harness with a mock workflow and a 10-example dataset produces correct aggregate metrics. Bootstrap CIs are computed. Results are in SQLite.

**Estimated effort:** 4–5 hours.

---

### Task 17 — Scorers

**Scope:** The three metrics required by the goal.

**Files:** `helix/eval/scorers.py`.

**`answer_f1(example, prediction) -> float`:**
- Token-level F1 between the predicted answer and the gold answer.
- Tokenization: lowercase, split on whitespace + punctuation, remove articles and stopwords (standard HotpotQA normalization).
- Returns 0.0 if either side is empty.

**`citation_precision(example, prediction) -> float`:**
- Fraction of cited `doc_id`s in the prediction that appear in `expected_output.supporting_facts`.
- Returns 1.0 if no citations (vacuous precision — flagged in metadata).

**`retrieval_recall_at_k(example, prediction, k=10) -> float`:**
- Requires the retriever to attach the top-k retrieved doc IDs to the prediction metadata.
- Fraction of gold `doc_id`s (from `supporting_facts`) found in the top-k retrieved set.
- This is the metric the future fine-tuning loop targets.

**Done when:** Unit tests with hand-crafted examples verify correct computation for each scorer. Edge cases (empty answer, no citations, partial overlap) are covered.

**Estimated effort:** 3–4 hours.

---

### Task 18 — CLI entry point + end-to-end integration

**Scope:** Wire everything together into three CLI commands and run the full pipeline.

**Files:** `helix/cli.py`.

**Commands:**

```bash
# 1. Index the corpus into Qdrant
python -m helix.cli index \
  --corpus data/corpus.jsonl \
  --collection corpus.base

# 2. Run the eval
python -m helix.cli eval \
  --workflow deep_research \
  --dataset evals/datasets/hotpotqa_dev_100.jsonl \
  --scorers answer_f1,citation_precision,retrieval_recall@10 \
  --concurrency 4 \
  --output evals/baselines/hotpotqa_dev_100_baseline.json

# 3. (convenience) Run a single question
python -m helix.cli run \
  --workflow deep_research \
  --input '{"question": "Were Scott Derrickson and Ed Wood of the same nationality?"}'
```

**Output of `eval`:**

```json
{
  "eval_id": "...",
  "workflow": "deep_research",
  "dataset": "hotpotqa_dev_100",
  "n": 100,
  "metrics": {
    "answer_f1":             {"mean": 0.XX, "ci_low": 0.XX, "ci_high": 0.XX},
    "citation_precision":    {"mean": 0.XX, "ci_low": 0.XX, "ci_high": 0.XX},
    "retrieval_recall@10":   {"mean": 0.XX, "ci_low": 0.XX, "ci_high": 0.XX}
  },
  "model": "claude-sonnet-4-20250514",
  "embedding_model": "nomic-ai/nomic-embed-text-v1.5",
  "timestamp": "..."
}
```

**Done when:** The full pipeline runs end-to-end: `prepare_corpus` → `prepare_hotpotqa` → `index` → `eval` → JSON report. The report contains all three metrics with bootstrap CIs. This is the deliverable.

**Estimated effort:** 3–4 hours.

---

## Task dependency graph

```
1 (scaffold + Makefile)
├── 2 (decorators)
│   └── 5 (engine)
│       └── 16 (eval harness + holdout guard)
│           └── 18 (CLI + E2E)
├── 3 (span logger)
│   └── 5
├── 4 (SQLite store)
│   └── 5
├── 6 (LiteLLM adapter + cache)
│   └── 15 (deep_research)
├── 7 (embedder)
│   └── 10 (indexer)
│   └── 12 (retriever)
├── 8 (Qdrant adapter)
│   └── 10
│   └── 12
├── 9 (chunker)
│   └── 10
├── 11 (BM25)
│   └── 12
├── 12 (retriever + reranker)
│   └── 15
├── 13 (corpus prep)
│   └── 14 (HotpotQA prep)
│   │   └── 18
│   └── 14b (holdout sequestration)
│       └── 18
├── 15 (deep_research)
│   └── 18
└── 17 (scorers)
    └── 18
```

## Suggested execution order

**Days 1–2:** Tasks 1, 2, 3, 4 (scaffold + Makefile, decorators, logger, SQLite store). All independent once scaffolding is done.

**Days 3–4:** Tasks 5, 6, 7, 8 (engine, LiteLLM adapter + cache, embedder, Qdrant). Engine depends on 2+3+4. Adapters are independent of each other.

**Days 5–6:** Tasks 9, 10, 11 (chunker, indexer, BM25). Indexer depends on 7+8+9.

**Days 7–8:** Tasks 12, 13, 14, 14b (retriever+reranker, corpus prep, HotpotQA dev + holdout). Retriever depends on 7+8+11. Data prep is independent. 14b extends 14's script and runs in the same session.

**Days 9–10:** Task 15 (deep_research workflow). Depends on 6+12.

**Day 11:** Tasks 16, 17 (eval harness + holdout guard, scorers). Harness depends on 5. Scorers are independent.

**Days 12–13:** Task 18 (CLI + E2E). Integration, debugging, baseline numbers.

**Day 14:** Buffer for debugging, prompt tuning, documenting baseline results.

## Risks

| Risk | Mitigation |
|---|---|
| LLM costs for 100 eval runs | Each run makes ~5 LLM calls (1 decompose + 1 synthesize + margin). At Claude Sonnet rates, ~$5–15 for the first uncached run. Subsequent reruns with the same prompts hit the disk cache and cost nothing (spans emit `cost_usd=0`). |
| Nomic Embed model download is slow on first run | Document the requirement. Model is ~500MB. Cache in `data/models/`. |
| HotpotQA gold passages don't align with our corpus IDs | Task 14 explicitly maps `supporting_facts` titles to corpus `doc_id`s using the same hash function as Task 13. Validated before eval. |
| Retrieval recall is very low on first run | Expected. The baseline is the starting point, not the goal. The fine-tuning loop (deferred) is what improves it. Document whatever baseline we get. |
| BM25 in-memory index is too large for 2k docs | It won't be. `rank_bm25` handles 100k+ docs in memory trivially. |
| Qdrant Docker not available | Provide fallback instructions for Qdrant Cloud free tier. |

## Success criteria for the slice

1. `python -m helix.cli eval` completes all 100 questions without crash.
2. All three metrics are computed with bootstrap CIs.
3. Results are persisted to SQLite and written to a JSON report.
4. Spans are logged to `data/spans.jsonl` with correct parent-child nesting.
5. `ruff check`, `mypy --strict`, and `pytest` pass.
6. The whole pipeline (index + eval) completes in under 30 minutes on a machine with a consumer GPU (for embedding) and API access (for LLM calls).

## Fine-tune loop (slice extension)

Originally deferred (see "What we defer"), the self-improving RAG loop is now built
as a single-process extension of the slice. It closes the experiment the runtime
exists to enable: mine retrieval failures from traces, fine-tune the embedder on
them, and promote the candidate only if it measurably improves recall.

**One command:** `python -m helix.cli finetune --train <split> --eval <split> --corpus <path>`
(wrapped by `make finetune`) runs three phases in order against one `embedding_jobs` row:

1. **Mine** (`helix/rag/miner/`) — evaluate the disjoint train split
   (`hotpotqa_train_1000.jsonl`, questions 600–1600) through the *current* retrieval
   pipeline, persisting per-example results and spans. Examples with `retrieval_recall@10 < 1.0`
   yield one `FailureCase` per missed gold doc, each classified by a four-signature rule set
   (`lexical_only`, `semantic_mismatch`, `multi_hop_miss`, `ambiguous`) and carrying the
   retrieved-but-wrong docs as hard negatives. Persisted to the `failure_cases` table.
2. **Train** (`helix/rag/trainer/`) — turn failure cases into prefixed
   `(query, gold, hard_negatives)` contrastive triplets and fine-tune Nomic Embed v1.5 with
   `MultipleNegativesRankingLoss` (InfoNCE), saving the candidate checkpoint to
   `data/models/<job_id>/`. The training backend is injectable; RNGs are seeded for reproducibility.
3. **Promote** (`helix/rag/promotion/`) — re-embed the corpus with the candidate into a fresh
   `corpus.candidate.<job_id>` Qdrant collection, measure `retrieval_recall@10` on the dev split
   before/after with bootstrap CIs, and atomically swap the `corpus.active` alias **only on a
   positive lift** (otherwise archive). The `embedding_jobs` row records the full before/after
   metrics and final status (`promoted` / `archived`).

**Split discipline.** Mining draws from train-1000 (questions 600–1600); the canary measures on
dev-100 (questions 0–100); the holdout-500 (questions 100–600) stays sequestered for
`make eval-final`. The three are disjoint so the lift is measured on data the fine-tune never saw.

The heavy backends (training fit, candidate indexing, canary retrieval) are all injectable, so
the full loop is unit-tested end-to-end without a model or a Qdrant server.
