-- M1 initial schema. Up-only. Do not edit after merging to main.
-- Covers all 10 tables from docs/data-model.md.

CREATE TYPE run_status AS ENUM ('pending','running','succeeded','failed','cancelled');
CREATE TYPE task_status AS ENUM ('pending','ready','running','succeeded','failed','cancelled','dead');
CREATE TYPE embedding_job_status AS ENUM
  ('queued','mining','training','evaluating','promoted','archived','failed');

-- ── workflows ─────────────────────────────────────────────────────────────────

CREATE TABLE workflows (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name          TEXT NOT NULL,
  version       INT  NOT NULL,
  spec          JSONB NOT NULL,
  registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  registered_by TEXT NOT NULL,
  UNIQUE (name, version)
);
CREATE INDEX workflows_name_idx ON workflows (name);

-- ── runs ──────────────────────────────────────────────────────────────────────

CREATE TABLE runs (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workflow_id  UUID NOT NULL REFERENCES workflows(id),
  status       run_status NOT NULL DEFAULT 'pending',
  input        JSONB NOT NULL,
  output       JSONB,
  error        JSONB,
  submitted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at   TIMESTAMPTZ,
  finished_at  TIMESTAMPTZ,
  submitted_by TEXT NOT NULL,
  metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
  trace_id     TEXT NOT NULL
);
CREATE INDEX runs_workflow_idx ON runs (workflow_id, submitted_at DESC);
CREATE INDEX runs_status_idx   ON runs (status) WHERE status IN ('pending','running');

-- ── tasks ─────────────────────────────────────────────────────────────────────

CREATE TABLE tasks (
  id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id   UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  node_id  TEXT NOT NULL,
  status   task_status NOT NULL DEFAULT 'pending',
  input    JSONB NOT NULL,
  output   JSONB,
  attempts INT  NOT NULL DEFAULT 0,
  UNIQUE (run_id, node_id)
);
CREATE INDEX tasks_run_idx    ON tasks (run_id);
CREATE INDEX tasks_status_idx ON tasks (status) WHERE status IN ('ready','running');

-- ── task_attempts ─────────────────────────────────────────────────────────────

CREATE TABLE task_attempts (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id        UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  attempt_number INT  NOT NULL,
  worker_id      UUID NOT NULL,
  status         task_status NOT NULL,
  started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at    TIMESTAMPTZ,
  error          JSONB,
  span_id        TEXT,
  UNIQUE (task_id, attempt_number)
);
CREATE INDEX task_attempts_task_idx ON task_attempts (task_id);

-- ── checkpoints ───────────────────────────────────────────────────────────────

CREATE TABLE checkpoints (
  task_id    UUID PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
  state      BYTEA NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── dead_letter_queue ─────────────────────────────────────────────────────────

CREATE TABLE dead_letter_queue (
  task_id     UUID PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
  reason      TEXT NOT NULL,
  context     JSONB NOT NULL,
  parked_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  replayed_at TIMESTAMPTZ
);
CREATE INDEX dlq_parked_idx ON dead_letter_queue (parked_at DESC);

-- ── workers ───────────────────────────────────────────────────────────────────

CREATE TABLE workers (
  id             UUID PRIMARY KEY,
  pool           TEXT NOT NULL,
  capabilities   JSONB NOT NULL,
  registered_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_heartbeat TIMESTAMPTZ NOT NULL DEFAULT now(),
  status         TEXT NOT NULL
);
CREATE INDEX workers_pool_idx ON workers (pool, status);

-- ── datasets ──────────────────────────────────────────────────────────────────

CREATE TABLE datasets (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name          TEXT NOT NULL,
  version       INT  NOT NULL,
  description   TEXT,
  examples_uri  TEXT NOT NULL,
  content_hash  TEXT NOT NULL,
  size          INT  NOT NULL,
  registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (name, version)
);

-- ── evals ─────────────────────────────────────────────────────────────────────

CREATE TABLE evals (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workflow_id UUID NOT NULL REFERENCES workflows(id),
  dataset_id  UUID NOT NULL REFERENCES datasets(id),
  scorer      TEXT NOT NULL,
  status      run_status NOT NULL DEFAULT 'pending',
  results     JSONB,
  started_at  TIMESTAMPTZ,
  finished_at TIMESTAMPTZ
);
CREATE INDEX evals_workflow_idx ON evals (workflow_id);

-- ── embedding_jobs ────────────────────────────────────────────────────────────

CREATE TABLE embedding_jobs (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  base_model    TEXT NOT NULL,
  status        embedding_job_status NOT NULL DEFAULT 'queued',
  config        JSONB NOT NULL,
  triplets_count INT,
  metrics       JSONB,
  artifact_uri  TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  promoted_at   TIMESTAMPTZ
);

-- ── failure_cases ─────────────────────────────────────────────────────────────

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
