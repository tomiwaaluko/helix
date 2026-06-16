# M3 Plan — Redis + MinIO

> **Status: APPROVED (Full M3) — implementation in progress.**
>
> Per `AGENTS.md` ("Things to never do without asking" → *Add a new datastore*) and
> the maintainer's plan-first rule for schema/datastore changes: Redis and MinIO are
> both new datastores. Signed off at **Full M3** scope: exactly-once + `helix.exactly_once`,
> LLM rate limiter, `BlobStore` + bucket bootstrap + signed URLs, and gated (default-off)
> >32 KB span-payload offload. `RedisLock` deferred.

---

## 1. What M3 delivers

| Component | M2 (today) | M3 target |
|---|---|---|
| Task delivery semantics | At-least-once (NATS); relies on orchestrator `CompleteTask` idempotency | + Redis sentinel so each `(task_id, attempt)` **executes once** even on redelivery |
| LLM provider rate limiting | LiteLLM per-call `num_retries` only | + distributed token-bucket gate (Redis) across all workers |
| Large span payloads | Inline only (not captured today) | `BlobStore` → MinIO `s3://helix-blobs/...`; payloads >32 KB stored by reference |
| Signed blob access | (none) | `BlobStore.presign_get()` — the primitive the M4 dashboard trace endpoint needs |
| Redis | (none) | Added to `docker-compose.yml` |
| MinIO | (none) | Added to `docker-compose.yml` (remapped host ports — see §6.3) |

### What M3 does NOT deliver

| Item | Target |
|---|---|
| Go orchestrator/collector Redis or MinIO wiring | Deferred. The worker offloads payloads *before* emitting spans, so the collector receives URIs, not blobs. Orchestrator MinIO (signed-URL trace endpoint) lands with the **M4** dashboard. |
| Checkpoint blob offload (Postgres → MinIO) | Deferred. M1 stores `checkpoints.state` as Postgres `BYTEA`; the data-model keeps it there. MinIO "checkpoints" in `tech-stack.md` means **model** checkpoints (training artifacts) — that is **M5** trainer territory. |
| Content-hash blob deduplication | M3 writes the SHA-256 as object metadata only; opportunistic dedup is deferred (data-model already calls it "opportunistic"). |
| Dataset / eval blob upload via REST (`POST /api/v1/datasets`, `s3://helix-datasets`, `s3://helix-evals`) | Buckets are bootstrapped, but the upload surfaces are **M4/M5** (API + eval runner). |
| Distributed lock for promotion alias-swap | Deferred. A `RedisLock` primitive is small, but the promotion path has no concurrent-writer problem in the slice; revisit in M5. |

`make eval` continues in local mode with **no** Redis or MinIO dependency. Both
integrations are no-ops when `REDIS_URL` / `S3_ENDPOINT` are unset, exactly like the
M2 OTel path — local determinism is unaffected.

---

## 2. Architecture delta

### M2 task path (today)

```
NATS JetStream ──▶ RemoteEngine._handle_envelope ──▶ handler ──▶ CompleteTask (gRPC)
                   (redelivery re-executes the handler;
                    orchestrator dedupes on (task_id, attempt))
```

### M3 task path

```
NATS JetStream ──▶ RemoteEngine._handle_envelope
                     └─ idempotency.begin(task_id, attempt)   [Redis SET NX PX]
                          ├─ False → already running/done → ACK + skip   (no re-exec)
                          └─ True  → handler ──▶ CompleteTask ──▶ idempotency.complete()
```

### M3 span-payload path (new, gated)

```
llm_call / retrieval span
  └─ maybe_offload(attrs, "prompt", text, blob_store, span_id)
       ├─ len(text) ≤ 32 KB → inline attribute (unchanged)
       └─ len(text) >  32 KB → BlobStore.put() ──▶ MinIO s3://helix-blobs/<y>/<m>/<d>/<span_id>.bin
                                attrs["prompt_uri"] = "<uri>"   (no invented field names —
                                prompt_uri / completion_uri are already in the data-model)
```

When `S3_ENDPOINT` is unset, `BlobStore` is `None` and payloads stay inline — and
payload capture itself is off by default (`HELIX_SPAN_PAYLOADS`, see §5.3), so current
behavior and the "no raw prompts logged by default" posture are preserved.

---

## 3. Redis design  ← **needs sign-off**

Redis is **ephemeral**, never a system of record (per `tech-stack.md`).

### 3.1 Connection (`helix/tools/redis_conn.py`)

- `get_redis(url: str | None) -> Redis | None` — builds a `redis.asyncio.Redis` from
  `REDIS_URL`; returns `None` when unset. Single shared client per worker process.
- `redis-py >= 5` ships `redis.asyncio`. No new event loop; reuses the worker's.

### 3.2 Exactly-once sentinel (`helix/runtime/idempotency.py`)

```python
class Idempotency:
    def __init__(self, redis: Redis | None, ttl_s: int = 300) -> None: ...
    async def begin(self, task_id: str, attempt: int) -> bool:
        # SET helix:task:{task_id}:{attempt} = worker_id  NX PX ttl
        # → True if we acquired (run it); False if a duplicate delivery
        # No-op (always True) when redis is None.
    async def complete(self, task_id: str, attempt: int) -> None: ...   # mark done, keep key for ttl
    async def release(self, task_id: str, attempt: int) -> None: ...    # DEL on handler failure → allow retry
```

- Keyed on **`(task_id, attempt)`**, not just `task_id`: a genuine orchestrator retry
  carries a new `attempt_number` and *must* run; only duplicate deliveries of the same
  attempt are deduped. Mirrors the gRPC contract (`CompleteTask` idempotent on
  `(task_id, attempt_number)`).
- TTL ≈ NATS visibility timeout × margin (default 300 s). On handler failure we
  `release()` so the next delivery re-runs.

Wired into `RemoteEngine._handle_envelope`: `begin()` at entry, `complete()` on success,
`release()` on exception. When Redis is unset the engine behaves exactly as M2.

### 3.3 Public helper (`helix.exactly_once`)

Re-exported from `helix/__init__.py` for **workflow authors** to guard a non-idempotent
side effect inside task code — the helper named in the `AGENTS.md` gotcha:

```python
async with helix.exactly_once("send-welcome-email:{user_id}"):
    await send_email(...)          # runs once across retries when Redis is configured
```

Pass-through (always executes the body) when Redis is unset — safe for local/dev/eval.

### 3.4 LLM rate limiter (`helix/tools/rate_limit.py`)  ← *optional, see scope question*

- `RedisRateLimiter(redis, key, rate, period_s)` — token bucket via a small Lua script
  (atomic check-and-decrement). `acquire()` blocks (bounded) until a token is free.
- Wired into `litellm_adapter.llm_call` **before** the provider call, keyed by
  `(provider, model)`. Skipped on cache hits (no provider call) and **no-op when Redis is
  unset**. Limits from env (`HELIX_LLM_RPM`, default unlimited → no-op).

---

## 4. MinIO design  ← **needs sign-off**

S3 is the lingua franca; MinIO is swappable for real S3 by changing `S3_ENDPOINT`.

### 4.1 Client + bucket bootstrap (`helix/tools/blob.py`)

- `BlobStore.from_env() -> BlobStore | None` — builds a `boto3` S3 client from
  `S3_ENDPOINT` / `S3_ACCESS_KEY` / `S3_SECRET_KEY` / `S3_REGION`; returns `None` when
  `S3_ENDPOINT` is unset.
- `ensure_buckets()` — creates `helix-blobs` (+ `helix-datasets`, `helix-evals` for
  future milestones) if absent. Idempotent, like the collector's `RunDDL`. Called once at
  worker startup.
- **boto3 is sync**; blocking calls run via `asyncio.to_thread`. Chosen over `aioboto3`
  to avoid the `aiobotocore`/`botocore` version-pin tax. `generate_presigned_url` is a
  cheap local signing call (no network, no thread).

### 4.2 Blob put + presign

```python
class BlobStore:
    async def put(self, data: bytes, span_id: str, *, now: datetime | None = None) -> str:
        # key = "{yyyy}/{mm}/{dd}/{span_id}.bin"  (exact data-model scheme)
        # metadata = {"sha256": <hex>}            (enables future dedup)
        # → returns "s3://helix-blobs/{key}"
    def presign_get(self, uri: str, expiry: timedelta = timedelta(hours=1)) -> str: ...
```

### 4.3 32 KB offload helper (`helix/tools/blob.py`)

```python
async def maybe_offload(
    attrs: dict[str, Any], field: str, text: str,
    blob_store: BlobStore | None, span_id: str,
) -> None:
    # len(text) ≤ 32 KB or blob_store is None → attrs[field] = text (inline)
    # else → uri = await blob_store.put(text.encode(), span_id); attrs[f"{field}_uri"] = uri
```

Keys used are `prompt_uri` / `completion_uri` — **already in `data-model.md`** (`llm_calls`)
and consistent with "analogous fields in the parent span's attributes map for tool calls."
No new span field names are invented (`AGENTS.md` slice gotcha).

### 4.4 Wiring (gated, off by default)

`maybe_offload` is wired into `litellm_adapter.llm_call` (prompt/completion) and the
retrieval span, **behind `HELIX_SPAN_PAYLOADS=1`** (default off). Default-off preserves:
the current "prompts not in spans" privacy posture, eval determinism, and every existing
test. When enabled with MinIO running, large payloads round-trip by reference — the exact
path the M5 miner will consume.

---

## 5. Component changes

### 5.1 Python deps (`worker/pyproject.toml`)

```
redis>=5
boto3>=1.34
```
mypy: `redis` ships `py.typed`; add an `ignore_missing_imports` override for
`boto3`/`botocore`. New modules added under existing strict coverage.

### 5.2 Worker config

Read at worker startup (`__main__` / `RemoteEngine`), passed down explicitly (the
`configure_otel` pattern — no global magic):

| Env var | Default (dev) | Purpose |
|---|---|---|
| `REDIS_URL` | unset → no-op | exactly-once + rate limiter |
| `S3_ENDPOINT` | unset → no-op | blob store |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` | `helix` / `helixhelix` | MinIO creds |
| `S3_REGION` | `us-east-1` | boto3 requires a region |
| `S3_BUCKET_BLOBS` | `helix-blobs` | blob bucket |
| `HELIX_SPAN_PAYLOADS` | unset → off | enable payload capture + offload |
| `HELIX_LLM_RPM` | unset → unlimited | rate-limiter budget |

### 5.3 Docker Compose (`infra/compose/docker-compose.yml`)

```yaml
  redis:
    image: redis:7
    ports: ["6379:6379"]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5

  minio:
    image: minio/minio:RELEASE.2024-06-13T22-53-53Z   # pinned per tech-stack
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: helix
      MINIO_ROOT_PASSWORD: helixhelix          # MinIO requires ≥ 8 chars
    ports:
      - "9100:9000"   # API  — host 9100 to AVOID ClickHouse's host 9000 (native)
      - "9101:9001"   # console
    volumes:
      - minio_data:/data
    healthcheck:
      test: ["CMD", "mc", "ready", "local"]
      interval: 5s
      timeout: 3s
      retries: 10
```

> **Gotcha (will be logged in `ISSUES.md`):** ClickHouse (M2) binds host `9000` for its
> native protocol; MinIO's API default is also `9000`. M3 remaps MinIO's API to host
> `9100` (console `9101`). `S3_ENDPOINT` default becomes `http://localhost:9100`.

Add `minio_data` to the `volumes:` block. Redis is ephemeral (no volume).

### 5.4 Makefile

`make dev` already runs `docker compose up -d`, so the new services boot automatically;
only the comment header changes (Qdrant + Postgres + NATS + ClickHouse **+ Redis + MinIO**).
`make worker` gains the `REDIS_URL` / `S3_*` env so a locally-run worker uses the stack.
No new top-level targets required.

### 5.5 Go

**No Go changes in M3.** Stated explicitly so the milestone stays a worker + infra slice.

---

## 6. Test plan

### Unit (no Docker)

- `tests/test_idempotency.py` — `begin` True-then-False on duplicate `(task_id, attempt)`;
  different attempt runs; `release` re-enables; no-op (always True) when redis is `None`.
  Uses an injected fake async redis (no `fakeredis` dependency).
- `tests/test_blob.py` — `put` returns the exact `s3://helix-blobs/<y>/<m>/<d>/<span_id>.bin`
  URI and writes SHA-256 metadata; `presign_get` returns a URL; `maybe_offload` inlines
  ≤32 KB and offloads >32 KB; no-op when store is `None`. Injected fake S3 client.
- `tests/test_rate_limit.py` — allows `rate` tokens then blocks; no-op when redis `None`.

### Integration (`HELIX_INTEGRATION=1` + `make dev`)

- Exactly-once: deliver the same envelope twice → handler body runs once, `CompleteTask`
  fires once.
- Blob round-trip: `put` then `presign_get` then HTTP GET returns the bytes.

### Gate

`make fmt && make lint && make test` (Python strict + Go unchanged). `make lint` holdout
SHA-256 check unaffected.

---

## 7. Task order (serial)

1. Docker Compose — add `redis` + `minio` (remapped ports), `minio_data` volume.
2. Python deps — `redis>=5`, `boto3>=1.34`; mypy overrides.
3. `helix/tools/redis_conn.py` — client factory.
4. `helix/runtime/idempotency.py` — sentinel; wire into `RemoteEngine`.
5. `helix/__init__.py` — export `helix.exactly_once`.
6. `helix/tools/blob.py` — `BlobStore`, `ensure_buckets`, `maybe_offload`.
7. `helix/tools/rate_limit.py` — token bucket; wire into `litellm_adapter` *(if in scope)*.
8. Worker config wiring (`__main__` / `RemoteEngine`): construct from env, no-op when unset.
9. Unit tests (idempotency, blob, rate_limit).
10. Integration test (skipped unless `HELIX_INTEGRATION`).
11. `make fmt && make lint && make test`.
12. Docs: `AGENTS.md` (M2 → M3 phase), `CHANGELOG.md`, `HANDOFF.md`, `ISSUES.md`
    (port-conflict entry), `tech-stack.md` only if a dep choice deviates.

---

## 8. Definition of done

- [x] `make dev` boots Qdrant + Postgres + NATS + ClickHouse + Redis + MinIO (compose updated).
- [x] Exactly-once sentinel dedups a redelivered envelope; no-op when `REDIS_URL` unset.
- [x] `helix.exactly_once` is importable and pass-through without Redis.
- [x] `BlobStore` round-trips a blob and presigns a URL; no-op when `S3_ENDPOINT` unset.
- [x] `maybe_offload` inlines ≤32 KB, offloads >32 KB; default-off preserves current behavior.
- [x] Rate limiter gates LLM calls; no-op when unset.
- [x] `make test` passes (169 Python tests; Go unchanged). `make lint` passes.
- [x] `AGENTS.md` updated (M3 stack), `CHANGELOG.md` + `HANDOFF.md` + `ISSUES.md` updated.

---

## 9. Open scope decisions (for sign-off)

1. **Rate limiter** — include the Redis LLM rate limiter (§3.4) in M3, or defer it and ship
   exactly-once as the only Redis feature?
2. **Span-payload offload wiring** — wire `maybe_offload` into the LLM/retrieval spans now
   (gated, default off), or ship `BlobStore` as a standalone primitive and defer wiring to M5?
3. **Distributed lock** — confirm deferral of `RedisLock` (no concurrent-writer problem in
   the slice).
