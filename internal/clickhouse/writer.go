// Package clickhouse provides ClickHouse DDL and write utilities for the Helix collector.
package clickhouse

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	clickhousego "github.com/ClickHouse/clickhouse-go/v2"
	"github.com/ClickHouse/clickhouse-go/v2/lib/driver"
)

// SpanRow is a single row written to the spans table.
// Field tags map to ClickHouse column names (snake_case).
type SpanRow struct {
	TraceID       string            `ch:"trace_id"`
	SpanID        string            `ch:"span_id"`
	ParentSpanID  string            `ch:"parent_span_id"`
	RunID         string            `ch:"run_id"`
	TaskID        string            `ch:"task_id"`
	AttemptNumber uint32            `ch:"attempt_number"`
	Name          string            `ch:"name"`
	Kind          string            `ch:"kind"`
	StartTime     time.Time         `ch:"start_time"`
	EndTime       time.Time         `ch:"end_time"`
	Status        string            `ch:"status"`
	StatusMessage string            `ch:"status_message"`
	ServiceName   string            `ch:"service_name"`
	WorkerID      string            `ch:"worker_id"`
	Attributes    map[string]string `ch:"attributes"`
}

// Open opens a ClickHouse connection using the provided DSN.
// The DSN must be in the form clickhouse://[user[:password]@]host[:port][/database][?param=value...].
func Open(ctx context.Context, dsn string) (driver.Conn, error) {
	opts, err := clickhousego.ParseDSN(dsn)
	if err != nil {
		return nil, fmt.Errorf("parse clickhouse DSN: %w", err)
	}

	// Enable async inserts for the spans table at the connection level.
	if opts.Settings == nil {
		opts.Settings = clickhousego.Settings{}
	}
	opts.Settings["async_insert"] = 1
	opts.Settings["wait_for_async_insert"] = 0

	conn, err := clickhousego.Open(opts)
	if err != nil {
		return nil, fmt.Errorf("open clickhouse connection: %w", err)
	}

	if err := conn.Ping(ctx); err != nil {
		conn.Close() //nolint:errcheck
		return nil, fmt.Errorf("clickhouse ping: %w", err)
	}

	return conn, nil
}

// BatchWriter buffers SpanRows and flushes them to ClickHouse periodically.
type BatchWriter struct {
	rows   chan SpanRow
	conn   driver.Conn
	logger *slog.Logger
	done   chan struct{}
}

// NewBatchWriter creates a BatchWriter backed by conn.
func NewBatchWriter(conn driver.Conn, log *slog.Logger) *BatchWriter {
	return &BatchWriter{
		rows:   make(chan SpanRow, 1000),
		conn:   conn,
		logger: log,
		done:   make(chan struct{}),
	}
}

// Start launches the background flush goroutine. It exits when ctx is cancelled
// or the rows channel is closed (via Stop).
func (b *BatchWriter) Start(ctx context.Context) {
	go func() {
		defer close(b.done)

		ticker := time.NewTicker(200 * time.Millisecond)
		defer ticker.Stop()

		const maxBatch = 500
		batch := make([]SpanRow, 0, maxBatch)

		flushBatch := func() {
			if len(batch) == 0 {
				return
			}
			if err := b.flush(batch); err != nil {
				b.logger.Error("clickhouse flush error", "err", err, "rows", len(batch))
			} else {
				b.logger.Debug("clickhouse flush ok", "rows", len(batch))
			}
			batch = batch[:0]
		}

		for {
			select {
			case row, ok := <-b.rows:
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
					case row, ok := <-b.rows:
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

// Write enqueues a SpanRow for async flushing.
// If the internal buffer is full, the row is dropped and a warning is logged.
func (b *BatchWriter) Write(row SpanRow) {
	select {
	case b.rows <- row:
	default:
		b.logger.Warn("clickhouse batch writer buffer full, dropping span", "span_id", row.SpanID)
	}
}

// flush writes a batch of rows to ClickHouse using a prepared batch insert.
func (b *BatchWriter) flush(batch []SpanRow) error {
	if len(batch) == 0 {
		return nil
	}

	ctx := context.Background()
	pb, err := b.conn.PrepareBatch(ctx, "INSERT INTO spans")
	if err != nil {
		return fmt.Errorf("prepare batch: %w", err)
	}
	defer pb.Close() //nolint:errcheck

	for i := range batch {
		if err := pb.AppendStruct(&batch[i]); err != nil {
			return fmt.Errorf("append struct: %w", err)
		}
	}

	if err := pb.Send(); err != nil {
		return fmt.Errorf("send batch: %w", err)
	}

	return nil
}

// Stop closes the rows channel and waits for the background goroutine to drain.
func (b *BatchWriter) Stop() {
	close(b.rows)
	<-b.done
}
