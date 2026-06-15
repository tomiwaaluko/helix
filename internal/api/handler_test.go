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

// ── eval test doubles ─────────────────────────────────────────────────────────

type mockEvalWriter struct {
	recorded []clickhouse.EvalEventRow
	err      error
}

func (m *mockEvalWriter) Record(_ context.Context, rows []clickhouse.EvalEventRow) error {
	if m.err != nil {
		return m.err
	}
	m.recorded = append(m.recorded, rows...)
	return nil
}

type mockEvalQuerier struct {
	listFn func(ctx context.Context) ([]clickhouse.EvalSummaryRow, []clickhouse.ScorerMeanRow, error)
	getFn  func(ctx context.Context, evalID string) ([]clickhouse.EvalEventRow, error)
}

func (m *mockEvalQuerier) ListEvals(ctx context.Context) ([]clickhouse.EvalSummaryRow, []clickhouse.ScorerMeanRow, error) {
	if m.listFn != nil {
		return m.listFn(ctx)
	}
	return []clickhouse.EvalSummaryRow{}, []clickhouse.ScorerMeanRow{}, nil
}

func (m *mockEvalQuerier) GetEval(ctx context.Context, evalID string) ([]clickhouse.EvalEventRow, error) {
	if m.getFn != nil {
		return m.getFn(ctx, evalID)
	}
	return []clickhouse.EvalEventRow{}, nil
}

func newEvalHandler(ew *mockEvalWriter, eq *mockEvalQuerier) http.Handler {
	h := api.NewHandler(&testutil.MockStore{}, &testutil.MockPublisher{}, slog.New(slog.NewTextHandler(io.Discard, nil)), testToken)
	h = h.WithEvals(ew, eq)
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
					Status:     "ok",
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

// ── POST /api/v1/evals/{eval_id}/events ───────────────────────────────────────

func TestRecordEvalEvents_NilWriter_Returns503(t *testing.T) {
	// Handler without WithEvals → 503.
	h := newHandler(&testutil.MockStore{}, &testutil.MockPublisher{})
	body := `{"events":[{"example_id":"ex-1","scorer":"answer_f1","score":0.5,"passed":false}]}`
	r := authed(httptest.NewRequest(http.MethodPost, "/api/v1/evals/eval-1/events", strings.NewReader(body)))
	r.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusServiceUnavailable {
		t.Fatalf("want 503, got %d", w.Code)
	}
}

func TestRecordEvalEvents_HappyPath_Returns204(t *testing.T) {
	ew := &mockEvalWriter{}
	h := newEvalHandler(ew, &mockEvalQuerier{})
	body := `{"events":[
		{"example_id":"ex-1","run_id":"run-1","scorer":"answer_f1","score":0.5,"passed":false,"details":{"note":"x"}},
		{"example_id":"ex-1","run_id":"run-1","scorer":"retrieval_recall@10","score":1.0,"passed":true},
		{"example_id":"ex-2","run_id":"run-1","scorer":"answer_f1","score":0.0,"passed":false}
	]}`
	r := authed(httptest.NewRequest(http.MethodPost, "/api/v1/evals/eval-42/events", strings.NewReader(body)))
	r.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)

	if w.Code != http.StatusNoContent {
		t.Fatalf("want 204, got %d: %s", w.Code, w.Body.String())
	}
	if len(ew.recorded) != 3 {
		t.Fatalf("want 3 recorded rows, got %d", len(ew.recorded))
	}
	for _, row := range ew.recorded {
		if row.EvalID != "eval-42" {
			t.Errorf("want eval_id=eval-42, got %q", row.EvalID)
		}
	}
	// passed bool → uint8 mapping
	if ew.recorded[1].Passed != 1 {
		t.Errorf("want recall row passed=1, got %d", ew.recorded[1].Passed)
	}
	if ew.recorded[0].Passed != 0 {
		t.Errorf("want f1 row passed=0, got %d", ew.recorded[0].Passed)
	}
	// details serialized to JSON
	if !strings.Contains(ew.recorded[0].Details, `"note":"x"`) {
		t.Errorf("want details JSON, got %q", ew.recorded[0].Details)
	}
}

func TestRecordEvalEvents_EmptyEvents_Returns400(t *testing.T) {
	h := newEvalHandler(&mockEvalWriter{}, &mockEvalQuerier{})
	r := authed(httptest.NewRequest(http.MethodPost, "/api/v1/evals/eval-1/events", strings.NewReader(`{"events":[]}`)))
	r.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusBadRequest {
		t.Fatalf("want 400, got %d", w.Code)
	}
}

func TestRecordEvalEvents_ScoreOutOfRange_Returns400(t *testing.T) {
	ew := &mockEvalWriter{}
	h := newEvalHandler(ew, &mockEvalQuerier{})
	body := `{"events":[{"example_id":"ex-1","scorer":"answer_f1","score":1.5,"passed":true}]}`
	r := authed(httptest.NewRequest(http.MethodPost, "/api/v1/evals/eval-1/events", strings.NewReader(body)))
	r.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusBadRequest {
		t.Fatalf("want 400, got %d", w.Code)
	}
	if len(ew.recorded) != 0 {
		t.Errorf("no rows should be recorded on validation failure, got %d", len(ew.recorded))
	}
}

func TestRecordEvalEvents_WriterError_Returns500(t *testing.T) {
	ew := &mockEvalWriter{err: fmt.Errorf("clickhouse down")}
	h := newEvalHandler(ew, &mockEvalQuerier{})
	body := `{"events":[{"example_id":"ex-1","scorer":"answer_f1","score":0.5,"passed":false}]}`
	r := authed(httptest.NewRequest(http.MethodPost, "/api/v1/evals/eval-1/events", strings.NewReader(body)))
	r.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusInternalServerError {
		t.Fatalf("want 500, got %d", w.Code)
	}
}

// ── GET /api/v1/evals ─────────────────────────────────────────────────────────

func TestListEvals_NilReader_Returns503(t *testing.T) {
	h := newHandler(&testutil.MockStore{}, &testutil.MockPublisher{})
	r := authed(httptest.NewRequest(http.MethodGet, "/api/v1/evals", nil))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusServiceUnavailable {
		t.Fatalf("want 503, got %d", w.Code)
	}
}

func TestListEvals_HappyPath_JoinsScorerMeans(t *testing.T) {
	now := time.Date(2026, 6, 15, 0, 0, 0, 0, time.UTC)
	eq := &mockEvalQuerier{
		listFn: func(_ context.Context) ([]clickhouse.EvalSummaryRow, []clickhouse.ScorerMeanRow, error) {
			return []clickhouse.EvalSummaryRow{
					{EvalID: "eval-1", Examples: 100, StartedAt: now, FinishedAt: now.Add(time.Minute)},
				},
				[]clickhouse.ScorerMeanRow{
					{EvalID: "eval-1", Scorer: "answer_f1", Mean: 0.15, N: 100},
					{EvalID: "eval-1", Scorer: "retrieval_recall@10", Mean: 0.65, N: 100},
				}, nil
		},
	}
	h := newEvalHandler(&mockEvalWriter{}, eq)
	r := authed(httptest.NewRequest(http.MethodGet, "/api/v1/evals", nil))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)

	if w.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", w.Code, w.Body.String())
	}
	var resp []struct {
		EvalID   string `json:"eval_id"`
		Examples uint64 `json:"examples"`
		Scorers  []struct {
			Scorer string  `json:"scorer"`
			Mean   float64 `json:"mean"`
			N      uint64  `json:"n"`
		} `json:"scorers"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("invalid JSON: %v", err)
	}
	if len(resp) != 1 {
		t.Fatalf("want 1 eval, got %d", len(resp))
	}
	if resp[0].EvalID != "eval-1" || resp[0].Examples != 100 {
		t.Errorf("unexpected summary: %+v", resp[0])
	}
	if len(resp[0].Scorers) != 2 {
		t.Fatalf("want 2 scorer means, got %d", len(resp[0].Scorers))
	}
}

func TestListEvals_Empty_ReturnsEmptyArray(t *testing.T) {
	h := newEvalHandler(&mockEvalWriter{}, &mockEvalQuerier{})
	r := authed(httptest.NewRequest(http.MethodGet, "/api/v1/evals", nil))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", w.Code)
	}
	if !strings.HasPrefix(strings.TrimSpace(w.Body.String()), "[") {
		t.Errorf("want JSON array, got %q", w.Body.String())
	}
}

// ── GET /api/v1/evals/{eval_id} ───────────────────────────────────────────────

func TestGetEval_NotFound_Returns404(t *testing.T) {
	// Default querier returns an empty slice → 404.
	h := newEvalHandler(&mockEvalWriter{}, &mockEvalQuerier{})
	r := authed(httptest.NewRequest(http.MethodGet, "/api/v1/evals/missing", nil))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusNotFound {
		t.Fatalf("want 404, got %d", w.Code)
	}
}

func TestGetEval_HappyPath_ReturnsEvents(t *testing.T) {
	now := time.Date(2026, 6, 15, 0, 0, 0, 0, time.UTC)
	eq := &mockEvalQuerier{
		getFn: func(_ context.Context, evalID string) ([]clickhouse.EvalEventRow, error) {
			return []clickhouse.EvalEventRow{
				{EvalID: evalID, ExampleID: "ex-1", RunID: "run-1", Scorer: "answer_f1", Score: 0.5, Passed: 0, Timestamp: now},
				{EvalID: evalID, ExampleID: "ex-1", RunID: "run-1", Scorer: "retrieval_recall@10", Score: 1.0, Passed: 1, Timestamp: now},
			}, nil
		},
	}
	h := newEvalHandler(&mockEvalWriter{}, eq)
	r := authed(httptest.NewRequest(http.MethodGet, "/api/v1/evals/eval-9", nil))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)

	if w.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", w.Code, w.Body.String())
	}
	var resp []struct {
		ExampleID string  `json:"example_id"`
		Scorer    string  `json:"scorer"`
		Score     float64 `json:"score"`
		Passed    bool    `json:"passed"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("invalid JSON: %v", err)
	}
	if len(resp) != 2 {
		t.Fatalf("want 2 events, got %d", len(resp))
	}
	if !resp[1].Passed {
		t.Errorf("want recall event passed=true, got false")
	}
}

func TestGetEval_ReaderError_Returns500(t *testing.T) {
	eq := &mockEvalQuerier{
		getFn: func(_ context.Context, _ string) ([]clickhouse.EvalEventRow, error) {
			return nil, fmt.Errorf("clickhouse down")
		},
	}
	h := newEvalHandler(&mockEvalWriter{}, eq)
	r := authed(httptest.NewRequest(http.MethodGet, "/api/v1/evals/eval-9", nil))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusInternalServerError {
		t.Fatalf("want 500, got %d", w.Code)
	}
}

// ── GET /api/v1/retrievals ────────────────────────────────────────────────────

type mockRetrievalQuerier struct {
	listFn func(ctx context.Context, runID string) ([]clickhouse.RetrievalQueryRow, error)
}

func (m *mockRetrievalQuerier) ListRetrievals(ctx context.Context, runID string) ([]clickhouse.RetrievalQueryRow, error) {
	if m.listFn != nil {
		return m.listFn(ctx, runID)
	}
	return []clickhouse.RetrievalQueryRow{}, nil
}

func newRetrievalHandler(rq *mockRetrievalQuerier) http.Handler {
	h := api.NewHandler(&testutil.MockStore{}, &testutil.MockPublisher{}, slog.New(slog.NewTextHandler(io.Discard, nil)), testToken)
	if rq != nil {
		h = h.WithRetrievals(rq)
	}
	return h.Router()
}

func TestListRetrievals_NilReader_Returns503(t *testing.T) {
	// Handler without WithRetrievals → 503.
	h := newRetrievalHandler(nil)
	r := authed(httptest.NewRequest(http.MethodGet, "/api/v1/retrievals", nil))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusServiceUnavailable {
		t.Fatalf("want 503, got %d", w.Code)
	}
}

func TestListRetrievals_HappyPath_ReturnsRows(t *testing.T) {
	now := time.Date(2026, 6, 15, 0, 0, 0, 0, time.UTC)
	rq := &mockRetrievalQuerier{
		listFn: func(_ context.Context, runID string) ([]clickhouse.RetrievalQueryRow, error) {
			return []clickhouse.RetrievalQueryRow{
				{
					TraceID:    "trace-1",
					SpanID:     "span-1",
					RunID:      runID,
					Query:      "what is helix?",
					Retriever:  "qdrant",
					TopK:       10,
					RecallAtK:  1,
					StartTime:  now,
					DurationMs: 42,
				},
				{
					TraceID:    "trace-1",
					SpanID:     "span-2",
					RunID:      runID,
					Query:      "distributed systems",
					Retriever:  "qdrant",
					TopK:       5,
					RecallAtK:  0,
					StartTime:  now,
					DurationMs: 30,
				},
			}, nil
		},
	}
	h := newRetrievalHandler(rq)
	r := authed(httptest.NewRequest(http.MethodGet, "/api/v1/retrievals?run_id=run-xyz", nil))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)

	if w.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", w.Code, w.Body.String())
	}

	var resp []struct {
		TraceID   string `json:"trace_id"`
		SpanID    string `json:"span_id"`
		RunID     string `json:"run_id"`
		Query     string `json:"query"`
		Retriever string `json:"retriever"`
		TopK      uint32 `json:"top_k"`
		RecallAtK uint8  `json:"recall_at_k"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("invalid JSON: %v", err)
	}
	if len(resp) != 2 {
		t.Fatalf("want 2 rows, got %d", len(resp))
	}
	if resp[0].Query != "what is helix?" {
		t.Errorf("want query=%q, got %q", "what is helix?", resp[0].Query)
	}
	if resp[0].RecallAtK != 1 {
		t.Errorf("want recall_at_k=1, got %d", resp[0].RecallAtK)
	}
	if resp[1].TopK != 5 {
		t.Errorf("want top_k=5, got %d", resp[1].TopK)
	}
}

func TestListRetrievals_EmptyRunID_ReturnsAll(t *testing.T) {
	var calledWithRunID string
	rq := &mockRetrievalQuerier{
		listFn: func(_ context.Context, runID string) ([]clickhouse.RetrievalQueryRow, error) {
			calledWithRunID = runID
			return []clickhouse.RetrievalQueryRow{}, nil
		},
	}
	h := newRetrievalHandler(rq)
	// Call without run_id param.
	r := authed(httptest.NewRequest(http.MethodGet, "/api/v1/retrievals", nil))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)

	if w.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", w.Code, w.Body.String())
	}
	if calledWithRunID != "" {
		t.Errorf("want listFn called with empty runID, got %q", calledWithRunID)
	}
}

// ── GET /api/v1/llm-calls ─────────────────────────────────────────────────────

type mockLlmCallQuerier struct {
	listFn func(ctx context.Context, runID string) ([]clickhouse.LlmCallQueryRow, error)
}

func (m *mockLlmCallQuerier) ListLlmCalls(ctx context.Context, runID string) ([]clickhouse.LlmCallQueryRow, error) {
	if m.listFn != nil {
		return m.listFn(ctx, runID)
	}
	return []clickhouse.LlmCallQueryRow{}, nil
}

func newLlmCallHandler(s *testutil.MockStore, lq *mockLlmCallQuerier) http.Handler {
	h := api.NewHandler(s, &testutil.MockPublisher{}, slog.New(slog.NewTextHandler(io.Discard, nil)), testToken)
	if lq != nil {
		h = h.WithLlmCalls(lq)
	}
	return h.Router()
}

func TestListLlmCalls_NilReader_Returns503(t *testing.T) {
	// Handler without WithLlmCalls → 503.
	h := newLlmCallHandler(&testutil.MockStore{}, nil)
	r := authed(httptest.NewRequest(http.MethodGet, "/api/v1/llm-calls", nil))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusServiceUnavailable {
		t.Fatalf("want 503, got %d", w.Code)
	}
}

func TestListLlmCalls_ReturnsTwoRows(t *testing.T) {
	now := time.Date(2026, 6, 15, 0, 0, 0, 0, time.UTC)
	lq := &mockLlmCallQuerier{
		listFn: func(_ context.Context, _ string) ([]clickhouse.LlmCallQueryRow, error) {
			return []clickhouse.LlmCallQueryRow{
				{
					TraceID: "trace-1", SpanID: "span-1", RunID: "run-1",
					Provider: "anthropic", Model: "claude-sonnet-4-20250514",
					PromptTokens: 100, CompletionTokens: 50, TotalTokens: 150,
					CostUSD: 0.001, StartTime: now, DurationMs: 500, Status: "ok",
				},
				{
					TraceID: "trace-1", SpanID: "span-2", RunID: "run-1",
					Provider: "openai", Model: "gpt-4o",
					PromptTokens: 200, CompletionTokens: 80, TotalTokens: 280,
					CostUSD: 0.005, StartTime: now, DurationMs: 800, Status: "ok",
				},
			}, nil
		},
	}
	h := newLlmCallHandler(&testutil.MockStore{}, lq)
	r := authed(httptest.NewRequest(http.MethodGet, "/api/v1/llm-calls", nil))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)

	if w.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", w.Code, w.Body.String())
	}
	var resp []struct {
		Provider string  `json:"provider"`
		Model    string  `json:"model"`
		CostUSD  float64 `json:"cost_usd"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("invalid JSON: %v", err)
	}
	if len(resp) != 2 {
		t.Fatalf("want 2 rows, got %d", len(resp))
	}
	if resp[0].Provider != "anthropic" {
		t.Errorf("want provider=anthropic, got %q", resp[0].Provider)
	}
	if resp[0].Model != "claude-sonnet-4-20250514" {
		t.Errorf("want model=claude-sonnet-4-20250514, got %q", resp[0].Model)
	}
	if resp[0].CostUSD != 0.001 {
		t.Errorf("want cost_usd=0.001, got %f", resp[0].CostUSD)
	}
}

func TestListLlmCalls_PassesRunIdParam(t *testing.T) {
	var calledWithRunID string
	lq := &mockLlmCallQuerier{
		listFn: func(_ context.Context, runID string) ([]clickhouse.LlmCallQueryRow, error) {
			calledWithRunID = runID
			return []clickhouse.LlmCallQueryRow{}, nil
		},
	}
	h := newLlmCallHandler(&testutil.MockStore{}, lq)
	r := authed(httptest.NewRequest(http.MethodGet, "/api/v1/llm-calls?run_id=run-xyz", nil))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)

	if w.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", w.Code, w.Body.String())
	}
	if calledWithRunID != "run-xyz" {
		t.Errorf("want listFn called with run-xyz, got %q", calledWithRunID)
	}
}

// ── finetune-jobs endpoints ───────────────────────────────────────────────────

type mockFinetuneJobStorer struct {
	createFn  func(ctx context.Context, in store.FinetuneJobInput) (store.FinetuneJob, error)
	setRunFn  func(ctx context.Context, jobID, runID string) error
	getFn     func(ctx context.Context, jobID string) (store.FinetuneJob, error)
	listFn    func(ctx context.Context) ([]store.FinetuneJob, error)
}

func (m *mockFinetuneJobStorer) CreateFinetuneJob(ctx context.Context, in store.FinetuneJobInput) (store.FinetuneJob, error) {
	if m.createFn != nil {
		return m.createFn(ctx, in)
	}
	return store.FinetuneJob{ID: "job-001", Status: "pending",
		TrainSplit: in.TrainSplit, EvalSplit: in.EvalSplit, CorpusAlias: in.CorpusAlias}, nil
}

func (m *mockFinetuneJobStorer) SetFinetuneJobRun(ctx context.Context, jobID, runID string) error {
	if m.setRunFn != nil {
		return m.setRunFn(ctx, jobID, runID)
	}
	return nil
}

func (m *mockFinetuneJobStorer) GetFinetuneJob(ctx context.Context, jobID string) (store.FinetuneJob, error) {
	if m.getFn != nil {
		return m.getFn(ctx, jobID)
	}
	return store.FinetuneJob{ID: jobID, Status: "promoted"}, nil
}

func (m *mockFinetuneJobStorer) ListFinetuneJobs(ctx context.Context) ([]store.FinetuneJob, error) {
	if m.listFn != nil {
		return m.listFn(ctx)
	}
	return []store.FinetuneJob{}, nil
}

func newFinetuneHandler(fj *mockFinetuneJobStorer) http.Handler {
	h := api.NewHandler(&testutil.MockStore{}, &testutil.MockPublisher{},
		slog.New(slog.NewTextHandler(io.Discard, nil)), testToken)
	h = h.WithFinetuneJobs(fj)
	return h.Router()
}

func TestCreateFinetuneJob_Returns503_WhenNotConfigured(t *testing.T) {
	h := newHandler(&testutil.MockStore{}, &testutil.MockPublisher{})
	body := strings.NewReader(`{"train_split":"train.jsonl","eval_split":"eval.jsonl"}`)
	r := authed(httptest.NewRequest(http.MethodPost, "/api/v1/finetune-jobs", body))
	r.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusServiceUnavailable {
		t.Fatalf("want 503, got %d", w.Code)
	}
}

func TestCreateFinetuneJob_Returns400_WhenMissingSplit(t *testing.T) {
	h := newFinetuneHandler(&mockFinetuneJobStorer{})
	body := strings.NewReader(`{"train_split":"train.jsonl"}`) // missing eval_split
	r := authed(httptest.NewRequest(http.MethodPost, "/api/v1/finetune-jobs", body))
	r.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusBadRequest {
		t.Fatalf("want 400, got %d", w.Code)
	}
}

func TestCreateFinetuneJob_HappyPath_Returns201(t *testing.T) {
	fj := &mockFinetuneJobStorer{}
	h := newFinetuneHandler(fj)
	body := strings.NewReader(`{"train_split":"train.jsonl","eval_split":"eval.jsonl"}`)
	r := authed(httptest.NewRequest(http.MethodPost, "/api/v1/finetune-jobs", body))
	r.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusCreated {
		t.Fatalf("want 201, got %d: %s", w.Code, w.Body.String())
	}
	var resp store.FinetuneJob
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("invalid JSON: %v", err)
	}
	if resp.ID == "" {
		t.Error("want non-empty job ID")
	}
}

func TestListFinetuneJobs_Returns503_WhenNotConfigured(t *testing.T) {
	h := newHandler(&testutil.MockStore{}, &testutil.MockPublisher{})
	r := authed(httptest.NewRequest(http.MethodGet, "/api/v1/finetune-jobs", nil))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusServiceUnavailable {
		t.Fatalf("want 503, got %d", w.Code)
	}
}

func TestListFinetuneJobs_ReturnsEmptyArray(t *testing.T) {
	h := newFinetuneHandler(&mockFinetuneJobStorer{})
	r := authed(httptest.NewRequest(http.MethodGet, "/api/v1/finetune-jobs", nil))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", w.Code)
	}
	if !strings.Contains(w.Body.String(), "[]") {
		t.Errorf("want empty array, got %s", w.Body.String())
	}
}

func TestGetFinetuneJob_Returns200(t *testing.T) {
	fj := &mockFinetuneJobStorer{
		getFn: func(_ context.Context, jobID string) (store.FinetuneJob, error) {
			f := 10
			return store.FinetuneJob{ID: jobID, Status: "promoted", Failures: &f}, nil
		},
	}
	h := newFinetuneHandler(fj)
	r := authed(httptest.NewRequest(http.MethodGet, "/api/v1/finetune-jobs/job-001", nil))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", w.Code, w.Body.String())
	}
	var resp store.FinetuneJob
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("invalid JSON: %v", err)
	}
	if resp.Status != "promoted" {
		t.Errorf("want status=promoted, got %q", resp.Status)
	}
}
