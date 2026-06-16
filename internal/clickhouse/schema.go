// Package clickhouse provides ClickHouse DDL and write utilities for the Helix collector.
package clickhouse

import (
	"context"
	"fmt"

	"github.com/ClickHouse/clickhouse-go/v2/lib/driver"
)

// tablesDDL lists all CREATE TABLE IF NOT EXISTS statements in execution order.
// SQL is copied verbatim from migrations/clickhouse/202606150001_initial_schema.sql.
var tablesDDL = []string{
	`CREATE TABLE IF NOT EXISTS spans (
  trace_id        String,
  span_id         String,
  parent_span_id  String,
  run_id          String,
  task_id         String,
  attempt_number  UInt32,
  name            LowCardinality(String),
  kind            LowCardinality(String),
  start_time      DateTime64(9, 'UTC'),
  end_time        DateTime64(9, 'UTC'),
  duration_ms     UInt32 MATERIALIZED toUInt32(
                    (toUnixTimestamp64Nano(end_time) - toUnixTimestamp64Nano(start_time)) / 1000000
                  ),
  status          LowCardinality(String),
  status_message  String,
  service_name    LowCardinality(String),
  worker_id       String,
  attributes      Map(String, String)
) ENGINE = MergeTree
PARTITION BY toYYYYMMDD(start_time)
ORDER BY (trace_id, start_time, span_id)
TTL toStartOfDay(start_time) + INTERVAL 90 DAY
SETTINGS async_insert = 1`,

	`CREATE TABLE IF NOT EXISTS llm_calls (
  trace_id          String,
  span_id           String,
  run_id            String,
  provider          LowCardinality(String),
  model             LowCardinality(String),
  prompt_tokens     UInt32,
  completion_tokens UInt32,
  total_tokens      UInt32,
  cost_usd          Decimal(10, 6),
  start_time        DateTime64(9, 'UTC'),
  duration_ms       UInt32,
  status            LowCardinality(String),
  prompt_uri        String,
  completion_uri    String
) ENGINE = MergeTree
PARTITION BY toYYYYMMDD(start_time)
ORDER BY (run_id, start_time)`,

	`CREATE TABLE IF NOT EXISTS retrievals (
  trace_id         String,
  span_id          String,
  run_id           String,
  query            String,
  query_embedding  Array(Float32),
  retriever        LowCardinality(String),
  top_k            UInt32,
  results          Array(Tuple(passage_id String, score Float32, rank UInt32)),
  gold_passage_id  String DEFAULT '',
  recall_at_k      UInt8 DEFAULT 0,
  start_time       DateTime64(9, 'UTC'),
  duration_ms      UInt32
) ENGINE = MergeTree
PARTITION BY toYYYYMMDD(start_time)
ORDER BY (run_id, start_time)`,

	`CREATE TABLE IF NOT EXISTS eval_events (
  eval_id    String,
  example_id String,
  run_id     String,
  scorer     LowCardinality(String),
  score      Float64,
  passed     UInt8,
  details    String,
  timestamp  DateTime64(9, 'UTC')
) ENGINE = MergeTree
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (eval_id, example_id)`,
}

// RunDDL executes all table CREATE statements against the given connection.
func RunDDL(ctx context.Context, conn driver.Conn) error {
	for _, ddl := range tablesDDL {
		if err := conn.Exec(ctx, ddl); err != nil {
			return fmt.Errorf("clickhouse DDL failed: %w", err)
		}
	}
	return nil
}
