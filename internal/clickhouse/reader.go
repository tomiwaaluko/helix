package clickhouse

import (
	"context"
	"fmt"

	"github.com/ClickHouse/clickhouse-go/v2/lib/driver"
)

// queryConn is the subset of driver.Conn used by SpanReader, enabling test mocking.
type queryConn interface {
	Query(ctx context.Context, query string, args ...interface{}) (driver.Rows, error)
}

// SpanReader reads span rows from ClickHouse.
type SpanReader struct {
	conn queryConn
}

// NewSpanReader creates a SpanReader backed by the given ClickHouse connection.
func NewSpanReader(conn driver.Conn) *SpanReader {
	return &SpanReader{conn: conn}
}

// GetTraceSpans returns all spans for the given trace_id, ordered by start_time.
// Returns an empty slice (not nil) when no spans are found.
func (r *SpanReader) GetTraceSpans(ctx context.Context, traceID string) ([]SpanRow, error) {
	const q = `
		SELECT trace_id, span_id, parent_span_id, run_id, task_id, attempt_number,
		       name, kind, start_time, end_time, status, status_message,
		       service_name, worker_id, attributes
		FROM spans
		WHERE trace_id = ?
		ORDER BY start_time`

	rows, err := r.conn.Query(ctx, q, traceID)
	if err != nil {
		return nil, fmt.Errorf("clickhouse query spans: %w", err)
	}
	defer rows.Close() //nolint:errcheck

	spans := make([]SpanRow, 0)
	for rows.Next() {
		var s SpanRow
		if err := rows.ScanStruct(&s); err != nil {
			return nil, fmt.Errorf("scan span row: %w", err)
		}
		spans = append(spans, s)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("span rows error: %w", err)
	}
	return spans, nil
}
