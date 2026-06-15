package clickhouse

import (
	"context"
	"fmt"
	"time"

	"github.com/ClickHouse/clickhouse-go/v2/lib/driver"
)

// LlmCallQueryRow is one row returned by ListLlmCalls.
type LlmCallQueryRow struct {
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
}

// LlmCallReader queries the llm_calls table.
type LlmCallReader struct {
	conn queryConn
}

// NewLlmCallReader creates an LlmCallReader backed by the given ClickHouse connection.
func NewLlmCallReader(conn driver.Conn) *LlmCallReader {
	return &LlmCallReader{conn: conn}
}

// ListLlmCalls returns llm_call rows for a run_id, most recent first, up to 500.
// When runID is empty, returns the 500 most recent rows across all runs.
func (r *LlmCallReader) ListLlmCalls(ctx context.Context, runID string) ([]LlmCallQueryRow, error) {
	var (
		rows driver.Rows
		err  error
	)

	if runID != "" {
		const q = `
			SELECT trace_id, span_id, run_id, provider, model,
			       prompt_tokens, completion_tokens, total_tokens,
			       cost_usd, start_time, duration_ms, status
			FROM llm_calls
			WHERE run_id = ?
			ORDER BY start_time DESC
			LIMIT 500`
		rows, err = r.conn.Query(ctx, q, runID)
	} else {
		const q = `
			SELECT trace_id, span_id, run_id, provider, model,
			       prompt_tokens, completion_tokens, total_tokens,
			       cost_usd, start_time, duration_ms, status
			FROM llm_calls
			ORDER BY start_time DESC
			LIMIT 500`
		rows, err = r.conn.Query(ctx, q)
	}

	if err != nil {
		return nil, fmt.Errorf("clickhouse query llm_calls: %w", err)
	}
	defer rows.Close() //nolint:errcheck

	result := make([]LlmCallQueryRow, 0)
	for rows.Next() {
		var row LlmCallQueryRow
		if err := rows.ScanStruct(&row); err != nil {
			return nil, fmt.Errorf("scan llm_call row: %w", err)
		}
		result = append(result, row)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("llm_call rows error: %w", err)
	}
	return result, nil
}
