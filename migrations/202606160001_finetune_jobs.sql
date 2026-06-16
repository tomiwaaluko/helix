-- M9b: finetune jobs table. Up-only. Do not edit after merging to main.
-- Tracks the mine → train → promote pipeline dispatched via NATS.

CREATE TABLE finetune_jobs (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id        UUID REFERENCES runs(id) ON DELETE SET NULL,
  status        TEXT NOT NULL DEFAULT 'pending',  -- pending|mining|training|promoting|done|failed
  train_split   TEXT NOT NULL,
  eval_split    TEXT NOT NULL,
  corpus_alias  TEXT NOT NULL DEFAULT 'corpus.active',
  failures      INTEGER,
  triplets      INTEGER,
  before_recall DOUBLE PRECISION,
  after_recall  DOUBLE PRECISION,
  outcome       TEXT,     -- promoted|archived|null
  error         TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX finetune_jobs_status_idx ON finetune_jobs (status, created_at DESC);
CREATE INDEX finetune_jobs_run_idx    ON finetune_jobs (run_id);
