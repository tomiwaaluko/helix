# M1 Plan — Go Orchestrator

> **Status: DRAFT — requires maintainer sign-off before any code is written.**
>
> Per `AGENTS.md`: schema changes, event-bus message shapes, and public API surfaces
> require a written plan first. This document covers all three.
>
> Sign-off needed on: proto scope (§3), Postgres migration scope (§4),
> worker mode split (§5.3), and open decisions (§8).

---

## 1. What M1 delivers

M1 replaces the M0 in-process dispatch layer (SQLite + asyncio.Queue) with the real
production control plane:

| Component | M0 (on disk today) | M1 target |
|---|---|---|
| State | SQLite via aiosqlite | Postgres 15 via `pgx/v5` |
| Dispatch | asyncio.Queue (in-process) | NATS JetStream |
| Worker coordination | Engine contextvar | gRPC (RegisterWorker / Heartbeat / CompleteTask) |
| Run submission | Python CLI | REST `/api/v1/runs` + Python SDK wrapper |
| Orchestrator | None (Engine in-process) | Go binary `cmd/orchestrator` |
| Proto stubs | None | `proto/helix/v1/` → `worker/helix_proto/` |
| Migrations | None | `migrations/` (up-only SQL) |

### What M1 does NOT deliver

Deferred to later milestones to keep M1 shippable in one sprint:

| Item | Target |
|---|---|
| ClickHouse (spans → OTel collector → CH) | M2 |
| Redis (rate limits, distributed locks) | M3 |
| MinIO (large payload blobs, checkpoints) | M3 |
| Replay tokens | M3 |
| Dashboard (`web/`) | M4 |
| Failure miner as a production workflow (currently Python CLI) | M5 |
| EvalRunner gRPC service | M5 |
| Embeddings gRPC service (StartFineTuneJob, PromoteModel) | M5 |
| Kubernetes / Helm chart | M6 |

`make eval` (the BRIGHT and HotpotQA eval loops) continues to run in local mode.
The embedding fine-tune CLI (`make finetune-bright`) continues unchanged. M1 must
not break either.

---

## 2. Architecture delta

### M0 call path (today)

```
helix CLI
  └─ _finetune / evaluate() [Python, in-process]
       └─ Engine (asyncio loop)
            └─ SQLite (aiosqlite)  ←  state
            └─ asyncio.Queue       ←  dispatch
                └─ task handler (deep_research, etc.)
```

### M1 call path (after landing)

```
helix CLI  /  REST POST /api/v1/runs
  │
  ▼
Go orchestrator  (cmd/orchestrator)
  ├─ Postgres [runs, tasks, task_attempts, …]   ←  state
  └─ NATS JetStream [helix.tasks.dispatch.*]    ←  dispatch
       │
       ▼
Python worker  (worker/)
  ├─ gRPC → RegisterWorker / Heartbeat          ←  registration
  ├─ NATS consumer (pulls task envelope)
  │    └─ deserialize → call workflow handler
  └─ gRPC → CompleteTask                        ←  completion
```

Local mode (contextvar = None) is unchanged. It runs without the orchestrator and
is the path used by `make eval`, `make finetune-bright`, and all unit tests.

---

## 3. Proto contracts  ← **sign-off required**

`proto/helix/v1/` — one service per file, per conventions in `AGENTS.md`.

### 3.1  `types.proto`

Shared message types used across services.

```proto
syntax = "proto3";
package helix.v1;
option go_package = "github.com/tomiwaaluko/helix/gen/go/helix/v1";

message TaskEnvelope {
  string task_id         = 1;
  string run_id          = 2;
  string workflow_name   = 3;
  int32  workflow_version = 4;
  string node_id         = 5;
  bytes  input_json      = 6;   // JSONB payload
  int32  attempt_number  = 7;
  string trace_id        = 8;
  string deadline        = 9;   // RFC 3339
  optional string replay_token = 10;
}

message TaskResult {
  string task_id        = 1;
  int32  attempt_number = 2;
  string worker_id      = 3;
  string status         = 4;   // "succeeded" | "failed"
  bytes  output_json    = 5;
  optional string error = 6;
  string span_id        = 7;
  int32  duration_ms    = 8;
}
```

### 3.2  `orchestrator.proto`

```proto
syntax = "proto3";
package helix.v1;
option go_package = "github.com/tomiwaaluko/helix/gen/go/helix/v1";

import "helix/v1/types.proto";

service Orchestrator {
  // Worker lifecycle
  rpc RegisterWorker(RegisterWorkerRequest)   returns (RegisterWorkerResponse);
  rpc Heartbeat(stream HeartbeatRequest)      returns (stream HeartbeatResponse);

  // Task completion (idempotent on task_id + attempt_number)
  rpc CompleteTask(CompleteTaskRequest)       returns (CompleteTaskResponse);

  // Long-running task progress
  rpc Checkpoint(CheckpointRequest)           returns (CheckpointResponse);
}

message RegisterWorkerRequest {
  string pool           = 1;
  repeated string capabilities = 2;
  int32  max_concurrency = 3;
}
message RegisterWorkerResponse {
  string worker_id      = 1;
  int32  lease_seconds  = 2;
}

message HeartbeatRequest {
  string worker_id  = 1;
  int32  in_flight  = 2;
  int32  capacity   = 3;
}
message HeartbeatResponse {
  bool drain = 1;   // orchestrator signals graceful shutdown
}

message CompleteTaskRequest {
  TaskResult result = 1;
}
message CompleteTaskResponse {
  string disposition = 1;  // "accepted" | "superseded" | "dropped"
}

message CheckpointRequest {
  string task_id       = 1;
  int32  attempt_number = 2;
  bytes  state         = 3;
}
message CheckpointResponse {}
```

**Out of M1 proto scope:** `GetWorkflow`, `EvalRunner`, `Embeddings` services.
Field numbers 1–10 reserved in each message; new M2+ fields start at 11.

### 3.3  Stub generation

```makefile
proto:
	protoc \
	  --go_out=gen/go --go_opt=paths=source_relative \
	  --go-grpc_out=gen/go --go-grpc_opt=paths=source_relative \
	  proto/helix/v1/types.proto proto/helix/v1/orchestrator.proto
	python -m grpc_tools.protoc \
	  -Iproto \
	  --python_out=worker/helix_proto \
	  --grpc_python_out=worker/helix_proto \
	  proto/helix/v1/types.proto proto/helix/v1/orchestrator.proto
```

`worker/helix_proto/` is generated; `# do not edit by hand` in each file.

---

## 4. Postgres migrations  ← **sign-off required**

`migrations/` — timestamp-prefixed, up-only, as per conventions.

### 4.1  `202606150001_initial_schema.sql`

Covers the 10 tables from `docs/data-model.md`. Only tables needed in M1 listed
explicitly; the rest (datasets, evals, failure_cases, embedding_jobs) are created
now so M2+ can add foreign keys without a retrofit migration.

```sql
-- Full schema per docs/data-model.md.
-- See the table definitions in that doc verbatim; pasted here for review.

CREATE TYPE run_status AS ENUM ('pending','running','succeeded','failed','cancelled');
CREATE TYPE task_status AS ENUM ('pending','ready','running','succeeded','failed','cancelled','dead');
CREATE TYPE embedding_job_status AS ENUM
  ('queued','mining','training','evaluating','promoted','archived','failed');

-- workflows, runs, tasks, task_attempts, checkpoints, dead_letter_queue,
-- workers, datasets, evals, embedding_jobs, failure_cases
-- (full DDL per data-model.md)
```

### 4.2  SQLite → Postgres mapping

The M0 SQLite tables map to Postgres as follows:

| SQLite table (M0) | Postgres table (M1) | Notes |
|---|---|---|
| `runs` | `runs` | UUID pk replaces TEXT; `workflow_id` FK added |
| `tasks` | `tasks` | Same shape; `node_id` is the M0 task name |
| `eval_results` | `evals` + `eval_events` (M2, via ClickHouse) | In M1, eval results stay in local JSONL (no migration needed yet) |
| `embedding_jobs` | `embedding_jobs` | Direct mapping; existing M0 job rows not migrated (start fresh) |
| `failure_cases` | `failure_cases` | Direct mapping |

M0 eval results in `data/helix.db` are **not migrated** — they are development
artifacts; the BRIGHT baseline lives in `evals/baselines/`.

### 4.3  Migration runner

Use `golang-migrate/migrate` inside the orchestrator. On startup:

```go
m, _ := migrate.NewWithDatabaseInstance("file://migrations", "postgres", driver)
m.Up()  // no-op if already at latest
```

---

## 5. Component changes

### 5.1  Go orchestrator  (`cmd/orchestrator/`)

Minimum viable binary for M1:

**gRPC layer** (`internal/grpc/`):
- Implement `Orchestrator` service (4 RPCs above).
- `RegisterWorker`: insert into `workers` table, return `worker_id` + lease.
- `Heartbeat`: stream — update `last_heartbeat`; push drain signal if worker is
  marked for drain via REST.
- `CompleteTask`: update `tasks` + `task_attempts` in a transaction; mark run
  `succeeded` if all tasks complete; publish `helix.events.completion` to NATS.
- `Checkpoint`: upsert `checkpoints` table.

**REST layer** (`internal/api/`):
- `POST /api/v1/runs` — create `runs` + `tasks` rows; publish first ready task(s)
  to NATS `helix.tasks.dispatch.<pool>`.
- `GET /api/v1/runs/{run_id}` — join `runs` + `tasks`.
- `GET /api/v1/runs` — filterable list.
- `POST /api/v1/runs/{run_id}/cancel` — set status to `cancelled`.

**NATS subscriber** (`internal/dispatch/`):
- Subscribe to `helix.events.completion` (worker completion events, as a
  redundant/alternative path to gRPC CompleteTask — gRPC is primary in M1).
- On receipt: identical Postgres update path as `CompleteTask`.

**Scheduler** (`internal/scheduler/`):
- After any task completes, re-evaluate DAG: find tasks whose dependencies are all
  `succeeded`, mark them `ready`, publish to NATS.
- In M1, `deep_research` is a single-node workflow (no sub-tasks), so the
  scheduler dispatches the root task immediately on run creation and marks the run
  done on task completion. Full DAG scheduling is exercised only when M2+ adds
  multi-step workflows.

**Wire-up** (`cmd/orchestrator/main.go`):
- Parse config from env (DATABASE_URL, NATS_URL, GRPC_PORT, HTTP_PORT).
- Run migration on startup.
- Start gRPC server and HTTP server concurrently.
- Structured `slog` logging with `run_id`/`trace_id` fields on every log line.

### 5.2  Postgres driver and libraries

| Library | Purpose |
|---|---|
| `github.com/jackc/pgx/v5` | Postgres driver + connection pool |
| `github.com/golang-migrate/migrate/v4` | Migration runner |
| `google.golang.org/grpc` | gRPC server/client |
| `google.golang.org/protobuf` | Proto generated code |
| `github.com/nats-io/nats.go` | NATS publisher + subscriber |
| `github.com/go-chi/chi/v5` | HTTP router |
| `log/slog` (stdlib) | Structured logging |

No global state. All dependencies injected via constructors.

### 5.3  Python worker changes  ← **sign-off required**

The worker already has the local/submit mode split via `contextvars.ContextVar[Engine | None]`.
M1 adds a third mode: **remote** (connected to the Go orchestrator).

**Mode detection** (unchanged contract per AGENTS.md):
- `contextvar = None` → local mode (unit tests, `make eval`). No change.
- `contextvar = Engine(...)` → submit mode (M0 full run). Kept for backward compat
  during M1 transition but deprecated.
- `contextvar = RemoteEngine(...)` → remote mode (M1 production path).

`RemoteEngine` wraps:
1. A gRPC stub (`OrchestratorStub`) — calls `RegisterWorker`, `Heartbeat`,
   `CompleteTask`.
2. A NATS subscriber — pulls `TaskEnvelope` from `helix.tasks.dispatch.<pool>`,
   deserializes, calls the workflow handler, then calls `CompleteTask`.

`SqliteStore` is NOT replaced — it continues to be used by local mode and the
existing Python fine-tune CLI. It does not need to talk to Postgres.

New file: `worker/helix/runtime/remote_engine.py` — implements `RemoteEngine`.
The `Engine` class in `engine.py` is untouched.

**Worker entrypoint** (`make worker`):
```
python -m helix.worker \
  --orchestrator grpc://localhost:50051 \
  --nats nats://localhost:4222 \
  --pool research
```

This creates a `RemoteEngine`, registers with the orchestrator, and enters the NATS
consume loop. Spans are written to `data/spans.jsonl` (same JSONL logger as M0;
OTel export added in M2).

### 5.4  Docker Compose update

`infra/docker-compose.yml` gains:

```yaml
postgres:
  image: postgres:15
  environment:
    POSTGRES_DB: helix
    POSTGRES_USER: helix
    POSTGRES_PASSWORD: helix
  ports: ["5432:5432"]

nats:
  image: nats:2.10
  command: ["--jetstream"]
  ports: ["4222:4222", "8222:8222"]
```

`make dev` boots Qdrant (existing) + Postgres + NATS.
`make dev-down` tears all three down.

---

## 6. Make targets delta

| Target | M0 | M1 |
|---|---|---|
| `make dev` | Qdrant only | Qdrant + Postgres + NATS |
| `make proto` | (missing) | Regenerate stubs from `proto/helix/v1/` |
| `make build` | (missing) | `go build ./cmd/orchestrator/` → `bin/orchestrator` |
| `make orchestrator` | (missing) | Run `bin/orchestrator` against local dev stack |
| `make worker` | (missing) | Run Python worker against local dev stack |
| `make eval` | Python in-process | **Unchanged** — still uses local mode |
| `make test` | pytest only | pytest + `go test ./...` |
| `make lint` | ruff + mypy | ruff + mypy + `golangci-lint run` |
| `make test-integration` | (missing) | End-to-end: submit run via REST, worker picks it up, result in Postgres |

---

## 7. Test plan

### Unit tests

- Go: table-driven tests for the scheduler DAG logic, `CompleteTask` idempotency,
  heartbeat expiry. Use `pgx` test helpers with an in-process Postgres (testcontainers).
- Python: existing 139 tests unchanged. Add `test_remote_engine.py` using a mock
  gRPC stub and mock NATS.

### Integration test  (`make test-integration`)

1. `docker compose up` Postgres + NATS.
2. Run migrations.
3. Start orchestrator.
4. Start Python worker (remote mode, pool=research).
5. POST `/api/v1/runs` with `{workflow_name: "deep_research", input: {question: "..."}}`.
6. Poll `GET /api/v1/runs/{run_id}` until `status=succeeded` or timeout 120s.
7. Assert `tasks` row has `status=succeeded` and non-null `output`.

The question uses a pre-cached LLM response so the test is deterministic and fast.

### Backward compat smoke test

`make eval` (BRIGHT or HotpotQA, 10 questions) must pass in local mode with no
orchestrator running. This is the regression guard that local mode is untouched.

---

## 8. Open decisions  ← **sign-off required**

These need maintainer answers before implementation begins.

**8.1  Workflow registration**

`docs/api.md` says workflows register at worker startup via `RegisterWorker`, and
the orchestrator stores DAG specs in the `workflows` table. In M1, `deep_research`
is a single-node workflow so the DAG topology is trivial. Two options:

- **Option A (simple):** Workers do not send a DAG spec in M1; the orchestrator
  treats every run as a single-task workflow. DAG materialization added in M2 when
  multi-step workflows arrive.
- **Option B (full):** Workers send `spec` JSONB in `RegisterWorkerRequest`; the
  orchestrator stores it now.

*Recommendation: Option A.* The M0 `deep_research` is effectively a single task;
building the full DAG router now adds complexity with no immediate payoff.

**8.2  gRPC vs NATS for task completion**

Workers can report task completion via `CompleteTask` RPC (gRPC, direct) or via
publishing to `helix.events.completion` (NATS, indirect). Both paths update the
same Postgres row. Two options:

- **Option A:** gRPC primary, NATS secondary (fail-over when gRPC drops).
- **Option B:** NATS only — workers never call CompleteTask directly; the
  orchestrator subscribes to the NATS completion topic.

*Recommendation: Option A.* It matches `docs/api.md`, keeps the gRPC stub's
`CompleteTask` exercised, and provides a fast path. The NATS subscriber is a
resilience layer, not the primary path.

**8.3  Bearer token auth in M1**

`docs/api.md` says v1 REST uses bearer tokens. In M1 this can be:

- **Option A:** No auth — REST endpoints unauthenticated. Acceptable while
  single-tenant local-dev only.
- **Option B:** Static token from env (`HELIX_API_TOKEN`). One token, no rotation.

*Recommendation: Option B.* Takes 30 min to add, avoids shipping an unauthenticated
REST endpoint even for a dev milestone.

**8.4  `make eval` routing**

After M1, should `make eval` continue using local mode (Python in-process), or should
it route through the real orchestrator? Options:

- **Option A:** Keep local mode for `make eval`. Orchestrator path exercised only
  by `make test-integration` and `make worker`.
- **Option B:** `make eval` submits to the real orchestrator in M1.

*Recommendation: Option A.* Preserves the fast iteration loop for eval and keeps
the BRIGHT experiment rerunnable without needing the full stack. The orchestrator
path is exercised by integration tests.

---

## 9. Task order (serial dependencies)

Implementation must proceed in this order due to hard dependencies:

1. **Proto files** — `proto/helix/v1/types.proto`, `orchestrator.proto`. No code before these are signed off.
2. **`make proto` target** — verify stubs generate cleanly in Go and Python.
3. **Migration 001** — `migrations/202606150001_initial_schema.sql`. Sign off on DDL before any Go code references it.
4. **Docker Compose** — add Postgres + NATS; verify `make dev` boots clean.
5. **Go orchestrator skeleton** — `cmd/orchestrator/main.go`, config, migration runner, health endpoint.
6. **Go gRPC server** — implement `RegisterWorker`, `Heartbeat`, `CompleteTask`, `Checkpoint`.
7. **Go REST layer** — `POST/GET /api/v1/runs`, `POST /api/v1/runs/{id}/cancel`.
8. **Go NATS publisher** — publish `TaskEnvelope` to `helix.tasks.dispatch.research` on run creation.
9. **Go scheduler** — single-task DAG (Option A from §8.1): mark run done when task completes.
10. **Python `RemoteEngine`** — gRPC client + NATS consumer in `worker/helix/runtime/remote_engine.py`.
11. **`make worker` target** — start Python worker in remote mode.
12. **Integration test** — `make test-integration` (end-to-end smoke).
13. **`make lint` + `make test`** — full gate, both Go and Python.
14. **Update `AGENTS.md`** — replace the SQLite/asyncio bullets with the M1 stack; update the commands table.

Steps 1–4 are plan-phase deliverables (no code). Steps 5–13 are implementation. Step 14 closes the milestone.

---

## 10. Definition of done

M1 is done when:

- [ ] `proto/helix/v1/` is committed and stubs regenerate cleanly (`make proto`).
- [ ] `migrations/202606150001_initial_schema.sql` is committed and idempotent.
- [ ] `make dev` boots Qdrant + Postgres + NATS in one command.
- [ ] `make build` produces `bin/orchestrator`.
- [ ] `make test-integration` passes end-to-end: run submitted via REST, worker
      picks it up via NATS, completion reported via gRPC, result readable in Postgres.
- [ ] `make eval` (BRIGHT, 10 questions, local mode) passes unchanged.
- [ ] `make test` passes (Go + Python).
- [ ] `make lint` passes (golangci-lint + ruff + mypy).
- [ ] `AGENTS.md` updated: SQLite/asyncio bullets removed, M1 stack documented.
- [ ] `CHANGELOG.md` has an M1 entry.
- [ ] `HANDOFF.md` updated.
