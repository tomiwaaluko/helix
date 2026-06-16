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

// EmbeddingJob represents one embedding fine-tune job linked to a finetune_job.
type EmbeddingJob struct {
	ID            string          `json:"id"`
	FinetuneJobID *string         `json:"finetune_job_id,omitempty"`
	BaseModel     string          `json:"base_model"`
	Status        string          `json:"status"`
	Config        json.RawMessage `json:"config,omitempty"`
	TripletsCount *int            `json:"triplets_count,omitempty"`
	Metrics       json.RawMessage `json:"metrics,omitempty"`
	ArtifactURI   *string         `json:"artifact_uri,omitempty"`
	CreatedAt     time.Time       `json:"created_at"`
	PromotedAt    *time.Time      `json:"promoted_at,omitempty"`
}

// EmbeddingJobStore manages embedding_jobs in Postgres.
type EmbeddingJobStore interface {
	// CreateEmbeddingJob inserts a new row with status=queued.
	CreateEmbeddingJob(ctx context.Context, baseModel string, finetuneJobID string) (EmbeddingJob, error)

	// GetEmbeddingJob returns a single embedding job by ID.
	GetEmbeddingJob(ctx context.Context, jobID string) (EmbeddingJob, error)

	// ListEmbeddingJobs returns the 50 most recent embedding jobs.
	ListEmbeddingJobs(ctx context.Context) ([]EmbeddingJob, error)

	// UpdateEmbeddingJobOutcome updates an embedding job when its finetune job completes.
	// Looks up the row via finetune_job_id. Best-effort: no-op when no matching row exists.
	// configJSON is the TrainConfig as JSON (may be nil when no training ran).
	// artifactURI is the s3:// URI of the uploaded checkpoint ("" leaves it NULL).
	UpdateEmbeddingJobOutcome(ctx context.Context, finetuneJobID, status string, triplets int, metricsJSON, configJSON []byte, artifactURI string, promoted bool) error

	// UpdateEmbeddingJobPhase sets an intermediate status on the embedding job linked to taskID.
	// Looks up via tasks→runs→embedding_jobs join.
	// Phase is one of "mining", "training", "evaluating".
	// No-op when no embedding_jobs row is linked to taskID's finetune_job workflow.
	UpdateEmbeddingJobPhase(ctx context.Context, taskID, phase string) error
}

// PostgresEmbeddingJobStore implements EmbeddingJobStore against Postgres.
type PostgresEmbeddingJobStore struct {
	pool *pgxpool.Pool
}

// NewPostgresEmbeddingJobStore creates a new store backed by the given connection URL.
func NewPostgresEmbeddingJobStore(ctx context.Context, databaseURL string) (*PostgresEmbeddingJobStore, error) {
	pool, err := pgxpool.New(ctx, databaseURL)
	if err != nil {
		return nil, fmt.Errorf("embedding store: pgxpool: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("embedding store: ping: %w", err)
	}
	return &PostgresEmbeddingJobStore{pool: pool}, nil
}

// Close releases the connection pool.
func (s *PostgresEmbeddingJobStore) Close() {
	s.pool.Close()
}

func (s *PostgresEmbeddingJobStore) CreateEmbeddingJob(ctx context.Context, baseModel string, finetuneJobID string) (EmbeddingJob, error) {
	var j EmbeddingJob
	var fjID *string
	var config json.RawMessage
	var tripletsCount *int
	var metrics json.RawMessage
	var artifactURI *string
	var promotedAt *time.Time

	err := s.pool.QueryRow(ctx,
		`INSERT INTO embedding_jobs (base_model, finetune_job_id, config, status)
		 VALUES ($1, $2, '{}', 'queued')
		 RETURNING id, finetune_job_id, base_model, status, config, triplets_count, metrics, artifact_uri, created_at, promoted_at`,
		baseModel, finetuneJobID,
	).Scan(
		&j.ID, &fjID, &j.BaseModel, &j.Status, &config,
		&tripletsCount, &metrics, &artifactURI, &j.CreatedAt, &promotedAt,
	)
	if err != nil {
		return EmbeddingJob{}, fmt.Errorf("embedding store: create job: %w", err)
	}
	j.FinetuneJobID = fjID
	if len(config) > 0 {
		j.Config = config
	}
	j.TripletsCount = tripletsCount
	if len(metrics) > 0 {
		j.Metrics = metrics
	}
	j.ArtifactURI = artifactURI
	j.PromotedAt = promotedAt
	return j, nil
}

func (s *PostgresEmbeddingJobStore) GetEmbeddingJob(ctx context.Context, jobID string) (EmbeddingJob, error) {
	var j EmbeddingJob
	var fjID *string
	var config json.RawMessage
	var tripletsCount *int
	var metrics json.RawMessage
	var artifactURI *string
	var promotedAt *time.Time

	err := s.pool.QueryRow(ctx,
		`SELECT id, finetune_job_id, base_model, status, config, triplets_count, metrics, artifact_uri, created_at, promoted_at
		 FROM embedding_jobs WHERE id = $1`,
		jobID,
	).Scan(
		&j.ID, &fjID, &j.BaseModel, &j.Status, &config,
		&tripletsCount, &metrics, &artifactURI, &j.CreatedAt, &promotedAt,
	)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return EmbeddingJob{}, fmt.Errorf("embedding store: job %q not found", jobID)
		}
		return EmbeddingJob{}, fmt.Errorf("embedding store: get job: %w", err)
	}
	j.FinetuneJobID = fjID
	if len(config) > 0 {
		j.Config = config
	}
	j.TripletsCount = tripletsCount
	if len(metrics) > 0 {
		j.Metrics = metrics
	}
	j.ArtifactURI = artifactURI
	j.PromotedAt = promotedAt
	return j, nil
}

func (s *PostgresEmbeddingJobStore) ListEmbeddingJobs(ctx context.Context) ([]EmbeddingJob, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT id, finetune_job_id, base_model, status, config, triplets_count, metrics, artifact_uri, created_at, promoted_at
		 FROM embedding_jobs
		 ORDER BY created_at DESC LIMIT 50`,
	)
	if err != nil {
		return nil, fmt.Errorf("embedding store: list jobs: %w", err)
	}
	defer rows.Close()

	var jobs []EmbeddingJob
	for rows.Next() {
		var j EmbeddingJob
		var fjID *string
		var config json.RawMessage
		var tripletsCount *int
		var metrics json.RawMessage
		var artifactURI *string
		var promotedAt *time.Time

		if err := rows.Scan(
			&j.ID, &fjID, &j.BaseModel, &j.Status, &config,
			&tripletsCount, &metrics, &artifactURI, &j.CreatedAt, &promotedAt,
		); err != nil {
			return nil, fmt.Errorf("embedding store: scan job: %w", err)
		}
		j.FinetuneJobID = fjID
		if len(config) > 0 {
			j.Config = config
		}
		j.TripletsCount = tripletsCount
		if len(metrics) > 0 {
			j.Metrics = metrics
		}
		j.ArtifactURI = artifactURI
		j.PromotedAt = promotedAt
		jobs = append(jobs, j)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("embedding store: iterate jobs: %w", err)
	}
	return jobs, nil
}

func (s *PostgresEmbeddingJobStore) UpdateEmbeddingJobOutcome(
	ctx context.Context,
	finetuneJobID string,
	status string,
	triplets int,
	metricsJSON []byte,
	configJSON []byte,
	artifactURI string,
	promoted bool,
) error {
	promotedExpr := "NULL"
	if promoted {
		promotedExpr = "now()"
	}

	cfg := configJSON
	if len(cfg) == 0 {
		cfg = []byte("{}")
	}

	// NULL artifact_uri when empty; COALESCE keeps any previously-set value.
	var artifact *string
	if artifactURI != "" {
		artifact = &artifactURI
	}

	query := fmt.Sprintf(`
		UPDATE embedding_jobs
		SET    status         = $2,
		       triplets_count = $3,
		       metrics        = $4,
		       config         = $5,
		       artifact_uri   = COALESCE($6, artifact_uri),
		       promoted_at    = %s
		WHERE  finetune_job_id = $1`, promotedExpr)

	_, err := s.pool.Exec(ctx, query,
		finetuneJobID,
		status,
		triplets,
		metricsJSON,
		cfg,
		artifact,
	)
	if err != nil {
		return fmt.Errorf("embedding store: update outcome: %w", err)
	}
	return nil
}

func (s *PostgresEmbeddingJobStore) UpdateEmbeddingJobPhase(ctx context.Context, taskID, phase string) error {
	_, err := s.pool.Exec(ctx, `
		UPDATE embedding_jobs ej
		SET    status     = $2,
		       updated_at = now()
		FROM   tasks   t
		JOIN   runs    r  ON r.id   = t.run_id
		JOIN   workflows w ON w.id  = r.workflow_id
		WHERE  t.id   = $1
		  AND  w.name = 'finetune_job'
		  AND  ej.run_id = r.id`,
		taskID, phase,
	)
	if err != nil {
		return fmt.Errorf("embedding store: update phase: %w", err)
	}
	return nil
}
