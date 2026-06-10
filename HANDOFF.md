# Handoff

> Latest at top. Both agents update this before ending a session, per the Session protocol in `AGENTS.md`.
> If you're picking up work, read the top entry and `docs/vertical-slice-plan.md`.

## Entry template

```
## YYYY-MM-DD HH:MM TZ — <Tool> → next session

**Last commit:** `<hash>` on `<branch>`
**Working tree:** <clean | dirty: files>
**Task plan position:** <task N — status>

**What shipped this session**
- ...

**What's next**
1. ...

**Open questions / decisions pending**
- ...

**Gotchas hit**
- ...
```

---

## 2026-06-10 18:43 UTC — Claude Code → next session

**Last commit:** `05f5e17` on `claude/current-phase-gotchas-tjkwsu`
**Working tree:** clean
**Task plan position:** Task 10 (indexer) — DONE. Task 11 (BM25 sparse retrieval) next.

**What shipped this session** (ship-first; composes the Task 7–9 tools)
- `helix/rag/indexer.py`: `index_corpus(...)` → `IndexResult`. Loads JSONL `{id,text,source}`,
  chunks every doc, embeds all chunk texts, upserts `Point`s with `{doc_id, chunk_id, source,
  text}` payloads, and points the `corpus.active` alias at the collection. Chunk ids map to a
  deterministic `uuid5` for the Qdrant point id (re-index overwrites instead of duplicating);
  the readable `chunk_id` lives in the payload. Whole run wrapped in a `kind="internal"` span
  (docs/chunks/elapsed_s); embedder + adapter are injected so their child spans nest under it.
- `tests/test_indexer.py`: 100-doc corpus is searchable via the alias with the expected payload,
  span nesting (embed/upsert under index_corpus), and an empty-corpus case. Suite now 41 tests.

**What's next**
1. Task 11: `helix/tools/bm25.py` — sparse retrieval with `rank_bm25`. Pure-Python, ship-first.
   Check the spec for tokenization and whether it indexes chunks (shares the chunk corpus) or
   docs, and the score/return shape the hybrid retriever (Task 12) will expect.

**Open questions / decisions pending**
- `index_corpus` calls `create_collection` unconditionally, so re-indexing the *same* collection
  name raises (collection exists). Fine for the slice's fresh-index flow; a real re-index would
  need a recreate/delete step or a new collection name + alias flip. Flag if needed sooner.

**Gotchas hit**
- **Alias bootstrap ordering.** The adapter always writes through the alias (production
  invariant), but on a *fresh* index there is no alias yet — upserting before `set_alias` raised
  "Collection corpus.active not found". Fixed by creating the alias right after the collection,
  before upsert (so the alias-routed write resolves). Reordered vs the spec's step list; behavior
  is equivalent and correct.
- Verified via `/tmp/helixvenv` (3.12): ruff clean, `mypy --strict helix/` clean (18 files),
  41 pytest pass.

---

## 2026-06-10 18:36 UTC — Claude Code → next session

**Last commit:** `d05c0a2` on `claude/current-phase-gotchas-tjkwsu`
**Working tree:** clean
**Task plan position:** Task 9 (chunker) — DONE. Task 10 (indexer workflow) next.

**What shipped this session** (ship-first; pure-Python, no service)
- `helix/rag/chunker.py`: `chunk_document(...)` → `list[Chunk]`. Structural splitting:
  paragraphs (blank-line) → sentences (for oversized paragraphs) → word-window fallback (for a
  sentence still over budget). Greedy packing to `max_tokens` (512) with `overlap_tokens` (64)
  carried from each chunk's tail into the next (capped so overlap + next segment still fits).
  Deterministic `{doc_id}#chunk{N}` ids; `tiktoken` `cl100k_base` sizing via an injectable
  counter.
- `worker/pyproject.toml`: added `tiktoken>=0.7` dependency + mypy override.
- `docs/tech-stack.md`: added the tiktoken rationale (DoD item 7 — new dependency).
- `tests/test_chunker.py`: ~2000-word doc within budget + sequential ids, tiny/empty docs,
  oversized-paragraph→sentences, oversized-sentence→words, and overlap duplication
  (sum of chunk tokens == doc tokens at overlap 0, > doc tokens at overlap 40). Suite now 39.

**What's next**
1. Task 10: `helix/rag/indexer.py` — the indexer workflow that ties chunker → embedder →
   Qdrant upsert (embed_documents on chunk text, upsert Points with payload). Likely a
   `@helix.workflow`/`@helix.task` composition. Check the spec for payload shape (doc_id,
   chunk_id, text, source) and collection/vector-size config (768 from Nomic).

**Open questions / decisions pending**
- Chunks are joined with a single space (segments lose original blank-line breaks). Fine for
  retrieval; flag if faithful reconstruction of original formatting is ever needed.

**Gotchas hit**
- None new. `tiktoken` counter is injectable so tests use a word-count stub (no tokenizer
  download). Verified via `/tmp/helixvenv` (3.12): ruff clean, `mypy --strict helix/` clean
  (17 files), 39 pytest pass.

---

## 2026-06-10 18:30 UTC — Claude Code → next session

**Last commit:** `65c1bf8` on `claude/current-phase-gotchas-tjkwsu`
**Working tree:** clean
**Task plan position:** Task 8 (Qdrant adapter) — DONE. Task 9 (chunker) next.

**What shipped this session** (ship-first; self-contained tool adapter)
- `helix/tools/qdrant_adapter.py`: `QdrantAdapter` with async `create_collection`, `set_alias`,
  `upsert` (batched at 100, `kind="internal"` span), and `search` (`kind="retrieval"` span).
  `Point`/`ScoredPoint` dataclasses keep the surface independent of qdrant's types. All
  reads/writes target the **alias** (default `corpus.active`), never a concrete collection — the
  production alias-swap invariant, honored from day one. Client is lazily imported and injectable.
- `worker/pyproject.toml`: bumped `qdrant-client` floor `>=1.9` → `>=1.12`.
- `tests/test_qdrant_adapter.py`: real integration test against `AsyncQdrantClient(location=
  ":memory:")` — create/alias/upsert(100)/search, batch boundaries (250 @ 100), and the
  url-or-client guard. Suite now 33 tests.

**What's next**
1. Task 9: `helix/rag/chunker.py` — semantic + structural splitting. Pure-Python, no external
   service, so straightforward ship-first. Check the spec for chunk size/overlap and whether it
   needs the embedder (semantic splitting) or is purely structural for the slice.

**Open questions / decisions pending**
- Upsert span is `kind="internal"` (it's a write, not a retrieval); only `search` is
  `kind="retrieval"`. Consistent with the embedder using `internal` for index-side work. Flag if
  you'd rather every Qdrant op be `retrieval`.

**Gotchas hit**
- **qdrant-client 1.18 removed `AsyncQdrantClient.search`** — use `query_points(collection_name,
  query=vector, limit, query_filter)`, which returns a `QueryResponse` with `.points`. Hence the
  `>=1.12` floor bump.
- mypy: a qdrant point `id` is `int | str | UUID`; coerce the non-`int`/`str` case to `str` when
  building `ScoredPoint`.
- Qdrant **local in-memory mode supports aliases**, so the alias path is genuinely covered by the
  test (verified, not assumed). Verified via `/tmp/helixvenv` (3.12), qdrant-client 1.18:
  ruff clean, `mypy --strict helix/` clean (16 files), 33 pytest pass.

---

## 2026-06-10 16:23 UTC — Claude Code → next session

**Last commit:** `77eeda5` on `claude/current-phase-gotchas-tjkwsu`
**Working tree:** clean
**Task plan position:** Task 7 (embedding client) — DONE. Task 8 (Qdrant adapter) next.

**What shipped this session** (ship-first; self-contained tool adapter)
- `helix/tools/embedder.py`: `Embedder` over Nomic Embed v1.5 (sentence-transformers).
  `embed_queries`/`embed_documents` apply the `search_query: ` / `search_document: ` task
  prefixes (kept here and nowhere else — mixing them silently hurts recall). Internal batching
  (default 64), one `kind="internal"` span per call with `model`/`count`/`dimension`. The
  SentenceTransformer is lazily loaded (`trust_remote_code=True`) on first encode; the encode
  fn is injectable (`encode_fn=`) so tests use a stub and never download the model.
- `worker/pyproject.toml`: extended the mypy `ignore_missing_imports` override to
  `sentence_transformers`.
- `tests/test_embedder.py`: prefix correctness for both methods, batch-size boundaries
  (10 texts @ batch 4 → [4,4,2]), 768-dim output, internal-span attributes. Suite now 30 tests.

**What's next**
1. Task 8: `helix/tools/qdrant_adapter.py` — Qdrant client wrapper (upsert + search). This is
   the first adapter against a *real* external service (Qdrant in Docker via `make dev`). Decide
   how to unit-test without a live Qdrant: same injectable-client pattern, or qdrant's in-memory
   mode (`QdrantClient(":memory:")`). Check the spec for collection/vector config and whether it
   wants the `corpus.active` alias indirection (that's a production gotcha; confirm slice scope).

**Open questions / decisions pending**
- None blocking Task 8.

**Gotchas hit**
- None new. Verified via `/tmp/helixvenv` (3.12): ruff clean, `mypy --strict helix/` clean
  (15 files), 30 pytest pass. sentence-transformers is a pre-existing pinned dep (no tech-stack
  change); it's untyped, hence the mypy override.

---

## 2026-06-10 15:40 UTC — Claude Code → next session

**Last commit:** `be53d15` on `claude/current-phase-gotchas-tjkwsu`
**Working tree:** clean
**Task plan position:** Task 6 (LiteLLM tool adapter) — DONE. Task 7 (embedding client) next.

**What shipped this session** (ship-first; self-contained tool adapter)
- `helix/tools/llm_cache.py`: `LLMCache` (JSON files under `data/llm_cache/`, keyed by
  `sha256` of canonical `{model, messages, temperature, max_tokens, top_p, stop}` with `None`
  values dropped) + `cache_enabled()` (on unless `HELIX_ENV=production` or `HELIX_LLM_CACHE=0`).
- `helix/tools/litellm_adapter.py`: `llm_call(...)` async wrapper. Emits a `kind="llm"` span
  with `model`/`prompt_tokens`/`completion_tokens`/`cost_usd`/`cache_hit`/`replayed`. Cache hit
  → returns immediately with `cost_usd=0`, `cache_hit=replayed=True`, tokens replayed. Miss →
  LiteLLM, write cache. `temperature=0` default; model from `HELIX_DEFAULT_MODEL`
  (default `claude-sonnet-4-20250514`). LiteLLM + cost fns are lazily imported and injectable,
  so tests need neither the dep nor a network/API key.
- `worker/pyproject.toml`: mypy override `ignore_missing_imports` for `litellm`.
- `tests/test_litellm_adapter.py`: key stability/sensitivity, miss→hit (LiteLLM called once),
  `--no-cache` bypass, `temperature=0` default, env model resolution, span attributes. 26 tests.

**What's next**
1. Task 7: `helix/tools/embedder.py` — Nomic Embed v1.5 wrapper (sentence-transformers).
   Likely the first real heavyweight dep; consider the same lazy-import + injectable pattern so
   tests don't pull the model. Check the spec for batching/normalization specifics.

**Open questions / decisions pending**
- None blocking Task 7.

**Gotchas hit**
- mypy `--strict` + untyped third-party: added a `[[tool.mypy.overrides]]` block for `litellm`
  (`ignore_missing_imports`). Keep LiteLLM usage behind `Any` (cast the lazy handles) so the
  adapter never depends on its annotations.
- Verified via `/tmp/helixvenv` (3.12): ruff clean, `mypy --strict helix/` clean (14 files),
  26 pytest pass. `data/llm_cache/` is already gitignored via the `data/` rule.

---

## 2026-06-10 15:01 UTC — Claude Code → next session

**Last commit:** `34d6464` on `claude/current-phase-gotchas-tjkwsu`
**Working tree:** clean
**Task plan position:** Task 5 (asyncio engine) — DONE. Task 6 (LiteLLM tool adapter) next.

**What shipped this session** (plan-first; maintainer signed off in chat)
- `helix/runtime/context.py`: `current_engine` / `current_run` contextvars. Separate module so
  `decorators.py` reads them without importing `engine.py` (no import cycle).
- `helix/runtime/engine.py`: `Engine` runs a submitted workflow as a coroutine; each `@task`
  call dispatches through an `asyncio.Queue` and returns a `Future`. Run success/failure is
  driven by the workflow coroutine returning/raising (dynamic DAG — no static graph). Per-task
  lifecycle persisted (`ready`→`running`→`succeeded`/`failed`, `attempts` bumped per dispatch,
  insert-before-enqueue so the `running` update never races a missing row). One `kind="task"`
  span per dispatch under the run's `trace_id`. Retry budget 1. `asyncio.Semaphore` concurrency
  (default 4). Background `_enqueue`/handler tasks are tracked in sets so they aren't GC'd.
- `helix/decorators.py`: wired the single dispatch point — `current_engine.get()` is `None`
  (local → direct await) or the engine (submit → `engine.dispatch`).
- `helix/runtime/sqlite_store.py`: added `get_tasks(run_id)`.
- `docs/vertical-slice-plan.md`: corrected the span-kind contract cell from `workflow` to
  `task` (decision #4, approved).
- `tests/test_engine.py`: gated lifecycle (ready→running→succeeded asserted mid-flight),
  parallel branches get distinct `node_id`s, retry-then-fail (`attempts==2`, run `failed`),
  task spans, and local-vs-submit invariance. Suite now 21 tests; ran 3× for flakiness — stable.

**Decisions (approved by maintainer)**
- Single writer = the `SqliteStore`'s one aiosqlite connection (already serializes); no separate
  writer task.
- Task spans are `kind="task"`, not `kind="workflow"` as the spec table originally read; the
  doc was corrected to match.
- Non-JSON task inputs (e.g. `list[Doc]`) travel in-memory; the `tasks.input/output` columns get
  a best-effort JSON snapshot (`{"args": [...], "kwargs": {...}}`, dataclasses→asdict, else str).
- Slice `tasks` table has no error column, so a failed task is `status='failed'` only; the error
  rides the Future up to the `runs.error` row.

**What's next**
1. Task 6: `helix/tools/litellm_adapter.py` + `llm_cache.py` — LiteLLM call path with the
   disk-backed cache. Mind the gotchas: cache key = `model+messages+temperature+max_tokens`
   (+`top_p`,`stop`); cache hits emit `cache_hit=true`/`cost_usd=0`; `temperature=0` default;
   never bypass LiteLLM.

**Open questions / decisions pending**
- Nested `@task`-from-`@task` isn't wired (handlers run in the worker-loop context without
  `current_engine`/`current_run` set). Not needed for `deep_research`'s leaf tasks; revisit if
  a future workflow dispatches tasks from inside a task.

**Gotchas hit**
- ruff `ASYNC109`: an async helper with a `timeout` param trips it; renamed to `timeout_s`.
- Same 3.12 toolchain requirement (PEP 695). Verified via `/tmp/helixvenv`: ruff clean,
  `mypy --strict helix/` clean (12 files), 21 pytest pass.

---

## 2026-06-10 14:19 UTC — Claude Code → next session

**Last commit:** `722d69c` on `claude/current-phase-gotchas-tjkwsu`
**Working tree:** clean
**Task plan position:** Task 4 (SQLite state store) — DONE. Task 5 (asyncio engine) next.

**What shipped this session**
- `helix/runtime/sqlite_store.py`: `SqliteStore` with async CRUD over `runs`, `tasks`,
  `eval_results`, `datasets` (simplified slice schema; JSON in TEXT columns). Methods:
  `create_run`/`update_run`/`get_run`, `create_task`/`update_task`/`get_task`,
  `store_eval_result`/`get_eval_results`, `register_dataset` (idempotent on `(name,version)`).
  Partial updates leave `None` columns unchanged; `update_run` auto-sets `finished_at` on
  terminal status. Frozen `*Row` dataclasses for reads (named with a `Row` suffix to avoid
  colliding with the SDK `Task`). Single connection + WAL + `foreign_keys=ON`; the connection's
  background thread serializes writes (the single-writer gotcha — engine owns this in Task 5).
- `tests/test_sqlite_store.py`: run/task lifecycles, error + no-op updates, eval result
  grouping/details, dataset idempotency. Suite now 16 tests.
- Added `aiosqlite` to the 3.12 verify venv (it's already a pinned runtime dep in pyproject).

**What's next**
1. Task 5 (Claude Code — load-bearing per CLAUDE.md): the asyncio engine + `contextvars`
   mode detection. Wires `Task.__call__` dispatch (the single point left in `decorators.py`)
   to the engine in submit mode while local mode stays a direct call. Route ALL state writes
   through the store via a single writer task.

**Open questions / decisions pending**
- `update_run`/`update_task` treat `None` as "unchanged", so a column can't be nulled back
  out once set. Fine for the slice's forward-only state machine; revisit if the engine ever
  needs to clear `output`/`error`.

**Gotchas hit**
- mypy: `aiosqlite.connect` wants `str | Path`, not `PathLike`. Coerced the stored path with
  `os.fspath(...)` in `__init__`.
- Same 3.12 toolchain requirement (PEP 695). Verified via `/tmp/helixvenv`: ruff clean,
  `mypy --strict helix/` clean (10 files), 16 pytest pass.

---

## 2026-06-10 13:57 UTC — Claude Code → next session

**Last commit:** `590b856` on `claude/current-phase-gotchas-tjkwsu`
**Working tree:** clean
**Task plan position:** Task 3 (structured span logger) — DONE. Task 4 (SQLite state store) next.

**What shipped this session**
- `helix/logging.py`: `SpanLogger` appending JSONL to `data/spans.jsonl`; a `span(name,
  kind, attributes)` context manager that yields the mutable attributes dict and writes the
  record on exit; a `trace(trace_id=None)` binder. Span stack + per-run `trace_id` propagate
  via `contextvars`, so `parent_span_id` is correct across `await`/`gather` (each asyncio
  task copies the context). Record fields are exactly `trace_id, span_id, parent_span_id,
  name, kind, start_time, end_time, attributes` — matching the target ClickHouse `spans`
  schema (`docs/data-model.md`); times are ISO-8601 UTC strings (simplified vs DateTime64).
- `tests/test_logging.py`: schema shape, nested parent-child, shared/auto trace_id, and
  cross-`gather` propagation. Full suite now 11 tests.

**What's next**
1. Task 4: `helix/runtime/sqlite_store.py` — the SQLite state store (runs/tasks). Mind the
   slice gotcha: `aiosqlite` has no clean concurrent writers, so all writes route through a
   single engine writer task (that engine arrives in Task 5).

**Open questions / decisions pending**
- None blocking Task 4.

**Gotchas hit**
- Same 3.12 toolchain requirement as the prior entry (PEP 695). Verified via the
  `/tmp/helixvenv` 3.12 venv: ruff clean, `mypy --strict helix/` clean (9 files), 11 pytest
  pass. Default sandbox `python3`/`mypy` are 3.11 and will choke on the syntax.
- ruff `UP017` under py312 wants `datetime.UTC`, not `timezone.utc`.

---

## 2026-06-10 13:45 UTC — Claude Code → next session

**Last commit:** `9699e4d` (Task 2 code) on `claude/current-phase-gotchas-tjkwsu`
**Working tree:** clean
**Task plan position:** Task 2 (SDK decorators, local execution) — DONE. Task 3 (span logger) next.

**What shipped this session**
- `helix/types.py`: `Doc`, `Citation`, `Answer` dataclasses (spec-exact fields/defaults).
- `helix/decorators.py`: `@task(retries=, timeout=)`, `@workflow(name=, version=)` with
  `.local(**kwargs)`, and `gather = asyncio.gather`. Tasks/workflows are thin generic
  wrappers that call through directly in local mode; retry/timeout are parsed into metadata
  but inert. `Task.__call__` is the single dispatch point Task 5's engine will hook.
- `helix/__init__.py`: public exports (`Doc`, `Citation`, `Answer`, `Task`, `Workflow`,
  `task`, `workflow`, `gather`).
- `tests/test_decorators.py`: toy two-task workflow via `.local()`, gather ordering, metadata,
  timeout parsing, type defaults. 7 tests pass.

**What's next**
1. Task 3: `helix/logging.py` — `SpanLogger` writing JSONL to `data/spans.jsonl`, with a
   `span(name, kind, attributes)` context manager and a contextvar span stack for
   `parent_span_id`. Schema fields are fixed (`trace_id`, `span_id`, `parent_span_id`,
   `name`, `kind`, `start_time`, `end_time`, `attributes`) — do not rename.

**Open questions / decisions pending**
- None blocking Task 3.

**Gotchas hit**
- **Toolchain must be Python 3.12+.** The code uses PEP 695 generics (`class Task[**P, R]`),
  which is a `SyntaxError` on 3.11. This sandbox's default `python3`/`mypy`/`ruff` are tied
  to **3.11**, so `make lint`/`make test` fail there with a misleading "Invalid syntax" from
  mypy. Verified instead via a 3.12 venv (`python3.12 -m venv`): ruff clean, `mypy --strict
  helix/` clean (8 files), 7 pytest pass. The Makefile is correct for the maintainer's 3.12
  env and was left unchanged. `python3.12`/`python3.13` are both present at `/usr/bin`.
- ParamSpec lives in the PEP 695 form (`[**P, R]`); ruff's UP046 rejects the old
  `Generic[P, R]` subclass form under `target-version = py312`.

---

## 2026-06-10 13:35 UTC — Claude Code → next session

**Last commit:** `34228f5` on `claude/current-phase-gotchas-tjkwsu`
**Working tree:** clean
**Task plan position:** Task 1 (scaffold) — DONE. Task 2 (SDK decorators) next.

**What shipped this session**
- Task 1 scaffold: root `Makefile` (faithful to the slice-plan spec), `worker/pyproject.toml`
  (pinned runtime deps + `dev` extras, with ruff/mypy/pytest config), the `helix/` package
  tree with empty `__init__.py` files (`runtime`, `tools`, `rag`, `workflows`, `eval`),
  `.gitignore` for `data/`/`*.db`, and a `tests/test_smoke.py` import check.
- Verified `make lint` (ruff clean, `mypy --strict helix/` clean on 6 files) and `make test`
  (1 passed) both green, invoked exactly as the Makefile defines them.

**What's next**
1. Task 2: `@helix.workflow`, `@helix.task`, `helix.gather` for local execution only, plus
   `types.py` (`Doc`, `Answer`, `Citation`) and the public exports in `helix/__init__.py`.

**Open questions / decisions pending**
- None blocking Task 2.

**Gotchas hit**
- Local interpreter is **Python 3.11.15**, but `pyproject` pins `requires-python >=3.12`
  per the spec. ruff/mypy/pytest all run fine under 3.11, so lint/test were verified, but a
  full `pip install -e '.[dev]'` (which pulls litellm/sentence-transformers/qdrant-client)
  was **not** run here — those heavy deps + the version pin make it a CI/3.12 concern, not a
  scaffold blocker. `import helix` itself needs none of them.
- `pytest` was not preinstalled; installed `pytest`/`pytest-asyncio` ad hoc to run the suite.
  `make test` relies on pytest's rootdir insertion for `import helix` (no `PYTHONPATH` set) —
  confirmed working.

---

## 2026-06-10 — Cowork (Claude) → next session

**Last commit:** operating-docs commit on `master` (run `git log -1 --oneline`; parent is `7dd58e8`)
**Working tree:** clean after this commit
**Task plan position:** Bootstrap finishing. Task 1 (scaffold) NOT started.

**What shipped this session**
- Added `.gitattributes` (`* text=auto eol=lf`) and cleared the phantom CRLF drift that had made README/CHANGELOG/`vertical-slice-plan.md` show as fully rewritten. The committed state was always LF; an editor was re-saving working copies as CRLF.
- Replaced the monolithic `CLAUDE.md` with a thin stub that imports `AGENTS.md`. Added a symmetric `CODEX.md`. `AGENTS.md` (the real manual, phase-aware) was already correct and is now committed.
- Reset this handoff log. The prior entries were placeholder/future-dated (06-11, 06-12) and described Tasks 7–8 as done against a commit `a3f81c2` that does not exist. Real HEAD before this session was `7dd58e8`.
- Verified the three pending edits to `docs/vertical-slice-plan.md` are already applied: LLM cache key includes `max_tokens`/`top_p`/`stop`; cache-hit spans set `cost_usd=0` + `replayed=true`; risks table notes near-zero rerun cost.

**What's next**
1. Task 1 (assigned to Claude Code): `Makefile` + `worker/pyproject.toml` + empty `worker/helix/` package; pass `make lint` on the empty package.
2. Verify the `@AGENTS.md` import: in a fresh Claude Code session, ask "what phase are we in and what gotchas apply today?" Expect "slice phase" plus the slice-phase gotchas (engine contextvars detection, LLM cache key, holdout unlock, `retrieved_doc_ids`, `temperature=0`, SQLite single-writer). A vague answer means the import is not resolving.

**Open questions / decisions pending**
- None blocking Task 1.

**Gotchas hit**
- The repo lives on a Windows filesystem. When operating on it from a Linux shell, the mount permits create and rename but **not unlink** — `git checkout`/index writes that delete-and-replace fail and can corrupt `.git/index`. Recovered by routing git through an index on a native fs and renaming a clean index back into place. If `.git/index` ever reports "corrupt," that is the cause. Prefer running git from Windows-side tooling.
- An editor is rewriting tracked files to CRLF on save. `.gitattributes` now pins LF; if CRLF diffs reappear, check the editor's line-ending setting.
