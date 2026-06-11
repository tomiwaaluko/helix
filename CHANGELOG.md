## Unreleased

- Make the eval harness resilient to per-example failures: `evaluate` now retries each
  example up to `max_attempts` (default 3) with exponential backoff, so a transient provider
  error on one question no longer aborts the whole batch. The backoff sleeps outside the
  concurrency semaphore, and because workflow LLM calls are cached, a retried example reuses
  the sub-calls that already succeeded and only re-issues the failed one.
- Enable LiteLLM's provider retries in the LLM adapter: pass `num_retries` (default 4,
  override via `HELIX_LLM_NUM_RETRIES`) to `acompletion` so transient provider errors
  (503/429/timeouts) are retried with exponential backoff instead of aborting a 100-question
  eval on the first blip. The adapter still implements no retry loop of its own — LiteLLM owns
  the backoff, consistent with the cache/metering/replay design. `num_retries` is not part of
  the cache key (it does not affect output).
- Pin `einops` in the worker dependencies: the Nomic Embed v1.5 remote modeling code
  imports it, so a clean checkout failed at the first `embed_documents` without it.
- Make the chunker's token counter resilient: `cl100k_base` lives on a blob store that
  is unreachable in network-restricted environments (403), which previously failed the
  entire `index`. `_tiktoken_counter` now degrades to a deterministic char-based heuristic
  (with a `RuntimeWarning`) when the tokenizer can't be fetched. Behavior is unchanged when
  tiktoken is reachable; for the HotpotQA corpus the impact is negligible (99.8% of docs are
  under the chunk budget, so chunk boundaries are essentially identical either way).
- Scaffold the vertical-slice worker package: root `Makefile`, `worker/pyproject.toml`
  (pinned deps + ruff/mypy/pytest config), the `helix/` package tree with empty
  `__init__.py` files, a `.gitignore` for `data/`/`*.db`, and a smoke test. `make lint`
  and `make test` pass on the empty package. (Task 1)
- Add the SDK authoring surface for local execution: `@helix.task`, `@helix.workflow`
  (with `.local()`), and `helix.gather`, plus the `Doc`/`Citation`/`Answer` core types.
  Decorators record retry/timeout metadata (inert locally) and keep a single dispatch
  point for the engine to slot into later. (Task 2)
- Add `helix.logging`: a structured span logger writing JSONL to `data/spans.jsonl`
  with a `SpanLogger.span()` context manager and a `trace()` binder. The span stack and
  per-run `trace_id` propagate via `contextvars`, so nested spans get correct
  `parent_span_id` across `await`/`gather`. Field names match the target ClickHouse
  `spans` schema. (Task 3)
- Add `helix.runtime.sqlite_store.SqliteStore`: async CRUD over the slice's SQLite state
  (`runs`, `tasks`, `eval_results`, `datasets`) with `create_run`/`update_run`,
  `create_task`/`update_task`, `store_eval_result`/`get_eval_results`, and an idempotent
  `register_dataset`. Single connection, serialized writes; JSON state in TEXT columns. (Task 4)
- Add the asyncio engine (`helix.runtime.engine.Engine`): runs a submitted workflow as a
  coroutine and dispatches each `@task` call through an `asyncio.Queue`, returning a `Future`.
  Mode is detected via a `contextvars.ContextVar[Engine | None]` (new `helix.runtime.context`),
  so workflow code is identical in `.local()` and `submit()`. Persists run/task lifecycle to
  SQLite, emits a `kind="task"` span per dispatch, and retries once before failing. Also adds
  `SqliteStore.get_tasks`. Corrected the slice-plan span-kind contract from `workflow` to
  `task`. (Task 5)
- Add the LiteLLM tool adapter (`helix.tools.litellm_adapter.llm_call`) and disk-backed
  response cache (`helix.tools.llm_cache.LLMCache`). Async wrapper over `acompletion` that
  emits a `kind="llm"` span (model, token counts, `cost_usd`, `cache_hit`, `replayed`),
  defaults `temperature=0`, and caches by `sha256` of canonical generation params. Cache hits
  replay with `cost_usd=0`/`replayed=true`; on by default outside production, off via
  `HELIX_LLM_CACHE=0`. LiteLLM is lazily imported and injectable for testing. (Task 6)
- Add the embedding client (`helix.tools.embedder.Embedder`): Nomic Embed v1.5 via
  sentence-transformers, with `embed_queries`/`embed_documents` applying the `search_query:` /
  `search_document:` task prefixes, internal batching (default 64), and a `kind="internal"`
  span (`model`, `count`, `dimension`). The model is lazily loaded and the encode fn is
  injectable, so tests run against a stub. (Task 7)
- Add the Qdrant adapter (`helix.tools.qdrant_adapter.QdrantAdapter`): async `upsert` (batched,
  100), `search` (via `query_points`), `create_collection`, and `set_alias`. All reads/writes go
  through a collection alias (default `corpus.active`); `search` emits a `kind="retrieval"` span.
  Client is lazily imported and injectable, so tests run against Qdrant's in-memory local mode.
  Bumped the `qdrant-client` floor to `>=1.12` for the `query_points` API. (Task 8)
- Add the document chunker (`helix.rag.chunker.chunk_document`): structural splitting
  (paragraphs → sentences → word-window fallback), greedy packing to `max_tokens` (default 512)
  with `overlap_tokens` (default 64) carried across boundaries, deterministic `{doc_id}#chunk{N}`
  ids, and `tiktoken` (`cl100k_base`) sizing. The token counter is injectable for testing. Adds
  `tiktoken` as a dependency. (Task 9)
- Add the indexer (`helix.rag.indexer.index_corpus`): loads a JSONL corpus, chunks every doc,
  embeds all chunks, and upserts them into Qdrant with `{doc_id, chunk_id, source, text}`
  payloads, pointing the `corpus.active` alias at the new collection. Chunk ids map to
  deterministic `uuid5` point ids (re-index overwrites). Returns an `IndexResult` summary and
  wraps the run in a `kind="internal"` span. (Task 10)
- Add BM25 sparse retrieval (`helix.tools.bm25.BM25Index`): builds an in-memory
  `rank_bm25.BM25Okapi` index over chunks (lowercase whitespace tokenization), `search` returns
  ranked `ScoredChunk`s, and `save`/`load` pickle the index for reuse. Empty corpus yields no
  results. (Task 11)
- Add the hybrid retriever (`helix.rag.retriever.HybridRetriever`) and BGE reranker
  (`helix.tools.reranker.Reranker`): dense (Qdrant) + sparse (BM25) candidates fused by RRF
  (`k=60`), reranked by a cross-encoder, returned as top-k `Doc`s carrying
  `metadata["doc_id"]` for the recall scorer. Emits a `kind="retrieval"` span
  (`query`, `retriever`, `top_k`, `results`). Reranker model is lazily loaded and the predict
  fn injectable for testing. (Task 12)
- Add corpus preparation: `helix.eval.corpus` (pure logic — `wiki_doc_id` hashing, `build_corpus`
  dedup-by-title over both HotpotQA context shapes, `write_corpus` JSONL) plus a thin
  `scripts/prepare_corpus.py` that downloads HotpotQA distractor-dev via `datasets` and writes
  `data/corpus.jsonl`. The `wiki_<title_hash>` id scheme is shared so Task 14's `supporting_facts`
  resolve against the corpus. Adds `datasets` as a dependency. (Task 13)
- Add HotpotQA eval-dataset prep: `helix.eval.hotpotqa` (pure — `build_questions` to the
  `data-model.md` schema over both supporting-fact shapes, `write_examples`, and
  `missing_doc_ids`/`supporting_doc_ids` integrity checks) plus `scripts/prepare_hotpotqa.py`,
  which writes `evals/datasets/hotpotqa_dev_100.jsonl` and validates supporting-fact `doc_id`s
  against `data/corpus.jsonl`. Supporting-fact titles reuse `wiki_doc_id`. (Task 14)
- Add the `deep_research` workflow (`helix.workflows.deep_research`): `decompose` →
  parallel `retrieve` → `synthesize`, as `@helix.task`/`@helix.workflow`. LLM calls run at
  `temperature=0`; the workflow attaches the deduped union of retrieved `doc_id`s to
  `Answer.metadata["retrieved_doc_ids"]`. Dependencies are injected via a `contextvars`
  `ResearchDeps` (`using_research_deps`); system prompts live in `helix/workflows/prompts/`.
  Also changes `HybridRetriever` so `Doc.id` is the corpus `doc_id` (chunk_id stays in
  metadata), so recall and citations key off the doc. (Task 15)
- Add the eval harness (`helix.eval.harness`): `load_dataset` (JSONL + schema validation),
  `evaluate(workflow_fn, dataset, scorers, concurrency, store)` (semaphore-bounded async runs,
  per-scorer SQLite persistence, mean/std/seeded-95%-bootstrap-CI aggregation), and `EvalReport`
  (`.metrics`/`.per_example`/`.to_json`). (Task 16)
- Add the holdout access guard (Task 14b, code): `load_dataset` raises `HoldoutAccessError` for
  any `holdout` path unless `HELIX_HOLDOUT_UNLOCK=1`; the holdout filename lives only in
  `harness.py`. `scripts/prepare_hotpotqa.py` extended to emit the 500-question holdout + a
  `.sha256` lock; `scripts/check_holdout_integrity.py` (run in `make lint`, skips if absent); and
  a `holdout-guard.yml` CI workflow that rejects unauthorized holdout filename references. (Task 14b)
- Add the eval scorers (`helix.eval.scorers`): `answer_f1` (token-level F1 with canonical
  HotpotQA/SQuAD normalization — lowercase, punctuation-split, articles removed),
  `citation_precision` (fraction of cited doc_ids that are gold; vacuous 1.0 when no citations),
  and `retrieval_recall_at_k` (gold doc_ids found in the top-k `retrieved_doc_ids`), plus
  `default_scorers()` keyed for the harness. (Task 17)
- Add the slice CLI (`helix.cli`, `python -m helix.cli`): `index` (chunk + embed a corpus into
  Qdrant and write the BM25 sidecar), `eval` (run `deep_research` over a dataset, score with the
  selected scorers, write the baseline JSON report with bootstrap CIs + model/embedding/timestamp
  metadata), and `run` (answer one question). Heavy components (embedder, Qdrant adapter, reranker,
  LLM completion, token counter) come from monkeypatchable module-level factories so the end-to-end
  CLI test (`tests/test_cli.py`) drives the full pipeline with stubs and on-disk local Qdrant.
  `--no-cache` disables the LLM cache; adds `infra/compose/docker-compose.yml` so `make dev` boots
  Qdrant. This makes `make eval` real. (Task 18)
- Fix the HotpotQA download in `scripts/prepare_corpus.py` and `scripts/prepare_hotpotqa.py`: the
  dataset moved to the `hotpotqa/hotpot_qa` namespace (and to Parquet), so the bare `hotpot_qa` id
  no longer resolves on the Hub. The pure parsers already handle the Parquet `context` /
  `supporting_facts` shapes, so only the dataset id changed.
- Fix corpus coverage in `scripts/prepare_corpus.py`: build from the first 600 questions (not 500)
  so the corpus covers the union of the dev (0–100) and holdout (100–600) splits. Under-covering
  silently failed holdout referential integrity (the holdout's supporting paragraphs were absent).
- Add an embedded Qdrant mode to the CLI (`--qdrant-path` on `index`/`eval`/`run`): runs Qdrant
  on-disk in-process via `AsyncQdrantClient(path=...)`, so the pipeline runs with no Qdrant server
  or Docker daemon. Without it, `--qdrant-url` connects to a server as before. The CLI test now
  exercises the real embedded branch instead of stubbing the adapter.
