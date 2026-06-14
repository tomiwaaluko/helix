// Package testutil provides test helpers shared across internal packages.
package testutil

import (
	"context"

	helixv1 "github.com/tomiwaaluko/helix/gen/go/helix/v1"
	"github.com/tomiwaaluko/helix/internal/store"
)

// MockStore is a test double for store.Store. Each field is a function that
// the test can replace; nil fields return zero values / no error by default.
type MockStore struct {
	EnsureWorkflowFn   func(ctx context.Context, name string) (string, error)
	CreateRunFn        func(ctx context.Context, in store.CreateRunInput) (store.Run, store.Task, error)
	GetRunFn           func(ctx context.Context, runID string) (store.Run, error)
	ListRunsFn         func(ctx context.Context, status string) ([]store.Run, error)
	CancelRunFn        func(ctx context.Context, runID string) error
	RegisterWorkerFn   func(ctx context.Context, pool string, caps []string, max int) (store.Worker, error)
	UpdateHeartbeatFn  func(ctx context.Context, workerID string) error
	IsWorkerDrainingFn func(ctx context.Context, workerID string) (bool, error)
	CompleteTaskFn     func(ctx context.Context, result store.TaskResult) (string, error)
	UpsertCheckpointFn func(ctx context.Context, taskID string, attempt int, state []byte) error
}

func (m *MockStore) EnsureWorkflow(ctx context.Context, name string) (string, error) {
	if m.EnsureWorkflowFn != nil {
		return m.EnsureWorkflowFn(ctx, name)
	}
	return "workflow-id", nil
}

func (m *MockStore) CreateRun(ctx context.Context, in store.CreateRunInput) (store.Run, store.Task, error) {
	if m.CreateRunFn != nil {
		return m.CreateRunFn(ctx, in)
	}
	run := store.Run{ID: "run-id", WorkflowID: "wf-id", Status: store.RunStatusRunning,
		Input: in.Input, SubmittedBy: in.SubmittedBy, TraceID: "trace-id"}
	task := store.Task{ID: "task-id", RunID: "run-id", NodeID: "root", Status: store.TaskStatusReady}
	return run, task, nil
}

func (m *MockStore) GetRun(ctx context.Context, runID string) (store.Run, error) {
	if m.GetRunFn != nil {
		return m.GetRunFn(ctx, runID)
	}
	return store.Run{ID: runID, Status: store.RunStatusRunning}, nil
}

func (m *MockStore) ListRuns(ctx context.Context, status string) ([]store.Run, error) {
	if m.ListRunsFn != nil {
		return m.ListRunsFn(ctx, status)
	}
	return []store.Run{}, nil
}

func (m *MockStore) CancelRun(ctx context.Context, runID string) error {
	if m.CancelRunFn != nil {
		return m.CancelRunFn(ctx, runID)
	}
	return nil
}

func (m *MockStore) RegisterWorker(ctx context.Context, pool string, caps []string, max int) (store.Worker, error) {
	if m.RegisterWorkerFn != nil {
		return m.RegisterWorkerFn(ctx, pool, caps, max)
	}
	return store.Worker{ID: "worker-id", Pool: pool, LeaseSeconds: 30}, nil
}

func (m *MockStore) UpdateHeartbeat(ctx context.Context, workerID string) error {
	if m.UpdateHeartbeatFn != nil {
		return m.UpdateHeartbeatFn(ctx, workerID)
	}
	return nil
}

func (m *MockStore) IsWorkerDraining(ctx context.Context, workerID string) (bool, error) {
	if m.IsWorkerDrainingFn != nil {
		return m.IsWorkerDrainingFn(ctx, workerID)
	}
	return false, nil
}

func (m *MockStore) CompleteTask(ctx context.Context, result store.TaskResult) (string, error) {
	if m.CompleteTaskFn != nil {
		return m.CompleteTaskFn(ctx, result)
	}
	return "accepted", nil
}

func (m *MockStore) UpsertCheckpoint(ctx context.Context, taskID string, attempt int, state []byte) error {
	if m.UpsertCheckpointFn != nil {
		return m.UpsertCheckpointFn(ctx, taskID, attempt, state)
	}
	return nil
}

func (m *MockStore) Close() {}

// MockPublisher is a test double for dispatch.Publisher.
type MockPublisher struct {
	PublishTaskEnvelopeFn func(ctx context.Context, pool string, env *helixv1.TaskEnvelope) error
	Published             []*helixv1.TaskEnvelope
}

func (p *MockPublisher) PublishTaskEnvelope(ctx context.Context, pool string, env *helixv1.TaskEnvelope) error {
	if p.PublishTaskEnvelopeFn != nil {
		return p.PublishTaskEnvelopeFn(ctx, pool, env)
	}
	p.Published = append(p.Published, env)
	return nil
}
