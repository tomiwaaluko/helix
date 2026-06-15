// Package api implements the Helix HTTP REST API.
package api

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"

	helixv1 "github.com/tomiwaaluko/helix/gen/go/helix/v1"
	"github.com/tomiwaaluko/helix/internal/clickhouse"
	"github.com/tomiwaaluko/helix/internal/dispatch"
	"github.com/tomiwaaluko/helix/internal/store"
)

// spanQuerier reads spans from ClickHouse by trace ID.
type spanQuerier interface {
	GetTraceSpans(ctx context.Context, traceID string) ([]clickhouse.SpanRow, error)
}

// attrPresigner rewrites s3:// URIs in span attributes to presigned GET URLs.
type attrPresigner interface {
	PresignAttrs(attrs map[string]string, expiry time.Duration) (map[string]string, error)
}

// evalWriter records per-example eval scores to ClickHouse synchronously.
type evalWriter interface {
	Record(ctx context.Context, rows []clickhouse.EvalEventRow) error
}

// evalQuerier reads eval summaries and per-example events from ClickHouse.
type evalQuerier interface {
	ListEvals(ctx context.Context) ([]clickhouse.EvalSummaryRow, []clickhouse.ScorerMeanRow, error)
	GetEval(ctx context.Context, evalID string) ([]clickhouse.EvalEventRow, error)
}

// retrievalQuerier reads retrieval rows from ClickHouse.
type retrievalQuerier interface {
	ListRetrievals(ctx context.Context, runID string) ([]clickhouse.RetrievalQueryRow, error)
}

// Handler holds the dependencies for the REST API.
type Handler struct {
	store      store.Store
	nats       dispatch.Publisher
	logger     *slog.Logger
	apiToken   string
	spanReader      spanQuerier      // nil → trace endpoint returns 503
	presigner       attrPresigner   // nil → blob URIs are omitted (not presigned)
	evalWriter      evalWriter      // nil → eval write endpoint returns 503
	evalReader      evalQuerier     // nil → eval read endpoints return 503
	retrievalReader retrievalQuerier // nil → retrievals endpoint returns 503
}

// NewHandler constructs a Handler with the given dependencies.
func NewHandler(s store.Store, d dispatch.Publisher, log *slog.Logger, token string) *Handler {
	return &Handler{
		store:    s,
		nats:     d,
		logger:   log,
		apiToken: token,
	}
}

// WithTrace adds ClickHouse span reading and optional MinIO presigning to the handler.
// Calling this enables GET /api/v1/runs/{run_id}/trace.
func (h *Handler) WithTrace(sr spanQuerier, p attrPresigner) *Handler {
	h.spanReader = sr
	h.presigner = p
	return h
}

// WithEvals adds ClickHouse eval read/write to the handler. Calling this enables
// POST /api/v1/evals/{eval_id}/events, GET /api/v1/evals, and GET /api/v1/evals/{eval_id}.
func (h *Handler) WithEvals(w evalWriter, r evalQuerier) *Handler {
	h.evalWriter = w
	h.evalReader = r
	return h
}

// WithRetrievals adds ClickHouse retrieval reading to the handler.
// Calling this enables GET /api/v1/retrievals.
func (h *Handler) WithRetrievals(r retrievalQuerier) *Handler {
	h.retrievalReader = r
	return h
}

// Router returns an http.Handler with all API routes registered.
func (h *Handler) Router() http.Handler {
	r := chi.NewRouter()
	r.Use(middleware.Recoverer)
	r.Use(h.bearerAuth)

	r.Post("/api/v1/runs", h.createRun)
	r.Get("/api/v1/runs", h.listRuns)
	r.Get("/api/v1/runs/{run_id}", h.getRun)
	r.Post("/api/v1/runs/{run_id}/cancel", h.cancelRun)
	r.Get("/api/v1/runs/{run_id}/trace", h.getTrace)

	r.Post("/api/v1/evals/{eval_id}/events", h.recordEvalEvents)
	r.Get("/api/v1/evals", h.listEvals)
	r.Get("/api/v1/evals/{eval_id}", h.getEval)

	r.Get("/api/v1/retrievals", h.listRetrievals)

	return r
}

// bearerAuth validates the Authorization: Bearer <token> header.
func (h *Handler) bearerAuth(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		authHeader := r.Header.Get("Authorization")
		if authHeader == "" {
			writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "authorization header required"})
			return
		}

		const prefix = "Bearer "
		if !strings.HasPrefix(authHeader, prefix) {
			writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "authorization must use Bearer scheme"})
			return
		}

		token := strings.TrimPrefix(authHeader, prefix)
		if token != h.apiToken {
			writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "invalid token"})
			return
		}

		next.ServeHTTP(w, r)
	})
}

// createRunBody is the request body for POST /api/v1/runs.
type createRunBody struct {
	WorkflowName string          `json:"workflow_name"`
	Input        json.RawMessage `json:"input"`
	SubmittedBy  string          `json:"submitted_by"`
}

// createRun handles POST /api/v1/runs.
func (h *Handler) createRun(w http.ResponseWriter, r *http.Request) {
	var body createRunBody
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON body: " + err.Error()})
		return
	}

	if body.WorkflowName == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "workflow_name is required"})
		return
	}

	inputBytes := []byte(body.Input)
	if len(inputBytes) == 0 {
		inputBytes = []byte("{}")
	}

	in := store.CreateRunInput{
		WorkflowName: body.WorkflowName,
		Input:        inputBytes,
		SubmittedBy:  body.SubmittedBy,
	}

	run, task, err := h.store.CreateRun(r.Context(), in)
	if err != nil {
		h.logger.ErrorContext(r.Context(), "createRun: store.CreateRun failed",
			"workflow_name", body.WorkflowName,
			"error", err,
		)
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "failed to create run"})
		return
	}

	env := &helixv1.TaskEnvelope{
		TaskId:          task.ID,
		RunId:           run.ID,
		WorkflowName:    body.WorkflowName,
		WorkflowVersion: 1,
		NodeId:          "root",
		InputJson:       inputBytes,
		AttemptNumber:   1,
		TraceId:         run.TraceID,
		Deadline:        time.Now().Add(5 * time.Minute).Format(time.RFC3339),
	}

	if err := h.nats.PublishTaskEnvelope(r.Context(), body.WorkflowName, env); err != nil {
		h.logger.ErrorContext(r.Context(), "createRun: dispatch.PublishTaskEnvelope failed",
			"run_id", run.ID,
			"task_id", task.ID,
			"error", err,
		)
		// Run is created but not dispatched; surface the error.
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "run created but dispatch failed"})
		return
	}

	h.logger.InfoContext(r.Context(), "run created and dispatched",
		"run_id", run.ID,
		"task_id", task.ID,
		"workflow_name", body.WorkflowName,
		"trace_id", run.TraceID,
	)

	writeJSON(w, http.StatusCreated, run)
}

// listRuns handles GET /api/v1/runs.
func (h *Handler) listRuns(w http.ResponseWriter, r *http.Request) {
	statusFilter := r.URL.Query().Get("status")

	runs, err := h.store.ListRuns(r.Context(), statusFilter)
	if err != nil {
		h.logger.ErrorContext(r.Context(), "listRuns: store.ListRuns failed",
			"status_filter", statusFilter,
			"error", err,
		)
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "failed to list runs"})
		return
	}

	// Return an empty array rather than null.
	if runs == nil {
		runs = []store.Run{}
	}

	writeJSON(w, http.StatusOK, runs)
}

// runDetailResponse is the GET /api/v1/runs/{run_id} body: a run plus its task tree.
type runDetailResponse struct {
	store.Run
	Tasks []store.Task `json:"tasks"`
}

// getRun handles GET /api/v1/runs/{run_id}.
func (h *Handler) getRun(w http.ResponseWriter, r *http.Request) {
	runID := chi.URLParam(r, "run_id")

	run, err := h.store.GetRun(r.Context(), runID)
	if err != nil {
		h.logger.ErrorContext(r.Context(), "getRun: store.GetRun failed",
			"run_id", runID,
			"error", err,
		)
		// Treat "not found" as 404 when the error message matches.
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "run not found"})
		return
	}

	tasks, err := h.store.ListTasksForRun(r.Context(), runID)
	if err != nil {
		h.logger.ErrorContext(r.Context(), "getRun: store.ListTasksForRun failed",
			"run_id", runID,
			"error", err,
		)
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "failed to load run tasks"})
		return
	}
	if tasks == nil {
		tasks = []store.Task{}
	}

	writeJSON(w, http.StatusOK, runDetailResponse{Run: run, Tasks: tasks})
}

// cancelRun handles POST /api/v1/runs/{run_id}/cancel.
func (h *Handler) cancelRun(w http.ResponseWriter, r *http.Request) {
	runID := chi.URLParam(r, "run_id")

	if err := h.store.CancelRun(r.Context(), runID); err != nil {
		h.logger.ErrorContext(r.Context(), "cancelRun: store.CancelRun failed",
			"run_id", runID,
			"error", err,
		)
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "failed to cancel run"})
		return
	}

	h.logger.InfoContext(r.Context(), "run cancelled", "run_id", runID)
	writeJSON(w, http.StatusOK, map[string]string{"status": "cancelled", "run_id": runID})
}

// traceResponse is the GET /api/v1/runs/{run_id}/trace response body.
type traceResponse struct {
	TraceID string         `json:"trace_id"`
	Spans   []spanResponse `json:"spans"`
}

// spanResponse is a single span within a traceResponse.
type spanResponse struct {
	SpanID        string            `json:"span_id"`
	ParentSpanID  string            `json:"parent_span_id,omitempty"`
	RunID         string            `json:"run_id,omitempty"`
	TaskID        string            `json:"task_id,omitempty"`
	AttemptNumber uint32            `json:"attempt_number,omitempty"`
	Name          string            `json:"name"`
	Kind          string            `json:"kind"`
	StartTime     time.Time         `json:"start_time"`
	EndTime       time.Time         `json:"end_time"`
	DurationMS    uint32            `json:"duration_ms"`
	Status        string            `json:"status,omitempty"`
	StatusMessage string            `json:"status_message,omitempty"`
	ServiceName   string            `json:"service_name,omitempty"`
	Attributes    map[string]string `json:"attributes,omitempty"`
}

// getTrace handles GET /api/v1/runs/{run_id}/trace.
// Returns 503 when ClickHouse is not configured (CLICKHOUSE_URL unset).
func (h *Handler) getTrace(w http.ResponseWriter, r *http.Request) {
	if h.spanReader == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{
			"error": "trace service not configured: CLICKHOUSE_URL is not set",
		})
		return
	}

	runID := chi.URLParam(r, "run_id")
	run, err := h.store.GetRun(r.Context(), runID)
	if err != nil {
		h.logger.ErrorContext(r.Context(), "getTrace: store.GetRun failed", "run_id", runID, "error", err)
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "run not found"})
		return
	}

	spans, err := h.spanReader.GetTraceSpans(r.Context(), run.TraceID)
	if err != nil {
		h.logger.ErrorContext(r.Context(), "getTrace: GetTraceSpans failed",
			"run_id", runID,
			"trace_id", run.TraceID,
			"error", err,
		)
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "failed to load trace spans"})
		return
	}

	const presignExpiry = time.Hour
	resp := traceResponse{
		TraceID: run.TraceID,
		Spans:   make([]spanResponse, 0, len(spans)),
	}

	for _, s := range spans {
		attrs := s.Attributes
		if h.presigner != nil && len(attrs) > 0 {
			attrs, err = h.presigner.PresignAttrs(attrs, presignExpiry)
			if err != nil {
				h.logger.ErrorContext(r.Context(), "getTrace: PresignAttrs failed",
					"span_id", s.SpanID,
					"error", err,
				)
				writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "failed to presign blob URLs"})
				return
			}
		}
		durMS := uint32(s.EndTime.Sub(s.StartTime).Milliseconds())
		resp.Spans = append(resp.Spans, spanResponse{
			SpanID:        s.SpanID,
			ParentSpanID:  s.ParentSpanID,
			RunID:         s.RunID,
			TaskID:        s.TaskID,
			AttemptNumber: s.AttemptNumber,
			Name:          s.Name,
			Kind:          s.Kind,
			StartTime:     s.StartTime,
			EndTime:       s.EndTime,
			DurationMS:    durMS,
			Status:        s.Status,
			StatusMessage: s.StatusMessage,
			ServiceName:   s.ServiceName,
			Attributes:    attrs,
		})
	}

	writeJSON(w, http.StatusOK, resp)
}

// ── eval endpoints ────────────────────────────────────────────────────────────

// evalEventBody is one scored result in a recordEvalEvents request.
type evalEventBody struct {
	ExampleID string         `json:"example_id"`
	RunID     string         `json:"run_id"`
	Scorer    string         `json:"scorer"`
	Score     float64        `json:"score"`
	Passed    bool           `json:"passed"`
	Details   map[string]any `json:"details,omitempty"`
}

// recordEvalEventsRequest is the POST /api/v1/evals/{eval_id}/events body.
type recordEvalEventsRequest struct {
	Events []evalEventBody `json:"events"`
}

// scorerMean is one scorer's mean score within an eval summary.
type scorerMean struct {
	Scorer string  `json:"scorer"`
	Mean   float64 `json:"mean"`
	N      uint64  `json:"n"`
}

// evalSummaryResponse is one row of GET /api/v1/evals.
type evalSummaryResponse struct {
	EvalID     string       `json:"eval_id"`
	Examples   uint64       `json:"examples"`
	StartedAt  time.Time    `json:"started_at"`
	FinishedAt time.Time    `json:"finished_at"`
	Scorers    []scorerMean `json:"scorers"`
}

// evalEventResponse is one row of GET /api/v1/evals/{eval_id}.
type evalEventResponse struct {
	EvalID    string    `json:"eval_id"`
	ExampleID string    `json:"example_id"`
	RunID     string    `json:"run_id,omitempty"`
	Scorer    string    `json:"scorer"`
	Score     float64   `json:"score"`
	Passed    bool      `json:"passed"`
	Details   string    `json:"details,omitempty"`
	Timestamp time.Time `json:"timestamp"`
}

// recordEvalEvents handles POST /api/v1/evals/{eval_id}/events.
// Returns 503 when ClickHouse is not configured (CLICKHOUSE_URL unset).
func (h *Handler) recordEvalEvents(w http.ResponseWriter, r *http.Request) {
	if h.evalWriter == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{
			"error": "eval service not configured: CLICKHOUSE_URL is not set",
		})
		return
	}

	evalID := chi.URLParam(r, "eval_id")

	var body recordEvalEventsRequest
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON body: " + err.Error()})
		return
	}
	if len(body.Events) == 0 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "events must be a non-empty array"})
		return
	}

	now := time.Now().UTC()
	rows := make([]clickhouse.EvalEventRow, 0, len(body.Events))
	for i, e := range body.Events {
		if e.ExampleID == "" || e.Scorer == "" {
			writeJSON(w, http.StatusBadRequest, map[string]string{
				"error": "each event requires example_id and scorer",
			})
			return
		}
		if e.Score < 0 || e.Score > 1 {
			writeJSON(w, http.StatusBadRequest, map[string]string{
				"error": "score must be within [0, 1]",
			})
			return
		}

		var details string
		if len(e.Details) > 0 {
			b, err := json.Marshal(e.Details)
			if err != nil {
				h.logger.ErrorContext(r.Context(), "recordEvalEvents: marshal details failed",
					"eval_id", evalID, "index", i, "error", err)
				writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid details object"})
				return
			}
			details = string(b)
		}

		var passed uint8
		if e.Passed {
			passed = 1
		}
		rows = append(rows, clickhouse.EvalEventRow{
			EvalID:    evalID,
			ExampleID: e.ExampleID,
			RunID:     e.RunID,
			Scorer:    e.Scorer,
			Score:     e.Score,
			Passed:    passed,
			Details:   details,
			Timestamp: now,
		})
	}

	if err := h.evalWriter.Record(r.Context(), rows); err != nil {
		h.logger.ErrorContext(r.Context(), "recordEvalEvents: Record failed",
			"eval_id", evalID, "rows", len(rows), "error", err)
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "failed to record eval events"})
		return
	}

	w.WriteHeader(http.StatusNoContent)
}

// listEvals handles GET /api/v1/evals.
func (h *Handler) listEvals(w http.ResponseWriter, r *http.Request) {
	if h.evalReader == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{
			"error": "eval service not configured: CLICKHOUSE_URL is not set",
		})
		return
	}

	summaries, means, err := h.evalReader.ListEvals(r.Context())
	if err != nil {
		h.logger.ErrorContext(r.Context(), "listEvals: ListEvals failed", "error", err)
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "failed to list evals"})
		return
	}

	// Group scorer means by eval_id.
	byEval := make(map[string][]scorerMean, len(summaries))
	for _, m := range means {
		byEval[m.EvalID] = append(byEval[m.EvalID], scorerMean{Scorer: m.Scorer, Mean: m.Mean, N: m.N})
	}

	resp := make([]evalSummaryResponse, 0, len(summaries))
	for _, s := range summaries {
		scorers := byEval[s.EvalID]
		if scorers == nil {
			scorers = []scorerMean{}
		}
		resp = append(resp, evalSummaryResponse{
			EvalID:     s.EvalID,
			Examples:   s.Examples,
			StartedAt:  s.StartedAt,
			FinishedAt: s.FinishedAt,
			Scorers:    scorers,
		})
	}

	writeJSON(w, http.StatusOK, resp)
}

// getEval handles GET /api/v1/evals/{eval_id}.
func (h *Handler) getEval(w http.ResponseWriter, r *http.Request) {
	if h.evalReader == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{
			"error": "eval service not configured: CLICKHOUSE_URL is not set",
		})
		return
	}

	evalID := chi.URLParam(r, "eval_id")
	events, err := h.evalReader.GetEval(r.Context(), evalID)
	if err != nil {
		h.logger.ErrorContext(r.Context(), "getEval: GetEval failed", "eval_id", evalID, "error", err)
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "failed to load eval"})
		return
	}
	if len(events) == 0 {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "eval not found"})
		return
	}

	resp := make([]evalEventResponse, 0, len(events))
	for _, e := range events {
		resp = append(resp, evalEventResponse{
			EvalID:    e.EvalID,
			ExampleID: e.ExampleID,
			RunID:     e.RunID,
			Scorer:    e.Scorer,
			Score:     e.Score,
			Passed:    e.Passed == 1,
			Details:   e.Details,
			Timestamp: e.Timestamp,
		})
	}

	writeJSON(w, http.StatusOK, resp)
}

// ── retrievals endpoint ───────────────────────────────────────────────────────

// retrievalRowResponse is one row of GET /api/v1/retrievals.
type retrievalRowResponse struct {
	TraceID    string    `json:"trace_id"`
	SpanID     string    `json:"span_id"`
	RunID      string    `json:"run_id"`
	Query      string    `json:"query"`
	Retriever  string    `json:"retriever"`
	TopK       uint32    `json:"top_k"`
	RecallAtK  uint8     `json:"recall_at_k"`
	StartTime  time.Time `json:"start_time"`
	DurationMs uint32    `json:"duration_ms"`
}

// listRetrievals handles GET /api/v1/retrievals.
// Returns 503 when ClickHouse is not configured (CLICKHOUSE_URL unset).
func (h *Handler) listRetrievals(w http.ResponseWriter, r *http.Request) {
	if h.retrievalReader == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{
			"error": "retrieval service not configured: CLICKHOUSE_URL is not set",
		})
		return
	}

	runID := r.URL.Query().Get("run_id")

	rows, err := h.retrievalReader.ListRetrievals(r.Context(), runID)
	if err != nil {
		h.logger.ErrorContext(r.Context(), "listRetrievals: ListRetrievals failed",
			"run_id", runID, "error", err)
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "failed to list retrievals"})
		return
	}

	resp := make([]retrievalRowResponse, 0, len(rows))
	for _, row := range rows {
		resp = append(resp, retrievalRowResponse{
			TraceID:    row.TraceID,
			SpanID:     row.SpanID,
			RunID:      row.RunID,
			Query:      row.Query,
			Retriever:  row.Retriever,
			TopK:       row.TopK,
			RecallAtK:  row.RecallAtK,
			StartTime:  row.StartTime,
			DurationMs: row.DurationMs,
		})
	}

	writeJSON(w, http.StatusOK, resp)
}

// writeJSON encodes v as JSON and writes it to w with the given status code.
func writeJSON(w http.ResponseWriter, code int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		// At this point the header is already sent; nothing useful we can do.
		_ = err
	}
}
