package api_test

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/tomiwaaluko/helix/internal/api"
	"github.com/tomiwaaluko/helix/internal/store"
	"github.com/tomiwaaluko/helix/internal/testutil"
)

const testToken = "test-token"

func newHandler(s *testutil.MockStore, p *testutil.MockPublisher) http.Handler {
	h := api.NewHandler(s, p, slog.New(slog.NewTextHandler(io.Discard, nil)), testToken)
	return h.Router()
}

func authed(r *http.Request) *http.Request {
	r.Header.Set("Authorization", "Bearer "+testToken)
	return r
}

// ── bearer auth ───────────────────────────────────────────────────────────────

func TestBearerAuth_NoHeader_Returns401(t *testing.T) {
	h := newHandler(&testutil.MockStore{}, &testutil.MockPublisher{})
	r := httptest.NewRequest(http.MethodGet, "/api/v1/runs", nil)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("want 401, got %d", w.Code)
	}
}

func TestBearerAuth_WrongToken_Returns401(t *testing.T) {
	h := newHandler(&testutil.MockStore{}, &testutil.MockPublisher{})
	r := httptest.NewRequest(http.MethodGet, "/api/v1/runs", nil)
	r.Header.Set("Authorization", "Bearer wrong-token")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("want 401, got %d", w.Code)
	}
}

func TestBearerAuth_CorrectToken_Passes(t *testing.T) {
	h := newHandler(&testutil.MockStore{}, &testutil.MockPublisher{})
	r := authed(httptest.NewRequest(http.MethodGet, "/api/v1/runs", nil))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code == http.StatusUnauthorized {
		t.Fatalf("expected request to pass auth, got 401")
	}
}

// ── POST /api/v1/runs ─────────────────────────────────────────────────────────

func TestCreateRun_HappyPath_Returns201WithRunID(t *testing.T) {
	pub := &testutil.MockPublisher{}
	h := newHandler(&testutil.MockStore{}, pub)

	body := `{"workflow_name":"deep_research","input":{"question":"hello"},"submitted_by":"test"}`
	r := authed(httptest.NewRequest(http.MethodPost, "/api/v1/runs", strings.NewReader(body)))
	r.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)

	if w.Code != http.StatusCreated {
		t.Fatalf("want 201, got %d: %s", w.Code, w.Body.String())
	}

	var resp map[string]interface{}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("invalid JSON response: %v", err)
	}
	if resp["id"] != "run-id" {
		t.Errorf("want id=run-id, got %v", resp["id"])
	}

	if len(pub.Published) != 1 {
		t.Fatalf("expected 1 published envelope, got %d", len(pub.Published))
	}
	env := pub.Published[0]
	if env.GetTaskId() != "task-id" {
		t.Errorf("want task_id=task-id, got %q", env.GetTaskId())
	}
	if env.GetRunId() != "run-id" {
		t.Errorf("want run_id=run-id, got %q", env.GetRunId())
	}
	if env.GetWorkflowName() != "deep_research" {
		t.Errorf("want workflow_name=deep_research, got %q", env.GetWorkflowName())
	}
}

func TestCreateRun_MissingWorkflowName_Returns400(t *testing.T) {
	h := newHandler(&testutil.MockStore{}, &testutil.MockPublisher{})
	body := `{"input":{"question":"hello"}}`
	r := authed(httptest.NewRequest(http.MethodPost, "/api/v1/runs", strings.NewReader(body)))
	r.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusBadRequest {
		t.Fatalf("want 400, got %d", w.Code)
	}
}

func TestCreateRun_InvalidJSON_Returns400(t *testing.T) {
	h := newHandler(&testutil.MockStore{}, &testutil.MockPublisher{})
	r := authed(httptest.NewRequest(http.MethodPost, "/api/v1/runs", strings.NewReader("not-json")))
	r.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusBadRequest {
		t.Fatalf("want 400, got %d", w.Code)
	}
}

func TestCreateRun_StoreError_Returns500(t *testing.T) {
	ms := &testutil.MockStore{
		CreateRunFn: func(_ context.Context, _ store.CreateRunInput) (store.Run, store.Task, error) {
			return store.Run{}, store.Task{}, fmt.Errorf("db error")
		},
	}
	h := newHandler(ms, &testutil.MockPublisher{})
	body := `{"workflow_name":"deep_research","input":{}}`
	r := authed(httptest.NewRequest(http.MethodPost, "/api/v1/runs", strings.NewReader(body)))
	r.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusInternalServerError {
		t.Fatalf("want 500, got %d", w.Code)
	}
}

// ── GET /api/v1/runs ─────────────────────────────────────────────────────────

func TestListRuns_ReturnsJSONArray(t *testing.T) {
	ms := &testutil.MockStore{
		ListRunsFn: func(_ context.Context, _ string) ([]store.Run, error) {
			return []store.Run{{ID: "run-1", Status: store.RunStatusSucceeded}}, nil
		},
	}
	h := newHandler(ms, &testutil.MockPublisher{})
	r := authed(httptest.NewRequest(http.MethodGet, "/api/v1/runs", nil))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)

	if w.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", w.Code)
	}
	var runs []map[string]interface{}
	if err := json.Unmarshal(w.Body.Bytes(), &runs); err != nil {
		t.Fatalf("invalid JSON: %v", err)
	}
	if len(runs) != 1 {
		t.Fatalf("want 1 run, got %d", len(runs))
	}
}

func TestListRuns_EmptyStore_ReturnsEmptyArray(t *testing.T) {
	h := newHandler(&testutil.MockStore{}, &testutil.MockPublisher{})
	r := authed(httptest.NewRequest(http.MethodGet, "/api/v1/runs", nil))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)

	if w.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", w.Code)
	}
	body := strings.TrimSpace(w.Body.String())
	if !strings.HasPrefix(body, "[") {
		t.Errorf("want JSON array, got %q", body)
	}
}

// ── GET /api/v1/runs/{run_id} ─────────────────────────────────────────────────

func TestGetRun_Found_Returns200WithTaskTree(t *testing.T) {
	ms := &testutil.MockStore{
		ListTasksForRunFn: func(_ context.Context, runID string) ([]store.Task, error) {
			return []store.Task{
				{ID: "task-1", RunID: runID, NodeID: "root", Status: store.TaskStatusSucceeded, Attempts: 1},
			}, nil
		},
	}
	h := newHandler(ms, &testutil.MockPublisher{})
	r := authed(httptest.NewRequest(http.MethodGet, "/api/v1/runs/run-abc", nil))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", w.Code)
	}
	var body struct {
		ID    string `json:"id"`
		Tasks []struct {
			ID     string `json:"id"`
			NodeID string `json:"node_id"`
			Status string `json:"status"`
		} `json:"tasks"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatalf("invalid JSON: %v", err)
	}
	if body.ID != "run-abc" {
		t.Errorf("want id=run-abc, got %v", body.ID)
	}
	if len(body.Tasks) != 1 {
		t.Fatalf("want 1 task, got %d", len(body.Tasks))
	}
	if body.Tasks[0].NodeID != "root" || body.Tasks[0].Status != "succeeded" {
		t.Errorf("unexpected task: %+v", body.Tasks[0])
	}
}

func TestGetRun_TasksEmpty_ReturnsEmptyArray(t *testing.T) {
	h := newHandler(&testutil.MockStore{}, &testutil.MockPublisher{})
	r := authed(httptest.NewRequest(http.MethodGet, "/api/v1/runs/run-abc", nil))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", w.Code)
	}
	// tasks must serialize as [] not null
	if !strings.Contains(w.Body.String(), `"tasks":[]`) {
		t.Errorf("want empty tasks array, got %s", w.Body.String())
	}
}

func TestGetRun_ListTasksError_Returns500(t *testing.T) {
	ms := &testutil.MockStore{
		ListTasksForRunFn: func(_ context.Context, _ string) ([]store.Task, error) {
			return nil, fmt.Errorf("db gone")
		},
	}
	h := newHandler(ms, &testutil.MockPublisher{})
	r := authed(httptest.NewRequest(http.MethodGet, "/api/v1/runs/run-abc", nil))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusInternalServerError {
		t.Fatalf("want 500, got %d", w.Code)
	}
}

func TestGetRun_StoreError_Returns404(t *testing.T) {
	ms := &testutil.MockStore{
		GetRunFn: func(_ context.Context, _ string) (store.Run, error) {
			return store.Run{}, fmt.Errorf("not found")
		},
	}
	h := newHandler(ms, &testutil.MockPublisher{})
	r := authed(httptest.NewRequest(http.MethodGet, "/api/v1/runs/missing", nil))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusNotFound {
		t.Fatalf("want 404, got %d", w.Code)
	}
}

// ── POST /api/v1/runs/{run_id}/cancel ─────────────────────────────────────────

func TestCancelRun_Returns200(t *testing.T) {
	cancelled := ""
	ms := &testutil.MockStore{
		CancelRunFn: func(_ context.Context, runID string) error {
			cancelled = runID
			return nil
		},
	}
	h := newHandler(ms, &testutil.MockPublisher{})
	r := authed(httptest.NewRequest(http.MethodPost, "/api/v1/runs/run-xyz/cancel", nil))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", w.Code)
	}
	if cancelled != "run-xyz" {
		t.Errorf("want CancelRun called with run-xyz, got %q", cancelled)
	}
}

func TestCancelRun_StoreError_Returns500(t *testing.T) {
	ms := &testutil.MockStore{
		CancelRunFn: func(_ context.Context, _ string) error {
			return fmt.Errorf("db error")
		},
	}
	h := newHandler(ms, &testutil.MockPublisher{})
	r := authed(httptest.NewRequest(http.MethodPost, "/api/v1/runs/run-xyz/cancel", nil))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusInternalServerError {
		t.Fatalf("want 500, got %d", w.Code)
	}
}
