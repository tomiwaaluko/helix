# M6 Plan — Eval Views Slice

> **Status: IMPLEMENTED.** See the M6 entry in `CHANGELOG.md`.
>
> M6 closes the observability loop for the research experiment by making eval results
> visible in the dashboard. The Python harness gains an optional reporter that posts
> scored results to the orchestrator; the orchestrator writes them synchronously to
> ClickHouse `eval_events` and exposes two new read endpoints; the dashboard gains
> `/evals` (list) and `/evals/[id]` (per-example breakdown) pages.
>
> The ClickHouse `retrievals` fan-out and production failure-miner are intentionally
> deferred to M7: they require the collector to detect `kind=retrieval` spans and write
> to a second table, which is a non-trivial change orthogonal to eval visibility.

---

## 1. What M6 delivers

| Piece | M5 state | M6 target |
|---|---|---|
| `eval_events` table | Empty shell | Written by orchestrator on `POST /api/v1/evals/{id}/events`; 503 when ClickHouse unset |
| `GET /api/v1/evals` | Not implemented | Returns eval run summaries (id, examples, started_at, per-scorer mean) |
| `GET /api/v1/evals/{eval_id}` | Not implemented | Returns per-example rows for one eval run |
| Dashboard `/evals` | None | Table of eval runs, auto-refreshing, per-scorer mean scores |
| Dashboard `/evals/[id]` | None | Per-example table with scores, link to source run |
| Python harness reporter | None | Optional `OrchestratorEvalReporter`; no-op when `HELIX_ORCHESTRATOR_URL` unset |
| Makefile | Unchanged | `make eval` posts results when orchestrator env vars are set |

### What M6 does NOT deliver

- `retrievals` table fan-out (collector-side, M7)
- Failure miner reading ClickHouse (M7 — depends on `retrievals` data)
- Helm / Kubernetes (M8)
- Fine-tune trigger from the dashboard (M7)

---

## 2. Architecture

```
make eval (Python harness)
  │  runs workflow over dataset, scores each example
  │  if HELIX_ORCHESTRATOR_URL + HELIX_API_TOKEN set:
  └─► POST /api/v1/evals/{eval_id}/events  (batch, per-example)
        │ (Go handler — authenticated)
        └─► EvalWriter.Record(ctx, rows)    (synchronous ClickHouse INSERT)
              eval_events table

Browser
  │  GET /evals  →  GET /api/evals (BFF)  →  GET /api/v1/evals
  └─► <EvalTable> client component (polls 10 s)

Browser
  │  GET /evals/[id]  →  GET /api/evals/[id] (BFF)  →  GET /api/v1/evals/{eval_id}
  └─► <EvalDetail> client component (static, evals are immutable)
```

**Synchronous ClickHouse writes:** `AGENTS.md` mandates synchronous writes for eval
results ("Eval results are synchronous (required for the research thesis)"). The
existing `BatchWriter` is async (correct for spans). `EvalWriter` is a new thin struct
that calls `PrepareBatch` + `Send` inline with `SETTINGS async_insert=0` appended to the
INSERT statement, overriding the connection-level `async_insert=1`. Both writers share the
same `driver.Conn` opened by the collector; the orchestrator opens its own connection.

---

## 3. Backend changes (Go)

### 3.1 `internal/clickhouse/eval_writer.go` (new)

```go
// EvalEventRow is a single row written to the eval_events table.
type EvalEventRow struct {
    EvalID    string    `ch:"eval_id"`
    ExampleID string    `ch:"example_id"`
    RunID     string    `ch:"run_id"`
    Scorer    string    `ch:"scorer"`
    Score     float64   `ch:"score"`
    Passed    uint8     `ch:"passed"`
    Details   string    `ch:"details"` // JSON
    Timestamp time.Time `ch:"timestamp"`
}

// EvalWriter writes eval event rows synchronously.
type EvalWriter struct{ conn driver.Conn }

func NewEvalWriter(conn driver.Conn) *EvalWriter

// Record writes rows to eval_events with async_insert disabled so the call
// blocks until ClickHouse has persisted the data.
func (w *EvalWriter) Record(ctx context.Context, rows []EvalEventRow) error
```

The INSERT statement uses `SETTINGS async_insert=0, wait_for_async_insert=0` in the
SQL to bypass the connection-level async setting. `PrepareBatch` + `AppendStruct` +
`Send` follow the same pattern as `BatchWriter.flush`.

### 3.2 `internal/clickhouse/eval_reader.go` (new)

```go
// EvalSummaryRow is one row from the evals list query.
type EvalSummaryRow struct {
    EvalID     string
    Examples   uint64
    StartedAt  time.Time
    FinishedAt time.Time
}

// ScorerMeanRow is one (eval_id, scorer, mean_score) row from the aggregation query.
type ScorerMeanRow struct {
    EvalID string
    Scorer string
    Mean   float64
    N      uint64
}

// EvalEventRow (read) reuses the type from eval_writer.go.

// EvalReader queries the eval_events table.
type EvalReader struct{ conn queryConn } // same private queryConn interface as reader.go

func NewEvalReader(conn driver.Conn) *EvalReader

// ListEvals returns eval run summaries (most recent first, up to 100).
func (r *EvalReader) ListEvals(ctx context.Context) ([]EvalSummaryRow, []ScorerMeanRow, error)

// GetEval returns all per-example rows for one eval run.
func (r *EvalReader) GetEval(ctx context.Context, evalID string) ([]EvalEventRow, error)
```

`ListEvals` runs two queries:
1. `SELECT eval_id, count() AS examples, min(timestamp) AS started_at, max(timestamp) AS finished_at FROM eval_events GROUP BY eval_id ORDER BY started_at DESC LIMIT 100`
2. `SELECT eval_id, scorer, avg(score) AS mean, count() AS n FROM eval_events GROUP BY eval_id, scorer`

The handler joins them in Go to build the response (no ClickHouse JOIN needed).

### 3.3 `internal/api/handler.go` additions

New interfaces (follows the `spanQuerier` / `attrPresigner` pattern):

```go
type evalWriter interface {
    Record(ctx context.Context, rows []clickhouse.EvalEventRow) error
}

type evalQuerier interface {
    ListEvals(ctx context.Context) ([]clickhouse.EvalSummaryRow, []clickhouse.ScorerMeanRow, error)
    GetEval(ctx context.Context, evalID string) ([]clickhouse.EvalEventRow, error)
}
```

New `Handler` fields: `evalWriter evalWriter`, `evalReader evalQuerier` (both nil → endpoints return 503).

```go
func (h *Handler) WithEvals(w evalWriter, r evalQuerier) *Handler
```

New routes registered in `Router()`:

```
POST /api/v1/evals/{eval_id}/events   → h.recordEvalEvents
GET  /api/v1/evals                    → h.listEvals
GET  /api/v1/evals/{eval_id}          → h.getEval
```

**`recordEvalEvents`**: decodes a body of `{"events": [...]}`, validates required fields,
calls `h.evalWriter.Record`. Returns 503 when `h.evalWriter == nil`, 204 on success.
Score must be in [0, 1]; rejects out-of-range with 400.

**`listEvals`**: 503 when reader nil; calls `ListEvals`, joins summaries with scorer means,
returns `[]evalSummaryResponse`.

**`getEval`**: 503 when reader nil; calls `GetEval`; returns 404 on empty result;
returns `[]evalEventResponse`.

Response types:

```go
type evalEventRequest struct {
    Events []evalEventBody `json:"events"`
}

type evalEventBody struct {
    ExampleID string  `json:"example_id"`
    RunID     string  `json:"run_id"`
    Scorer    string  `json:"scorer"`
    Score     float64 `json:"score"`
    Passed    bool    `json:"passed"`
    Details   map[string]any `json:"details,omitempty"`
}

type scorerMean struct {
    Scorer string  `json:"scorer"`
    Mean   float64 `json:"mean"`
    N      uint64  `json:"n"`
}

type evalSummaryResponse struct {
    EvalID     string       `json:"eval_id"`
    Examples   uint64       `json:"examples"`
    StartedAt  time.Time    `json:"started_at"`
    FinishedAt time.Time    `json:"finished_at"`
    Scorers    []scorerMean `json:"scorers"`
}

type evalEventResponse struct {
    EvalID    string  `json:"eval_id"`
    ExampleID string  `json:"example_id"`
    RunID     string  `json:"run_id"`
    Scorer    string  `json:"scorer"`
    Score     float64 `json:"score"`
    Passed    bool    `json:"passed"`
    Details   string  `json:"details"`
    Timestamp string  `json:"timestamp"`
}
```

### 3.4 `cmd/orchestrator/main.go` additions

After the existing `WithTrace` wiring, when `cfg.ClickHouseURL` is set, also open an
`EvalWriter` and `EvalReader` and call `h.WithEvals(ew, er)`. Same error pattern: failure
to connect is a warning (not fatal), leaves eval endpoints at 503.

The orchestrator can reuse the same ClickHouse connection as the span reader (one
connection, both structs share it).

### 3.5 `internal/config/config.go`

No change needed. `ClickHouseURL` is already present.

### 3.6 Tests

**`internal/clickhouse/eval_writer_test.go`:**
- `TestEvalWriter_Record_EmptyRows_Noop` — no SQL on empty batch
- `TestEvalWriter_Record_WritesRows` — mock `driver.Conn` captures INSERT, verifies `async_insert=0` in SQL

**`internal/clickhouse/eval_reader_test.go`:**
- `TestEvalReader_ListEvals_Empty` — returns empty slices, no error
- `TestEvalReader_GetEval_MatchesRows` — verifies column mapping
- `TestEvalReader_GetEval_NotFound` — returns empty slice (handler turns it into 404)

**`internal/api/handler_test.go` additions (5 new tests):**
- `TestRecordEvalEvents_NilWriter_Returns503`
- `TestRecordEvalEvents_HappyPath_Returns204` — posts 3 events, mock writer records them
- `TestListEvals_NilReader_Returns503`
- `TestListEvals_HappyPath` — verifies scorer means joined correctly
- `TestGetEval_NotFound_Returns404` — reader returns empty slice

---

## 4. Python changes

### 4.1 `worker/helix/eval/reporter.py` (new)

```python
from __future__ import annotations
import json
import os
from typing import Protocol
import httpx

class EvalReporter(Protocol):
    async def record(
        self,
        eval_id: str,
        events: list[dict[str, object]],
    ) -> None: ...


class OrchestratorEvalReporter:
    """Posts eval events to the orchestrator REST API.

    No-op when HELIX_ORCHESTRATOR_URL or HELIX_API_TOKEN are unset.
    Failures are logged but do not abort the eval run.
    """
    def __init__(self, base_url: str, token: str) -> None: ...

    async def record(self, eval_id: str, events: list[dict[str, object]]) -> None:
        # POST /api/v1/evals/{eval_id}/events
        # Failures are caught and logged; eval is not aborted.
        ...

def default_reporter() -> OrchestratorEvalReporter | None:
    """Return a reporter if env vars are set, else None."""
    url = os.environ.get("HELIX_ORCHESTRATOR_URL", "")
    token = os.environ.get("HELIX_API_TOKEN", "")
    if not url or not token:
        return None
    return OrchestratorEvalReporter(url, token)
```

Uses `httpx.AsyncClient` (already a transitive dep via litellm). `mypy --strict` clean;
add `httpx` to mypy overrides if not already there.

### 4.2 `worker/helix/eval/harness.py` modifications

`evaluate()` gains an optional `reporter: EvalReporter | None = None` parameter.

Inside `run_one`, after scoring:

```python
if reporter is not None:
    events = [
        {
            "example_id": example.id,
            "run_id": "",   # slice mode has no gRPC run_id; orchestrator accepts ""
            "scorer": scorer_name,
            "score": score,
            "passed": score >= 1.0,
            "details": details or {},
        }
        for scorer_name, (score, details) in scored.items()
    ]
    try:
        await reporter.record(eval_id, events)
    except Exception as exc:
        # Non-fatal: SQLite store has the results regardless.
        import logging
        logging.getLogger(__name__).warning("eval reporter failed: %s", exc)
```

`run_one` collects scores all at once (already does so), then posts in one batch call
per example. No per-scorer HTTP call.

### 4.3 `worker/helix/cli.py` modification

In the `eval` command, after `evaluate()`:

```python
from helix.eval.reporter import default_reporter

reporter = default_reporter()
report = await evaluate(..., reporter=reporter)
```

This is a one-line addition; `reporter` is None when env vars are absent.

### 4.4 No new Python dependencies

`httpx` is already a transitive dep (litellm pulls it in). No `clickhouse-connect` needed
— the Python worker never directly touches ClickHouse, consistent with the architecture.

---

## 5. Frontend (`web/`)

### 5.1 `web/openapi.yaml` additions

Add three paths and four schemas:

```yaml
/evals:
  get:
    operationId: listEvals
    summary: List eval runs (most recent first, up to 100).
    responses:
      "200":
        content:
          application/json:
            schema:
              type: array
              items: { $ref: "#/components/schemas/EvalSummary" }
      "503":
        description: ClickHouse not configured.

/evals/{eval_id}:
  get:
    operationId: getEval
    summary: Per-example scored events for one eval run.
    parameters:
      - in: path
        name: eval_id
        required: true
        schema: { type: string }
    responses:
      "200":
        content:
          application/json:
            schema:
              type: array
              items: { $ref: "#/components/schemas/EvalEvent" }
      "404":
        description: Eval not found.
      "503":
        description: ClickHouse not configured.

/evals/{eval_id}/events:
  post:
    operationId: recordEvalEvents
    summary: Record per-example eval scores (called by the Python harness).
    parameters:
      - in: path
        name: eval_id
        required: true
        schema: { type: string }
    requestBody:
      required: true
      content:
        application/json:
          schema: { $ref: "#/components/schemas/RecordEvalEventsRequest" }
    responses:
      "204":
        description: Recorded.
      "503":
        description: ClickHouse not configured.
```

Schemas:

```yaml
ScorerMean:
  type: object
  required: [scorer, mean, n]
  properties:
    scorer: { type: string }
    mean: { type: number, format: double }
    n: { type: integer }

EvalSummary:
  type: object
  required: [eval_id, examples, started_at, finished_at, scorers]
  properties:
    eval_id: { type: string }
    examples: { type: integer }
    started_at: { type: string, format: date-time }
    finished_at: { type: string, format: date-time }
    scorers:
      type: array
      items: { $ref: "#/components/schemas/ScorerMean" }

EvalEvent:
  type: object
  required: [eval_id, example_id, scorer, score, passed, timestamp]
  properties:
    eval_id: { type: string }
    example_id: { type: string }
    run_id: { type: string }
    scorer: { type: string }
    score: { type: number, format: double }
    passed: { type: boolean }
    details: { type: string }
    timestamp: { type: string, format: date-time }

RecordEvalEventsRequest:
  type: object
  required: [events]
  properties:
    events:
      type: array
      items:
        type: object
        required: [example_id, scorer, score, passed]
        properties:
          example_id: { type: string }
          run_id: { type: string }
          scorer: { type: string }
          score: { type: number, format: double }
          passed: { type: boolean }
          details: { type: object }
```

After editing `openapi.yaml`, run `npm run gen:types` to regenerate `web/lib/api-types.ts`.

### 5.2 `web/lib/types.ts` additions

```typescript
export type EvalSummary = components["schemas"]["EvalSummary"];
export type ScorerMean  = components["schemas"]["ScorerMean"];
export type EvalEvent   = components["schemas"]["EvalEvent"];
```

### 5.3 `web/lib/api.ts` additions

```typescript
export async function fetchEvals(): Promise<EvalSummary[]> { ... }
export async function fetchEval(id: string): Promise<EvalEvent[]> { ... }
```

Same pattern as `fetchRuns`/`fetchTrace`: calls same-origin BFF paths.

### 5.4 New BFF routes

```
web/app/api/evals/route.ts              GET → /api/v1/evals        (+ proxyJSON)
web/app/api/evals/[id]/route.ts         GET → /api/v1/evals/{id}   (+ proxyJSON)
```

No POST BFF is needed: the Python harness calls the orchestrator directly (it already
knows the orchestrator URL and token via env vars). The browser never writes eval events.

### 5.5 New pages and components

```
web/app/evals/page.tsx                 Server wrapper → <EvalTable runId?={…} />
web/app/evals/[id]/page.tsx            Server wrapper → <EvalDetail evalId={id} />
web/components/eval-table.tsx          Client: list of eval runs with scorer columns
web/components/eval-detail.tsx         Client: per-example table with score cells
```

**`eval-table.tsx`:**
- `useQuery(["evals"], fetchEvals, { refetchInterval: 10_000 })` (evals may be running)
- Table columns: Eval ID (link to `/evals/[id]`), Examples, Started, and one column per
  unique scorer name across all evals (present in the `scorers` array)
- Score cells formatted as `XX.X%`
- 503 message: "Eval history not available — set `CLICKHOUSE_URL` on the orchestrator."
- Empty state: "No eval runs recorded yet."

**`eval-detail.tsx`:**
- `useQuery(["eval", id], () => fetchEval(id), { staleTime: Infinity })` (immutable once done)
- Metric cards at top (one per scorer): shows mean score as large percentage
- Per-example table: Example ID, one Score column per scorer, Run ID (linked to `/runs/[id]`)
- Rows sortable by example_id (default) or any scorer column

**Nav link:** Add "Evals" link to the existing nav (wherever Runs is linked). Check
`web/app/layout.tsx` or the nav component for the right place.

### 5.6 Tests (vitest)

**`web/components/__tests__/eval-table.test.tsx`:**
- Shows skeletons while loading
- Renders eval IDs and scorer columns for fixture data
- Shows 503 message when API returns 503
- Shows empty state when array is empty
- Each eval ID is a link to `/evals/{id}`

**`web/components/__tests__/eval-detail.test.tsx`:**
- Shows per-example rows with correct scores
- Links run_id to `/runs/{id}`
- Shows empty column header if no scorers in data

**`web/lib/__tests__/api.test.ts` additions:**
- `fetchEvals` calls `/api/evals`
- `fetchEval` calls `/api/evals/eval-xyz`

---

## 6. Task order (serial)

1. **Go: `internal/clickhouse/eval_writer.go`** + `eval_writer_test.go`
2. **Go: `internal/clickhouse/eval_reader.go`** + `eval_reader_test.go`
3. **Go: `handler.go` additions** — new interfaces, `WithEvals`, three new routes + handlers
4. **Go: `handler_test.go`** — 5 new tests
5. **Go: `cmd/orchestrator/main.go`** — wire `WithEvals` after `WithTrace`
6. **Python: `worker/helix/eval/reporter.py`** (new) + unit test
7. **Python: `worker/helix/eval/harness.py`** — add `reporter` param; update `cli.py`
8. **Web: `web/openapi.yaml`** additions + `npm run gen:types`
9. **Web: BFF routes** — `evals/route.ts`, `evals/[id]/route.ts`
10. **Web: `lib/types.ts` + `lib/api.ts`** additions
11. **Web: `eval-table.tsx` + `eval-detail.tsx`** + page files + nav link
12. **Web: vitest tests** for both components + api.test.ts additions
13. **Docs + changelog**: `docs/api.md`, `AGENTS.md` phase note (M5→M6), `CHANGELOG.md`, `HANDOFF.md`

---

## 7. Definition of done

- [ ] `POST /api/v1/evals/{id}/events` writes to ClickHouse synchronously; returns 503 when
      `CLICKHOUSE_URL` unset; returns 400 on out-of-range score.
- [ ] `GET /api/v1/evals` and `GET /api/v1/evals/{id}` return correct shapes; Go unit tests pass.
- [ ] Python `OrchestratorEvalReporter` posts results to orchestrator when env vars set;
      no-op when unset; `mypy --strict` clean.
- [ ] `make eval` with `HELIX_ORCHESTRATOR_URL` + `HELIX_API_TOKEN` set populates `eval_events`.
- [ ] `/evals` renders eval list with scorer columns; `/evals/[id]` renders per-example table.
- [ ] All vitest tests (new + existing) pass; `tsc --noEmit`, ESLint, Prettier, `next build` clean.
- [ ] `make lint` and `make test` pass end-to-end (Python + Go + web gates all green).
- [ ] `docs/api.md`, `CHANGELOG.md`, `HANDOFF.md` updated.
