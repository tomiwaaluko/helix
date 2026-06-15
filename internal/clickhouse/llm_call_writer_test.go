package clickhouse

import (
	"errors"
	"testing"
	"time"
)

func sampleLlmCallRows() []LlmCallRow {
	return []LlmCallRow{
		{TraceID: "t1", SpanID: "s1", RunID: "r1", Provider: "anthropic",
			Model: "claude-sonnet-4-20250514", PromptTokens: 100, CompletionTokens: 50,
			TotalTokens: 150, CostUSD: 0.001, StartTime: time.Now().UTC(), DurationMs: 500, Status: "ok"},
	}
}

func TestLlmCallWriter_Flush_EmptyBatch_Noop(t *testing.T) {
	conn := &mockBatchConn{batch: &mockBatch{}}
	lw := &LlmCallWriter{conn: conn}

	if err := lw.flush(nil); err != nil {
		t.Fatalf("flush(nil) = %v, want nil", err)
	}
	if conn.calls != 0 {
		t.Errorf("PrepareBatch called %d times on empty batch, want 0", conn.calls)
	}

	if err := lw.flush([]LlmCallRow{}); err != nil {
		t.Fatalf("flush([]) = %v, want nil", err)
	}
	if conn.calls != 0 {
		t.Errorf("PrepareBatch called %d times on empty batch, want 0", conn.calls)
	}
}

func TestLlmCallWriter_Flush_WritesRows(t *testing.T) {
	batch := &mockBatch{}
	conn := &mockBatchConn{batch: batch}
	lw := &LlmCallWriter{conn: conn}

	rows := sampleLlmCallRows()
	if err := lw.flush(rows); err != nil {
		t.Fatalf("flush = %v, want nil", err)
	}
	if conn.calls != 1 {
		t.Errorf("PrepareBatch calls = %d, want 1", conn.calls)
	}
	if conn.query != "INSERT INTO llm_calls" {
		t.Errorf("query = %q, want %q", conn.query, "INSERT INTO llm_calls")
	}
	if batch.appended != len(rows) {
		t.Errorf("appended = %d, want %d", batch.appended, len(rows))
	}
	if !batch.sent {
		t.Error("batch.Send was not called")
	}
	if !batch.closed {
		t.Error("batch.Close was not called")
	}
}

func TestLlmCallWriter_Flush_AppendError_Wrapped(t *testing.T) {
	batch := &mockBatch{appendErr: errors.New("append boom")}
	lw := &LlmCallWriter{conn: &mockBatchConn{batch: batch}}

	err := lw.flush(sampleLlmCallRows())
	if err == nil {
		t.Fatal("flush = nil, want error")
	}
	if batch.sent {
		t.Error("Send should not be called after an append error")
	}
}

func TestLlmCallWriter_Flush_SendError_Wrapped(t *testing.T) {
	batch := &mockBatch{sendErr: errors.New("send boom")}
	lw := &LlmCallWriter{conn: &mockBatchConn{batch: batch}}

	if err := lw.flush(sampleLlmCallRows()); err == nil {
		t.Fatal("flush = nil, want error")
	}
}

func TestLlmCallWriter_Flush_PrepareError_Wrapped(t *testing.T) {
	conn := &mockBatchConn{prepErr: errors.New("no conn")}
	lw := &LlmCallWriter{conn: conn}

	if err := lw.flush(sampleLlmCallRows()); err == nil {
		t.Fatal("flush = nil, want error")
	}
}
