package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math/rand"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// postgresStore implements Store against a Postgres database via pgxpool.
type postgresStore struct {
	pool *pgxpool.Pool
}

// NewPostgresStore creates a new Postgres-backed Store using the given connection URL.
func NewPostgresStore(ctx context.Context, databaseURL string) (Store, error) {
	pool, err := pgxpool.New(ctx, databaseURL)
	if err != nil {
		return nil, fmt.Errorf("store: failed to create pgxpool: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("store: failed to ping postgres: %w", err)
	}
	return &postgresStore{pool: pool}, nil
}

// EnsureWorkflow upserts a workflow by name (version=1) and returns its UUID.
func (s *postgresStore) EnsureWorkflow(ctx context.Context, name string) (string, error) {
	spec := []byte(`{"nodes":[]}`)
	_, err := s.pool.Exec(ctx,
		`INSERT INTO workflows (name, version, spec, registered_by)
		 VALUES ($1, 1, $2::jsonb, 'orchestrator')
		 ON CONFLICT (name, version) DO NOTHING`,
		name, spec,
	)
	if err != nil {
		return "", fmt.Errorf("store: upsert workflow %q: %w", name, err)
	}

	var id string
	err = s.pool.QueryRow(ctx,
		`SELECT id FROM workflows WHERE name = $1 AND version = 1`,
		name,
	).Scan(&id)
	if err != nil {
		return "", fmt.Errorf("store: query workflow id for %q: %w", name, err)
	}
	return id, nil
}

// CreateRun creates a new run + root task and immediately marks the run as running.
func (s *postgresStore) CreateRun(ctx context.Context, in CreateRunInput) (Run, Task, error) {
	workflowID, err := s.EnsureWorkflow(ctx, in.WorkflowName)
	if err != nil {
		return Run{}, Task{}, err
	}

	traceID := in.TraceID
	if traceID == "" {
		traceID = fmt.Sprintf("%x-%x", rand.Int63(), rand.Int63()) //nolint:gosec // non-crypto uniqueness
	}

	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return Run{}, Task{}, fmt.Errorf("store: begin CreateRun tx: %w", err)
	}
	defer func() {
		if err != nil {
			_ = tx.Rollback(ctx)
		}
	}()

	var run Run
	run.WorkflowID = workflowID
	run.TraceID = traceID
	run.Input = in.Input
	run.SubmittedBy = in.SubmittedBy

	err = tx.QueryRow(ctx,
		`INSERT INTO runs (workflow_id, input, submitted_by, trace_id)
		 VALUES ($1, $2::jsonb, $3, $4)
		 RETURNING id, status, submitted_at`,
		workflowID, in.Input, in.SubmittedBy, traceID,
	).Scan(&run.ID, &run.Status, &run.SubmittedAt)
	if err != nil {
		return Run{}, Task{}, fmt.Errorf("store: insert run: %w", err)
	}

	var task Task
	task.RunID = run.ID
	task.NodeID = "root"
	task.Input = in.Input

	err = tx.QueryRow(ctx,
		`INSERT INTO tasks (run_id, node_id, input, status)
		 VALUES ($1, 'root', $2::jsonb, 'ready')
		 RETURNING id, status`,
		run.ID, in.Input,
	).Scan(&task.ID, &task.Status)
	if err != nil {
		return Run{}, Task{}, fmt.Errorf("store: insert root task: %w", err)
	}

	_, err = tx.Exec(ctx,
		`UPDATE runs SET status = 'running', started_at = now() WHERE id = $1`,
		run.ID,
	)
	if err != nil {
		return Run{}, Task{}, fmt.Errorf("store: set run running: %w", err)
	}
	run.Status = RunStatusRunning

	if err = tx.Commit(ctx); err != nil {
		return Run{}, Task{}, fmt.Errorf("store: commit CreateRun tx: %w", err)
	}

	run.TaskIDs = []string{task.ID}
	return run, task, nil
}

// GetRun retrieves a run by ID and populates its TaskIDs.
func (s *postgresStore) GetRun(ctx context.Context, runID string) (Run, error) {
	var run Run
	var outputRaw, errorRaw []byte

	err := s.pool.QueryRow(ctx,
		`SELECT id, workflow_id, status, input, output, error, submitted_at, submitted_by, trace_id
		 FROM runs WHERE id = $1`,
		runID,
	).Scan(
		&run.ID, &run.WorkflowID, &run.Status,
		&run.Input, &outputRaw, &errorRaw,
		&run.SubmittedAt, &run.SubmittedBy, &run.TraceID,
	)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return Run{}, fmt.Errorf("store: run %q not found", runID)
		}
		return Run{}, fmt.Errorf("store: get run %q: %w", runID, err)
	}
	run.Output = outputRaw
	run.Error = errorRaw

	rows, err := s.pool.Query(ctx,
		`SELECT id FROM tasks WHERE run_id = $1 ORDER BY id`,
		runID,
	)
	if err != nil {
		return Run{}, fmt.Errorf("store: list task ids for run %q: %w", runID, err)
	}
	defer rows.Close()

	for rows.Next() {
		var tid string
		if err := rows.Scan(&tid); err != nil {
			return Run{}, fmt.Errorf("store: scan task id: %w", err)
		}
		run.TaskIDs = append(run.TaskIDs, tid)
	}
	if err := rows.Err(); err != nil {
		return Run{}, fmt.Errorf("store: iterate task ids: %w", err)
	}

	return run, nil
}

// ListRuns returns up to 100 runs, optionally filtered by status.
func (s *postgresStore) ListRuns(ctx context.Context, status string) ([]Run, error) {
	var rows pgx.Rows
	var err error

	if status != "" {
		rows, err = s.pool.Query(ctx,
			`SELECT id, workflow_id, status, input, output, error, submitted_at, submitted_by, trace_id
			 FROM runs WHERE status = $1 ORDER BY submitted_at DESC LIMIT 100`,
			status,
		)
	} else {
		rows, err = s.pool.Query(ctx,
			`SELECT id, workflow_id, status, input, output, error, submitted_at, submitted_by, trace_id
			 FROM runs ORDER BY submitted_at DESC LIMIT 100`,
		)
	}
	if err != nil {
		return nil, fmt.Errorf("store: list runs: %w", err)
	}
	defer rows.Close()

	var runs []Run
	for rows.Next() {
		var run Run
		var outputRaw, errorRaw []byte
		if err := rows.Scan(
			&run.ID, &run.WorkflowID, &run.Status,
			&run.Input, &outputRaw, &errorRaw,
			&run.SubmittedAt, &run.SubmittedBy, &run.TraceID,
		); err != nil {
			return nil, fmt.Errorf("store: scan run row: %w", err)
		}
		run.Output = outputRaw
		run.Error = errorRaw
		runs = append(runs, run)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("store: iterate runs: %w", err)
	}
	return runs, nil
}

// CancelRun cancels a run and all its tasks.
func (s *postgresStore) CancelRun(ctx context.Context, runID string) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("store: begin CancelRun tx: %w", err)
	}
	defer func() {
		if err != nil {
			_ = tx.Rollback(ctx)
		}
	}()

	_, err = tx.Exec(ctx,
		`UPDATE runs SET status = 'cancelled' WHERE id = $1`,
		runID,
	)
	if err != nil {
		return fmt.Errorf("store: cancel run %q: %w", runID, err)
	}

	_, err = tx.Exec(ctx,
		`UPDATE tasks SET status = 'cancelled' WHERE run_id = $1`,
		runID,
	)
	if err != nil {
		return fmt.Errorf("store: cancel tasks for run %q: %w", runID, err)
	}

	if err = tx.Commit(ctx); err != nil {
		return fmt.Errorf("store: commit CancelRun tx: %w", err)
	}
	return nil
}

// RegisterWorker inserts a new worker record and returns Worker with a 30-second lease.
func (s *postgresStore) RegisterWorker(ctx context.Context, pool string, capabilities []string, maxConcurrency int) (Worker, error) {
	workerID := fmt.Sprintf("%x%x", rand.Int63(), rand.Int63()) //nolint:gosec // non-crypto uniqueness

	capsJSON, err := json.Marshal(capabilities)
	if err != nil {
		return Worker{}, fmt.Errorf("store: marshal capabilities: %w", err)
	}

	_, err = s.pool.Exec(ctx,
		`INSERT INTO workers (id, pool, capabilities, status)
		 VALUES ($1, $2, $3::jsonb, 'active')`,
		workerID, pool, capsJSON,
	)
	if err != nil {
		return Worker{}, fmt.Errorf("store: register worker: %w", err)
	}

	return Worker{
		ID:           workerID,
		Pool:         pool,
		LeaseSeconds: 30,
	}, nil
}

// UpdateHeartbeat refreshes the last_heartbeat timestamp for a worker.
func (s *postgresStore) UpdateHeartbeat(ctx context.Context, workerID string) error {
	_, err := s.pool.Exec(ctx,
		`UPDATE workers SET last_heartbeat = now() WHERE id = $1`,
		workerID,
	)
	if err != nil {
		return fmt.Errorf("store: update heartbeat for worker %q: %w", workerID, err)
	}
	return nil
}

// IsWorkerDraining returns true if the worker's status is "draining".
func (s *postgresStore) IsWorkerDraining(ctx context.Context, workerID string) (bool, error) {
	var status string
	err := s.pool.QueryRow(ctx,
		`SELECT status FROM workers WHERE id = $1`,
		workerID,
	).Scan(&status)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return false, fmt.Errorf("store: worker %q not found", workerID)
		}
		return false, fmt.Errorf("store: check draining for worker %q: %w", workerID, err)
	}
	return status == "draining", nil
}

// CompleteTask records a task attempt result, advances task/run status, and returns a disposition.
func (s *postgresStore) CompleteTask(ctx context.Context, result TaskResult) (string, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return "", fmt.Errorf("store: begin CompleteTask tx: %w", err)
	}
	defer func() {
		if err != nil {
			_ = tx.Rollback(ctx)
		}
	}()

	// Lock task row and check for duplicate completion.
	var currentStatus TaskStatus
	err = tx.QueryRow(ctx,
		`SELECT status FROM tasks WHERE id = $1 FOR UPDATE`,
		result.TaskID,
	).Scan(&currentStatus)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return "", fmt.Errorf("store: task %q not found", result.TaskID)
		}
		return "", fmt.Errorf("store: lock task %q: %w", result.TaskID, err)
	}

	if currentStatus == TaskStatusSucceeded || currentStatus == TaskStatusFailed {
		if err = tx.Rollback(ctx); err != nil {
			return "", fmt.Errorf("store: rollback superseded: %w", err)
		}
		return "superseded", nil
	}

	finishedAt := time.Now()
	var errorJSON []byte
	if result.Error != "" {
		errorJSON, err = json.Marshal(map[string]string{"message": result.Error})
		if err != nil {
			return "", fmt.Errorf("store: marshal task error: %w", err)
		}
	}

	_, err = tx.Exec(ctx,
		`INSERT INTO task_attempts (task_id, attempt_number, worker_id, status, finished_at, error, span_id)
		 VALUES ($1, $2, $3, $4::task_status, $5, $6::jsonb, $7)
		 ON CONFLICT (task_id, attempt_number) DO NOTHING`,
		result.TaskID, result.AttemptNumber, result.WorkerID, result.Status,
		finishedAt, errorJSON, result.SpanID,
	)
	if err != nil {
		return "", fmt.Errorf("store: insert task_attempt: %w", err)
	}

	_, err = tx.Exec(ctx,
		`UPDATE tasks SET status = $1::task_status, output = $2::jsonb, attempts = attempts + 1 WHERE id = $3`,
		result.Status, result.OutputJSON, result.TaskID,
	)
	if err != nil {
		return "", fmt.Errorf("store: update task status: %w", err)
	}

	// Get the run_id for the task so we can update the run.
	var runID string
	err = tx.QueryRow(ctx,
		`SELECT run_id FROM tasks WHERE id = $1`,
		result.TaskID,
	).Scan(&runID)
	if err != nil {
		return "", fmt.Errorf("store: get run_id for task %q: %w", result.TaskID, err)
	}

	switch result.Status {
	case "succeeded":
		_, err = tx.Exec(ctx,
			`UPDATE runs SET status = 'succeeded', output = $1::jsonb, finished_at = now() WHERE id = $2`,
			result.OutputJSON, runID,
		)
		if err != nil {
			return "", fmt.Errorf("store: mark run succeeded: %w", err)
		}
	case "failed":
		failErr := []byte(`{"message":"task failed"}`)
		_, err = tx.Exec(ctx,
			`UPDATE runs SET status = 'failed', error = $1::jsonb, finished_at = now() WHERE id = $2`,
			failErr, runID,
		)
		if err != nil {
			return "", fmt.Errorf("store: mark run failed: %w", err)
		}
	}

	if err = tx.Commit(ctx); err != nil {
		return "", fmt.Errorf("store: commit CompleteTask tx: %w", err)
	}
	return "accepted", nil
}

// UpsertCheckpoint stores or replaces a task's checkpoint state.
func (s *postgresStore) UpsertCheckpoint(ctx context.Context, taskID string, attemptNumber int, state []byte) error {
	_, err := s.pool.Exec(ctx,
		`INSERT INTO checkpoints (task_id, state, updated_at)
		 VALUES ($1, $2, now())
		 ON CONFLICT (task_id) DO UPDATE SET state = EXCLUDED.state, updated_at = now()`,
		taskID, state,
	)
	if err != nil {
		return fmt.Errorf("store: upsert checkpoint for task %q attempt %d: %w", taskID, attemptNumber, err)
	}
	return nil
}

// Close releases the underlying connection pool.
func (s *postgresStore) Close() {
	s.pool.Close()
}
