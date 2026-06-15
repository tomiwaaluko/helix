package clickhouse

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"github.com/ClickHouse/clickhouse-go/v2/lib/driver"
)

// LlmCallRow is a single row written to the llm_calls table.
type LlmCallRow struct {
	TraceID          string    `ch:"trace_id"`
	SpanID           string    `ch:"span_id"`
	RunID            string    `ch:"run_id"`
	Provider         string    `ch:"provider"`
	Model            string    `ch:"model"`
	PromptTokens     uint32    `ch:"prompt_tokens"`
	CompletionTokens uint32    `ch:"completion_tokens"`
	TotalTokens      uint32    `ch:"total_tokens"`
	CostUSD          float64   `ch:"cost_usd"`
	StartTime        time.Time `ch:"start_time"`
	DurationMs       uint32    `ch:"duration_ms"`
	Status           string    `ch:"status"`
	PromptURI        string    `ch:"prompt_uri"`
	CompletionURI    string    `ch:"completion_uri"`
}

// LlmCallWriter buffers LlmCallRows and flushes them to ClickHouse periodically.
// LLM call telemetry is fire-and-forget (same pattern as RetrievalWriter for spans).
type LlmCallWriter struct {
	rows   chan LlmCallRow
	conn   batchConn
	logger *slog.Logger
	done   chan struct{}
}

// NewLlmCallWriter creates an LlmCallWriter backed by conn.
func NewLlmCallWriter(conn driver.Conn, log *slog.Logger) *LlmCallWriter {
	return &LlmCallWriter{
		rows:   make(chan LlmCallRow, 1000),
		conn:   conn,
		logger: log,
		done:   make(chan struct{}),
	}
}

// Write enqueues an LlmCallRow for async flushing.
// If the internal buffer is full, the row is dropped and a warning is logged.
func (lw *LlmCallWriter) Write(row LlmCallRow) {
	select {
	case lw.rows <- row:
	default:
		lw.logger.Warn("llm call writer buffer full, dropping row", "span_id", row.SpanID)
	}
}

// Start launches the background flush goroutine. It exits when ctx is cancelled
// or the rows channel is closed (via Stop).
func (lw *LlmCallWriter) Start(ctx context.Context) {
	go func() {
		defer close(lw.done)

		ticker := time.NewTicker(200 * time.Millisecond)
		defer ticker.Stop()

		const maxBatch = 500
		batch := make([]LlmCallRow, 0, maxBatch)

		flushBatch := func() {
			if len(batch) == 0 {
				return
			}
			if err := lw.flush(batch); err != nil {
				lw.logger.Error("llm call flush error", "err", err, "rows", len(batch))
			} else {
				lw.logger.Debug("llm call flush ok", "rows", len(batch))
			}
			batch = batch[:0]
		}

		for {
			select {
			case row, ok := <-lw.rows:
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
					case row, ok := <-lw.rows:
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
func (lw *LlmCallWriter) Stop() {
	close(lw.rows)
	<-lw.done
}

// flush writes a batch of rows to ClickHouse using a prepared batch insert.
func (lw *LlmCallWriter) flush(batch []LlmCallRow) error {
	if len(batch) == 0 {
		return nil
	}

	ctx := context.Background()
	pb, err := lw.conn.PrepareBatch(ctx, "INSERT INTO llm_calls")
	if err != nil {
		return fmt.Errorf("prepare llm_calls batch: %w", err)
	}
	defer pb.Close() //nolint:errcheck

	for i := range batch {
		if err := pb.AppendStruct(&batch[i]); err != nil {
			return fmt.Errorf("append llm_call row: %w", err)
		}
	}

	if err := pb.Send(); err != nil {
		return fmt.Errorf("send llm_calls batch: %w", err)
	}

	return nil
}
