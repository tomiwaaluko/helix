## Unreleased

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
