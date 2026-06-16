package clickhouse

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/ClickHouse/clickhouse-go/v2/lib/column"
	"github.com/ClickHouse/clickhouse-go/v2/lib/driver"
)

// ── driver.Batch / batchConn test doubles ─────────────────────────────────────

type mockBatch struct {
	appended  int
	sent      bool
	closed    bool
	appendErr error
	sendErr   error
}

func (m *mockBatch) Abort() error                  { return nil }
func (m *mockBatch) Append(_ ...any) error         { m.appended++; return m.appendErr }
func (m *mockBatch) AppendStruct(_ any) error      { m.appended++; return m.appendErr }
func (m *mockBatch) Column(int) driver.BatchColumn { return nil }
func (m *mockBatch) Flush() error                  { return nil }
func (m *mockBatch) Send() error                   { m.sent = true; return m.sendErr }
func (m *mockBatch) IsSent() bool                  { return m.sent }
func (m *mockBatch) Rows() int                     { return m.appended }
func (m *mockBatch) Columns() []column.Interface   { return nil }
func (m *mockBatch) Close() error                  { m.closed = true; return nil }

type mockBatchConn struct {
	query   string
	calls   int
	batch   *mockBatch
	prepErr error
}

func (m *mockBatchConn) PrepareBatch(_ context.Context, query string, _ ...driver.PrepareBatchOption) (driver.Batch, error) {
	m.calls++
	m.query = query
	if m.prepErr != nil {
		return nil, m.prepErr
	}
	return m.batch, nil
}

func sampleEvalRows() []EvalEventRow {
	now := time.Date(2026, 6, 15, 0, 0, 0, 0, time.UTC)
	return []EvalEventRow{
		{EvalID: "eval-1", ExampleID: "ex-1", Scorer: "answer_f1", Score: 0.5, Passed: 0, Timestamp: now},
		{EvalID: "eval-1", ExampleID: "ex-1", Scorer: "retrieval_recall@10", Score: 1.0, Passed: 1, Timestamp: now},
	}
}

// ── tests ─────────────────────────────────────────────────────────────────────

func TestEvalWriter_Record_EmptyRows_Noop(t *testing.T) {
	conn := &mockBatchConn{batch: &mockBatch{}}
	w := &EvalWriter{conn: conn}

	if err := w.Record(context.Background(), nil); err != nil {
		t.Fatalf("Record(nil) = %v, want nil", err)
	}
	if conn.calls != 0 {
		t.Errorf("PrepareBatch called %d times on empty rows, want 0", conn.calls)
	}
}

func TestEvalWriter_Record_WritesRows(t *testing.T) {
	batch := &mockBatch{}
	conn := &mockBatchConn{batch: batch}
	w := &EvalWriter{conn: conn}

	rows := sampleEvalRows()
	if err := w.Record(context.Background(), rows); err != nil {
		t.Fatalf("Record = %v, want nil", err)
	}
	if conn.calls != 1 {
		t.Errorf("PrepareBatch calls = %d, want 1", conn.calls)
	}
	if conn.query != insertEvalEvents {
		t.Errorf("query = %q, want %q", conn.query, insertEvalEvents)
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

func TestEvalWriter_Record_AppendError_Wrapped(t *testing.T) {
	batch := &mockBatch{appendErr: errors.New("boom")}
	w := &EvalWriter{conn: &mockBatchConn{batch: batch}}

	err := w.Record(context.Background(), sampleEvalRows())
	if err == nil {
		t.Fatal("Record = nil, want error")
	}
	if batch.sent {
		t.Error("Send should not be called after an append error")
	}
}

func TestEvalWriter_Record_SendError_Wrapped(t *testing.T) {
	batch := &mockBatch{sendErr: errors.New("network")}
	w := &EvalWriter{conn: &mockBatchConn{batch: batch}}

	if err := w.Record(context.Background(), sampleEvalRows()); err == nil {
		t.Fatal("Record = nil, want error")
	}
}

func TestEvalWriter_Record_PrepareError_Wrapped(t *testing.T) {
	conn := &mockBatchConn{prepErr: errors.New("no conn")}
	w := &EvalWriter{conn: conn}

	if err := w.Record(context.Background(), sampleEvalRows()); err == nil {
		t.Fatal("Record = nil, want error")
	}
}
