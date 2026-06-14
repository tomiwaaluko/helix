// Package grpcserver implements the Helix OrchestratorServer gRPC interface.
package grpcserver

import (
	"context"
	"io"
	"log/slog"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	helixv1 "github.com/tomiwaaluko/helix/gen/go/helix/v1"
	"github.com/tomiwaaluko/helix/internal/dispatch"
	"github.com/tomiwaaluko/helix/internal/store"
)

// Server implements helixv1.OrchestratorServer.
type Server struct {
	helixv1.UnimplementedOrchestratorServer

	store  store.Store
	nats   *dispatch.Client
	logger *slog.Logger
}

// NewServer returns a new Server backed by the given store and dispatch client.
func NewServer(s store.Store, d *dispatch.Client, log *slog.Logger) *Server {
	return &Server{
		store:  s,
		nats:   d,
		logger: log,
	}
}

// RegisterWorker registers a worker and returns its ID and heartbeat lease.
func (s *Server) RegisterWorker(ctx context.Context, req *helixv1.RegisterWorkerRequest) (*helixv1.RegisterWorkerResponse, error) {
	w, err := s.store.RegisterWorker(ctx, req.GetPool(), req.GetCapabilities(), int(req.GetMaxConcurrency()))
	if err != nil {
		s.logger.ErrorContext(ctx, "RegisterWorker failed",
			"pool", req.GetPool(),
			"error", err,
		)
		return nil, status.Errorf(codes.Internal, "register worker: %v", err)
	}

	s.logger.InfoContext(ctx, "worker registered",
		"worker_id", w.ID,
		"pool", w.Pool,
		"lease_seconds", w.LeaseSeconds,
	)

	return &helixv1.RegisterWorkerResponse{
		WorkerId:     w.ID,
		LeaseSeconds: int32(w.LeaseSeconds), //nolint:gosec // lease never overflows int32
	}, nil
}

// Heartbeat is a bidirectional streaming RPC. Each received HeartbeatRequest
// triggers a heartbeat update and optionally signals drain to the worker.
func (s *Server) Heartbeat(stream grpc.BidiStreamingServer[helixv1.HeartbeatRequest, helixv1.HeartbeatResponse]) error {
	ctx := stream.Context()

	for {
		req, err := stream.Recv()
		if err != nil {
			if err == io.EOF {
				return nil
			}
			return status.Errorf(codes.Unavailable, "heartbeat recv: %v", err)
		}

		workerID := req.GetWorkerId()

		if hbErr := s.store.UpdateHeartbeat(ctx, workerID); hbErr != nil {
			s.logger.WarnContext(ctx, "UpdateHeartbeat failed",
				"worker_id", workerID,
				"error", hbErr,
			)
			return status.Errorf(codes.Internal, "update heartbeat: %v", hbErr)
		}

		draining, drainErr := s.store.IsWorkerDraining(ctx, workerID)
		if drainErr != nil {
			s.logger.WarnContext(ctx, "IsWorkerDraining failed",
				"worker_id", workerID,
				"error", drainErr,
			)
			return status.Errorf(codes.Internal, "check draining: %v", drainErr)
		}

		if sendErr := stream.Send(&helixv1.HeartbeatResponse{Drain: draining}); sendErr != nil {
			return status.Errorf(codes.Unavailable, "heartbeat send: %v", sendErr)
		}

		s.logger.DebugContext(ctx, "heartbeat",
			"worker_id", workerID,
			"in_flight", req.GetInFlight(),
			"capacity", req.GetCapacity(),
			"drain", draining,
		)
	}
}

// CompleteTask records the outcome of a task attempt.
func (s *Server) CompleteTask(ctx context.Context, req *helixv1.CompleteTaskRequest) (*helixv1.CompleteTaskResponse, error) {
	r := req.GetResult()
	if r == nil {
		return nil, status.Error(codes.InvalidArgument, "result is required")
	}

	storeResult := store.TaskResult{
		TaskID:        r.GetTaskId(),
		AttemptNumber: int(r.GetAttemptNumber()),
		WorkerID:      r.GetWorkerId(),
		Status:        r.GetStatus(),
		OutputJSON:    r.GetOutputJson(),
		Error:         r.GetError(),
		SpanID:        r.GetSpanId(),
		DurationMS:    int(r.GetDurationMs()),
	}

	disposition, err := s.store.CompleteTask(ctx, storeResult)
	if err != nil {
		s.logger.ErrorContext(ctx, "CompleteTask failed",
			"task_id", storeResult.TaskID,
			"error", err,
		)
		return nil, status.Errorf(codes.Internal, "complete task: %v", err)
	}

	s.logger.InfoContext(ctx, "task completed",
		"task_id", storeResult.TaskID,
		"status", storeResult.Status,
		"disposition", disposition,
	)

	return &helixv1.CompleteTaskResponse{Disposition: disposition}, nil
}

// Checkpoint stores an in-progress task's state for crash recovery.
func (s *Server) Checkpoint(ctx context.Context, req *helixv1.CheckpointRequest) (*helixv1.CheckpointResponse, error) {
	err := s.store.UpsertCheckpoint(ctx, req.GetTaskId(), int(req.GetAttemptNumber()), req.GetState())
	if err != nil {
		s.logger.ErrorContext(ctx, "Checkpoint failed",
			"task_id", req.GetTaskId(),
			"attempt", req.GetAttemptNumber(),
			"error", err,
		)
		return nil, status.Errorf(codes.Internal, "upsert checkpoint: %v", err)
	}

	s.logger.DebugContext(ctx, "checkpoint stored",
		"task_id", req.GetTaskId(),
		"attempt", req.GetAttemptNumber(),
	)

	return &helixv1.CheckpointResponse{}, nil
}
