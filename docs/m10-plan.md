# M10 Plan — Embedding-jobs read path + promotion view

> **Status: DRAFT — awaiting sign-off.**
>
> M10 makes the dormant `embedding_jobs` Postgres table live in production and surfaces
> embedding/promotion history in the dashboard. It adds `GET /api/v1/embedding-jobs` (+ detail)
> to the orchestrator and an `/embeddings` page. No new datastore, no proto change, no new
> worker code — the write path is folded into the existing finetune-job lifecycle.

---

## Background and gap

M9b shipped the production failure miner: `POST /api/v1/finetune-jobs` creates a `finetune_jobs`
row, dispatches a NATS task, the worker runs mine → train → promote, and the gRPC `CompleteTask`
hook (`FinalizeFinetuneJob`) writes the outcome back to Postgres.

The `embedding_jobs` table (`migrations/202606150001_initial_schema.sql`) has existed since M1 but
**is never written in production**:

- `create_embedding_job` exists only on `SqliteStore` (`worker/helix/runtime/sqlite_store.py`).
- The M9b worker (`worker/helix/worker/__main__.py:156`) calls it against a **throwaway temp SQLite
  file** to satisfy `promote_candidate`'s store dependency. That row never reaches Postgres.
- There is no Go-side or Postgres-side writer for `embedding_jobs`.

So `embedding_jobs` is dead in production. `finetune_jobs` is the live record of a fine-tune *request*
(train/eval split, corpus alias, before/after recall, outcome). `embedding_jobs` is the complementary
record of the *model artifact* it produced: base model, hyperparameter `config`, `triplets_count`,
`artifact_uri`, and `promoted_at`. The two are 1:1 per job but capture different facets — the
embedding view answers "which model, trained how, promoted when, artifact where," which the
finetune-jobs view does not.

**Decision point:** rather than add a read endpoint over an empty table, M10 also wires the
production write path. The minimal way keeps everything in `{orchestrator, web}` and needs no
proto or worker change (see "Write path" below).

## Schema (already on disk — no migration)

```sql
CREATE TYPE embedding_job_status AS ENUM
  ('queued','mining','training','evaluating','promoted','archived','failed');

CREATE TABLE embedding_jobs (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  base_model     TEXT NOT NULL,
  status         embedding_job_status NOT NULL DEFAULT 'queued',
  config         JSONB NOT NULL,
  triplets_count INT,
  metrics        JSONB,
  artifact_uri   TEXT,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  promoted_at    TIMESTAMPTZ
);
```

No migration is required for the table itself. **One small migration** adds a nullable
`finetune_job_id UUID REFERENCES finetune_jobs(id) ON DELETE SET NULL` column + index so the
embedding-job row can be linked to its finetune job and updated from the same `CompleteTask` hook.
(Up-only, additive — consistent with the SQL conventions in AGENTS.md.)

## Architecture

```
POST /api/v1/finetune-jobs
  → create finetune_jobs row            (existing)
  → create embedding_jobs row           (NEW: status='queued', base_model from request/default,
       linked via finetune_job_id            config={}, triplets_count=NULL)
  → CreateRun + dispatch NATS task      (existing)

worker runs mine→train→promote, CompleteTask(output_json)   (existing, unchanged)
        ↓ gRPC
  FinalizeFinetuneJob hook              (existing) — also NEW:
    UpdateEmbeddingJobOutcome: set status (promoted|archived|failed), triplets_count,
      metrics (before/after recall JSON), promoted_at (now() iff promoted) on the linked row

GET /api/v1/embedding-jobs              → list (id, base_model, status, triplets, Δrecall, promoted_at)
GET /api/v1/embedding-jobs/{id}         → detail (config, metrics, artifact_uri, finetune_job_id)
```

Status granularity for v1 is `queued → {promoted|archived|failed}` (the terminal states the worker
reports). Intermediate `mining/training/evaluating` would require the worker to emit phase
checkpoints — **deferred**; the column already supports them.

## Changes

### 1. Migration

- `migrations/202606160002_embedding_jobs_finetune_link.sql` — add `finetune_job_id` column + index.

### 2. Go store — `internal/store/embedding.go` (NEW)

- `EmbeddingJob` struct (pointer fields for nullable `triplets_count`, `metrics`, `artifact_uri`,
  `promoted_at`, `finetune_job_id`); `Metrics` surfaced as `map[string]any`.
- `EmbeddingJobStore` interface: `CreateEmbeddingJob`, `GetEmbeddingJob`, `ListEmbeddingJobs`,
  `UpdateEmbeddingJobOutcome(ctx, finetuneJobID, status, triplets, metricsJSON, promoted bool)`.
- `PostgresEmbeddingJobStore` implementation. Mirrors `PostgresFinetuneJobStore`.
- Unit-testable via a narrow interface; no change to the existing `Store` interface or `MockStore`.

### 3. Wire the write path

- `internal/api/handler.go` — in `createFinetuneJob`, after creating the finetune row + run,
  call `embeddingJobs.CreateEmbeddingJob` (status queued, linked by finetune_job_id). Best-effort
  / non-fatal on error, like `SetFinetuneJobRun`. Add `WithEmbeddingJobs` setter + optional field.
- `internal/grpc/server.go` — extend the existing finetune `CompleteTask` hook to also call
  `UpdateEmbeddingJobOutcome` (best-effort, `WarnContext` on error). Reuses the parsed
  `FinetuneTaskOutput`; no new gRPC RPC.
- `cmd/orchestrator/main.go` — construct `PostgresEmbeddingJobStore`, inject into handler + gRPC.

### 4. Read endpoints — `internal/api/handler.go`

- `GET /api/v1/embedding-jobs` → `ListEmbeddingJobs` (50 most recent), `[]` when empty.
- `GET /api/v1/embedding-jobs/{job_id}` → `GetEmbeddingJob`, 404 when missing.
- 503 when the store is unconfigured (mirrors finetune handler).
- Handler tests in `internal/api/handler_test.go` with a `mockEmbeddingJobStorer` (happy path,
  503-not-configured, empty list, get-by-id, 404).

### 5. Web dashboard

- `web/openapi.yaml` — add `EmbeddingJob` schema + `/embedding-jobs` and `/embedding-jobs/{job_id}` paths.
- `web/lib/types.ts`, `web/lib/api.ts` — generated type + `fetchEmbeddingJobs` / `fetchEmbeddingJob`.
- `web/app/api/embedding-jobs/route.ts` + `[job_id]/route.ts` — BFF proxies (Bearer server-side).
- `web/components/embedding-job-table.tsx` — table: ID, base model, status badge, triplets,
  before/after %, Δrecall, promoted-at. Reuses the `StatusBadge`/`pct` conventions from
  `finetune-job-table.tsx`. `staleTime: 30_000`.
- `web/app/embeddings/page.tsx` + nav link in `web/app/layout.tsx`.
- Vitest component tests: renders a row, empty state, 503 message, generic error.

## Sequencing

1. Migration + `internal/store/embedding.go` + (compile).
2. Read endpoints + handler tests (table empty but contract verified).
3. Write path: handler create + gRPC hook update + orchestrator wiring.
4. Integration test: extend `worker/tests/integration/test_finetune.py` (or a new
   `test_embedding.py`) to assert a linked embedding-job row appears and reaches a terminal status.
5. Web: OpenAPI → types → BFF → component → page → nav + tests.

## Definition of done

- `embedding_jobs` rows are created on `POST /api/v1/finetune-jobs` and finalized by the
  `CompleteTask` hook (status, triplets_count, metrics, promoted_at).
- `GET /api/v1/embedding-jobs` and `GET /api/v1/embedding-jobs/{id}` return correct data.
- `/embeddings` dashboard page lists jobs with status, Δrecall, promotion time.
- `docs/api.md` updated (new REST surface); `CHANGELOG.md` + `HANDOFF.md` updated.
- `make fmt && make lint && make test` pass; `make test-integration` exercises the linked row.

## Out of scope (future)

- Intermediate phase statuses (`mining/training/evaluating`) — needs worker checkpoints.
- Presigned MinIO URLs for `artifact_uri` (show the raw URI for now).
- Real `config` hyperparameters from the worker — v1 stores `{}` / defaults; populating the actual
  `TrainConfig` requires threading it through `FinetuneTaskOutput` (additive, deferred).
