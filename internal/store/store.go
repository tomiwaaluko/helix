// Package store defines the persistence interface for orchestrator state.
package store

import (
	"context"
	"time"
)

// RunStatus represents the lifecycle state of a run.
type RunStatus string

const (
	RunStatusPending   RunStatus = "pending"
	RunStatusRunning   RunStatus = "running"
	RunStatusSucceeded RunStatus = "succeeded"
	RunStatusFailed    RunStatus = "failed"
	RunStatusCancelled RunStatus = "cancelled"
)

// TaskStatus represents the lifecycle state of a task.
type TaskStatus string

const (
	TaskStatusPending   TaskStatus = "pending"
	TaskStatusReady     TaskStatus = "ready"
	TaskStatusRunning   TaskStatus = "running"
	TaskStatusSucceeded TaskStatus = "succeeded"
	TaskStatusFailed    TaskStatus = "failed"
	TaskStatusCancelled TaskStatus = "cancelled"
	TaskStatusDead      TaskStatus = "dead"
)

// Run represents a single workflow execution.
type Run struct {
	ID          string    `json:"id"`
	WorkflowID  string    `json:"workflow_id"`
	Status      RunStatus `json:"status"`
	Input       []byte    `json:"input,omitempty"`
	Output      []byte    `json:"output,omitempty"`
	Error       []byte    `json:"error,omitempty"`
	SubmittedAt time.Time `json:"submitted_at"`
	SubmittedBy string    `json:"submitted_by"`
	TraceID     string    `json:"trace_id"`
	TaskIDs     []string  `json:"task_ids,omitempty"`
}

// Task represents a unit of work within a run.
type Task struct {
	ID       string     `json:"id"`
	RunID    string     `json:"run_id"`
	NodeID   string     `json:"node_id"`
	Status   TaskStatus `json:"status"`
	Input    []byte     `json:"input,omitempty"`
	Output   []byte     `json:"output,omitempty"`
	Attempts int        `json:"attempts"`
}

// Worker represents a registered worker process.
type Worker struct {
	ID           string
	Pool         string
	LeaseSeconds int
}

// CreateRunInput holds the parameters for creating a new run.
type CreateRunInput struct {
	WorkflowName string
	Input        []byte
	SubmittedBy  string
	TraceID      string
}

// TaskResult carries the outcome of a completed task attempt.
type TaskResult struct {
	TaskID        string
	AttemptNumber int
	WorkerID      string
	Status        string // "succeeded" | "failed"
	OutputJSON    []byte
	Error         string // empty if succeeded
	SpanID        string
	DurationMS    int
}

// Store is the persistence interface for all orchestrator state.
// All methods take a context.Context as their first argument.
type Store interface {
	// EnsureWorkflow upserts a workflow by name and returns its UUID.
	EnsureWorkflow(ctx context.Context, name string) (workflowID string, err error)

	// CreateRun creates a new run and its root task, returning both.
	CreateRun(ctx context.Context, in CreateRunInput) (Run, Task, error)

	// GetRun retrieves a run by ID, populating TaskIDs.
	GetRun(ctx context.Context, runID string) (Run, error)

	// ListRuns returns up to 100 runs, optionally filtered by status.
	ListRuns(ctx context.Context, status string) ([]Run, error)

	// CancelRun cancels a run and all its pending/ready/running tasks.
	CancelRun(ctx context.Context, runID string) error

	// RegisterWorker registers a new worker and returns its ID and lease.
	RegisterWorker(ctx context.Context, pool string, capabilities []string, maxConcurrency int) (Worker, error)

	// UpdateHeartbeat refreshes the last_heartbeat timestamp for a worker.
	UpdateHeartbeat(ctx context.Context, workerID string) error

	// IsWorkerDraining returns true if the worker's status is "draining".
	IsWorkerDraining(ctx context.Context, workerID string) (bool, error)

	// CompleteTask records a task attempt result and advances run/task status.
	// Returns "accepted" or "superseded".
	CompleteTask(ctx context.Context, result TaskResult) (disposition string, err error)

	// UpsertCheckpoint stores or replaces a task's checkpoint state.
	UpsertCheckpoint(ctx context.Context, taskID string, attemptNumber int, state []byte) error

	// Close releases underlying resources (e.g. connection pool).
	Close()
}
