package grpcserver_test

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net"
	"testing"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"

	helixv1 "github.com/tomiwaaluko/helix/gen/go/helix/v1"
	grpcserver "github.com/tomiwaaluko/helix/internal/grpc"
	"github.com/tomiwaaluko/helix/internal/store"
	"github.com/tomiwaaluko/helix/internal/testutil"
)

const bufSize = 1 << 20 // 1 MB

// startServer spins up an in-memory gRPC server and returns a connected client.
func startServer(t *testing.T, ms *testutil.MockStore) helixv1.OrchestratorClient {
	t.Helper()
	lis := bufconn.Listen(bufSize)
	t.Cleanup(func() { _ = lis.Close() })

	srv := grpc.NewServer()
	helixv1.RegisterOrchestratorServer(
		srv,
		grpcserver.NewServer(ms, nil, slog.New(slog.NewTextHandler(io.Discard, nil))),
	)

	go func() {
		if err := srv.Serve(lis); err != nil && err != grpc.ErrServerStopped {
			t.Logf("bufconn server error: %v", err)
		}
	}()
	t.Cleanup(srv.GracefulStop)

	dialer := func(ctx context.Context, _ string) (net.Conn, error) {
		return lis.DialContext(ctx)
	}
	conn, err := grpc.NewClient(
		"passthrough:///bufnet",
		grpc.WithContextDialer(dialer),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		t.Fatalf("failed to dial bufconn: %v", err)
	}
	t.Cleanup(func() { _ = conn.Close() })

	return helixv1.NewOrchestratorClient(conn)
}

// ── RegisterWorker ────────────────────────────────────────────────────────────

func TestRegisterWorker_HappyPath(t *testing.T) {
	client := startServer(t, &testutil.MockStore{})
	resp, err := client.RegisterWorker(context.Background(), &helixv1.RegisterWorkerRequest{
		Pool:           "research",
		Capabilities:   []string{"deep_research"},
		MaxConcurrency: 4,
	})
	if err != nil {
		t.Fatalf("RegisterWorker failed: %v", err)
	}
	if resp.GetWorkerId() == "" {
		t.Error("expected non-empty worker_id")
	}
	if resp.GetLeaseSeconds() != 30 {
		t.Errorf("want lease_seconds=30, got %d", resp.GetLeaseSeconds())
	}
}

func TestRegisterWorker_StoreError_ReturnsInternal(t *testing.T) {
	ms := &testutil.MockStore{
		RegisterWorkerFn: func(_ context.Context, _ string, _ []string, _ int) (store.Worker, error) {
			return store.Worker{}, fmt.Errorf("db error")
		},
	}
	client := startServer(t, ms)
	_, err := client.RegisterWorker(context.Background(), &helixv1.RegisterWorkerRequest{Pool: "research"})
	if err == nil {
		t.Fatal("expected error, got nil")
	}
	if status.Code(err) != codes.Internal {
		t.Errorf("want codes.Internal, got %v", status.Code(err))
	}
}

// ── CompleteTask ──────────────────────────────────────────────────────────────

func TestCompleteTask_HappyPath_ReturnsAccepted(t *testing.T) {
	client := startServer(t, &testutil.MockStore{})
	resp, err := client.CompleteTask(context.Background(), &helixv1.CompleteTaskRequest{
		Result: &helixv1.TaskResult{
			TaskId:        "task-123",
			AttemptNumber: 1,
			WorkerId:      "worker-abc",
			Status:        "succeeded",
			OutputJson:    []byte(`{"answer":"Paris"}`),
		},
	})
	if err != nil {
		t.Fatalf("CompleteTask failed: %v", err)
	}
	if resp.GetDisposition() != "accepted" {
		t.Errorf("want disposition=accepted, got %q", resp.GetDisposition())
	}
}

func TestCompleteTask_NilResult_ReturnsInvalidArgument(t *testing.T) {
	client := startServer(t, &testutil.MockStore{})
	_, err := client.CompleteTask(context.Background(), &helixv1.CompleteTaskRequest{})
	if err == nil {
		t.Fatal("expected error for nil result")
	}
	if status.Code(err) != codes.InvalidArgument {
		t.Errorf("want codes.InvalidArgument, got %v", status.Code(err))
	}
}

func TestCompleteTask_Idempotent_ReturnSuperseded(t *testing.T) {
	ms := &testutil.MockStore{
		CompleteTaskFn: func(_ context.Context, _ store.TaskResult) (string, error) {
			return "superseded", nil
		},
	}
	client := startServer(t, ms)
	resp, err := client.CompleteTask(context.Background(), &helixv1.CompleteTaskRequest{
		Result: &helixv1.TaskResult{
			TaskId:   "task-123",
			Status:   "succeeded",
			WorkerId: "worker-abc",
		},
	})
	if err != nil {
		t.Fatalf("CompleteTask failed: %v", err)
	}
	if resp.GetDisposition() != "superseded" {
		t.Errorf("want disposition=superseded, got %q", resp.GetDisposition())
	}
}

func TestCompleteTask_StoreError_ReturnsInternal(t *testing.T) {
	ms := &testutil.MockStore{
		CompleteTaskFn: func(_ context.Context, _ store.TaskResult) (string, error) {
			return "", fmt.Errorf("db gone")
		},
	}
	client := startServer(t, ms)
	_, err := client.CompleteTask(context.Background(), &helixv1.CompleteTaskRequest{
		Result: &helixv1.TaskResult{TaskId: "task-x", Status: "succeeded", WorkerId: "w"},
	})
	if err == nil {
		t.Fatal("expected error, got nil")
	}
	if status.Code(err) != codes.Internal {
		t.Errorf("want codes.Internal, got %v", status.Code(err))
	}
}

// ── Checkpoint ────────────────────────────────────────────────────────────────

func TestCheckpoint_HappyPath(t *testing.T) {
	called := false
	ms := &testutil.MockStore{
		UpsertCheckpointFn: func(_ context.Context, taskID string, attempt int, state []byte) error {
			called = true
			if taskID != "task-999" || attempt != 2 || string(state) != "state-bytes" {
				return fmt.Errorf("unexpected args: taskID=%q attempt=%d", taskID, attempt)
			}
			return nil
		},
	}
	client := startServer(t, ms)
	_, err := client.Checkpoint(context.Background(), &helixv1.CheckpointRequest{
		TaskId:        "task-999",
		AttemptNumber: 2,
		State:         []byte("state-bytes"),
	})
	if err != nil {
		t.Fatalf("Checkpoint failed: %v", err)
	}
	if !called {
		t.Error("UpsertCheckpoint was not called")
	}
}

func TestCheckpoint_StoreError_ReturnsInternal(t *testing.T) {
	ms := &testutil.MockStore{
		UpsertCheckpointFn: func(_ context.Context, _ string, _ int, _ []byte) error {
			return fmt.Errorf("disk full")
		},
	}
	client := startServer(t, ms)
	_, err := client.Checkpoint(context.Background(), &helixv1.CheckpointRequest{TaskId: "task-x"})
	if err == nil {
		t.Fatal("expected error, got nil")
	}
	if status.Code(err) != codes.Internal {
		t.Errorf("want codes.Internal, got %v", status.Code(err))
	}
}
