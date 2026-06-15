package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// FinetuneJob represents one production mine→train→promote run.
type FinetuneJob struct {
	ID          string     `json:"id"`
	RunID       string     `json:"run_id,omitempty"`
	Status      string     `json:"status"`
	TrainSplit  string     `json:"train_split"`
	EvalSplit   string     `json:"eval_split"`
	CorpusAlias string     `json:"corpus_alias"`
	Failures    *int       `json:"failures,omitempty"`
	Triplets    *int       `json:"triplets,omitempty"`
	BeforeRecall *float64  `json:"before_recall,omitempty"`
	AfterRecall  *float64  `json:"after_recall,omitempty"`
	Outcome     string     `json:"outcome,omitempty"`
	Error       string     `json:"error,omitempty"`
	CreatedAt   time.Time  `json:"created_at"`
	UpdatedAt   time.Time  `json:"updated_at"`
}

// FinetuneJobInput holds the parameters for creating a new finetune job.
type FinetuneJobInput struct {
	TrainSplit  string
	EvalSplit   string
	CorpusAlias string
}

// FinetuneTaskOutput is the shape of the CompleteTask output_json for finetune_job tasks.
type FinetuneTaskOutput struct {
	JobID        string  `json:"job_id"`
	Outcome      string  `json:"outcome"`       // "promoted" | "archived" | "no_failures" | "no_triplets"
	BeforeRecall float64 `json:"before_recall"`
	AfterRecall  float64 `json:"after_recall"`
	Failures     int     `json:"failures"`
	Triplets     int     `json:"triplets"`
	Error        string  `json:"error,omitempty"`
}

// FinetuneJobStore manages finetune_jobs in Postgres.
type FinetuneJobStore interface {
	// CreateFinetuneJob inserts a new row and returns it with ID populated.
	CreateFinetuneJob(ctx context.Context, in FinetuneJobInput) (FinetuneJob, error)

	// SetFinetuneJobRun links a finetune job to its dispatched run.
	SetFinetuneJobRun(ctx context.Context, jobID, runID string) error

	// GetFinetuneJob returns a single finetune job by ID.
	GetFinetuneJob(ctx context.Context, jobID string) (FinetuneJob, error)

	// ListFinetuneJobs returns the 50 most recent finetune jobs.
	ListFinetuneJobs(ctx context.Context) ([]FinetuneJob, error)

	// FinalizeFinetuneJob updates a finetune job when its task completes.
	// It looks up the job via the task_id → run_id → finetune_jobs.run_id chain.
	// No-op when no matching job exists (safe to call for non-finetune tasks).
	FinalizeFinetuneJob(ctx context.Context, taskID string, outputJSON []byte, taskStatus string) error
}

// PostgresFinetuneJobStore implements FinetuneJobStore against Postgres.
type PostgresFinetuneJobStore struct {
	pool *pgxpool.Pool
}

// NewPostgresFinetuneJobStore creates a new store backed by the given connection URL.
func NewPostgresFinetuneJobStore(ctx context.Context, databaseURL string) (*PostgresFinetuneJobStore, error) {
	pool, err := pgxpool.New(ctx, databaseURL)
	if err != nil {
		return nil, fmt.Errorf("finetune store: pgxpool: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("finetune store: ping: %w", err)
	}
	return &PostgresFinetuneJobStore{pool: pool}, nil
}

// Close releases the connection pool.
func (s *PostgresFinetuneJobStore) Close() {
	s.pool.Close()
}

func (s *PostgresFinetuneJobStore) CreateFinetuneJob(ctx context.Context, in FinetuneJobInput) (FinetuneJob, error) {
	alias := in.CorpusAlias
	if alias == "" {
		alias = "corpus.active"
	}

	var j FinetuneJob
	err := s.pool.QueryRow(ctx,
		`INSERT INTO finetune_jobs (train_split, eval_split, corpus_alias)
		 VALUES ($1, $2, $3)
		 RETURNING id, status, train_split, eval_split, corpus_alias, created_at, updated_at`,
		in.TrainSplit, in.EvalSplit, alias,
	).Scan(&j.ID, &j.Status, &j.TrainSplit, &j.EvalSplit, &j.CorpusAlias, &j.CreatedAt, &j.UpdatedAt)
	if err != nil {
		return FinetuneJob{}, fmt.Errorf("finetune store: create job: %w", err)
	}
	return j, nil
}

func (s *PostgresFinetuneJobStore) SetFinetuneJobRun(ctx context.Context, jobID, runID string) error {
	_, err := s.pool.Exec(ctx,
		`UPDATE finetune_jobs SET run_id = $1, updated_at = now() WHERE id = $2`,
		runID, jobID,
	)
	if err != nil {
		return fmt.Errorf("finetune store: set run: %w", err)
	}
	return nil
}

func (s *PostgresFinetuneJobStore) GetFinetuneJob(ctx context.Context, jobID string) (FinetuneJob, error) {
	var j FinetuneJob
	var runID, outcome, errStr *string
	var failures, triplets *int
	var beforeRecall, afterRecall *float64

	err := s.pool.QueryRow(ctx,
		`SELECT id, run_id, status, train_split, eval_split, corpus_alias,
		        failures, triplets, before_recall, after_recall, outcome, error, created_at, updated_at
		 FROM finetune_jobs WHERE id = $1`,
		jobID,
	).Scan(
		&j.ID, &runID, &j.Status, &j.TrainSplit, &j.EvalSplit, &j.CorpusAlias,
		&failures, &triplets, &beforeRecall, &afterRecall,
		&outcome, &errStr, &j.CreatedAt, &j.UpdatedAt,
	)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return FinetuneJob{}, fmt.Errorf("finetune store: job %q not found", jobID)
		}
		return FinetuneJob{}, fmt.Errorf("finetune store: get job: %w", err)
	}
	if runID != nil {
		j.RunID = *runID
	}
	j.Failures = failures
	j.Triplets = triplets
	j.BeforeRecall = beforeRecall
	j.AfterRecall = afterRecall
	if outcome != nil {
		j.Outcome = *outcome
	}
	if errStr != nil {
		j.Error = *errStr
	}
	return j, nil
}

func (s *PostgresFinetuneJobStore) ListFinetuneJobs(ctx context.Context) ([]FinetuneJob, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT id, run_id, status, train_split, eval_split, corpus_alias,
		        failures, triplets, before_recall, after_recall, outcome, error, created_at, updated_at
		 FROM finetune_jobs
		 ORDER BY created_at DESC LIMIT 50`,
	)
	if err != nil {
		return nil, fmt.Errorf("finetune store: list jobs: %w", err)
	}
	defer rows.Close()

	var jobs []FinetuneJob
	for rows.Next() {
		var j FinetuneJob
		var runID, outcome, errStr *string
		var failures, triplets *int
		var beforeRecall, afterRecall *float64
		if err := rows.Scan(
			&j.ID, &runID, &j.Status, &j.TrainSplit, &j.EvalSplit, &j.CorpusAlias,
			&failures, &triplets, &beforeRecall, &afterRecall,
			&outcome, &errStr, &j.CreatedAt, &j.UpdatedAt,
		); err != nil {
			return nil, fmt.Errorf("finetune store: scan job: %w", err)
		}
		if runID != nil {
			j.RunID = *runID
		}
		j.Failures = failures
		j.Triplets = triplets
		j.BeforeRecall = beforeRecall
		j.AfterRecall = afterRecall
		if outcome != nil {
			j.Outcome = *outcome
		}
		if errStr != nil {
			j.Error = *errStr
		}
		jobs = append(jobs, j)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("finetune store: iterate jobs: %w", err)
	}
	return jobs, nil
}

func (s *PostgresFinetuneJobStore) FinalizeFinetuneJob(
	ctx context.Context,
	taskID string,
	outputJSON []byte,
	taskStatus string,
) error {
	if taskStatus == "failed" {
		// Mark failed; no metrics to extract.
		_, err := s.pool.Exec(ctx, `
			UPDATE finetune_jobs fj
			SET    status     = 'failed',
			       error      = 'task failed',
			       updated_at = now()
			FROM   tasks   t
			JOIN   runs    r  ON r.id   = t.run_id
			JOIN   workflows w ON w.id  = r.workflow_id
			WHERE  t.id   = $1
			  AND  w.name = 'finetune_job'
			  AND  fj.run_id = r.id`,
			taskID,
		)
		if err != nil {
			return fmt.Errorf("finetune store: finalize failed job: %w", err)
		}
		return nil
	}

	if len(outputJSON) == 0 {
		return nil
	}

	var out FinetuneTaskOutput
	if err := json.Unmarshal(outputJSON, &out); err != nil {
		return fmt.Errorf("finetune store: unmarshal task output: %w", err)
	}
	if out.JobID == "" {
		// Not a finetune task output.
		return nil
	}

	finalStatus := out.Outcome
	if finalStatus == "" {
		finalStatus = "done"
	}

	_, err := s.pool.Exec(ctx, `
		UPDATE finetune_jobs fj
		SET    status       = $2,
		       failures     = $3,
		       triplets     = $4,
		       before_recall = $5,
		       after_recall  = $6,
		       outcome      = $7,
		       error        = $8,
		       updated_at   = now()
		FROM   tasks   t
		JOIN   runs    r  ON r.id   = t.run_id
		JOIN   workflows w ON w.id  = r.workflow_id
		WHERE  t.id   = $1
		  AND  w.name = 'finetune_job'
		  AND  fj.run_id = r.id`,
		taskID,
		finalStatus,
		out.Failures,
		out.Triplets,
		out.BeforeRecall,
		out.AfterRecall,
		out.Outcome,
		out.Error,
	)
	if err != nil {
		return fmt.Errorf("finetune store: finalize job: %w", err)
	}
	return nil
}
