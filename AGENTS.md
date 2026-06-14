# AGENTS.md

Operating manual for any coding agent working in this repository. Tool-agnostic. If you are an agent reading this, follow it as written.

For tool-specific notes (e.g. how the maintainer invokes a particular agent), see `CLAUDE.md` for Claude Code or `CODEX.md` for Codex CLI. Both import this file.

---

## Current phase: M1 — Go orchestrator

**The authoritative scope right now is `docs/m1-plan.md` (implemented) and `docs/vertical-slice-plan.md` (BRIGHT experiment complete).**

M1 is the control plane. The stack on disk:

- **Postgres 15** for run/task/worker state (`migrations/202606150001_initial_schema.sql`)
- **NATS JetStream** for task dispatch (`helix.tasks.dispatch.<pool>`)
- **Go orchestrator** (`cmd/orchestrator/`) — gRPC + REST server, migration runner
- **Python worker** (`worker/helix/worker/__main__.py`) — gRPC client + NATS consumer, runs workflows in local mode
- **Proto contracts** (`proto/helix/v1/`) — stubs in `gen/go/helix/v1/` (Go) and `worker/helix/v1/` (Python)
- **Qdrant** for vectors (same as M0)
- **JSONL spans** (`data/spans.jsonl`) — OTel export deferred to M2

`make eval` continues to run in local mode (Python in-process). The orchestrator + worker path is exercised by `make test-integration`.

What is NOT yet on disk (future milestones):
- ClickHouse (M2), Redis (M3), MinIO (M3), Next.js dashboard (M4), failure miner as production workflow (M5), Helm/Kubernetes (M6).

Update this section when M2 lands (ClickHouse / OTel collector).

## What this repo is

Helix is a distributed agent runtime with first-class evaluation, observability, and a self-improving RAG loop. The thesis is that systematic mining of retrieval failures from production traces, used as training signal for embedding fine-tuning, yields measurable recall improvements over a strong baseline. The runtime exists to make that experiment possible at scale and to be useful on its own as a serious agent execution platform.

Treat the runtime as a production system. Treat the research loop as an experiment whose result must be reproducible end-to-end from a clean checkout.

## Repo layout (target state)

The directories below are what the repo looks like at the end of M7. In the current slice phase, only `worker/`, `evals/`, `scripts/`, `docs/`, and `.github/` are populated.

```
cmd/
  orchestrator/      Go binary: control plane API, scheduler, dispatcher    [M1+]
  collector/         Go binary: OTel collector that writes spans to ClickHouse [M2+]

worker/              Python: worker runtime, tool adapters, SDK decorators  [M0+]
  helix/             public SDK surface (`helix.workflow`, `helix.task`)
  helix/v1/          generated gRPC stubs (do not edit by hand)             [M1+]
  tools/             LiteLLM adapter, Qdrant adapter, web search, sandbox

rag/                 Python: indexer, retriever, reranker, miner, trainer   [M0+ partial]
  miner/             failure mining: signature classifiers, hard-negatives  [M5+]
  trainer/           sentence-transformers fine-tuning loop                 [M5+]
  promotion/         alias-swap + canary eval                               [M5+]

web/                 Next.js 14 dashboard                                   [M4+]
proto/               .proto files. Single source of truth for gRPC.        [M1+]
evals/               eval datasets, scorers, harness configs, baselines    [M0+]
migrations/          up-only SQL migrations                                [M1+]
infra/               Dockerfiles, compose, Helm, OTel config

docs/                architecture.md, tech-stack.md, data-model.md, api.md,
                     vertical-slice-plan.md
```

## Required reading before non-trivial changes

In this order:

1. **`docs/vertical-slice-plan.md`** — the only doc that matches reality today. Always read this first while in slice phase.
2. `docs/architecture.md` — what plane owns what in target state
3. `docs/data-model.md` — durable state shape in target state
4. `docs/api.md` — contracts between planes in target state
5. `docs/tech-stack.md` — why each tool is here

For cross-component changes (when those components exist), update `proto/helix/v1/` first, regenerate stubs, then change implementations on both sides. The proto is the contract; everything else is downstream.

## How the maintainer works with agents

The maintainer runs agents in autonomous mode (`--dangerously-skip-permissions` for Claude Code, `--yolo` for Codex CLI) after reviewing a plan. That means:

- For schema changes, event-bus message shape changes, or public API changes (REST, SDK, CLI), produce a written plan first. Do not write code until the maintainer has signed off. These are hard to reverse and easy to fan out across the codebase.
- For self-contained changes inside a single component (e.g., a new scorer, a UI tweak, a worker tool adapter), proceed without asking. Tests and lint must still pass.
- When you finish a unit of work, summarize what changed in 5 lines or fewer and point at the diff. Do not restate the implementation.

If a change requires touching more than three of {orchestrator, worker, rag, web, proto, migrations} — or, in slice phase, more than {worker, evals, scripts} — that is a sign to stop and write a plan.

## Session protocol

Before ending any session that produced commits or left work in progress, you must:

1. Append a new entry to the top of `HANDOFF.md` using the template at the top of that file.
2. Include: timestamp (your local time + timezone), last commit hash, working tree status, current task plan position, what shipped, what's next, open questions, and any gotchas worth surfacing.
3. Verify the entry by running `git log -1 --oneline` and `git status` and pasting the output verbatim into the entry.
4. Identify yourself by tool name in the entry header (e.g. `Claude Code → next session`, `Codex CLI → next session`).

If the session produced no changes (e.g., you only investigated something), append a one-line entry noting that and what was learned.

Do not skip this step. The next agent — possibly a different tool — depends on it.

### Log issues as you hit them

Whenever you run into a non-trivial problem — an environment quirk, a third-party bug, a silent failure or no-op, a design trap, a missing dependency surfaced only at runtime — append an entry to `ISSUES.md` describing the symptom, root cause, fix, and any guard (test/lint/CI) that keeps it fixed. Use the template at the top of that file. Do this **when you solve the issue**, not only at session end, so the record is accurate while it's fresh. The goal is that no agent re-debugs a problem we already understand. If a fix you ship resolves an issue already logged, update that entry rather than adding a duplicate.

## Commands

| Command | Phase | Purpose | Time |
|---|---|---|---|
| `make dev` | slice | Boot Qdrant via docker-compose | ~10s |
| `make dev` | production | Boot Postgres, ClickHouse, Qdrant, NATS, Redis, MinIO, OTel collector | ~30s |
| `make dev-down` | both | Tear down dev stack and volumes | ~5s |
| `make seed` | slice | Prepare corpus + HotpotQA, embed, index in Qdrant | ~10 min |
| `make seed` | production | Load BRIGHT + 50k arXiv slice into MinIO, embed, index | ~10 min |
| `make eval` | slice | Run deep_research over 100 HotpotQA questions, write baseline JSON | ~3 min |
| `make eval-full` | slice | Alias for `make eval` until production eval lands | ~3 min |
| `make eval-full` | production | Full eval suite. Requires a running cluster with workers. | ~1 hr |
| `make eval-final` | both | Run on the sequestered holdout set. Sets `HELIX_HOLDOUT_UNLOCK=1`. **Only run when intentionally measuring final results.** | ~15 min |
| `make test` | slice | Pytest in `worker/` | ~30s |
| `make test` | production | Unit tests for Go and Python | ~30s |
| `make test-integration` | production | Integration tests via testcontainers (real Postgres, ClickHouse, NATS) | ~5 min |
| `make test-eval-smoke` | slice | Alias for `make eval` | ~3 min |
| `make fmt` | both | `ruff format` (slice), plus `gofmt`, `prettier` (production) | ~5s |
| `make lint` | both | `ruff check`, `mypy --strict`, plus `golangci-lint`, `prettier --check` (production). Verifies holdout SHA-256. | ~30s |
| `make build` | production | Build Go binaries to `bin/` | ~20s |
| `make proto` | production | Regenerate gRPC stubs from `proto/helix/v1/` | ~3s |
| `make worker` | production | Run a single Python worker against the local dev stack | foreground |
| `make orchestrator` | production | Run the orchestrator against the local dev stack | foreground |
| `make web` | production | Run the Next.js dashboard in dev mode | foreground |

Always run `make fmt && make lint && make test` before claiming work is done. In production phase with schema or proto changes, also run `make test-integration`.

## Conventions

### Python (current)

- `mypy --strict` is required, not aspirational. `Any` requires a `# type: ignore[<code>]` with a reason.
- `ruff format` + `ruff check`. No black, no isort; ruff covers both.
- Async-first. Workflow tasks are `async def`. Synchronous code is allowed inside tasks but the task itself is async.
- No `print`. Use the `helix.logging` module (slice) or the OTel span context (production).
- Public SDK API is small and documented. Anything in `helix.internal` can change without notice.

### Go (production)

- Structured logging with `slog`. Every log line includes `run_id` and `trace_id` when in a request scope. No `fmt.Println`.
- Context first: every function that does I/O or spans goroutines takes `ctx context.Context` as its first arg.
- No global state. Dependencies are passed in via constructors.
- Error wrapping with `%w`. Sentinel errors live next to their producers.
- Table-driven tests by default.

### TypeScript / Next.js (production)

- App Router. Server Components by default; opt into client components only when interactivity demands it.
- Data fetching through TanStack Query against the REST API. No direct fetch in components.
- shadcn/ui components only. No competing UI libraries.
- Tailwind classes, no CSS modules.
- API types generated from the OpenAPI schema (`web/lib/api-types.ts`). Do not hand-write them.

### SQL (production)

- `snake_case` everywhere.
- Migrations are up-only. Name: `YYYYMMDDHHMM_description.sql`. Never edit a migration after it is merged to main.
- Indices are explicit. Columns used in WHERE or JOIN of any hot path get an index in the same migration that adds them.
- ClickHouse tables default to 90-day TTL on the partition key unless documented otherwise.

### Protobuf (production)

- One service per file. One message per concept.
- Field numbers never reused. Removed fields are `reserved`.
- New fields are `optional` scalars or `repeated`. Required is unavailable in proto3 and we do not fake it.
- Breaking changes go to `proto/helix/v2/`. v1 stays.

## Definition of done

A change is done when:

1. Tests pass (`make test`).
2. Lint passes (`make lint`). This includes the holdout SHA-256 check.
3. For schema, proto, or cross-component changes (production phase): integration tests pass (`make test-integration`).
4. A migration exists if state shape changed (production phase only).
5. `docs/vertical-slice-plan.md` is updated if the slice scope changed.
6. `docs/api.md` is updated if a public API surface changed (production phase).
7. `docs/tech-stack.md` is updated if a dependency was added.
8. `CHANGELOG.md` has a line under `## Unreleased`.
9. `HANDOFF.md` has an updated entry (see Session protocol).

## Slice-phase gotchas

These apply to the current code on disk. Internalize before touching anything.

- **The engine detects local vs submit mode via `contextvars.ContextVar[Engine | None]`.** Workflow code is identical in both modes. If you find yourself special-casing the decorators per mode, you are doing it wrong.
- **The LLM cache key includes `model + messages + temperature + max_tokens` (and `top_p`, `stop`).** Cache hits emit `cache_hit=true` spans with `cost_usd=0`. Do not sum `cost_usd` across runs without filtering by `cache_hit=false`.
- **The holdout file `hotpotqa_dev_holdout_500.jsonl` requires `HELIX_HOLDOUT_UNLOCK=1` to read.** The only legitimate caller is `make eval-final`. CI greps Python files for unauthorized reads of the filename. If you need to test the harness against the holdout, use a mock dataset, not the real one.
- **`Answer.metadata["retrieved_doc_ids"]` must be populated by the workflow.** If you forget, `retrieval_recall@10` is silently zero. The scorer cannot reach into spans to recover this.
- **`temperature=0` is the default in the LLM adapter.** Eval determinism depends on this. Do not override without a reason.
- **The span logger writes JSONL to `data/spans.jsonl`.** It is not OTel and the format is intentionally simplified — but the schema (`trace_id`, `span_id`, `parent_span_id`, `name`, `kind`, `attributes`) matches what the future miner will consume. Do not invent new field names.
- **SQLite under `aiosqlite` does not support concurrent writers cleanly.** The engine serializes writes through a single writer task. If you add a write path, route it through the engine.

## Production-phase gotchas (future)

These do not apply yet. They will apply when the corresponding milestone lands.

- **The orchestrator is the only Postgres writer for run state.** Workers never write to `runs` or `tasks`; they emit events and call `CompleteTask` / `Checkpoint`.
- **No long Postgres transactions.** The orchestrator holds connections briefly. Long-running work happens in workers and lands via gRPC.
- **NATS message visibility timeout is 30s.** Tasks expected to run longer must call `Checkpoint`. Worker SDK does this automatically every 15s.
- **ClickHouse async inserts can lose data on shutdown.** Span writes are async (acceptable). Eval results are synchronous (required for the research thesis).
- **Qdrant collections cannot be renamed.** Embedding promotions flip the `corpus.active` alias. Never write to a specific collection name from production code; always go through the alias.
- **OTel collector queue drops on backpressure silently.** Monitor `otelcol_exporter_send_failed_spans`. If it climbs, scale the collector before debugging "missing spans."
- **Replay tokens are not cryptographic.** They identify recorded calls by `(input_hash, model_id, tool_name)`. Do not use them for auth.
- **`@helix.task` retries are at-least-once.** Tasks must be idempotent on `task_id` or use the SDK's exactly-once helper (Redis sentinel).
- **The dashboard hydrates blobs via signed MinIO URLs.** Local dev uses path-style; production uses virtual-host style. Do not hardcode URL shapes.

## Adding a new feature

### Slice phase

1. Update `docs/vertical-slice-plan.md` if the change adjusts scope.
2. Implement in `worker/helix/` (or `scripts/` for tooling).
3. Add unit tests in `worker/tests/`.
4. If the change touches the eval harness, the engine, or storage, add an integration test that exercises the full path.
5. Update `CHANGELOG.md` and `HANDOFF.md`.
6. Run `make fmt && make lint && make test`.

### Production phase

1. Write the contract in `docs/api.md` (REST/SDK surface) and `proto/helix/v1/` (internal gRPC, if affected).
2. Add the Postgres migration (and ClickHouse migration, if affected).
3. Implement the control-plane side in `cmd/orchestrator/`. Unit tests.
4. Regenerate proto stubs: `make proto`.
5. Implement the worker side in `worker/`. Unit tests.
6. Add an integration test in `worker/tests/integration/` that exercises the full path.
7. Add the dashboard surface in `web/` if user-visible.
8. Update `CHANGELOG.md` and `HANDOFF.md`.
9. Run `make fmt && make lint && make test-integration` before opening the PR.

## Things to never do without asking

- Add a new datastore. The slice has SQLite, Qdrant, and the filesystem. The production system has Postgres, ClickHouse, Qdrant, NATS, Redis, MinIO. Adding to either list is a decision, not a refactor.
- Make a breaking change to `/api/v1/`. Add a v2, or use additive evolution.
- Bypass LiteLLM. Direct calls to Anthropic, OpenAI, or vLLM endpoints are forbidden because they break the cache, replay, and metering.
- Log raw prompts or completions outside the span pipeline.
- Read or reference `evals/datasets/hotpotqa_dev_holdout_500.jsonl` from any file other than `helix/eval/harness.py` or `scripts/prepare_hotpotqa.py`.
- Commit secrets, dataset blobs, model weights, or anything over 1 MB. Use the local cache (gitignored) or, in production, MinIO and git-lfs.
- Delete or edit a migration that has been merged.
- Reuse a proto field number.

## Values

Boring tech wherever boring wins. SQLite, Postgres, Go, Python, Next.js — not because they are exciting but because they are predictable, well-instrumented, and have answers when something breaks at 2 AM.

Prefer deleting code to adding code. The runtime should get smaller as it gets clearer.

Measure before optimizing. The slice has a working span pipeline by design; use it. Hot paths are identified by querying the spans, not by intuition.

The research result is the headline feature. Every architectural decision should make the embedding fine-tune experiment easier to run, easier to reproduce, and easier to trust.