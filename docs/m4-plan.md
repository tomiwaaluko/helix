# M4 Plan — Dashboard (thin Runs slice)

> **Status: APPROVED — implementation in progress.**
>
> New component (`web/`) + a small additive public-API change (run detail gains a task
> tree). Scope: **thin dashboard slice** — Runs list + Run detail only; trace/eval/
> retrieval/embedding views deferred until their data lands (M5).
>
> Sign-off decisions: (1) generate `web/lib/api-types.ts` from a hand-maintained
> `web/openapi.yaml` via `openapi-typescript`; (2) keep `web` out of the root
> `make lint`/`make test` — separate `web-*` targets, CI wiring in M5; (3) polling, not SSE.

---

## 1. What M4 (thin slice) delivers

| Piece | Today | M4 target |
|---|---|---|
| Dashboard | none | Next.js 14 (App Router) app in `web/` |
| Runs list | REST only (`GET /api/v1/runs`) | `/runs` page: table of runs, status filter, poll-refresh |
| Run detail | REST returns task **IDs** only | `/runs/[id]` page: metadata, status, input/output, **task tree** |
| Run detail API | `GET /api/v1/runs/{id}` → task IDs | additive: response gains a `tasks` array (node_id + status + attempts) |
| Cancel | `POST /api/v1/runs/{id}/cancel` | a Cancel button on run detail |
| Auth | bearer token, server-to-server | token stays **server-side** via a Next.js BFF proxy |

### What M4 does NOT deliver

| Item | Target |
|---|---|
| Traces / spans view (signed MinIO URLs, ClickHouse) | M5 — needs populated span payloads + the orchestrator trace endpoint |
| Evals, retrievals, embeddings, workers views | M5+ — endpoints and/or data don't exist yet |
| SSE live updates (`/runs/{id}/events`) | Deferred — the slice polls; SSE lands when the orchestrator emits it |
| User auth / OAuth on the dashboard | Out of scope in v1 (`tech-stack.md`) |
| Orchestrator-emitted OpenAPI schema | Deferred — a hand-maintained `web/openapi.yaml` (runs subset) seeds type generation |

The dashboard runs end-to-end only against a live orchestrator (`make dev` + `make
orchestrator` + a worker), which needs the local Docker stack. In the cloud we build,
lint, type-check, and unit-test it with the data layer mocked (see §6).

---

## 2. Architecture

```
Browser (client components, TanStack Query)
  │  same-origin fetch — no token in the browser
  ▼
Next.js BFF route handlers  (web/app/api/runs/...)   [server-side]
  │  attaches Authorization: Bearer ${HELIX_API_TOKEN}
  ▼
Go orchestrator REST  (${ORCHESTRATOR_URL}/api/v1/runs...)
  ▼
Postgres
```

**Why a BFF proxy:** the REST API authenticates with a bearer token. Routing browser
fetches through Next.js server route handlers keeps `HELIX_API_TOKEN` server-side and
makes every browser call same-origin, so the orchestrator needs **no CORS change**.
This satisfies the `AGENTS.md` rule "Data fetching through TanStack Query against the
REST API. No direct fetch in components" — components call the same-origin BFF; the BFF
calls the orchestrator.

---

## 3. Backend changes (Go) — small, additive

### 3.1 `store.ListTasksForRun`

`GET /api/v1/runs/{id}` should return the task tree, not just IDs (`docs/api.md`: "Fetch
run state including task tree"). Add one read method:

```go
// Store interface
ListTasksForRun(ctx context.Context, runID string) ([]Task, error)
```

- `pg.go`: `SELECT id, run_id, node_id, status, attempts FROM tasks WHERE run_id=$1 ORDER BY id`.
- `internal/testutil/mock.go`: add `ListTasksForRunFn` to `MockStore`.
- `Task` already carries `json:"..."` tags (`id`, `run_id`, `node_id`, `status`, `attempts`).

### 3.2 `getRun` returns a task tree

```go
type runDetailResponse struct {
    store.Run
    Tasks []store.Task `json:"tasks"`
}
```

`getRun` calls `GetRun` then `ListTasksForRun` and writes `runDetailResponse`. This is
**additive** — existing fields are unchanged; the JSON gains a `tasks` array. (Single-task
DAG today means one task per run; the shape is correct for future fan-out.)

### 3.3 Tests

- `internal/api/handler_test.go`: `getRun` happy path asserts `tasks` is present and shaped.
- Existing handler/store mock tests updated for the new interface method.

No migration (no schema change). No proto change.

---

## 4. Frontend (`web/`)

### 4.1 Stack (per `AGENTS.md` TS conventions)

- **Next.js 14** App Router, React 18, TypeScript 5 (strict)
- **Server Components by default**; client components only where interactivity needs them
  (the runs table's polling/filter, the cancel button)
- **TanStack Query** for client data fetching against the same-origin BFF
- **shadcn/ui** primitives only (`button`, `table`, `badge`, `card`, `skeleton`), **Tailwind**
- **Types generated**, not hand-written: `web/openapi.yaml` (runs subset, hand-maintained
  for now) → `openapi-typescript` → `web/lib/api-types.ts`

### 4.2 Structure

```
web/
  app/
    layout.tsx                 root layout, providers (TanStack QueryClient)
    page.tsx                   redirect → /runs
    runs/
      page.tsx                 Runs list (server shell + client table)
      [id]/page.tsx            Run detail (metadata, JSON, task tree, cancel)
    api/
      runs/route.ts            BFF: GET list (status filter) — proxies + bearer token
      runs/[id]/route.ts       BFF: GET detail
      runs/[id]/cancel/route.ts BFF: POST cancel
  components/
    ui/                        shadcn primitives
    runs-table.tsx             client component (TanStack Query, poll, filter)
    run-detail.tsx             client component (detail + cancel mutation)
    status-badge.tsx           run/task status → colored badge
  lib/
    api-types.ts               generated from openapi.yaml
    orchestrator.ts            server-only fetch helper (reads HELIX_API_TOKEN, ORCHESTRATOR_URL)
    query-client.tsx           TanStack provider
  openapi.yaml                 runs-subset schema (source of api-types.ts)
  package.json / tsconfig.json / tailwind.config.ts / components.json / .eslintrc / vitest.config.ts
```

### 4.3 Pages

- **`/runs`** — table: `id` (links to detail), `workflow_id`, `status` (badge),
  `submitted_by`, `submitted_at`. A status `<select>` filter (drives `?status=`). Polls
  every ~3 s via TanStack `refetchInterval`. Loading skeletons; empty + error states.
- **`/runs/[id]`** — run metadata (id, status badge, trace_id, submitted_by/at),
  collapsible `input`/`output`/`error` JSON, and a **task tree** (node_id, status badge,
  attempts). A **Cancel** button (mutation → BFF → orchestrator) shown when the run is
  pending/running.

### 4.4 Config (server-side only)

| Env var | Default | Purpose |
|---|---|---|
| `ORCHESTRATOR_URL` | `http://localhost:8080` | orchestrator REST base |
| `HELIX_API_TOKEN` | (required at runtime) | bearer token, never sent to the browser |

---

## 5. Tooling, Makefile, lint

- `web/package.json` scripts: `dev`, `build`, `start`, `lint` (eslint), `test` (vitest),
  `gen:types` (openapi-typescript).
- Root `Makefile` (additive, guarded so slice-phase `make test` is unchanged):
  - `make web` — `cd web && npm run dev` (already named in the commands table)
  - `make web-install` / `make web-build` / `make web-lint` / `make web-test`
- `make lint` / `make test` are **not** changed to require web yet (keeps the Python+Go
  gate green without a node install); we run the web gate explicitly until M5 wires CI.
- Prettier + eslint (Next.js config) per `AGENTS.md` ("prettier --check").

---

## 6. Cloud vs local — what's verifiable where

| Check | Cloud (here) | Local |
|---|---|---|
| Go: `ListTasksForRun`, `getRun` tasks, unit tests, lint | ✅ | ✅ |
| `npm install`, `next build`, eslint, vitest (mocked data layer) | ✅ (node 22 present) | ✅ |
| Dashboard against a live orchestrator + worker (real runs) | ❌ (no Docker daemon) | ✅ |

Frontend unit tests mock the BFF/`fetch`, so components, the table, the cancel mutation,
and the BFF route handlers are all tested in the cloud. The only thing reserved for local
is the live end-to-end view.

---

## 7. Test plan

**Go**
- `handler_test.go`: `getRun` returns `tasks`; `ListTasksForRun` store error → 500.

**Frontend (vitest + React Testing Library, fetch/BFF mocked)**
- `runs-table` renders rows, applies the status filter, shows empty/error states.
- `status-badge` maps each status → expected variant.
- `run-detail` renders metadata + task tree; Cancel triggers the mutation; button hidden
  when terminal.
- BFF route handlers: attach the bearer token, forward status filter, propagate non-2xx.

**Build/lint**: `next build` compiles; eslint + prettier clean; `tsc --noEmit` strict.

---

## 8. Task order (serial)

1. Go: `store.ListTasksForRun` (interface + pg + mock) and `getRun` task tree + tests.
2. Scaffold `web/` (Next 14, TS strict, Tailwind, shadcn init, eslint, vitest).
3. `web/openapi.yaml` (runs subset) → generate `web/lib/api-types.ts`.
4. `lib/orchestrator.ts` (server fetch helper) + BFF route handlers + their tests.
5. `query-client` provider + root layout.
6. `/runs` page + `runs-table` + `status-badge` + tests.
7. `/runs/[id]` page + `run-detail` (+ cancel mutation) + tests.
8. Makefile targets; `next build`, eslint, vitest, prettier all green.
9. Docs: `AGENTS.md` (M3→M4 phase), `docs/api.md` (run detail `tasks`), `CHANGELOG.md`,
   `HANDOFF.md`, `ISSUES.md` if anything bites.

---

## 9. Open decisions (for sign-off)

1. **Type generation** — generate `api-types.ts` from a hand-maintained `web/openapi.yaml`
   (honors the `AGENTS.md` "do not hand-write" rule; ~recommended), or hand-write a small
   types module as a documented slice exception (less tooling)?
2. **Polling vs SSE** — confirm polling for the slice; defer SSE (`/runs/{id}/events`)
   until the orchestrator emits it.
3. **CI** — leave `web` out of the root `make lint`/`make test` for now (run explicitly),
   wiring it into CI in M5? Recommended, to keep the Python+Go gate node-free.

---

## 10. Definition of done

- [x] `store.ListTasksForRun` added (interface + pg + mock); `getRun` returns `tasks`; Go tests pass.
- [x] `web/` builds (`next build`), eslint + prettier + `tsc --noEmit` clean, 18 vitest tests green.
- [x] `/runs` lists runs with status filter + polling; `/runs/[id]` shows metadata + task tree + cancel.
- [x] Bearer token stays server-side (BFF); no token in client bundles; no orchestrator CORS change.
- [x] `docs/api.md` updated (run detail `tasks`); `AGENTS.md` (M4 phase); `CHANGELOG.md` + `HANDOFF.md` updated.
