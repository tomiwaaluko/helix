# M8 Plan — LLM Calls Fan-out + Cost Dashboard

## Goal

Populate the `llm_calls` ClickHouse table (created as an empty shell in M2) by
dual-writing from the Python LiteLLM adapter via OTel, expose a `GET /api/v1/llm-calls`
endpoint, and add a `/llm-calls` cost-and-token dashboard page.

This closes the last observability gap: after M8 every production event
(spans, retrievals, eval scores, LLM calls) flows through ClickHouse and is
visible on the dashboard.

---

## Architecture

```
Python llm_call()
  └─ SpanLogger.span("llm_call", kind="llm")  ← JSONL (unchanged)
  └─ _emit_llm_call_otel(attrs)                ← NEW: OTel span after JSONL span closes
        │
        ▼  OTLP/gRPC
  Go collector (server.go)
    ├─ spanSink       → spans table (unchanged)
    └─ llmCallSink    → llm_calls table  ← NEW: LlmCallWriter fan-out
        │
        ▼  ClickHouse
  llm_calls table (already in DDL)
        │
        ▼
  GET /api/v1/llm-calls  ← NEW: LlmCallReader + handler
        │
        ▼
  /llm-calls dashboard page  ← NEW
```

---

## Task order

### 1. Python: `_emit_llm_call_otel()` in `litellm_adapter.py`

After the `with logger.span("llm_call", kind="llm") as attrs:` block closes
(after JSONL span is written), emit a lightweight OTel span carrying:

```python
otel_attrs = {
    "kind": "llm",
    "model": attrs["model"],           # e.g. "claude-sonnet-4-20250514"
    "provider": _provider_from_model(attrs["model"]),  # "anthropic" / "openai" / ...
    "prompt_tokens": str(attrs["prompt_tokens"]),
    "completion_tokens": str(attrs["completion_tokens"]),
    "cost_usd": str(attrs["cost_usd"]),
    "cache_hit": "true" if attrs["cache_hit"] else "false",
    "run_id": current_eval_run_id.get(),
}
with OtelSpanExporter("llm_call", otel_attrs):
    pass
```

No-op when `is_configured()` is False (same gate as M7 retrieval fan-out).

`_provider_from_model(model: str) -> str`:
- `"claude"` prefix → `"anthropic"`
- `"gpt-"` / `"o1-"` / `"o3-"` / `"o4-"` prefix → `"openai"`
- `"gemini"` prefix → `"google"`
- else → `"unknown"`

### 2. Python test: `worker/tests/test_llm_call_otel.py` (3 tests)

- `test_emit_llm_call_otel_noop_when_not_configured` — OtelSpanExporter not called
- `test_emit_llm_call_otel_emits_span_with_correct_attrs` — captures (name, attrs), checks kind/model/provider/tokens/cost
- `test_emit_llm_call_otel_includes_run_id_from_contextvar`

### 3. Go: `internal/clickhouse/llm_call_writer.go` (new)

`LlmCallRow` struct (mirrors DDL):
```go
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
```

`LlmCallWriter` — same async buffered-channel pattern as `RetrievalWriter`
(channel size 1000, 200 ms ticker, maxBatch 500, INSERT INTO llm_calls).

### 4. Go: `internal/clickhouse/llm_call_reader.go` (new)

`LlmCallQueryRow` (omits embedding/blob columns):
```go
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
```

`LlmCallReader.ListLlmCalls(ctx, runID string) ([]LlmCallQueryRow, error)`:
- With runID: `WHERE run_id = ? ORDER BY start_time DESC LIMIT 500`
- Without: `ORDER BY start_time DESC LIMIT 500`

### 5. Go: `internal/clickhouse/llm_call_writer_test.go` (new, 5 tests)

Reuses `mockBatch`/`mockBatchConn` from `eval_writer_test.go`:
- `TestLlmCallWriter_Flush_EmptyBatch_Noop`
- `TestLlmCallWriter_Flush_WritesRows` (verifies INSERT target, row count, Send/Close)
- `TestLlmCallWriter_Flush_AppendError`
- `TestLlmCallWriter_Flush_SendError`
- `TestLlmCallWriter_Flush_PrepareError`

### 6. Go: `internal/otlp/server.go` (modify)

Add `llmCallSink` interface and optional field to `Server`:
```go
type llmCallSink interface {
    Write(chwriter.LlmCallRow)
}
```

Add `WithLlmCalls(sink llmCallSink) *Server` setter (same pattern as HTTP Handler).

In `Export()`, after the existing `retrievalSink` fan-out block:
```go
if s.llmCallSink != nil && span.GetName() == "llm_call" && attrs["kind"] == "llm" {
    lc := parseLlmCallRow(row, attrs, span)
    s.llmCallSink.Write(lc)
}
```

`parseLlmCallRow(sr SpanRow, attrs map[string]string, span *tracev1.Span) LlmCallRow`:
- `PromptTokens`: `strconv.ParseUint(attrs["prompt_tokens"], 10, 32)`
- `CompletionTokens`: `strconv.ParseUint(attrs["completion_tokens"], 10, 32)`
- `TotalTokens`: prompt + completion
- `CostUSD`: `strconv.ParseFloat(attrs["cost_usd"], 64)`
- `Provider`: `attrs["provider"]`
- `DurationMs`: from nano timestamps (same as `parseRetrievalRow`)
- `Status`: `sr.Status`
- `PromptURI`/`CompletionURI`: empty (blob offload is future work)

### 7. Go: `internal/otlp/server_test.go` (modify, 3 new tests)

Add `captureLlmCallSink` (same pattern as `captureRetrievalSink`):
- `TestExport_LlmCallSpan_CallsLlmCallSink` — verifies model, provider, tokens, cost
- `TestExport_NonLlmSpan_DoesNotCallLlmCallSink`
- `TestExport_NilLlmCallSink_DoesNotPanic`

### 8. Go: `internal/api/handler.go` (modify)

Add `llmCallQuerier` interface, `llmCallReader` field, `WithLlmCalls(r)` setter,
`GET /api/v1/llm-calls` route, `listLlmCalls` handler, `llmCallRowResponse` type.

Response type:
```go
type llmCallRowResponse struct {
    TraceID          string    `json:"trace_id"`
    SpanID           string    `json:"span_id"`
    RunID            string    `json:"run_id"`
    Provider         string    `json:"provider"`
    Model            string    `json:"model"`
    PromptTokens     uint32    `json:"prompt_tokens"`
    CompletionTokens uint32    `json:"completion_tokens"`
    TotalTokens      uint32    `json:"total_tokens"`
    CostUSD          float64   `json:"cost_usd"`
    StartTime        time.Time `json:"start_time"`
    DurationMs       uint32    `json:"duration_ms"`
    Status           string    `json:"status"`
}
```

Handler returns 503 when `llmCallReader == nil`.

### 9. Go: `internal/api/handler_test.go` (modify, 3 new tests)

- `TestListLlmCalls_NilReader_Returns503`
- `TestListLlmCalls_ReturnsTwoRows`
- `TestListLlmCalls_PassesRunIdParam`

### 10. Go: `cmd/collector/main.go` (modify)

```go
lcw := ch.NewLlmCallWriter(conn, log)
lcw.Start(ctx)
// ...
collectorv1.RegisterTraceServiceServer(grpcSrv, otlpserver.NewServerWithRetrieval(writer, rw, log).WithLlmCalls(lcw))
// ...
lcw.Stop()
```

### 11. Go: `cmd/orchestrator/main.go` (modify)

```go
lcr := clickhouse.NewLlmCallReader(chConn)
h = h.WithLlmCalls(lcr)
log.Info("llm-calls endpoint enabled")
```

### 12. Web: `web/openapi.yaml` (modify)

Add `LlmCallRow` schema and `GET /llm-calls` path with `?run_id=` param.

### 13. Web: `npm run gen:types`

### 14. Web: BFF route `web/app/api/llm-calls/route.ts`

GET proxy with `?run_id=` passthrough.

### 15. Web: `web/lib/types.ts` + `web/lib/api.ts`

Add `LlmCallRow` type alias and `fetchLlmCalls(runId?)`.

### 16. Web: `web/components/llm-call-table.tsx`

Client component. Columns: provider, model, prompt tokens, completion tokens,
total tokens, cost ($), run ID, start time, duration ms.
`staleTime: Infinity`. 503-tolerant error UX.
Cache hits shown in muted color (cost = $0.00).

### 17. Web: `web/app/llm-calls/page.tsx`

```tsx
import { LlmCallTable } from "@/components/llm-call-table";
export default function LlmCallsPage() {
  return <div className="space-y-4"><h1 …>LLM Calls</h1><LlmCallTable /></div>;
}
```

### 18. Web: `web/app/layout.tsx` — add "LLM Calls" nav link

### 19. Web: `web/components/eval-detail.tsx` — add "View LLM calls →" link

Alongside existing "View retrievals →".

### 20. Web: `web/components/__tests__/llm-call-table.test.tsx` (4 vitest tests)

Same pattern as `retrieval-table.test.tsx`:
- renders a row with all fields
- shows empty state
- shows 503 message
- shows generic error

---

## Definition of done

- `go build ./...` clean
- `go test ./internal/...` green (all existing + 11 new)
- `python -m pytest tests/ -q` green (all existing + 3 new)
- `npm run test -- --run` green (all existing + 4 new)
- `ruff check .` + `mypy --strict helix/` clean
- `CHANGELOG.md` and `HANDOFF.md` updated
- Pushed to `claude/eloquent-clarke-qiha1x`

## Files changed

| File | Change |
|---|---|
| `worker/helix/tools/litellm_adapter.py` | add `_provider_from_model`, `_emit_llm_call_otel` |
| `worker/tests/test_llm_call_otel.py` | new, 3 tests |
| `internal/clickhouse/llm_call_writer.go` | new |
| `internal/clickhouse/llm_call_reader.go` | new |
| `internal/clickhouse/llm_call_writer_test.go` | new, 5 tests |
| `internal/otlp/server.go` | llmCallSink + WithLlmCalls + parseLlmCallRow |
| `internal/otlp/server_test.go` | 3 new tests |
| `internal/api/handler.go` | llmCallQuerier + WithLlmCalls + listLlmCalls |
| `internal/api/handler_test.go` | 3 new tests |
| `cmd/collector/main.go` | LlmCallWriter wired |
| `cmd/orchestrator/main.go` | WithLlmCalls wired |
| `web/openapi.yaml` | LlmCallRow schema + /llm-calls path |
| `web/lib/api-types.ts` | regenerated |
| `web/lib/types.ts` | LlmCallRow alias |
| `web/lib/api.ts` | fetchLlmCalls |
| `web/app/api/llm-calls/route.ts` | new BFF route |
| `web/app/llm-calls/page.tsx` | new page |
| `web/components/llm-call-table.tsx` | new component |
| `web/components/__tests__/llm-call-table.test.tsx` | new, 4 tests |
| `web/app/layout.tsx` | nav link |
| `web/components/eval-detail.tsx` | "View LLM calls →" link |
| `CHANGELOG.md` | M8 entry |
| `HANDOFF.md` | M8 entry |
