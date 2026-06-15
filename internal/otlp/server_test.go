package otlp_test

import (
	"context"
	"encoding/hex"
	"io"
	"log/slog"
	"testing"
	"time"

	collectorv1 "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	commonv1 "go.opentelemetry.io/proto/otlp/common/v1"
	resourcev1 "go.opentelemetry.io/proto/otlp/resource/v1"
	tracev1 "go.opentelemetry.io/proto/otlp/trace/v1"

	chwriter "github.com/tomiwaaluko/helix/internal/clickhouse"
	otlpserver "github.com/tomiwaaluko/helix/internal/otlp"
)

// captureSink records every SpanRow written to it.
type captureSink struct {
	rows []chwriter.SpanRow
}

func (c *captureSink) Write(row chwriter.SpanRow) {
	c.rows = append(c.rows, row)
}

func discardLog() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func stringAttr(key, val string) *commonv1.KeyValue {
	return &commonv1.KeyValue{
		Key: key,
		Value: &commonv1.AnyValue{
			Value: &commonv1.AnyValue_StringValue{StringValue: val},
		},
	}
}

// ── Export ────────────────────────────────────────────────────────────────────

func TestExport_TranslatesSpanFields(t *testing.T) {
	sink := &captureSink{}
	srv := otlpserver.NewServer(sink, discardLog())

	traceID := make([]byte, 16)
	for i := range traceID {
		traceID[i] = byte(i + 1)
	}
	spanID := make([]byte, 8)
	for i := range spanID {
		spanID[i] = byte(i + 17)
	}

	startNs := uint64(time.Date(2026, 6, 14, 12, 0, 0, 0, time.UTC).UnixNano())
	endNs := startNs + uint64(500*time.Millisecond)

	req := &collectorv1.ExportTraceServiceRequest{
		ResourceSpans: []*tracev1.ResourceSpans{
			{
				Resource: &resourcev1.Resource{
					Attributes: []*commonv1.KeyValue{
						stringAttr("service.name", "helix-worker"),
					},
				},
				ScopeSpans: []*tracev1.ScopeSpans{
					{
						Spans: []*tracev1.Span{
							{
								TraceId:           traceID,
								SpanId:            spanID,
								Name:              "deep_research",
								Kind:              tracev1.Span_SPAN_KIND_INTERNAL,
								StartTimeUnixNano: startNs,
								EndTimeUnixNano:   endNs,
								Attributes: []*commonv1.KeyValue{
									stringAttr("run_id", "run-abc"),
									stringAttr("task_id", "task-xyz"),
									stringAttr("worker_id", "worker-1"),
									stringAttr("attempt_number", "3"),
								},
								Status: &tracev1.Status{
									Code:    tracev1.Status_STATUS_CODE_OK,
									Message: "success",
								},
							},
						},
					},
				},
			},
		},
	}

	resp, err := srv.Export(context.Background(), req)
	if err != nil {
		t.Fatalf("Export: %v", err)
	}
	if resp == nil {
		t.Fatal("expected non-nil response")
	}
	if len(sink.rows) != 1 {
		t.Fatalf("want 1 row, got %d", len(sink.rows))
	}

	row := sink.rows[0]

	checks := []struct {
		field string
		want  string
		got   string
	}{
		{"Name", "deep_research", row.Name},
		{"ServiceName", "helix-worker", row.ServiceName},
		{"RunID", "run-abc", row.RunID},
		{"TaskID", "task-xyz", row.TaskID},
		{"WorkerID", "worker-1", row.WorkerID},
		{"Kind", "internal", row.Kind},
		{"Status", "ok", row.Status},
		{"StatusMessage", "success", row.StatusMessage},
		{"TraceID", hex.EncodeToString(traceID), row.TraceID},
		{"SpanID", hex.EncodeToString(spanID), row.SpanID},
	}
	for _, c := range checks {
		if c.got != c.want {
			t.Errorf("%s: want %q, got %q", c.field, c.want, c.got)
		}
	}

	if row.AttemptNumber != 3 {
		t.Errorf("AttemptNumber: want 3, got %d", row.AttemptNumber)
	}
	if row.StartTime.IsZero() {
		t.Error("StartTime must not be zero")
	}
}

func TestExport_EmptyRequest_ReturnsNoError(t *testing.T) {
	sink := &captureSink{}
	srv := otlpserver.NewServer(sink, discardLog())

	resp, err := srv.Export(context.Background(), &collectorv1.ExportTraceServiceRequest{})
	if err != nil {
		t.Fatalf("Export: %v", err)
	}
	if resp == nil {
		t.Fatal("expected non-nil response")
	}
	if len(sink.rows) != 0 {
		t.Errorf("want 0 rows, got %d", len(sink.rows))
	}
}

func TestExport_ErrorStatus_MapsToErrorString(t *testing.T) {
	sink := &captureSink{}
	srv := otlpserver.NewServer(sink, discardLog())

	req := &collectorv1.ExportTraceServiceRequest{
		ResourceSpans: []*tracev1.ResourceSpans{
			{
				ScopeSpans: []*tracev1.ScopeSpans{
					{
						Spans: []*tracev1.Span{
							{
								Status: &tracev1.Status{Code: tracev1.Status_STATUS_CODE_ERROR},
							},
						},
					},
				},
			},
		},
	}

	_, err := srv.Export(context.Background(), req)
	if err != nil {
		t.Fatalf("Export: %v", err)
	}
	if len(sink.rows) != 1 {
		t.Fatalf("want 1 row, got %d", len(sink.rows))
	}
	if got := sink.rows[0].Status; got != "error" {
		t.Errorf("Status: want %q, got %q", "error", got)
	}
}

func TestExport_NilStatus_DefaultsToUnset(t *testing.T) {
	sink := &captureSink{}
	srv := otlpserver.NewServer(sink, discardLog())

	req := &collectorv1.ExportTraceServiceRequest{
		ResourceSpans: []*tracev1.ResourceSpans{
			{
				ScopeSpans: []*tracev1.ScopeSpans{
					{Spans: []*tracev1.Span{{Name: "no-status"}}},
				},
			},
		},
	}

	_, _ = srv.Export(context.Background(), req)
	if len(sink.rows) == 1 {
		if got := sink.rows[0].Status; got != "unset" {
			t.Errorf("Status with nil Status field: want %q, got %q", "unset", got)
		}
	}
}

// ── retrieval sink tests ──────────────────────────────────────────────────────

// captureRetrievalSink records every RetrievalRow written to it.
type captureRetrievalSink struct {
	rows []chwriter.RetrievalRow
}

func (c *captureRetrievalSink) Write(row chwriter.RetrievalRow) {
	c.rows = append(c.rows, row)
}

func retrievalSpanReq(name string, attrs []*commonv1.KeyValue) *collectorv1.ExportTraceServiceRequest {
	return &collectorv1.ExportTraceServiceRequest{
		ResourceSpans: []*tracev1.ResourceSpans{
			{
				ScopeSpans: []*tracev1.ScopeSpans{
					{
						Spans: []*tracev1.Span{
							{
								Name:              name,
								StartTimeUnixNano: uint64(time.Date(2026, 6, 15, 0, 0, 0, 0, time.UTC).UnixNano()),
								EndTimeUnixNano:   uint64(time.Date(2026, 6, 15, 0, 0, 0, 0, time.UTC).UnixNano()) + 5_000_000,
								Attributes:        attrs,
							},
						},
					},
				},
			},
		},
	}
}

func TestExport_RetrievalSpan_CallsRetrievalSink(t *testing.T) {
	sink := &captureSink{}
	rsink := &captureRetrievalSink{}
	srv := otlpserver.NewServerWithRetrieval(sink, rsink, discardLog())

	attrs := []*commonv1.KeyValue{
		stringAttr("kind", "retrieval"),
		stringAttr("query", "foo"),
		stringAttr("retriever", "qdrant"),
		stringAttr("top_k", "3"),
		stringAttr("results", `[{"doc_id":"d1","score":0.9}]`),
		stringAttr("run_id", "run-abc"),
	}
	req := retrievalSpanReq("retrieve", attrs)

	_, err := srv.Export(context.Background(), req)
	if err != nil {
		t.Fatalf("Export: %v", err)
	}
	if len(rsink.rows) != 1 {
		t.Fatalf("want 1 retrieval row, got %d", len(rsink.rows))
	}
	row := rsink.rows[0]
	if row.Query != "foo" {
		t.Errorf("Query: want %q, got %q", "foo", row.Query)
	}
	if len(row.Results) != 1 {
		t.Fatalf("Results: want 1 entry, got %d", len(row.Results))
	}
	if row.Results[0].PassageID != "d1" {
		t.Errorf("Results[0].PassageID: want %q, got %q", "d1", row.Results[0].PassageID)
	}
	if row.TopK != 3 {
		t.Errorf("TopK: want 3, got %d", row.TopK)
	}
	if row.RunID != "run-abc" {
		t.Errorf("RunID: want %q, got %q", "run-abc", row.RunID)
	}
}

func TestExport_NonRetrievalSpan_DoesNotCallRetrievalSink(t *testing.T) {
	sink := &captureSink{}
	rsink := &captureRetrievalSink{}
	srv := otlpserver.NewServerWithRetrieval(sink, rsink, discardLog())

	req := retrievalSpanReq("deep_research", []*commonv1.KeyValue{
		stringAttr("run_id", "run-1"),
	})

	_, err := srv.Export(context.Background(), req)
	if err != nil {
		t.Fatalf("Export: %v", err)
	}
	if len(rsink.rows) != 0 {
		t.Errorf("want 0 retrieval rows, got %d", len(rsink.rows))
	}
	// span still written to main sink
	if len(sink.rows) != 1 {
		t.Errorf("want 1 span row, got %d", len(sink.rows))
	}
}

func TestExport_RetrievalSinkNil_DoesNotPanic(t *testing.T) {
	sink := &captureSink{}
	// Use NewServer (not NewServerWithRetrieval) — retrievalSink is nil.
	srv := otlpserver.NewServer(sink, discardLog())

	attrs := []*commonv1.KeyValue{
		stringAttr("kind", "retrieval"),
		stringAttr("query", "foo"),
	}
	req := retrievalSpanReq("retrieve", attrs)

	// Must not panic.
	_, err := srv.Export(context.Background(), req)
	if err != nil {
		t.Fatalf("Export: %v", err)
	}
	if len(sink.rows) != 1 {
		t.Errorf("want 1 span row, got %d", len(sink.rows))
	}
}
