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
