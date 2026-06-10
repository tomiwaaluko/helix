# Data Model

## Postgres (control plane)

### `workflows`

Registered workflow definitions. Versioned. The orchestrator never executes the code; this table stores the materialized DAG.

```sql
CREATE TABLE workflows (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name            TEXT NOT NULL,
  version         INT NOT NULL,
  spec            JSONB NOT NULL,
  registered_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  registered_by   TEXT NOT NULL,
  UNIQUE (name, version)
);
CREATE INDEX workflows_name_idx ON workflows (name);
```

`spec` shape:

```json
{
  "nodes": [
    {"id": "search", "handler": "deep_research.search", "retry": {"max": 3, "backoff": "exponential"}},
    {"id": "read",   "handler": "deep_research.read",   "depends_on": ["search"]}
  ],
  "entry": "search",
  "timeout_seconds": 300
}
```

### `runs`

Workflow run instances.

```sql
CREATE TYPE run_status AS ENUM ('pending', 'running', 'succeeded', 'failed', 'cancelled');

CREATE TABLE runs (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workflow_id     UUID NOT NULL REFERENCES workflows(id),
  status          run_status NOT NULL DEFAULT 'pending',
  input           JSONB NOT NULL,
  output          JSONB,
  error           JSONB,
  submitted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at      TIMESTAMPTZ,
  finished_at     TIMESTAMPTZ,
  submitted_by    TEXT NOT NULL,
  metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
  trace_id        TEXT NOT NULL
);
CREATE INDEX runs_workflow_idx ON runs (workflow_id, submitted_at DESC);
CREATE INDEX runs_status_idx   ON runs (status) WHERE status IN ('pending', 'running');
```

### `tasks`

Logical task instances within a run. One row per `(run, node)`.

```sql
CREATE TYPE task_status AS ENUM ('pending', 'ready', 'running', 'succeeded', 'failed', 'cancelled', 'dead');

CREATE TABLE tasks (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id          UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  node_id         TEXT NOT NULL,
  status          task_status NOT NULL DEFAULT 'pending',
  input           JSONB NOT NULL,
  output          JSONB,
  attempts        INT NOT NULL DEFAULT 0,
  UNIQUE (run_id, node_id)
);
CREATE INDEX tasks_run_idx    ON tasks (run_id);
CREATE INDEX tasks_status_idx ON tasks (status) WHERE status IN ('ready', 'running');
```

### `task_attempts`

Each retry is a row. Append-only.

```sql
CREATE TABLE task_attempts (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id         UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  attempt_number  INT NOT NULL,
  worker_id       UUID NOT NULL,
  status          task_status NOT NULL,
  started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at     TIMESTAMPTZ,
  error           JSONB,
  span_id         TEXT,
  UNIQUE (task_id, attempt_number)
);
CREATE INDEX task_attempts_task_idx ON task_attempts (task_id);
```

### `checkpoints`

Per-task durable state for long-running tasks. Opaque bytes; semantics are owned by the task handler.

```sql
CREATE TABLE checkpoints (
  task_id     UUID PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
  state       BYTEA NOT NULL,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### `dead_letter_queue`

Tasks that exhausted retries. Replayable by hand or by API.

```sql
CREATE TABLE dead_letter_queue (
  task_id      UUID PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
  reason       TEXT NOT NULL,
  context      JSONB NOT NULL,
  parked_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  replayed_at  TIMESTAMPTZ
);
CREATE INDEX dlq_parked_idx ON dead_letter_queue (parked_at DESC);
```

### `workers`

Registered workers and heartbeats.

```sql
CREATE TABLE workers (
  id              UUID PRIMARY KEY,
  pool            TEXT NOT NULL,
  capabilities    JSONB NOT NULL,
  registered_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_heartbeat  TIMESTAMPTZ NOT NULL DEFAULT now(),
  status          TEXT NOT NULL
);
CREATE INDEX workers_pool_idx ON workers (pool, status);
```

### `datasets`

Eval datasets, versioned, content-hashed.

```sql
CREATE TABLE datasets (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name            TEXT NOT NULL,
  version         INT NOT NULL,
  description     TEXT,
  examples_uri    TEXT NOT NULL,
  content_hash    TEXT NOT NULL,
  size            INT NOT NULL,
  registered_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (name, version)
);
```

### `evals`

Eval run instances. Each eval is itself a Helix workflow.

```sql
CREATE TABLE evals (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workflow_id     UUID NOT NULL REFERENCES workflows(id),
  dataset_id      UUID NOT NULL REFERENCES datasets(id),
  scorer          TEXT NOT NULL,
  status          run_status NOT NULL DEFAULT 'pending',
  results         JSONB,
  started_at      TIMESTAMPTZ,
  finished_at     TIMESTAMPTZ
);
CREATE INDEX evals_workflow_idx ON evals (workflow_id);
```

`results` shape:

```json
{
  "mean": 0.6342,
  "ci_low": 0.6188,
  "ci_high": 0.6493,
  "n": 1000,
  "per_example_uri": "s3://helix-evals/<eval_id>/results.jsonl"
}
```

### `embedding_jobs`

State of fine-tune jobs.

```sql
CREATE TYPE embedding_job_status AS ENUM (
  'queued', 'mining', 'training', 'evaluating', 'promoted', 'archived', 'failed'
);

CREATE TABLE embedding_jobs (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  base_model      TEXT NOT NULL,
  status          embedding_job_status NOT NULL DEFAULT 'queued',
  config          JSONB NOT NULL,
  triplets_count  INT,
  metrics         JSONB,
  artifact_uri    TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  promoted_at     TIMESTAMPTZ
);
```

`config` shape:

```json
{
  "lookback_days": 7,
  "lr": 2e-5,
  "batch_size": 64,
  "epochs": 3,
  "loss": "info_nce",
  "negatives_per_query": 4
}
```

`metrics` shape:

```json
{
  "before": {"recall@10": 0.61, "ci_low": 0.59, "ci_high": 0.63},
  "after":  {"recall@10": 0.68, "ci_low": 0.66, "ci_high": 0.70},
  "delta":  {"recall@10": 0.07, "p_value": 0.003}
}
```

### `failure_cases`

Mined retrieval failures.

```sql
CREATE TABLE failure_cases (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  span_id         TEXT NOT NULL,
  run_id          UUID REFERENCES runs(id) ON DELETE SET NULL,
  query           TEXT NOT NULL,
  gold_passage_id TEXT NOT NULL,
  retrieved_top_k JSONB NOT NULL,
  signature       TEXT NOT NULL,
  mined_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX failure_cases_signature_idx ON failure_cases (signature);
CREATE INDEX failure_cases_mined_idx     ON failure_cases (mined_at DESC);
```

`retrieved_top_k` shape:

```json
[
  {"passage_id": "arxiv:2310.12345#sec3", "score": 0.81, "rank": 1},
  {"passage_id": "arxiv:2401.99999#abs",  "score": 0.79, "rank": 2}
]
```

## ClickHouse (telemetry)

### `spans`

OTel spans. Partitioned by day, ordered by `(trace_id, start_time, span_id)`.

```sql
CREATE TABLE spans (
  trace_id        String,
  span_id         String,
  parent_span_id  String,
  run_id          String,
  task_id         String,
  attempt_number  UInt32,
  name            LowCardinality(String),
  kind            LowCardinality(String),
  start_time      DateTime64(9),
  end_time        DateTime64(9),
  duration_ms     UInt32 MATERIALIZED toUInt32((toUnixTimestamp64Nano(end_time) - toUnixTimestamp64Nano(start_time)) / 1000000),
  status          LowCardinality(String),
  status_message  String,
  service_name    LowCardinality(String),
  worker_id       String,
  attributes      Map(String, String),
  events          Array(Tuple(timestamp DateTime64(9), name String, attributes Map(String, String)))
) ENGINE = MergeTree
PARTITION BY toYYYYMMDD(start_time)
ORDER BY (trace_id, start_time, span_id)
TTL toStartOfDay(start_time) + INTERVAL 90 DAY;
```

`kind` values: `llm`, `tool`, `retrieval`, `workflow`, `internal`.

### `llm_calls`

Materialized denormalization of `spans WHERE kind = 'llm'` for fast cost and latency queries.

```sql
CREATE TABLE llm_calls (
  trace_id           String,
  span_id            String,
  run_id             String,
  provider           LowCardinality(String),
  model              LowCardinality(String),
  prompt_tokens      UInt32,
  completion_tokens  UInt32,
  total_tokens       UInt32,
  cost_usd           Decimal(10, 6),
  start_time         DateTime64(9),
  duration_ms        UInt32,
  status             LowCardinality(String),
  prompt_uri         String,
  completion_uri     String
) ENGINE = MergeTree
PARTITION BY toYYYYMMDD(start_time)
ORDER BY (run_id, start_time);
```

### `retrievals`

Retrieval events. The miner reads from here.

```sql
CREATE TABLE retrievals (
  trace_id         String,
  span_id          String,
  run_id           String,
  query            String,
  query_embedding  Array(Float32),
  retriever        LowCardinality(String),
  top_k            UInt32,
  results          Array(Tuple(passage_id String, score Float32, rank UInt32)),
  gold_passage_id  String DEFAULT '',
  recall_at_k      UInt8 DEFAULT 0,
  start_time       DateTime64(9),
  duration_ms      UInt32
) ENGINE = MergeTree
PARTITION BY toYYYYMMDD(start_time)
ORDER BY (run_id, start_time);
```

`retriever` values: `dense`, `sparse`, `hybrid`, `reranked`.

### `eval_events`

Per-example eval outcomes.

```sql
CREATE TABLE eval_events (
  eval_id      String,
  example_id   String,
  run_id       String,
  scorer       LowCardinality(String),
  score        Float64,
  passed       UInt8,
  details      String,
  timestamp    DateTime64(9)
) ENGINE = MergeTree
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (eval_id, example_id);
```

Eval events are written synchronously (not via the async OTel queue) because we cannot lose evaluation outcomes.

## Qdrant (vector store)

### Collections

Two collection roles, with an alias indirection for atomic swap on promotion.

**`corpus.base`** — vectors from the base embedding model.

- Vector size: 768 (Nomic Embed v1.5)
- Distance: Cosine
- HNSW: `m=16`, `ef_construct=200`
- Payload schema:
  - `doc_id: keyword`
  - `chunk_id: keyword`
  - `source: keyword` (e.g., `arxiv`, `bright`, `wikipedia`)
  - `section: keyword`
  - `text: text` (indexed for BM25 hybrid)
  - `published_at: datetime`

**`corpus.candidate.<job_id>`** — vectors from a candidate (fine-tuned) embedding model. Created during a fine-tune job; promoted to `corpus.base` via alias swap if the held-out lift is significant.

### Alias

```
corpus.active → corpus.base       (initial state)
corpus.active → corpus.candidate.<job_id>   (after promotion)
```

The retriever always reads from `corpus.active`. Promotion is a single Qdrant API call.

### Filter shape

Reads typically filter on `source` and optionally `published_at`. Indexed payload fields enable fast filtered ANN.

## Event schemas (NATS)

### `helix.tasks.dispatch`

Published by the orchestrator. Consumed by workers in the target pool.

```json
{
  "task_id": "uuid",
  "run_id": "uuid",
  "workflow_name": "deep_research",
  "workflow_version": 3,
  "node_id": "search",
  "input": {},
  "attempt_number": 1,
  "replay_token": null,
  "trace_id": "...",
  "deadline": "2026-05-20T18:30:00Z"
}
```

### `helix.events.completion`

Published by workers. Consumed by the orchestrator.

```json
{
  "task_id": "uuid",
  "attempt_number": 1,
  "worker_id": "uuid",
  "status": "succeeded",
  "output": {},
  "error": null,
  "span_id": "...",
  "duration_ms": 1842
}
```

### `helix.events.heartbeat`

Published by workers every 5 seconds.

```json
{
  "worker_id": "uuid",
  "pool": "research",
  "in_flight": 3,
  "capacity": 8,
  "timestamp": "..."
}
```

### `helix.events.checkpoint`

Published by workers when a task emits a checkpoint.

```json
{
  "task_id": "uuid",
  "attempt_number": 1,
  "checkpoint_size_bytes": 4096,
  "timestamp": "..."
}
```

## Dataset format

Every eval dataset is a JSONL file with one example per line:

```json
{
  "id": "hotpotqa_dev_001",
  "input": {"question": "..."},
  "expected_output": {"answer": "...", "supporting_facts": [{"doc": "...", "sent": 3}]},
  "metadata": {"hops": 2, "type": "comparison"}
}
```

Stored in MinIO at `s3://helix-datasets/<name>/<version>/examples.jsonl`. Registered in the `datasets` table with a SHA-256 `content_hash` for integrity. Re-uploading the same content under a new version is allowed but flagged in the registration log.

## Trace payload storage (MinIO)

LLM prompts, completions, and tool outputs over 32 KB are stored in MinIO under:

```
s3://helix-blobs/<yyyy>/<mm>/<dd>/<span_id>.bin
```

Spans reference these blobs via the `prompt_uri` and `completion_uri` columns in `llm_calls` (or analogous fields in the parent `spans` row's attributes map for tool calls).

Blobs are content-hashed; deduplication is opportunistic (identical prompts map to the same blob).

## Migration policy

- Migrations live in `migrations/` with timestamp-prefixed filenames.
- Up-only. We do not write down migrations. Reverting a schema change requires writing a forward migration that undoes it.
- ClickHouse migrations use `ON CLUSTER` clauses gated by a Helm value (single-node dev does not need them).
- Every breaking schema change bumps the workflow spec version that depends on it.
