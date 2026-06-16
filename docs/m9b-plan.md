# M9b Plan — Production Failure Miner

## Goal

Wire the Python failure miner (`mine_from_clickhouse` + `trainer.fit` + `promoter.run`) into
the production system so it can be triggered via the Go orchestrator, tracks job state in
Postgres, and runs as a Python worker task over NATS — the same path as all other workflow tasks.

After M9b, `POST /api/v1/finetune-jobs` kicks off the full mine → train → promote loop and
the orchestrator tracks its status. `make finetune` (local) continues to work unchanged.

---

## Architecture

```
POST /api/v1/finetune-jobs
  → orchestrator creates finetune_jobs row (Postgres) + publishes FinetuneJobTask to NATS
        ↓ NATS
  Python worker receives FinetuneJobTask
    1. mine_from_clickhouse(ch_http_url)      → FailureCase list
    2. trainer.fit(cases, checkpoint_dir)     → candidate checkpoint
    3. promoter.run(before/after recall)      → promoted | archived
    → calls CompleteTask / Checkpoint via gRPC
        ↓ gRPC
  orchestrator updates finetune_jobs.status in Postgres

GET /api/v1/finetune-jobs           → list jobs (status, delta, outcome)
GET /api/v1/finetune-jobs/{job_id}  → detail (per-phase timing, metrics)
```

---

## Changes required

### 1. Postgres migration

New migration: `migrations/202606160001_finetune_jobs.sql`

```sql
CREATE TABLE finetune_jobs (
  id            TEXT PRIMARY KEY,
  status        TEXT NOT NULL DEFAULT 'pending',  -- pending|mining|training|promoting|done|failed
  train_split   TEXT NOT NULL,
  eval_split    TEXT NOT NULL,
  corpus_alias  TEXT NOT NULL DEFAULT 'corpus.active',
  failures      INTEGER,
  triplets      INTEGER,
  before_recall DOUBLE PRECISION,
  after_recall  DOUBLE PRECISION,
  outcome       TEXT,  -- promoted|archived|null
  error         TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 2. Proto update — `helix/v1/finetune.proto`

New file (or extend `tasks.proto`):

```proto
message FinetuneJobTask {
  string job_id     = 1;
  string train_split = 2;   // e.g. "hotpotqa_train_1000.jsonl"
  string eval_split  = 3;   // e.g. "hotpotqa_dev_100.jsonl"
  string corpus_alias = 4;  // e.g. "corpus.active"
}
```

### 3. Go orchestrator

**`internal/store/finetune.go`** — CRUD for `finetune_jobs`:
- `CreateFinetuneJob(ctx, input) (FinetuneJob, error)`
- `GetFinetuneJob(ctx, jobID) (FinetuneJob, error)`
- `ListFinetuneJobs(ctx) ([]FinetuneJob, error)`
- `UpdateFinetuneJob(ctx, jobID, fields) error`

**`internal/api/handler.go`** additions:
- `POST /api/v1/finetune-jobs` — creates job row + publishes NATS task
- `GET  /api/v1/finetune-jobs` — lists jobs
- `GET  /api/v1/finetune-jobs/{job_id}` — detail

### 4. Python worker

**`worker/helix/worker/__main__.py`** — add handler for `FinetuneJobTask`:
```python
async def handle_finetune_job(task: FinetuneJobTask) -> None:
    # Phase 1: mine
    cases = await mine_from_clickhouse(...)
    # Checkpoint progress: report failures count
    # Phase 2: train
    ckpt = await trainer.fit(cases, ...)
    # Phase 3: promote
    outcome = await promoter.run(ckpt, ...)
    # Complete task: report outcome
```

**`worker/helix/cli.py`** — `_finetune` continues to work unchanged for local runs.

### 5. Web dashboard

- `openapi.yaml` — add `FinetuneJob` schema + `/finetune-jobs` paths
- `web/app/api/finetune-jobs/route.ts` — BFF proxy
- `web/components/finetune-job-table.tsx` — shows status, delta, outcome
- `web/app/finetune-jobs/page.tsx` + nav link
- `web/components/eval-detail.tsx` — "Trigger fine-tune →" button linking to POST

---

## Sequencing

1. Postgres migration + `internal/store/finetune.go` + unit tests
2. Proto update + `make proto`
3. `internal/api/handler.go` — POST + GET endpoints + handler tests
4. Python worker `handle_finetune_job` + unit tests
5. Integration test: POST → NATS → Python worker runs → Postgres updated
6. Web dashboard

## Definition of done

- `POST /api/v1/finetune-jobs` creates a job and dispatches a NATS task
- Python worker picks up the task and runs the full mine → train → promote pipeline
- Job status tracked in Postgres through all phases
- `GET /api/v1/finetune-jobs/{job_id}` returns final status + metrics
- Web dashboard shows jobs with status, delta, outcome
- `make test` and `make lint` pass
- `make test-integration` exercises the full NATS → worker → gRPC path
