//go:build integration

// Integration tests for ClickHouse writers and readers.
//
// Requires CLICKHOUSE_URL to be set and a running ClickHouse instance
// (start with `make dev`). Run with:
//
//	CLICKHOUSE_URL=clickhouse://localhost:9000?database=default \
//	  go test -tags integration ./internal/clickhouse/... -v
package clickhouse_test

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"os"
	"testing"
	"time"

	"github.com/ClickHouse/clickhouse-go/v2/lib/driver"

	ch "github.com/tomiwaaluko/helix/internal/clickhouse"
)

// openTestConn opens a real ClickHouse connection and runs DDL.
// The test is skipped when CLICKHOUSE_URL is not set.
func openTestConn(t *testing.T) driver.Conn {
	t.Helper()
	dsn := os.Getenv("CLICKHOUSE_URL")
	if dsn == "" {
		t.Skip("CLICKHOUSE_URL not set; start the dev stack with `make dev`")
	}
	ctx := context.Background()
	conn, err := ch.Open(ctx, dsn)
	if err != nil {
		t.Fatalf("ch.Open: %v", err)
	}
	if err := ch.RunDDL(ctx, conn); err != nil {
		conn.Close() //nolint:errcheck
		t.Fatalf("ch.RunDDL: %v", err)
	}
	t.Cleanup(func() { conn.Close() }) //nolint:errcheck
	return conn
}

func silentLog() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// runID returns a unique run ID for the test to avoid cross-test row pollution.
func runID(prefix string) string {
	return fmt.Sprintf("itest-%s-%d", prefix, time.Now().UnixNano())
}

// ── RetrievalWriter ───────────────────────────────────────────────────────────

func TestRetrievalWriter_RoundTrip(t *testing.T) {
	conn := openTestConn(t)
	ctx := context.Background()
	rid := runID("ret")

	w := ch.NewRetrievalWriter(conn, silentLog())
	w.Start(context.Background())
	w.Write(ch.RetrievalRow{
		TraceID:    "trace-itest",
		SpanID:     "span-itest-ret",
		RunID:      rid,
		Query:      "integration test query?",
		Retriever:  "hybrid+reranked",
		TopK:       10,
		Results:    []ch.ResultTuple{{PassageID: "doc-1", Score: 0.9, Rank: 1}},
		StartTime:  time.Now().UTC(),
		DurationMs: 42,
	})
	w.Stop() // closes channel → drains → flushes to ClickHouse

	r := ch.NewRetrievalReader(conn)
	rows, err := r.ListRetrievals(ctx, rid)
	if err != nil {
		t.Fatalf("ListRetrievals: %v", err)
	}
	if len(rows) != 1 {
		t.Fatalf("got %d rows, want 1", len(rows))
	}
	if got := rows[0].Query; got != "integration test query?" {
		t.Errorf("Query: got %q, want %q", got, "integration test query?")
	}
	if got := rows[0].Retriever; got != "hybrid+reranked" {
		t.Errorf("Retriever: got %q, want %q", got, "hybrid+reranked")
	}
	if got := rows[0].TopK; got != 10 {
		t.Errorf("TopK: got %d, want 10", got)
	}
}

func TestRetrievalReader_EmptyRun(t *testing.T) {
	conn := openTestConn(t)
	ctx := context.Background()

	r := ch.NewRetrievalReader(conn)
	rows, err := r.ListRetrievals(ctx, "nonexistent-run-id")
	if err != nil {
		t.Fatalf("ListRetrievals: %v", err)
	}
	if len(rows) != 0 {
		t.Errorf("got %d rows, want 0", len(rows))
	}
}

// ── LlmCallWriter ─────────────────────────────────────────────────────────────

func TestLlmCallWriter_RoundTrip(t *testing.T) {
	conn := openTestConn(t)
	ctx := context.Background()
	rid := runID("llm")

	w := ch.NewLlmCallWriter(conn, silentLog())
	w.Start(context.Background())
	w.Write(ch.LlmCallRow{
		TraceID:          "trace-itest",
		SpanID:           "span-itest-llm",
		RunID:            rid,
		Provider:         "anthropic",
		Model:            "claude-sonnet-4-20250514",
		PromptTokens:     512,
		CompletionTokens: 128,
		TotalTokens:      640,
		CostUSD:          0.003,
		StartTime:        time.Now().UTC(),
		DurationMs:       1500,
		Status:           "ok",
	})
	w.Stop()

	r := ch.NewLlmCallReader(conn)
	rows, err := r.ListLlmCalls(ctx, rid)
	if err != nil {
		t.Fatalf("ListLlmCalls: %v", err)
	}
	if len(rows) != 1 {
		t.Fatalf("got %d rows, want 1", len(rows))
	}
	got := rows[0]
	if got.Model != "claude-sonnet-4-20250514" {
		t.Errorf("Model: got %q, want %q", got.Model, "claude-sonnet-4-20250514")
	}
	if got.Provider != "anthropic" {
		t.Errorf("Provider: got %q, want %q", got.Provider, "anthropic")
	}
	if got.PromptTokens != 512 {
		t.Errorf("PromptTokens: got %d, want 512", got.PromptTokens)
	}
	if got.TotalTokens != 640 {
		t.Errorf("TotalTokens: got %d, want 640", got.TotalTokens)
	}
}

func TestLlmCallReader_EmptyRun(t *testing.T) {
	conn := openTestConn(t)
	ctx := context.Background()

	r := ch.NewLlmCallReader(conn)
	rows, err := r.ListLlmCalls(ctx, "nonexistent-run-id")
	if err != nil {
		t.Fatalf("ListLlmCalls: %v", err)
	}
	if len(rows) != 0 {
		t.Errorf("got %d rows, want 0", len(rows))
	}
}

// ── EvalWriter ────────────────────────────────────────────────────────────────

func TestEvalWriter_RoundTrip(t *testing.T) {
	conn := openTestConn(t)
	ctx := context.Background()
	evalID := runID("eval")
	now := time.Now().UTC().Round(time.Millisecond)

	w := ch.NewEvalWriter(conn)
	events := []ch.EvalEventRow{
		{
			EvalID:    evalID,
			ExampleID: "ex-001",
			RunID:     "run-itest",
			Scorer:    "answer_f1",
			Score:     0.85,
			Passed:    1,
			Details:   `{"note":"integration test"}`,
			Timestamp: now,
		},
		{
			EvalID:    evalID,
			ExampleID: "ex-002",
			RunID:     "run-itest",
			Scorer:    "answer_f1",
			Score:     0.60,
			Passed:    0,
			Details:   "",
			Timestamp: now,
		},
	}
	if err := w.Record(ctx, events); err != nil {
		t.Fatalf("Record: %v", err)
	}

	// Read back individual events.
	reader := ch.NewEvalReader(conn)
	got, err := reader.GetEval(ctx, evalID)
	if err != nil {
		t.Fatalf("GetEval: %v", err)
	}
	if len(got) != 2 {
		t.Fatalf("GetEval: got %d events, want 2", len(got))
	}
	if got[0].ExampleID != "ex-001" {
		t.Errorf("ExampleID[0]: got %q, want %q", got[0].ExampleID, "ex-001")
	}
	if got[1].Score != 0.60 {
		t.Errorf("Score[1]: got %f, want 0.60", got[1].Score)
	}

	// Verify the eval appears in ListEvals summaries.
	summaries, means, err := reader.ListEvals(ctx)
	if err != nil {
		t.Fatalf("ListEvals: %v", err)
	}
	var found bool
	for _, s := range summaries {
		if s.EvalID == evalID {
			found = true
			if s.Examples != 2 {
				t.Errorf("Examples: got %d, want 2", s.Examples)
			}
		}
	}
	if !found {
		t.Errorf("eval %q not found in ListEvals summaries", evalID)
	}
	for _, m := range means {
		if m.EvalID == evalID && m.Scorer == "answer_f1" {
			wantMean := (0.85 + 0.60) / 2
			if diff := m.Mean - wantMean; diff > 0.001 || diff < -0.001 {
				t.Errorf("mean answer_f1: got %.4f, want %.4f", m.Mean, wantMean)
			}
		}
	}
}

// ── BatchWriter (spans) ───────────────────────────────────────────────────────

func TestBatchWriter_RoundTrip(t *testing.T) {
	conn := openTestConn(t)
	ctx := context.Background()
	traceID := runID("trace")
	now := time.Now().UTC()

	w := ch.NewBatchWriter(conn, silentLog())
	w.Start(context.Background())
	w.Write(ch.SpanRow{
		TraceID:   traceID,
		SpanID:    "span-itest-batch",
		RunID:     "run-itest",
		Name:      "test_span",
		Kind:      "internal",
		StartTime: now,
		EndTime:   now.Add(10 * time.Millisecond),
		Status:    "ok",
		Attributes: map[string]string{
			"test": "integration",
		},
	})
	w.Stop()

	r := ch.NewSpanReader(conn)
	spans, err := r.GetTraceSpans(ctx, traceID)
	if err != nil {
		t.Fatalf("GetTraceSpans: %v", err)
	}
	if len(spans) != 1 {
		t.Fatalf("got %d spans, want 1", len(spans))
	}
	if got := spans[0].Name; got != "test_span" {
		t.Errorf("Name: got %q, want %q", got, "test_span")
	}
	if got := spans[0].Attributes["test"]; got != "integration" {
		t.Errorf("Attributes[test]: got %q, want %q", got, "integration")
	}
}
