# M5 Plan — Trace Endpoint + Dashboard Trace View + CI Wiring

> **Status: DRAFT — awaiting sign-off.**
>
> M5 closes the observability loop: the Go orchestrator gains a trace endpoint that
> reads ClickHouse spans and presigns MinIO blob URIs; the dashboard gains a span-tree
> trace view; and the `web` gate is wired into the root CI targets so the dashboard
> is no longer a second-class citizen.
>
> The failure miner (reading ClickHouse `retrievals` + `eval_events`) and the
> retrieval writer (Python worker → ClickHouse `retrievals` via the Go collector) are
> intentionally deferred to M6: they require the collector to fan out retrieval spans
> into a second table, which is an independent change that would bloat this milestone.

---

## 1. What M5 delivers

| Piece | M4 state | M5 target |
|---|---|---|
| `GET /api/v1/runs/{id}/trace` | Not implemented | Returns span tree from ClickHouse; `*_uri` attributes replaced by presigned MinIO URLs |
| Dashboard trace view | None | `/runs/[id]/trace` — collapsible span tree, lazy blob hydration |
| Run detail → trace link | None | "View trace" button on run-detail page |
| `web` in CI | Separate `make web-gate` | Folded into root `make lint` + `make test` |
| ClickHouse `retrievals` / `eval_events` | Empty shell tables | Still empty (M6) |
| Failure miner reading ClickHouse | Not started | M6 |

### What M5 does NOT deliver

- Retrieval writer: Python worker writing to `retrievals` table (M6 — needs collector fan-out)
- Eval REST endpoints (`/api/v1/evals/...`) and eval dashboard view (M6)
- SSE live updates (`/runs/{id}/events`) (deferred)
- OIDC / OAuth2 user auth (out of scope v1)

---

## 2. Architecture

### 2.1 Trace endpoint

```
GET /api/v1/runs/{run_id}/trace
  │ (Go handler)
  ├── store.GetRun(runID) → trace_id
  ├── clickhouse.GetTraceSpans(trace_id) → []SpanRecord
  └── minio.Presigner.PresignAttrs(attrs, expiry=1h)
        for each attr value starting with "s3://": replace with presigned HTTPS URL
  → JSON response: { "trace_id": "...", "spans": [...] }
```

Span attributes containing `s3://` URIs (e.g. `prompt_uri`, `completion_uri`) are
rewritten to presigned HTTPS URLs before the response is serialised. Spans without
blob payloads are returned unchanged. The presigner is a no-op when `S3_ENDPOINT` is
not set (local dev without MinIO).

### 2.2 Dashboard trace view

```
Browser (span-tree.tsx — client component, TanStack Query)
  │  same-origin fetch /api/runs/{id}/trace
  ▼
Next.js BFF  web/app/api/runs/[id]/trace/route.ts   [server-side, attaches token]
  │
  ▼
Go orchestrator  GET /api/v1/runs/{id}/trace
  ├── ClickHouse spans table
  └── MinIO presign (for blob attrs)
```

The BFF follows the same pattern as the existing runs BFF. The span-tree client
component receives the pre-signed URL list; blobs are fetched lazily on accordion
expand (direct browser fetch of the presigned URL — no BFF hop, no token needed).

---

## 3. Backend changes (Go)

### 3.1 ClickHouse reader

New file `internal/clickhouse/reader.go`:

```go
type SpanRecord struct {
    TraceID       string
    SpanID        string
    ParentSpanID  string
    RunID         string
    TaskID        string
    AttemptNumber uint32
    Name          string
    Kind          string
    StartTime     time.Time
    EndTime       time.Time
    DurationMS    uint32
    Status        string
    StatusMessage string
    ServiceName   string
    WorkerID      string
    Attributes    map[string]string
}

type SpanReader struct { conn driver.Conn }

func NewSpanReader(conn driver.Conn) *SpanReader

func (r *SpanReader) GetTraceSpans(ctx context.Context, traceID string) ([]SpanRecord, error)
// SELECT ... FROM spans WHERE trace_id = ? ORDER BY start_time
```

### 3.2 MinIO presigner

New file `internal/minio/presigner.go`:

```go
type Presigner struct {
    client *minio.Client
}

// FromEnv returns a Presigner from S3_ENDPOINT / S3_ACCESS_KEY / S3_SECRET_KEY,
// or nil when S3_ENDPOINT is unset.
func FromEnv() (*Presigner, error)

// PresignAttrs rewrites any attribute value starting with "s3://" to a presigned URL.
// No-op when p is nil.
func (p *Presigner) PresignAttrs(attrs map[string]string, expiry time.Duration) (map[string]string, error)

// presignURI converts "s3://bucket/key" to a presigned HTTPS GET URL.
func (p *Presigner) presignURI(uri string, expiry time.Duration) (string, error)
```

New Go dependency: `github.com/minio/minio-go/v7`.

### 3.3 `getTrace` handler

`internal/api/handler.go`:

- New route: `r.Get("/api/v1/runs/{run_id}/trace", h.getTrace)`
- `Handler` gains two optional fields: `spanReader *clickhouse.SpanReader` and
  `presigner *minio.Presigner` (both nil-safe — endpoint returns 503 with a clear
  message when ClickHouse is not configured).
- `getTrace` implementation:
  1. `store.GetRun(runID)` → get `trace_id`
  2. `spanReader.GetTraceSpans(ctx, traceID)` → `[]SpanRecord`
  3. For each span, `presigner.PresignAttrs(span.Attributes, 1h)` → rewritten attrs
  4. Write `traceResponse{TraceID, Spans}` as JSON

`traceResponse` and `spanResponse` are response-only types (not stored in the DB):

```go
type traceResponse struct {
    TraceID string         `json:"trace_id"`
    Spans   []spanResponse `json:"spans"`
}

type spanResponse struct {
    SpanID        string            `json:"span_id"`
    ParentSpanID  string            `json:"parent_span_id"`
    RunID         string            `json:"run_id"`
    TaskID        string            `json:"task_id"`
    AttemptNumber uint32            `json:"attempt_number"`
    Name          string            `json:"name"`
    Kind          string            `json:"kind"`
    StartTime     time.Time         `json:"start_time"`
    EndTime       time.Time         `json:"end_time"`
    DurationMS    uint32            `json:"duration_ms"`
    Status        string            `json:"status"`
    StatusMessage string            `json:"status_message,omitempty"`
    ServiceName   string            `json:"service_name,omitempty"`
    Attributes    map[string]string `json:"attributes,omitempty"`
}
```

`Handler.NewHandler` signature **does not change** — the new deps are injected via
separate setters or a config struct to keep the existing handler tests untouched:

```go
func (h *Handler) WithTrace(sr *clickhouse.SpanReader, p *minio.Presigner) *Handler
```

`cmd/orchestrator/main.go` calls `WithTrace(sr, presigner)` when both `CLICKHOUSE_URL`
and (optionally) `S3_ENDPOINT` are set.

### 3.4 Tests

- `internal/clickhouse/reader_test.go` — unit test with a mock `driver.Conn` stub
  verifying the SQL query shape and row mapping.
- `internal/minio/presigner_test.go` — `PresignAttrs` with a fake presigner; verifies
  only `s3://` values are rewritten, other values are preserved, nil presigner is no-op.
- `internal/api/handler_test.go` — `TestGetTrace_*`:
  - happy path: GetRun succeeds, GetTraceSpans returns 2 spans, presigner rewrites one
    `s3://` attribute, response JSON is correct.
  - run not found → 404.
  - ClickHouse unavailable (nil spanReader) → 503.
  - GetTraceSpans error → 500.

No migration needed (ClickHouse schema is unchanged; MinIO is already in docker-compose).

---

## 4. Frontend (`web/`)

### 4.1 New files

```
web/
  app/
    api/
      runs/[id]/trace/route.ts        BFF: GET trace
    runs/[id]/trace/
      page.tsx                        Trace view shell (server)
  components/
    span-tree.tsx                     Client: collapsible span tree (TanStack Query)
    span-detail.tsx                   Attributes table + blob accordion
  lib/
    api.ts                            + fetchTrace(id)
  openapi.yaml                        + /runs/{run_id}/trace path + SpanRecord schema
```

### 4.2 `web/openapi.yaml` additions

Add `/api/v1/runs/{run_id}/trace` path and the `TraceResponse` / `SpanRecord` schemas.
Regenerate `web/lib/api-types.ts` with `npm run gen:types`.

### 4.3 Pages and components

**`/runs/[id]/trace` page:**
- Server shell component — just passes `id` to the client component.
- Breadcrumb: Runs → `{id}` (links to `/runs/[id]`) → Trace.

**`span-tree.tsx` (client component):**
- TanStack Query `useQuery(['trace', id], () => fetchTrace(id))`, `refetchInterval: false`
  (traces are immutable once the run completes).
- Builds a parent-child tree from `parent_span_id` → nested accordion rows.
- Each row: span name, kind badge (same `StatusBadge` pattern), duration, expand arrow.
- On expand: shows `SpanDetail` with all attributes; any `*_url` attribute (presigned) is
  rendered as a lazy-load "Load payload" button.

**`span-detail.tsx`:**
- Renders `attributes` as a `<dl>` / table.
- For values that are HTTPS URLs (`prompt_url`, `completion_url` — post-presign they are
  HTTPS): renders a `<button>Load payload</button>` that fetches the URL directly (no
  BFF — presigned URL is public within expiry) and shows the text in a `<pre>`.

**`run-detail.tsx` update:**
- Add a "View trace" `<Link>` to `/runs/[id]/trace` (shown whenever `trace_id` is set).

### 4.4 BFF route

`web/app/api/runs/[id]/trace/route.ts`:
- `GET` — proxy to `${ORCHESTRATOR_URL}/api/v1/runs/${id}/trace` with Bearer token.
- Propagates non-2xx status codes (503 when ClickHouse is not configured renders as "Trace not available").

### 4.5 Tests (vitest)

- `span-tree.test.tsx` — renders flat list of 3 spans, verifies nesting, expand shows attributes.
- `span-detail.test.tsx` — attribute with `_url` suffix renders "Load payload" button; non-URL
  attribute renders inline text.
- `orchestrator.test.ts` — `fetchTrace` calls correct BFF path.
- BFF route test — attaches bearer token, propagates 503.

---

## 5. CI wiring

`Makefile` changes (additive):

```makefile
# Fold web into the root gates now that Node 22 is confirmed in the cloud.
lint: web-lint
	cd worker && ruff check . && mypy --strict helix/ && ...
	golangci-lint run ./...

test: web-test
	cd worker && python -m pytest tests/ -x -q
	go test ./cmd/... ./internal/... ./gen/...
```

Or, if adding web to both is too noisy for the local dev loop, add an explicit `make ci`
target that callers (GitHub Actions) invoke instead of the root `make test`/`make lint`:

```makefile
ci: lint test web-gate
```

**Sign-off question:** prefer folding into root targets (simpler) or a separate `make ci`
(less surprise for local dev that doesn't have Node)?

`AGENTS.md` phase note updated: M4 → M5.

---

## 6. What's verifiable where

| Check | Cloud | Local |
|---|---|---|
| Go: reader/presigner/handler unit tests, build | ✅ | ✅ |
| Web: BFF route, span-tree, vitest, next build | ✅ (Node 22) | ✅ |
| Trace endpoint against live ClickHouse + MinIO | ❌ (no Docker) | ✅ |
| Span payloads round-trip (worker → collector → MinIO → presign → UI) | ❌ | ✅ |

---

## 7. Open decisions (for sign-off)

1. **CI target** — fold `web-gate` into root `make lint` / `make test` (simple, recommended),
   or add a separate `make ci` target (keeps local dev node-free)?
2. **Blob fetch strategy** — lazy per-span (recommended, cheap when trace has few blobs),
   or eager all-at-once on page load? Lazy is the M4 plan default; confirming.
3. **503 UX** — when ClickHouse is not configured (local without `make dev`), show "Trace not
   available" inline or redirect back to run detail?

---

## 8. Task order (serial)

1. Go: `internal/clickhouse/reader.go` + tests.
2. Go: `internal/minio/presigner.go` + new dependency + tests.
3. Go: `getTrace` handler + `WithTrace` setter + handler tests + route registration.
4. `web/openapi.yaml` additions + `npm run gen:types`.
5. BFF route `web/app/api/runs/[id]/trace/route.ts` + test.
6. `span-tree.tsx` + `span-detail.tsx` + trace page + tests.
7. `run-detail.tsx` — add "View trace" link.
8. Makefile CI wiring + AGENTS.md phase update.
9. Docs: `docs/api.md` (trace endpoint), `CHANGELOG.md`, `HANDOFF.md`.

---

## 9. Definition of done

- [ ] `GET /api/v1/runs/{id}/trace` returns ClickHouse spans with presigned MinIO URLs;
      Go unit tests pass; `go build` clean.
- [ ] `/runs/[id]/trace` renders a collapsible span tree; "View trace" link on run detail.
- [ ] `next build`, eslint, prettier, `tsc --noEmit`, vitest all green.
- [ ] `web-gate` wired into root CI (or `make ci` target agreed); `make lint` + `make test`
      + `make web-gate` all green.
- [ ] `docs/api.md` updated (trace endpoint); `CHANGELOG.md` + `HANDOFF.md` updated.
