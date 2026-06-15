package clickhouse

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"github.com/ClickHouse/clickhouse-go/v2/lib/driver"
)

// ResultTuple is one retrieval result entry stored in the results column.
type ResultTuple struct {
	PassageID string  `ch:"passage_id"`
	Score     float32 `ch:"score"`
	Rank      uint32  `ch:"rank"`
}

// RetrievalRow is a single row written to the retrievals table.
type RetrievalRow struct {
	TraceID        string        `ch:"trace_id"`
	SpanID         string        `ch:"span_id"`
	RunID          string        `ch:"run_id"`
	Query          string        `ch:"query"`
	QueryEmbedding []float32     `ch:"query_embedding"`
	Retriever      string        `ch:"retriever"`
	TopK           uint32        `ch:"top_k"`
	Results        []ResultTuple `ch:"results"`
	StartTime      time.Time     `ch:"start_time"`
	DurationMs     uint32        `ch:"duration_ms"`
}

// RetrievalWriter buffers RetrievalRows and flushes them to ClickHouse periodically.
// Retrieval telemetry is fire-and-forget (same pattern as BatchWriter for spans).
type RetrievalWriter struct {
	rows   chan RetrievalRow
	conn   batchConn
	logger *slog.Logger
	done   chan struct{}
}

// NewRetrievalWriter creates a RetrievalWriter backed by conn.
func NewRetrievalWriter(conn driver.Conn, log *slog.Logger) *RetrievalWriter {
	return &RetrievalWriter{
		rows:   make(chan RetrievalRow, 1000),
		conn:   conn,
		logger: log,
		done:   make(chan struct{}),
	}
}

// Write enqueues a RetrievalRow for async flushing.
// If the internal buffer is full, the row is dropped and a warning is logged.
func (rw *RetrievalWriter) Write(row RetrievalRow) {
	select {
	case rw.rows <- row:
	default:
		rw.logger.Warn("retrieval writer buffer full, dropping row", "span_id", row.SpanID)
	}
}

// Start launches the background flush goroutine. It exits when ctx is cancelled
// or the rows channel is closed (via Stop).
func (rw *RetrievalWriter) Start(ctx context.Context) {
	go func() {
		defer close(rw.done)

		ticker := time.NewTicker(200 * time.Millisecond)
		defer ticker.Stop()

		const maxBatch = 500
		batch := make([]RetrievalRow, 0, maxBatch)

		flushBatch := func() {
			if len(batch) == 0 {
				return
			}
			if err := rw.flush(batch); err != nil {
				rw.logger.Error("retrieval flush error", "err", err, "rows", len(batch))
			} else {
				rw.logger.Debug("retrieval flush ok", "rows", len(batch))
			}
			batch = batch[:0]
		}

		for {
			select {
			case row, ok := <-rw.rows:
				if !ok {
					// Channel closed — drain and exit.
					flushBatch()
					return
				}
				batch = append(batch, row)
				if len(batch) >= maxBatch {
					flushBatch()
				}
			case <-ticker.C:
				flushBatch()
			case <-ctx.Done():
				// Drain remaining items before exiting.
				for {
					select {
					case row, ok := <-rw.rows:
						if !ok {
							flushBatch()
							return
						}
						batch = append(batch, row)
					default:
						flushBatch()
						return
					}
				}
			}
		}
	}()
}

// Stop closes the rows channel and waits for the background goroutine to drain.
func (rw *RetrievalWriter) Stop() {
	close(rw.rows)
	<-rw.done
}

// flush writes a batch of rows to ClickHouse using a prepared batch insert.
func (rw *RetrievalWriter) flush(batch []RetrievalRow) error {
	if len(batch) == 0 {
		return nil
	}

	ctx := context.Background()
	pb, err := rw.conn.PrepareBatch(ctx, "INSERT INTO retrievals")
	if err != nil {
		return fmt.Errorf("prepare retrieval batch: %w", err)
	}
	defer pb.Close() //nolint:errcheck

	for i := range batch {
		if err := pb.AppendStruct(&batch[i]); err != nil {
			return fmt.Errorf("append retrieval row: %w", err)
		}
	}

	if err := pb.Send(); err != nil {
		return fmt.Errorf("send retrieval batch: %w", err)
	}

	return nil
}
