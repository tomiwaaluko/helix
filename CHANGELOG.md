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
