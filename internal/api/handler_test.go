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
	"time"

	"github.com/tomiwaaluko/helix/internal/api"
	"github.com/tomiwaaluko/helix/internal/clickhouse"
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

// ── trace test doubles ────────────────────────────────────────────────────────

type mockSpanQuerier struct {
	fn func(ctx context.Context, traceID string) ([]clickhouse.SpanRow, error)
}

func (m *mockSpanQuerier) GetTraceSpans(ctx context.Context, traceID string) ([]clickhouse.SpanRow, error) {
	if m.fn != nil {
		return m.fn(ctx, traceID)
	}
	return []clickhouse.SpanRow{}, nil
}

type mockPresigner struct {
	fn func(attrs map[string]string, expiry time.Duration) (map[string]string, error)
}

func (m *mockPresigner) PresignAttrs(attrs map[string]string, expiry time.Duration) (map[string]string, error) {
	if m.fn != nil {
		return m.fn(attrs, expiry)
	}
	return attrs, nil
}

func newTraceHandler(s *testutil.MockStore, sq *mockSpanQuerier, ap *mockPresigner) http.Handler {
	h := api.NewHandler(s, &testutil.MockPublisher{}, slog.New(slog.NewTextHandler(io.Discard, nil)), testToken)
	h = h.WithTrace(sq, ap)
	return h.Router()
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

// ── GET /api/v1/runs/{run_id}/trace ───────────────────────────────────────────

func TestGetTrace_NoClickHouse_Returns503(t *testing.T) {
	// Handler without WithTrace → 503.
	h := newHandler(&testutil.MockStore{}, &testutil.MockPublisher{})
	r := authed(httptest.NewRequest(http.MethodGet, "/api/v1/runs/run-abc/trace", nil))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusServiceUnavailable {
		t.Fatalf("want 503, got %d", w.Code)
	}
}

func TestGetTrace_RunNotFound_Returns404(t *testing.T) {
	ms := &testutil.MockStore{
		GetRunFn: func(_ context.Context, _ string) (store.Run, error) {
			return store.Run{}, fmt.Errorf("not found")
		},
	}
	h := newTraceHandler(ms, &mockSpanQuerier{}, nil)
	r := authed(httptest.NewRequest(http.MethodGet, "/api/v1/runs/missing/trace", nil))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusNotFound {
		t.Fatalf("want 404, got %d", w.Code)
	}
}

func TestGetTrace_SpanQueryError_Returns500(t *testing.T) {
	sq := &mockSpanQuerier{
		fn: func(_ context.Context, _ string) ([]clickhouse.SpanRow, error) {
			return nil, fmt.Errorf("clickhouse down")
		},
	}
	h := newTraceHandler(&testutil.MockStore{}, sq, nil)
	r := authed(httptest.NewRequest(http.MethodGet, "/api/v1/runs/run-abc/trace", nil))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusInternalServerError {
		t.Fatalf("want 500, got %d", w.Code)
	}
}

func TestGetTrace_HappyPath_ReturnsSpanTree(t *testing.T) {
	now := time.Date(2026, 6, 15, 0, 0, 0, 0, time.UTC)
	ms := &testutil.MockStore{
		GetRunFn: func(_ context.Context, runID string) (store.Run, error) {
			return store.Run{ID: runID, TraceID: "trace-xyz", Status: store.RunStatusSucceeded}, nil
		},
	}
	sq := &mockSpanQuerier{
		fn: func(_ context.Context, traceID string) ([]clickhouse.SpanRow, error) {
			return []clickhouse.SpanRow{
				{
					TraceID: traceID, SpanID: "span-1", ParentSpanID: "",
					Name: "task", Kind: "task",
					StartTime: now, EndTime: now.Add(500 * time.Millisecond),
					Status: "ok",
					Attributes: map[string]string{"model": "claude"},
				},
				{
					TraceID: traceID, SpanID: "span-2", ParentSpanID: "span-1",
					Name: "llm", Kind: "llm",
					StartTime: now.Add(10 * time.Millisecond), EndTime: now.Add(400 * time.Millisecond),
					Status: "ok",
					Attributes: map[string]string{
						"cost_usd":   "0.001",
						"prompt_uri": "s3://helix-blobs/2026/06/15/span-2.bin",
					},
				},
			}, nil
		},
	}
	// Presigner replaces s3:// values with a stub HTTPS URL.
	ap := &mockPresigner{
		fn: func(attrs map[string]string, _ time.Duration) (map[string]string, error) {
			out := make(map[string]string, len(attrs))
			for k, v := range attrs {
				if strings.HasPrefix(v, "s3://") {
					out[k] = "https://minio.example.com/presigned/" + k
				} else {
					out[k] = v
				}
			}
			return out, nil
		},
	}

	h := newTraceHandler(ms, sq, ap)
	r := authed(httptest.NewRequest(http.MethodGet, "/api/v1/runs/run-abc/trace", nil))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)

	if w.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", w.Code, w.Body.String())
	}

	var resp struct {
		TraceID string `json:"trace_id"`
		Spans   []struct {
			SpanID       string            `json:"span_id"`
			ParentSpanID string            `json:"parent_span_id"`
			Name         string            `json:"name"`
			DurationMS   uint32            `json:"duration_ms"`
			Attributes   map[string]string `json:"attributes"`
		} `json:"spans"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("invalid JSON: %v", err)
	}

	if resp.TraceID != "trace-xyz" {
		t.Errorf("want trace_id=trace-xyz, got %q", resp.TraceID)
	}
	if len(resp.Spans) != 2 {
		t.Fatalf("want 2 spans, got %d", len(resp.Spans))
	}
	// Root span
	if resp.Spans[0].SpanID != "span-1" || resp.Spans[0].ParentSpanID != "" {
		t.Errorf("unexpected root span: %+v", resp.Spans[0])
	}
	if resp.Spans[0].DurationMS != 500 {
		t.Errorf("want duration_ms=500, got %d", resp.Spans[0].DurationMS)
	}
	// Child span: s3:// attribute should be presigned.
	if got := resp.Spans[1].Attributes["prompt_uri"]; !strings.HasPrefix(got, "https://") {
		t.Errorf("want presigned HTTPS prompt_uri, got %q", got)
	}
	// Non-blob attribute preserved.
	if resp.Spans[1].Attributes["cost_usd"] != "0.001" {
		t.Errorf("want cost_usd=0.001, got %q", resp.Spans[1].Attributes["cost_usd"])
	}
}

func TestGetTrace_PresignError_Returns500(t *testing.T) {
	sq := &mockSpanQuerier{
		fn: func(_ context.Context, _ string) ([]clickhouse.SpanRow, error) {
			return []clickhouse.SpanRow{
				{SpanID: "span-1", Attributes: map[string]string{"prompt_uri": "s3://bucket/key"}},
			}, nil
		},
	}
	ap := &mockPresigner{
		fn: func(_ map[string]string, _ time.Duration) (map[string]string, error) {
			return nil, fmt.Errorf("minio unreachable")
		},
	}
	h := newTraceHandler(&testutil.MockStore{}, sq, ap)
	r := authed(httptest.NewRequest(http.MethodGet, "/api/v1/runs/run-abc/trace", nil))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusInternalServerError {
		t.Fatalf("want 500, got %d", w.Code)
	}
}
