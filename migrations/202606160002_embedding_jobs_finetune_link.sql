-- M10: link embedding_jobs to finetune_jobs. Up-only. Do not edit after merging to main.
-- Adds the finetune_job_id FK so embedding jobs can be looked up by their originating finetune job.

ALTER TABLE embedding_jobs
  ADD COLUMN IF NOT EXISTS finetune_job_id UUID REFERENCES finetune_jobs(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS embedding_jobs_finetune_job_idx ON embedding_jobs (finetune_job_id);
