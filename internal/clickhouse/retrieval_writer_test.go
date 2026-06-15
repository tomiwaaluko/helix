package clickhouse

import (
	"errors"
	"testing"
	"time"
)

func sampleRetrievalRows() []RetrievalRow {
	now := time.Date(2026, 6, 15, 0, 0, 0, 0, time.UTC)
	return []RetrievalRow{
		{
			TraceID:        "trace-1",
			SpanID:         "span-1",
			RunID:          "run-1",
			Query:          "what is helix?",
			QueryEmbedding: []float32{},
			Retriever:      "qdrant",
			TopK:           10,
			Results: []ResultTuple{
				{PassageID: "doc-1", Score: 0.9, Rank: 1},
			},
			StartTime:  now,
			DurationMs: 50,
		},
		{
			TraceID:        "trace-1",
			SpanID:         "span-2",
			RunID:          "run-1",
			Query:          "distributed agent runtime",
			QueryEmbedding: []float32{},
			Retriever:      "qdrant",
			TopK:           5,
			Results: []ResultTuple{
				{PassageID: "doc-2", Score: 0.85, Rank: 1},
				{PassageID: "doc-3", Score: 0.75, Rank: 2},
			},
			StartTime:  now,
			DurationMs: 30,
		},
	}
}

func TestRetrievalWriter_Write_EmptyBatch_Noop(t *testing.T) {
	conn := &mockBatchConn{batch: &mockBatch{}}
	rw := &RetrievalWriter{conn: conn}

	if err := rw.flush(nil); err != nil {
		t.Fatalf("flush(nil) = %v, want nil", err)
	}
	if conn.calls != 0 {
		t.Errorf("PrepareBatch called %d times on empty batch, want 0", conn.calls)
	}

	if err := rw.flush([]RetrievalRow{}); err != nil {
		t.Fatalf("flush([]) = %v, want nil", err)
	}
	if conn.calls != 0 {
		t.Errorf("PrepareBatch called %d times on empty batch, want 0", conn.calls)
	}
}

func TestRetrievalWriter_Write_SendsRows(t *testing.T) {
	batch := &mockBatch{}
	conn := &mockBatchConn{batch: batch}
	rw := &RetrievalWriter{conn: conn}

	rows := sampleRetrievalRows()
	if err := rw.flush(rows); err != nil {
		t.Fatalf("flush = %v, want nil", err)
	}
	if conn.calls != 1 {
		t.Errorf("PrepareBatch calls = %d, want 1", conn.calls)
	}
	if conn.query != "INSERT INTO retrievals" {
		t.Errorf("query = %q, want %q", conn.query, "INSERT INTO retrievals")
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

func TestRetrievalWriter_Write_AppendError_Propagates(t *testing.T) {
	batch := &mockBatch{appendErr: errors.New("append boom")}
	rw := &RetrievalWriter{conn: &mockBatchConn{batch: batch}}

	err := rw.flush(sampleRetrievalRows())
	if err == nil {
		t.Fatal("flush = nil, want error")
	}
	if batch.sent {
		t.Error("Send should not be called after an append error")
	}
}

func TestRetrievalWriter_Write_SendError_Propagates(t *testing.T) {
	batch := &mockBatch{sendErr: errors.New("send boom")}
	rw := &RetrievalWriter{conn: &mockBatchConn{batch: batch}}

	if err := rw.flush(sampleRetrievalRows()); err == nil {
		t.Fatal("flush = nil, want error")
	}
}

func TestRetrievalWriter_Write_PrepareError_Propagates(t *testing.T) {
	conn := &mockBatchConn{prepErr: errors.New("no conn")}
	rw := &RetrievalWriter{conn: conn}

	if err := rw.flush(sampleRetrievalRows()); err == nil {
		t.Fatal("flush = nil, want error")
	}
}
