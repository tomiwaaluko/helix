# M2 Plan — ClickHouse + OTel Collector

> **Status: APPROVED — plan-phase complete, implementation in progress.**
>
> Per `AGENTS.md`: ClickHouse is a new datastore; schema additions require a written
> plan before code. Approved as part of the documented M2 milestone.

---

## 1. What M2 delivers

| Component | M1 (today) | M2 target |
|---|---|---|
| Span storage | JSONL file (`data/spans.jsonl`) | ClickHouse 24 `spans` table |
| Span transport | Synchronous file write in-process | OTLP/gRPC → `cmd/collector` |
| Python observability | Custom `SpanLogger` (JSONL only) | `SpanLogger` + OTel SDK dual-write |
| Go collector binary | (none) | `cmd/collector/` |
| ClickHouse | (none) | Added to `docker-compose.yml` |

### What M2 does NOT deliver

| Item | Target |
|---|---|
| `llm_calls`, `retrievals`, `eval_events` ClickHouse tables | Created as empty shells; mined data lands in M5 |
| Failure miner reading from ClickHouse | M5 |
| Redis (rate limits, locks) | M3 |
| MinIO (blobs, checkpoints) | M3 |
| eval_events synchronous write | M5 — requires EvalRunner service |

`make eval` continues in local mode, emitting JSONL only. The OTel path is exercised
only when `OTEL_EXPORTER_OTLP_ENDPOINT` is set (i.e., when the collector is running).

---

## 2. Architecture delta

### M1 span path

```
Python worker
  └─ SpanLogger.span(...)
       └─ data/spans.jsonl   (JSONL, in-process write)
```

### M2 span path

```
Python worker
  └─ SpanLogger.span(...)          (unchanged — JSONL still written for local dev)
  └─ OtelSpanExporter (new)        (wraps opentelemetry-sdk)
       └─ OTLP/gRPC ──────────────────────────────────▶ cmd/collector
                                                          ├─ internal/clickhouse/
                                                          │    └─ spans table (async insert)
                                                          └─ (future) llm_calls, retrievals
```

The JSONL file remains the local-dev fallback. When `OTEL_EXPORTER_OTLP_ENDPOINT` is
unset, the `OtelSpanExporter` is a no-op and nothing changes from M1.

---

## 3. ClickHouse schema  ← **sign-off: approved**

`migrations/clickhouse/202606150001_initial_schema.sql` — runs on `cmd/collector`
startup via DDL statements (`CREATE TABLE IF NOT EXISTS`). ClickHouse does not use
`golang-migrate`; the collector owns its own DDL runner.

Tables from `docs/data-model.md`:

```sql
-- spans (primary sink)
CREATE TABLE IF NOT EXISTS spans ( ... ) ENGINE = MergeTree ...

-- shells for future milestones (M5+)
CREATE TABLE IF NOT EXISTS llm_calls   ( ... ) ENGINE = MergeTree ...
CREATE TABLE IF NOT EXISTS retrievals  ( ... ) ENGINE = MergeTree ...
CREATE TABLE IF NOT EXISTS eval_events ( ... ) ENGINE = MergeTree ...
```

Full DDL in `migrations/clickhouse/202606150001_initial_schema.sql` per
`docs/data-model.md`.

**Async inserts**: spans use `async_insert=1` in the ClickHouse settings. Acceptable
for observability data — some loss on hard shutdown is OK. Eval results (future) will
use sync writes.

---

## 4. Component changes

### 4.1  Go collector  (`cmd/collector/`)

Minimal binary:

```
cmd/collector/main.go
  - Config from env: CLICKHOUSE_URL, OTLP_GRPC_PORT (default 4317)
  - Run ClickHouse DDL on startup (CREATE TABLE IF NOT EXISTS)
  - Start OTLP/gRPC ExportTraceService (proto: opentelemetry/collector/trace/v1)
  - Batch spans → ClickHouse insert (async, 200ms flush interval)
  - Graceful shutdown on SIGINT/SIGTERM

internal/clickhouse/
  writer.go   — connection + BatchWriter (channels → CH insert)
  schema.go   — DDL strings and DDL runner

internal/otlp/
  server.go   — implements collector.trace.v1.TraceService
               translates ResourceSpan protos → internal span rows
               hands rows to clickhouse.BatchWriter
```

**Libraries**:
| Library | Purpose |
|---|---|
| `github.com/ClickHouse/clickhouse-go/v2` | ClickHouse native driver |
| `go.opentelemetry.io/proto/otlp` | OTLP proto definitions (no collector framework) |
| `google.golang.org/grpc` | Already present |

We do NOT pull in the full `go.opentelemetry.io/collector` framework — it adds 50+
transitive deps for functionality we don't need.

### 4.2  Python OTel integration  (`worker/helix/`)

New file: `worker/helix/otel.py`

- `OtelSpanExporter` class — wraps `opentelemetry.sdk.trace.TracerProvider` and
  `opentelemetry.exporter.otlp.proto.grpc.OTLPSpanExporter`
- `configure_otel(endpoint: str | None)` — call once at worker startup; no-op if
  endpoint is None
- The existing `SpanLogger.span(...)` context manager is unchanged (JSONL path)
- The `RemoteEngine` in `_handle_envelope` will call `configure_otel` on startup

New deps in `worker/pyproject.toml`:
```
opentelemetry-sdk>=1.25
opentelemetry-exporter-otlp-proto-grpc>=1.25
```

The `SpanLogger` itself does NOT change — it stays pure JSONL. The OTel span is emitted
separately using the OTel SDK's `tracer.start_span()` API. Both paths run side-by-side
when `OTEL_EXPORTER_OTLP_ENDPOINT` is set.

### 4.3  Docker Compose

Add `clickhouse` service to `infra/compose/docker-compose.yml`:

```yaml
clickhouse:
  image: clickhouse/clickhouse-server:24.3
  ports:
    - "8123:8123"  # HTTP
    - "9000:9000"  # native protocol
  volumes:
    - clickhouse_data:/var/lib/clickhouse
  healthcheck:
    test: ["CMD-SHELL", "clickhouse-client --query 'SELECT 1'"]
    interval: 5s
    retries: 10
```

### 4.4  Makefile additions

| Target | Description |
|---|---|
| `make collector` | Run `bin/collector` against local dev stack |
| `make build` | Now builds `bin/orchestrator` **and** `bin/collector` |

---

## 5. Make targets delta

| Target | M1 | M2 |
|---|---|---|
| `make dev` | Qdrant + Postgres + NATS | + ClickHouse |
| `make build` | `bin/orchestrator` | + `bin/collector` |
| `make collector` | (missing) | Run collector against local stack |
| `make test` | pytest + go test | + collector unit tests |

---

## 6. Test plan

### Unit tests (no Docker)

- Go: `internal/clickhouse/writer_test.go` — mock ClickHouse connection, verify batch
  flush logic, test DDL runner string output
- Go: `internal/otlp/server_test.go` — verify ResourceSpan translation to row structs
- Python: `tests/test_otel.py` — verify `OtelSpanExporter` is a no-op when endpoint is
  None; verify it configures a TracerProvider when endpoint is set

### Integration test (requires `make dev`)

- `HELIX_INTEGRATION=1 make test-integration` exercises the full path:
  1. `make collector` running
  2. Worker emits spans via OTLP
  3. Spans appear in ClickHouse `SELECT count() FROM spans`

---

## 7. Open decisions (all pre-approved per AGENTS.md pattern)

**7.1  OTLP transport**
gRPC (port 4317). Standard; already used by workers. HTTP/JSON alternative
is simpler but non-standard for production.

**7.2  ClickHouse driver**
`clickhouse-go/v2` native protocol. The HTTP interface works but native is
faster and handles batch inserts cleanly.

**7.3  Async vs sync inserts**
Spans: async (`async_insert=1` in DSN). Acceptable loss on hard shutdown.
Eval events (M5): sync.

**7.4  SpanLogger backward compat**
`SpanLogger` unchanged. OTel is additive via `otel.py`. Local `make eval`
unaffected.

---

## 8. Task order (serial dependencies)

1. **ClickHouse schema** — `migrations/clickhouse/202606150001_initial_schema.sql`
2. **Docker Compose** — add `clickhouse` service
3. **Go deps** — `go get` clickhouse-go + otlp proto
4. **`internal/clickhouse/`** — `writer.go`, `schema.go`
5. **`internal/otlp/`** — `server.go` (OTLP receiver → CH writer)
6. **`cmd/collector/main.go`** — entry point
7. **`make build` update** — include collector
8. **Python `helix/otel.py`** — `OtelSpanExporter`, `configure_otel`
9. **`make collector` target** — Makefile
10. **`make dev` update** — add ClickHouse
11. **Unit tests** — Go + Python
12. **`make lint` + `make test`**
13. **Update `AGENTS.md`** — note ClickHouse landed

---

## 9. Definition of done

- [ ] `migrations/clickhouse/202606150001_initial_schema.sql` committed.
- [ ] `make dev` boots Qdrant + Postgres + NATS + ClickHouse.
- [ ] `make build` produces `bin/orchestrator` and `bin/collector`.
- [ ] `make collector` runs without error against the local stack.
- [ ] `make test` passes (Go + Python).
- [ ] `make lint` passes.
- [ ] Python: `OtelSpanExporter` is a no-op when no endpoint configured.
- [ ] `AGENTS.md` updated: M2 stack documented, M1-only note removed.
- [ ] `CHANGELOG.md` has an M2 entry.
- [ ] `HANDOFF.md` updated.
