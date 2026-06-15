# M7 Plan — Retrievals fan-out, ClickHouse miner path, retrieval view

> **Status: DRAFT — awaiting sign-off.**
>
> M7 makes the `retrievals` ClickHouse table live, updates the Python miner to read
> from it when `CLICKHOUSE_URL` is set, and surfaces retrieval data in the dashboard.
> The JSONL/local path stays untouched as the fallback so `make eval` with no
> infrastructure still works.

---

## Background and gap

The `retrievals` table was created as an empty shell in `migrations/clickhouse/202606150001_initial_schema.sql`.
The Python miner (`worker/helix/rag/miner/miner.py`) reads from `data/spans.jsonl` and
works correctly in local mode — the BRIGHT experiment validated the full mine → train →
promote loop this way.

The gap: the Python retriever emits spans via `SpanLogger` (→ JSONL) only. The OTel
integration (`otel.py`) wraps only the outer `deep_research` workflow span. Retrieval
sub-spans never reach the Go collector and therefore never reach ClickHouse. M7 closes
this by dual-writing retrieval spans through OTel, fanning them out in the collector, and
updating the miner to query ClickHouse when `CLICKHOUSE_URL` is set.

---

## What M7 delivers

| Piece | Before M7 | After M7 |
|---|---|---|
| `retrievals` ClickHouse table | Always empty | Written by Go collector for every `retrieve` span |
| Python retriever OTel emit | Outer workflow span only | Also emits nested `retrieve` span with query + results attrs |
| Python miner data source | `data/spans.jsonl` always | ClickHouse `retrievals` when `CLICKHOUSE_URL` set; falls back to JSONL |
| `GET /api/v1/retrievals` | Not implemented | Filterable by `run_id`; 503 when ClickHouse unset |
| Dashboard `/retrievals` | None | Table of retrieval spans with recall coloring |
| `make finetune` | Reads JSONL (local) | Reads ClickHouse when env vars set |

### What M7 does NOT deliver
- `query_embedding` population (requires forwarding the float vector; deferred — column stays empty Array)
- `gold_passage_id` / `recall_at_k` population at write time (no gold data at span time; the miner computes recall from the eval dataset as before)
- Helm / Kubernetes (M8)
- Fine-tune trigger from the dashboard (M8)

---

## Architecture

```
HybridRetriever.retrieve()
  │  JSONL path (always)       → data/spans.jsonl
  └► OTel path (when configured)
       └► nested span "retrieve", attrs: kind, query, retriever, top_k, results (JSON)
              │
              ▼
       Go collector (OTLP gRPC)
         ├─► BatchWriter → spans table    (existing, all spans)
         └─► RetrievalWriter → retrievals table  (NEW, retrieve spans only)
                                  (async insert — ok; no durability requirement on spans)

Browser
  │  GET /retrievals?run_id=X  →  BFF  →  GET /api/v1/retrievals?run_id=X
  └─► <RetrievalTable> (static, 503-tolerant)

make finetune (Python)
  └─► mine_from_clickhouse(ch_url)   ← NEW, used when CLICKHOUSE_URL set
      OR mine_failures(spans=_load_spans(spans_path))  ← existing JSONL path
```

---

## 1. Python: emit retrieval spans via OTel

**File:** `worker/helix/rag/retriever.py`

`HybridRetriever.retrieve()` already wraps in `self._spans.span("retrieve", kind="retrieval")`.
Add a parallel OTel span using a new helper `_otel_span(name, attrs)` from `helix.otel`:

```python
from helix.otel import OtelSpanExporter   # already importable; no-op when unconfigured

async def retrieve(self, query: str, top_k: int = 10, ...) -> list[Doc]:
    with self._spans.span("retrieve", kind="retrieval") as attrs:
        # ... existing logic ...
        attrs["query"] = query
        attrs["retriever"] = "hybrid+reranked"
        attrs["top_k"] = top_k
        attrs["results"] = [{"chunk_id": ..., "doc_id": ..., "score": ...} for d in docs]

    # Dual-write: emit OTel span for collector fan-out when OTel is configured.
    # Must run AFTER the JSONL span closes (attrs are populated then).
    with OtelSpanExporter("retrieve", {
        "kind": "retrieval",
        "query": query,
        "retriever": "hybrid+reranked",
        "top_k": str(top_k),
        "results": json.dumps(attrs["results"]),
        "run_id": attrs.get("run_id", ""),
    }):
        pass  # span body is empty — work already done above
```

Wait — `OtelSpanExporter` is a context manager wrapping work; here the work already
happened inside the `SpanLogger` span. We just need to emit a lightweight OTel span
with the finalized attributes **after** computing them. The implementation is:

```python
# After the with self._spans.span(...) block:
_emit_retrieval_otel(attrs)
```

New private function in `retriever.py`:

```python
def _emit_retrieval_otel(attrs: dict[str, Any]) -> None:
    """Fire-and-forget OTel span for retrieval fan-out to ClickHouse."""
    if not _otel_configured():
        return
    with OtelSpanExporter("retrieve", {
        "kind": "retrieval",
        "query": str(attrs.get("query", "")),
        "retriever": str(attrs.get("retriever", "")),
        "top_k": str(attrs.get("top_k", 0)),
        "results": json.dumps(attrs.get("results", [])),
        "run_id": str(attrs.get("run_id", "")),
    }):
        pass
```

A new `_otel_configured() -> bool` is exported from `helix.otel` (reads the `_configured` flag).

**`helix/otel.py` addition:**
```python
def is_configured() -> bool:
    """Return True if configure_otel() has been called with a live endpoint."""
    return _configured
```

**Tests:** `worker/tests/test_retriever_otel.py` — 3 tests:
- When OTel not configured: `OtelSpanExporter` constructor not called / span not emitted
- When configured: attrs dict passed to `OtelSpanExporter` contains `kind`, `query`, `results`
- Retriever still works identically without OTel (existing retriever tests unaffected)

---

## 2. Go: `RetrievalWriter` — fan-out to `retrievals` table

**File:** `internal/clickhouse/retrieval_writer.go` (new)

```go
// RetrievalRow is a single row written to the retrievals table.
type RetrievalRow struct {
    TraceID   string    `ch:"trace_id"`
    SpanID    string    `ch:"span_id"`
    RunID     string    `ch:"run_id"`
    Query     string    `ch:"query"`
    Retriever string    `ch:"retriever"`
    TopK      uint32    `ch:"top_k"`
    Results   []struct {   // Array(Tuple(passage_id String, score Float32, rank UInt32))
        PassageID string  `ch:"passage_id"`
        Score     float32 `ch:"score"`
        Rank      uint32  `ch:"rank"`
    } `ch:"results"`
    StartTime   time.Time `ch:"start_time"`
    DurationMs  uint32    `ch:"duration_ms"`
}

// RetrievalWriter buffers retrieval rows and flushes them asynchronously.
// Same async pattern as BatchWriter — retrieval spans are telemetry, not
// durable state, so async_insert=1 (connection-level default) is correct.
type RetrievalWriter struct { /* same shape as BatchWriter */ }

func NewRetrievalWriter(conn driver.Conn, log *slog.Logger) *RetrievalWriter
func (w *RetrievalWriter) Write(row RetrievalRow)
func (w *RetrievalWriter) Start(ctx context.Context)
func (w *RetrievalWriter) Stop()
// flush uses "INSERT INTO retrievals"
```

**Parsing `results` from attributes:** The `results` attribute in the OTel span is a JSON
string: `[{"chunk_id":"...","doc_id":"...","score":0.9}, ...]`. The Go server parses this
into `[]RetrievalRow.Results` where `passage_id = doc_id` (the `retrievals` schema uses
`passage_id`; this is a deliberate rename to match the architecture doc, which refers to
passage-level IDs).

**File:** `internal/otlp/server.go` (modified)

`Server` gains an optional `retrievalSink` field:

```go
type retrievalSink interface {
    Write(chwriter.RetrievalRow)
}

type Server struct {
    collectorv1.UnimplementedTraceServiceServer
    writer        spanSink
    retrievalSink retrievalSink  // nil → no retrieval fan-out
    logger        *slog.Logger
}

func NewServer(w spanSink, log *slog.Logger) *Server
func NewServerWithRetrieval(w spanSink, rw retrievalSink, log *slog.Logger) *Server
```

In `Export`, after writing the span row, check:

```go
if s.retrievalSink != nil && span.GetName() == "retrieve" && attrs["kind"] == "retrieval" {
    row := parseRetrievalRow(spanRow, attrs)
    s.retrievalSink.Write(row)
}
```

`parseRetrievalRow` is a private function that:
1. Copies `TraceID`, `SpanID`, `RunID` from the `SpanRow`
2. Reads `query`, `retriever` from attrs (string)
3. Parses `top_k` from attrs string → uint32
4. Parses `results` JSON string → `[]struct{PassageID, Score, Rank}`
5. Computes `DurationMs` from `EndTime - StartTime`

**`cmd/collector/main.go` (modified):**

```go
rw := ch.NewRetrievalWriter(conn, log)
rw.Start(ctx)
// ...
grpcSrv := grpc.NewServer()
collectorv1.RegisterTraceServiceServer(
    grpcSrv,
    otlpserver.NewServerWithRetrieval(writer, rw, log),
)
// ...
// on shutdown:
rw.Stop()
```

**Tests:** `internal/clickhouse/retrieval_writer_test.go` (5 tests, same pattern as `eval_writer_test.go`):
- Empty rows → noop
- Writes rows → verifies INSERT target + count + Send
- Parse error (malformed results JSON) → row skipped, no crash

`internal/otlp/server_test.go` additions (3 tests):
- Non-retrieve span → retrievalSink NOT called
- Retrieve span with correct attrs → retrievalSink called once with parsed row
- Retrieve span with malformed results → retrievalSink not called (or called with empty results)

---

## 3. Go: `GET /api/v1/retrievals`

**File:** `internal/clickhouse/retrieval_reader.go` (new)

```go
// RetrievalQueryRow is one row returned by ListRetrievals.
type RetrievalQueryRow struct {
    TraceID   string
    SpanID    string
    RunID     string
    Query     string
    Retriever string
    TopK      uint32
    RecallAtK uint8
    StartTime time.Time
    DurationMs uint32
}

type RetrievalReader struct{ conn queryConn }

func NewRetrievalReader(conn driver.Conn) *RetrievalReader

// ListRetrievals returns retrieval rows for a run, most recent first, up to 500.
func (r *RetrievalReader) ListRetrievals(ctx context.Context, runID string) ([]RetrievalQueryRow, error)
```

Query: `SELECT trace_id, span_id, run_id, query, retriever, top_k, recall_at_k, start_time, duration_ms FROM retrievals WHERE run_id = ? ORDER BY start_time DESC LIMIT 500`

**`internal/api/handler.go` additions:**

```go
type retrievalQuerier interface {
    ListRetrievals(ctx context.Context, runID string) ([]clickhouse.RetrievalQueryRow, error)
}
```

New field `retrievalReader retrievalQuerier` + setter `WithRetrievals(r retrievalQuerier) *Handler`.

New route: `GET /api/v1/retrievals` (query param `?run_id=`).

Response JSON: `[{trace_id, span_id, run_id, query, retriever, top_k, recall_at_k, start_time, duration_ms}]`.
503 when `h.retrievalReader == nil`.

**`cmd/orchestrator/main.go`:** After `WithEvals`, also wire:
```go
rr := clickhouse.NewRetrievalReader(chConn)
h = h.WithRetrievals(rr)
```

**Tests:** 3 new handler tests (503 when nil, happy path, empty result).

---

## 4. Python: ClickHouse miner path

**File:** `worker/helix/rag/miner/miner.py` (modified)

New function:

```python
async def mine_from_clickhouse(
    examples: list[Example],
    eval_results: list[EvalResultRow],
    corpus: dict[str, str],
    *,
    ch_url: str,
    recall_scorer: str = _RECALL_SCORER,
    max_hard_negatives: int = 4,
) -> list[FailureCase]:
    """Mine retrieval failures from ClickHouse retrievals table.

    Queries the `retrievals` table for spans whose run_id appears in the
    eval_results, then performs the same failure-identification logic as
    mine_failures().
    """
    ...
```

Uses `clickhouse-connect` (already available in the worker env) to query:
```sql
SELECT span_id, run_id, query, retriever, top_k, results
FROM retrievals
WHERE run_id IN ({run_ids})
ORDER BY start_time
```

The `results` column is `Array(Tuple(...))` — `clickhouse-connect` returns this as a list
of tuples. Convert to the same shape the JSONL miner expects, then reuse the existing
`_union_retrieved`, `_best_retrieval_span`, etc. helpers.

**`helix/cli.py` `_finetune` update:** Replace the inline `mine_failures(...)` call with:

```python
ch_url = os.environ.get("CLICKHOUSE_URL", "")
if ch_url:
    cases = await mine_from_clickhouse(
        train_dataset, eval_results, corpus, ch_url=ch_url
    )
else:
    cases = mine_failures(train_dataset, eval_results, corpus, spans_path=spans_path)
```

**Tests:** `worker/tests/test_miner_clickhouse.py` (4 tests):
- `mine_from_clickhouse` with mocked `clickhouse-connect` client returns correct `FailureCase` list
- Falls back gracefully when `ch_url` is empty (tested via `_finetune` path)
- Handles empty `retrievals` result (no failures)
- Handles span with malformed `results` tuple

---

## 5. Web: retrieval view

**`web/openapi.yaml` additions:**

```yaml
/retrievals:
  get:
    operationId: listRetrievals
    parameters:
      - in: query
        name: run_id
        schema: { type: string }
    responses:
      "200":
        content:
          application/json:
            schema:
              type: array
              items: { $ref: "#/components/schemas/RetrievalRow" }
      "503":
        description: ClickHouse not configured.

RetrievalRow:
  type: object
  required: [span_id, run_id, query, retriever, top_k, recall_at_k, start_time]
  properties:
    trace_id: { type: string }
    span_id: { type: string }
    run_id: { type: string }
    query: { type: string }
    retriever: { type: string }
    top_k: { type: integer }
    recall_at_k: { type: integer }
    start_time: { type: string, format: date-time }
    duration_ms: { type: integer }
```

**BFF route:** `web/app/api/retrievals/route.ts` — GET with optional `?run_id=` param, proxy to orchestrator.

**Component:** `web/components/retrieval-table.tsx` — client component, `useQuery(["retrievals", runId], ..., { staleTime: Infinity })`. Columns: Query (truncated to 80 chars), Retriever, top_k, Recall (✓/✗). 503-tolerant (shows "Retrieval data unavailable" message). No polling (results are immutable once the eval run finishes).

**Page:** `web/app/retrievals/page.tsx` — accepts optional `?run_id=` search param for deep-linking from the eval detail page.

**Nav:** Add "Retrievals" link to layout nav.

**Eval detail link:** `web/components/eval-detail.tsx` — add "View retrievals →" button linking to `/retrievals?run_id={eval_id}`.

**Tests:** 4 vitest tests for `<RetrievalTable>` (renders rows, 503 message, empty state, truncates long queries).

---

## 6. Task order

1. **Python: `helix/otel.py`** — add `is_configured()` export
2. **Python: `worker/helix/rag/retriever.py`** — dual-write OTel retrieval span after JSONL span
3. **Python tests:** `test_retriever_otel.py` (3 tests)
4. **Go: `internal/clickhouse/retrieval_writer.go`** + `retrieval_writer_test.go` (5 tests)
5. **Go: `internal/clickhouse/retrieval_reader.go`** (new)
6. **Go: `internal/otlp/server.go`** — add `retrievalSink` + `NewServerWithRetrieval`; detection + parsing
7. **Go: `internal/otlp/server_test.go`** — 3 new tests
8. **Go: `internal/api/handler.go`** — `WithRetrievals`, `GET /api/v1/retrievals`
9. **Go: `internal/api/handler_test.go`** — 3 new tests
10. **Go: `cmd/orchestrator/main.go`** — wire `WithRetrievals`
11. **Go: `cmd/collector/main.go`** — wire `RetrievalWriter` + `NewServerWithRetrieval`
12. **Python: `worker/helix/rag/miner/miner.py`** — `mine_from_clickhouse`
13. **Python: `worker/helix/cli.py`** — route to ClickHouse path when `CLICKHOUSE_URL` set
14. **Python tests:** `test_miner_clickhouse.py` (4 tests)
15. **Web: `openapi.yaml`** additions + `npm run gen:types`
16. **Web: BFF route** + types + api function
17. **Web: `<RetrievalTable>` + page** + nav + eval-detail link
18. **Web: vitest tests** (4 tests)
19. **Docs + changelog + HANDOFF**

---

## 7. Definition of done

- [ ] `retrievals` table receives rows when a `retrieve` span is exported via OTel.
- [ ] Go unit tests pass: `RetrievalWriter` + `RetrievalReader` + `otlp/server` fan-out + handler.
- [ ] `mine_from_clickhouse` correctly identifies the same failures as the JSONL miner on equivalent data.
- [ ] `make finetune` routes to ClickHouse path when `CLICKHOUSE_URL` set; JSONL path still works without it.
- [ ] `GET /api/v1/retrievals?run_id=X` returns retrieval rows; 503 when ClickHouse unconfigured.
- [ ] `/retrievals` dashboard page renders, 503-tolerant.
- [ ] All vitest tests pass; `tsc --noEmit` clean.
- [ ] `make lint && make test` pass (Python + Go + web).
- [ ] `CHANGELOG.md`, `HANDOFF.md` updated.
