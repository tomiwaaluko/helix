# CLAUDE.md

Operating manual for Claude Code (and any other coding agent) working in this repository.

## What this repo is

Helix is a distributed agent runtime with first-class evaluation, observability, and a self-improving RAG loop. The thesis is that systematic mining of retrieval failures from production traces, used as training signal for embedding fine-tuning, yields measurable recall improvements over a strong baseline. The runtime exists to make that experiment possible at scale and to be useful on its own as a serious agent execution platform.

Treat the runtime as a production system. Treat the research loop as an experiment whose result must be reproducible end-to-end from a clean checkout.

## Repo layout

```
cmd/
  orchestrator/      Go binary: control plane API, scheduler, dispatcher
  collector/         Go binary: OTel collector that writes spans to ClickHouse

worker/              Python: worker runtime, tool adapters, SDK decorators
  helix/             public SDK surface (`helix.workflow`, `helix.task`, etc.)
  helix_proto/       generated gRPC stubs (do not edit by hand)
  tools/             LiteLLM adapter, Qdrant adapter, web search, sandbox

rag/                 Python: indexer, retriever, reranker, miner, trainer
  miner/             failure mining: signature classifiers + hard-negative builder
  trainer/           sentence-transformers fine-tuning loop
  promotion/         alias-swap + canary eval

web/                 Next.js 14 dashboard
  app/               App Router pages
  components/        shadcn/ui components and composites
  lib/               TanStack Query hooks, API client

proto/               .proto files. Single source of truth for gRPC.
  helix/v1/

evals/               eval datasets, scorers, harness configs, baseline results
  scorers/
  datasets/          (small fixtures only; large data lives in MinIO)
  baselines/

migrations/
  postgres/          up-only SQL migrations: YYYYMMDDHHMM_name.sql
  clickhouse/        up-only DDL files

infra/
  docker/            Dockerfiles per binary
  compose/           docker-compose.yml for local dev
  helm/              Helm chart for production
  otel/              collector config

docs/                architecture.md, tech-stack.md, data-model.md, api.md
```

## Required reading before non-trivial changes

Before any change that touches more than one component, read in this order:

1. `docs/architecture.md` — what plane owns what
2. `docs/data-model.md` — the durable state shape
3. `docs/api.md` — the contracts between planes
4. `docs/tech-stack.md` — why each tool is here

For cross-component changes, update `proto/helix/v1/` first, regenerate stubs, then change implementations on both sides. The proto is the contract; everything else is downstream.

## How I (Tomiwa) use Claude Code here

I run Claude Code with `--dangerously-skip-permissions` after I've reviewed the plan. That means:

- For schema changes, event-bus message shape changes, or public API changes (REST, SDK, CLI), produce a written plan first. Don't write code until I've signed off. These are hard to reverse and easy to fan out across the codebase.
- For self-contained changes inside a single component (e.g., a new scorer, a UI tweak, a worker tool adapter), proceed without asking. Tests and lint must still pass.
- When you finish a unit of work, summarize what changed in 5 lines or fewer and point at the diff. Don't restate the implementation.

If a change requires touching more than three of {orchestrator, worker, rag, web, proto, migrations}, that is a sign to stop and write a plan.

## Commands

All commands are make targets at repo root. Read the Makefile if a target is missing here.

| Command | Purpose | Time |
|---|---|---|
| `make dev` | Boot Postgres, ClickHouse, Qdrant, NATS, Redis, MinIO, OTel collector via docker-compose | ~30s |
| `make dev-down` | Tear down dev stack and volumes | ~5s |
| `make seed` | Load BRIGHT + 50k arXiv slice into MinIO, embed with base model, index in Qdrant | ~10 min |
| `make build` | Build Go binaries to `bin/` | ~20s |
| `make proto` | Regenerate gRPC stubs from `proto/helix/v1/` for Go and Python | ~3s |
| `make worker` | Run a single Python worker against the local dev stack | foreground |
| `make orchestrator` | Run the orchestrator against the local dev stack | foreground |
| `make web` | Run the Next.js dashboard in dev mode against the local API | foreground |
| `make test` | Unit tests for Go and Python | ~30s |
| `make test-integration` | Integration tests via testcontainers (real Postgres, ClickHouse, NATS) | ~5 min |
| `make test-eval-smoke` | Run reference workflow against 100 HotpotQA dev questions | ~3 min |
| `make eval-full` | Full eval suite. Requires a running cluster with workers. | ~1 hr |
| `make fmt` | `gofmt`, `ruff format`, `prettier` | ~5s |
| `make lint` | `golangci-lint`, `ruff check`, `mypy --strict`, `prettier --check` | ~30s |

Always run `make fmt && make lint && make test` before claiming work is done. For schema or proto changes, also run `make test-integration`.

## Conventions

### Go

- Structured logging with `slog`. Every log line includes `run_id` and `trace_id` when in a request scope. No `fmt.Println`.
- Context first: every function that does I/O or spans goroutines takes `ctx context.Context` as its first arg.
- No global state. Dependencies are passed in via constructors. The orchestrator wires everything in `cmd/orchestrator/main.go`.
- Error wrapping with `%w`. Sentinel errors live next to their producers. Never `errors.New` inside a hot path; declare once.
- Table-driven tests by default.

### Python

- `mypy --strict` is required, not aspirational. `Any` requires a `# type: ignore[<code>]` with a reason.
- `ruff format` + `ruff check`. No black, no isort; ruff covers both.
- Async-first. Workflow tasks are `async def`. Synchronous code is allowed inside tasks but the task itself is async.
- No `print`. Use the `helix.logging` module which routes through the OTel span context.
- Public SDK API is small and documented. Anything in `helix.internal` can change without notice.

### TypeScript / Next.js

- App Router. Server Components by default; opt into client components only when interactivity demands it.
- Data fetching through TanStack Query against the REST API. No direct fetch in components.
- shadcn/ui components only. No competing UI libraries.
- Tailwind classes, no CSS modules.
- API types generated from the OpenAPI schema (`web/lib/api-types.ts`). Do not hand-write them.

### SQL

- `snake_case` everywhere.
- Migrations are up-only. Name: `YYYYMMDDHHMM_description.sql`. Never edit a migration after it's merged to main.
- Indices are explicit. Columns used in WHERE or JOIN of any hot path get an index in the same migration that adds them.
- ClickHouse tables default to 90-day TTL on the partition key unless documented otherwise.

### Protobuf

- One service per file. One message per concept.
- Field numbers never reused. Removed fields are `reserved`.
- New fields are `optional` scalars or `repeated`. Required is unavailable in proto3 and we don't fake it.
- Breaking changes go to `proto/helix/v2/`. v1 stays.

## Definition of done

A change is done when:

1. Tests pass (`make test`).
2. Lint passes (`make lint`).
3. For schema, proto, or cross-component changes: integration tests pass (`make test-integration`).
4. A migration exists if state shape changed (`migrations/postgres/` or `migrations/clickhouse/`).
5. `docs/api.md` is updated if the public API changed.
6. `docs/tech-stack.md` is updated if a dependency was added.
7. `CHANGELOG.md` has a line under `## Unreleased`.

## Common gotchas

These have bitten me at least once. Internalize them.

- **The orchestrator is the only Postgres writer for run state.** Workers never write to `runs` or `tasks`. They emit events and call `CompleteTask` / `Checkpoint`. Workers writing directly will cause torn updates.
- **No long Postgres transactions.** The orchestrator holds connections briefly. Long-running work (LLM calls, retrieval) happens in workers and lands via gRPC.
- **NATS message visibility timeout is 30s.** Tasks expected to run longer must call `Checkpoint` to extend. Worker SDK does this automatically every 15s; don't disable that.
- **ClickHouse async inserts can lose data on shutdown.** Span writes are async (acceptable). Eval results are synchronous (required for the research thesis).
- **Qdrant collections cannot be renamed.** Embedding promotions flip the `corpus.active` alias. Never write to a specific collection name from production code; always go through the alias.
- **OTel collector queue drops on backpressure silently.** Monitor `otelcol_exporter_send_failed_spans` in Grafana. If it climbs, scale the collector before debugging "missing spans."
- **Replay tokens are not cryptographic.** They identify recorded calls by `(input_hash, model_id, tool_name)`. Don't use them for auth or access control.
- **`@helix.task` retries are at-least-once.** Tasks must be idempotent on `task_id` or use the SDK's exactly-once helper which writes a sentinel to Redis.
- **The dashboard hydrates blobs via signed MinIO URLs.** Local dev uses MinIO's path-style URLs; production uses virtual-host style. The OpenAPI client handles this; don't hardcode URL shapes in components.

## Adding a new feature: the canonical walk

A new feature that touches the public API and storage shape should be implemented in this order:

1. Write the contract in `docs/api.md` (the REST/SDK surface) and `proto/helix/v1/` (the internal gRPC, if affected).
2. Add the Postgres migration (and ClickHouse migration, if affected).
3. Implement the control-plane side in `cmd/orchestrator/`. Unit tests.
4. Regenerate proto stubs: `make proto`.
5. Implement the worker side in `worker/`. Unit tests.
6. Add an integration test in `worker/tests/integration/` that exercises the full path.
7. Add the dashboard surface in `web/` if user-visible.
8. Update `CHANGELOG.md`.
9. Run `make fmt && make lint && make test-integration` before opening the PR.

## Things to never do without asking

- Add a new datastore. The system already has Postgres, ClickHouse, Qdrant, NATS, Redis, MinIO. Adding a seventh is a decision, not a refactor.
- Make a breaking change to `/api/v1/`. Add a v2, or use additive evolution.
- Bypass LiteLLM. Direct calls to Anthropic, OpenAI, or vLLM endpoints from workers are forbidden because they break replay and metering.
- Log raw prompts or completions outside the OTel pipeline. They go to MinIO via the span exporter, and the dashboard hydrates them. Logging them elsewhere bypasses retention and access controls.
- Commit secrets, dataset blobs, model weights, or anything over 1 MB. Use MinIO or, for code-adjacent artifacts, git-lfs.
- Delete or edit a migration that has been merged.
- Reuse a proto field number.

## Values

Boring tech wherever boring wins. Postgres, Go, Python, Next.js — not because they are exciting but because they are predictable, well-instrumented, and have answers when something breaks at 2 AM.

Prefer deleting code to adding code. The runtime should get smaller as it gets clearer.

Measure before optimizing. The system has a working observability pipeline by design; use it. Hot paths are identified by ClickHouse queries against real spans, not by intuition.

The research result is the headline feature. Every architectural decision should make the embedding fine-tune experiment easier to run, easier to reproduce, and easier to trust.
