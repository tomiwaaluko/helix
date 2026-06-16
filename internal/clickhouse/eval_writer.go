package clickhouse

import (
	"context"
	"fmt"
	"time"

	clickhousego "github.com/ClickHouse/clickhouse-go/v2"
	"github.com/ClickHouse/clickhouse-go/v2/lib/driver"
)

// EvalEventRow is a single row in the eval_events table: one scorer's result for
// one example within an eval run.
type EvalEventRow struct {
	EvalID    string    `ch:"eval_id"`
	ExampleID string    `ch:"example_id"`
	RunID     string    `ch:"run_id"`
	Scorer    string    `ch:"scorer"`
	Score     float64   `ch:"score"`
	Passed    uint8     `ch:"passed"`
	Details   string    `ch:"details"`
	Timestamp time.Time `ch:"timestamp"`
}

// batchConn is the subset of driver.Conn used by EvalWriter, enabling test mocking.
type batchConn interface {
	PrepareBatch(ctx context.Context, query string, opts ...driver.PrepareBatchOption) (driver.Batch, error)
}

// insertEvalEvents is the batch INSERT target for eval rows.
const insertEvalEvents = "INSERT INTO eval_events"

// EvalWriter writes eval event rows to ClickHouse synchronously.
//
// Unlike the span BatchWriter (async, fire-and-forget — acceptable for spans),
// eval results must be durable before the API call returns: the research thesis
// depends on never silently losing an eval outcome. Record forces a synchronous
// insert by overriding the connection-level async_insert=1 with async_insert=0
// on the query context.
type EvalWriter struct {
	conn batchConn
}

// NewEvalWriter creates an EvalWriter backed by the given ClickHouse connection.
func NewEvalWriter(conn driver.Conn) *EvalWriter {
	return &EvalWriter{conn: conn}
}

// Record writes rows to eval_events and blocks until ClickHouse has accepted them.
// It is a no-op on an empty slice.
func (w *EvalWriter) Record(ctx context.Context, rows []EvalEventRow) error {
	if len(rows) == 0 {
		return nil
	}

	// Force a synchronous insert regardless of the connection-level async_insert
	// setting used for the span pipeline.
	syncCtx := clickhousego.Context(ctx, clickhousego.WithSettings(clickhousego.Settings{
		"async_insert": 0,
	}))

	batch, err := w.conn.PrepareBatch(syncCtx, insertEvalEvents)
	if err != nil {
		return fmt.Errorf("prepare eval_events batch: %w", err)
	}
	defer batch.Close() //nolint:errcheck

	for i := range rows {
		if err := batch.AppendStruct(&rows[i]); err != nil {
			return fmt.Errorf("append eval event row: %w", err)
		}
	}

	if err := batch.Send(); err != nil {
		return fmt.Errorf("send eval_events batch: %w", err)
	}
	return nil
}
