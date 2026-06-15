package clickhouse

import (
	"context"
	"fmt"
	"time"

	"github.com/ClickHouse/clickhouse-go/v2/lib/driver"
)

// RetrievalQueryRow is one row returned by ListRetrievals.
type RetrievalQueryRow struct {
	TraceID    string    `ch:"trace_id"`
	SpanID     string    `ch:"span_id"`
	RunID      string    `ch:"run_id"`
	Query      string    `ch:"query"`
	Retriever  string    `ch:"retriever"`
	TopK       uint32    `ch:"top_k"`
	RecallAtK  uint8     `ch:"recall_at_k"`
	StartTime  time.Time `ch:"start_time"`
	DurationMs uint32    `ch:"duration_ms"`
}

// RetrievalReader queries the retrievals table.
type RetrievalReader struct {
	conn queryConn
}

// NewRetrievalReader creates a RetrievalReader backed by the given ClickHouse connection.
func NewRetrievalReader(conn driver.Conn) *RetrievalReader {
	return &RetrievalReader{conn: conn}
}

// ListRetrievals returns retrieval rows for a run_id, most recent first, up to 500.
// When runID is empty, returns the 500 most recent rows across all runs.
func (r *RetrievalReader) ListRetrievals(ctx context.Context, runID string) ([]RetrievalQueryRow, error) {
	var (
		rows driver.Rows
		err  error
	)

	if runID != "" {
		const q = `
			SELECT trace_id, span_id, run_id, query, retriever, top_k, recall_at_k, start_time, duration_ms
			FROM retrievals
			WHERE run_id = ?
			ORDER BY start_time DESC
			LIMIT 500`
		rows, err = r.conn.Query(ctx, q, runID)
	} else {
		const q = `
			SELECT trace_id, span_id, run_id, query, retriever, top_k, recall_at_k, start_time, duration_ms
			FROM retrievals
			ORDER BY start_time DESC
			LIMIT 500`
		rows, err = r.conn.Query(ctx, q)
	}

	if err != nil {
		return nil, fmt.Errorf("clickhouse query retrievals: %w", err)
	}
	defer rows.Close() //nolint:errcheck

	result := make([]RetrievalQueryRow, 0)
	for rows.Next() {
		var row RetrievalQueryRow
		if err := rows.ScanStruct(&row); err != nil {
			return nil, fmt.Errorf("scan retrieval row: %w", err)
		}
		result = append(result, row)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("retrieval rows error: %w", err)
	}
	return result, nil
}
