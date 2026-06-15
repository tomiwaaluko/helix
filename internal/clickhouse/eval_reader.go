package clickhouse

import (
	"context"
	"fmt"
	"time"

	"github.com/ClickHouse/clickhouse-go/v2/lib/driver"
)

// EvalSummaryRow is one eval-run summary from the list query.
type EvalSummaryRow struct {
	EvalID     string    `ch:"eval_id"`
	Examples   uint64    `ch:"examples"`
	StartedAt  time.Time `ch:"started_at"`
	FinishedAt time.Time `ch:"finished_at"`
}

// ScorerMeanRow is one (eval_id, scorer, mean, n) row from the aggregation query.
type ScorerMeanRow struct {
	EvalID string  `ch:"eval_id"`
	Scorer string  `ch:"scorer"`
	Mean   float64 `ch:"mean"`
	N      uint64  `ch:"n"`
}

// EvalReader queries the eval_events table.
type EvalReader struct {
	conn queryConn
}

// NewEvalReader creates an EvalReader backed by the given ClickHouse connection.
func NewEvalReader(conn driver.Conn) *EvalReader {
	return &EvalReader{conn: conn}
}

// ListEvals returns eval-run summaries (most recent first, up to 100) and the
// per-(eval, scorer) mean scores. The caller joins the two by eval_id. Both
// slices are non-nil (possibly empty) when no error is returned.
func (r *EvalReader) ListEvals(ctx context.Context) ([]EvalSummaryRow, []ScorerMeanRow, error) {
	const summaryQ = `
		SELECT eval_id,
		       count() AS examples,
		       min(timestamp) AS started_at,
		       max(timestamp) AS finished_at
		FROM eval_events
		GROUP BY eval_id
		ORDER BY started_at DESC
		LIMIT 100`

	summaries := make([]EvalSummaryRow, 0)
	sRows, err := r.conn.Query(ctx, summaryQ)
	if err != nil {
		return nil, nil, fmt.Errorf("clickhouse query eval summaries: %w", err)
	}
	for sRows.Next() {
		var row EvalSummaryRow
		if err := sRows.ScanStruct(&row); err != nil {
			sRows.Close() //nolint:errcheck
			return nil, nil, fmt.Errorf("scan eval summary row: %w", err)
		}
		summaries = append(summaries, row)
	}
	if err := sRows.Err(); err != nil {
		sRows.Close() //nolint:errcheck
		return nil, nil, fmt.Errorf("eval summary rows error: %w", err)
	}
	sRows.Close() //nolint:errcheck

	const meanQ = `
		SELECT eval_id,
		       scorer,
		       avg(score) AS mean,
		       count() AS n
		FROM eval_events
		GROUP BY eval_id, scorer`

	means := make([]ScorerMeanRow, 0)
	mRows, err := r.conn.Query(ctx, meanQ)
	if err != nil {
		return nil, nil, fmt.Errorf("clickhouse query scorer means: %w", err)
	}
	defer mRows.Close() //nolint:errcheck
	for mRows.Next() {
		var row ScorerMeanRow
		if err := mRows.ScanStruct(&row); err != nil {
			return nil, nil, fmt.Errorf("scan scorer mean row: %w", err)
		}
		means = append(means, row)
	}
	if err := mRows.Err(); err != nil {
		return nil, nil, fmt.Errorf("scorer mean rows error: %w", err)
	}

	return summaries, means, nil
}

// GetEval returns all per-example scored events for one eval run, ordered by
// example then scorer. Returns an empty slice (not nil) when the eval is unknown.
func (r *EvalReader) GetEval(ctx context.Context, evalID string) ([]EvalEventRow, error) {
	const q = `
		SELECT eval_id, example_id, run_id, scorer, score, passed, details, timestamp
		FROM eval_events
		WHERE eval_id = ?
		ORDER BY example_id, scorer`

	rows, err := r.conn.Query(ctx, q, evalID)
	if err != nil {
		return nil, fmt.Errorf("clickhouse query eval events: %w", err)
	}
	defer rows.Close() //nolint:errcheck

	events := make([]EvalEventRow, 0)
	for rows.Next() {
		var e EvalEventRow
		if err := rows.ScanStruct(&e); err != nil {
			return nil, fmt.Errorf("scan eval event row: %w", err)
		}
		events = append(events, e)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("eval event rows error: %w", err)
	}
	return events, nil
}
