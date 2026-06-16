package clickhouse

import (
	"context"
	"io"
	"log/slog"
	"strconv"
	"strings"
	"testing"
	"time"
)

func newDiscardLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// ── DDL strings ───────────────────────────────────────────────────────────────

func TestTablesDDL_HasFourStatements(t *testing.T) {
	if len(tablesDDL) != 4 {
		t.Errorf("want 4 DDL statements, got %d", len(tablesDDL))
	}
}

func TestTablesDDL_FirstCreatesSpans(t *testing.T) {
	if !strings.Contains(tablesDDL[0], "CREATE TABLE IF NOT EXISTS spans") {
		t.Error("first DDL statement must create the spans table")
	}
}

func TestTablesDDL_AllUseMergeTree(t *testing.T) {
	for i, ddl := range tablesDDL {
		if !strings.Contains(ddl, "ENGINE = MergeTree") {
			t.Errorf("tablesDDL[%d] missing MergeTree engine", i)
		}
	}
}

func TestTablesDDL_SpansHasTTL(t *testing.T) {
	if !strings.Contains(tablesDDL[0], "TTL") {
		t.Error("spans DDL must include a TTL clause")
	}
}

func TestTablesDDL_SpansHasAsyncInsert(t *testing.T) {
	if !strings.Contains(tablesDDL[0], "async_insert") {
		t.Error("spans DDL must set async_insert")
	}
}

// ── BatchWriter buffering ─────────────────────────────────────────────────────

func TestBatchWriter_WriteDropsWhenChannelFull(t *testing.T) {
	// No goroutine running — nothing drains the channel.
	w := NewBatchWriter(nil, newDiscardLogger())
	const capacity = 1000
	for i := 0; i < capacity+50; i++ {
		w.Write(SpanRow{SpanID: strconv.Itoa(i)})
	}
	if got := len(w.rows); got != capacity {
		t.Errorf("want channel at capacity %d, got %d", capacity, got)
	}
}

func TestBatchWriter_StartStop_DoesNotHang(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	w := NewBatchWriter(nil, newDiscardLogger())
	w.Start(ctx)
	cancel()

	done := make(chan struct{})
	go func() { w.Stop(); close(done) }()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Error("Stop() hung after context cancel")
	}
}
