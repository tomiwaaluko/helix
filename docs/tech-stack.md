# Tech Stack

Every choice below is justified against the alternatives we considered. Where the obvious pick was rejected, we say why.

## Languages

### Go for the control plane

**Pick:** Go 1.22+ for the orchestrator, scheduler, gRPC server, and the OTel-to-ClickHouse writer.

**Why:** Distributed systems work in this space (Temporal, Cadence, NATS itself, Kubernetes) is canonical Go. The runtime needs predictable concurrency, low GC pressure, and a strong gRPC and protobuf story. Go gives us all three with minimal ceremony. It also signals systems-language proficiency to readers of the codebase.

**Alternatives considered:**

- **Rust.** More impressive on paper, but the async ecosystem is still settling and the tonic + sqlx tax buys us complexity that the project does not need. Rejected.
- **Python.** Keeps the codebase monolingual but loses the systems signal and forces us to re-implement primitives Go gives us for free. Rejected.
- **TypeScript/Node.** Fine for the dashboard backend, weak for the runtime. Rejected for the runtime.

### Python for workers and ML code

**Pick:** Python 3.12+ for workers, agent code, the RAG subsystem, and the eval harness.

**Why:** The LLM and ML ecosystem is Python. LiteLLM, sentence-transformers, Qdrant client, OTel SDK, FastAPI — all first-class in Python. Workers are stateless and the GIL is not a bottleneck because we are I/O-bound on LLM calls.

**Alternatives considered:** None seriously. Python is the right language for this layer.

### TypeScript for the dashboard

**Pick:** TypeScript 5+ with Next.js 14 (App Router).

**Why:** The dashboard is a CRUD-and-charts surface over our REST and SSE APIs. Next.js + TS + Tailwind + shadcn is the path of least resistance.

## Datastores

### Postgres for the control plane

**Pick:** Postgres 15+ for all transactional state.

**Why:** Strong transactions, mature, well-known. Helix's control-plane state is small (millions of rows, not billions) and benefits from ACID guarantees and SQL ergonomics. The orchestrator is the single writer; that simplifies the consistency story.

**Alternatives considered:**

- **MySQL.** Workable, but Postgres's `jsonb` support is materially better and we use it for workflow specs.
- **CockroachDB.** Buys us nothing at this scale; trades local-dev simplicity for distributed semantics we do not need.

### ClickHouse for telemetry

**Pick:** ClickHouse 24+ for spans, retrieval events, and eval events.

**Why:** This is the differentiated pick. Span and event data is high-volume, append-mostly, and queried with aggregations and filters — exactly ClickHouse's wheelhouse. Real observability vendors (PostHog, Signoz, Uptrace) use ClickHouse for the same reason. Putting telemetry in Postgres would work at small scale and fall over once we cross a few million spans.

**Alternatives considered:**

- **Postgres + TimescaleDB.** Easier ops, but a worse fit for wide trace queries with high-cardinality attribute maps.
- **Elasticsearch.** Heavier, slower writes, more expensive to host, awkward query language for this access pattern.
- **DuckDB.** Embedded, fast, but does not fit a multi-process write pattern.

### Qdrant for vectors

**Pick:** Qdrant 1.10+ with HNSW indices and payload-based filtering.

**Why:** Purpose-built. The fine-tuning loop benefits from Qdrant's clean per-collection model — we host the base-model collection and a candidate-model collection side by side, then atomically swap via alias. Rust under the hood, single binary, no JVM tax. Payload filtering is fast and supports the structural filters our retriever needs.

**Alternatives considered:**

- **pgvector.** Easier ops, but HNSW + filter performance lags Qdrant at our target corpus size in filtered ANN benchmarks. Acceptable as a baseline, not the primary store.
- **Milvus.** More features than we need, heavier dependency footprint.
- **Weaviate.** Comparable. Qdrant's API ergonomics and operator footprint edged it out.

### Redis for ephemeral state

**Pick:** Redis 7 for rate-limit counters, distributed locks, and worker heartbeats.

**Why:** Standard. Not the system of record for anything.

### MinIO for blob storage

**Pick:** MinIO (S3-compatible) for large trace payloads, model checkpoints, and corpus snapshots.

**Why:** The S3 API is the lingua franca. MinIO runs in-cluster for dev and prod and is swappable for actual S3 with one config flag.

## Messaging

### NATS JetStream

**Pick:** NATS JetStream as the event bus.

**Why:** Lower operational footprint than Kafka, stronger guarantees than Redis Streams. The subject-based routing model is a clean fit for per-pool task dispatch. JetStream's persistence + ack model gives us at-least-once with redelivery on visibility timeout, which is exactly what the worker model expects.

**Alternatives considered:**

- **Kafka.** Overkill. Ops burden is real; per-partition ordering is more than we need.
- **Redis Streams.** Workable but persistence and replay are weaker.
- **RabbitMQ.** Possible. NATS feels more aligned with modern cloud-native deployments.
- **Direct gRPC dispatch.** Couples the orchestrator more tightly to workers and loses buffering. We keep this as a fallback path inside the orchestrator for the NATS-unavailable case.

## LLM and embedding stack

### LiteLLM as the provider abstraction

**Pick:** LiteLLM with Anthropic Claude Sonnet 4 as the default for the reference agent, OpenAI `gpt-4o-mini` and a local vLLM-hosted Llama 3.1 8B as fallbacks for eval.

**Why:** Multi-provider routing without vendor lock-in. Built-in retry, fallback, and cost tracking. The eval harness can compare agent behavior across providers with a one-line config change, which is exactly the kind of comparison the project should support.

### Nomic Embed v1.5 as the base embedding model

**Pick:** `nomic-ai/nomic-embed-text-v1.5` (768d, Apache 2.0).

**Why:** Open license, fine-tunable, MTEB-competitive, and supported by sentence-transformers for the fine-tuning loop. Nomic ships task-prefix tokens (`search_query:`, `search_document:`) that the retriever uses out of the box.

**Alternatives considered:**

- **BGE-base-en-v1.5.** Comparable performance, similar license. Either is acceptable; Nomic edges it on Matryoshka-friendly truncation we may use later.
- **OpenAI `text-embedding-3-small`.** Closed and not fine-tunable. Rejected for the research workload.
- **E5-base-v2.** Older; Nomic is the modern equivalent.

### BGE reranker for the second stage

**Pick:** `BAAI/bge-reranker-base` as the cross-encoder reranker.

**Why:** Reasonable size, strong results, MIT license. The reranker is not the research target; it stays fixed across experiments so the embedding fine-tune's contribution is isolated.

### sentence-transformers for fine-tuning

**Pick:** `sentence-transformers` + PyTorch for the fine-tuning loop, with `accelerate` as the training backend.

**Why:** Mature library, sensible defaults, native InfoNCE loss support, integrates with HuggingFace model hub for sharing fine-tuned checkpoints. `sentence-transformers` `.fit()` delegates to `transformers.Trainer`, which requires `accelerate>=1.1.0` even for single-device CPU training; it is pinned explicitly in `worker/pyproject.toml` so the trainer is not a runtime-only surprise.

### tiktoken for chunk sizing

**Pick:** `tiktoken` with the `cl100k_base` encoding for the indexer's chunker.

**Why:** Fast, dependency-light token counting to size chunks consistently. It is not tied to the embedding or generation model — it only needs to be a stable proxy for length, and `cl100k_base` is good enough for that.

### HuggingFace datasets for corpus/eval seeding

**Pick:** `datasets` to download HotpotQA distractor-dev for the seed scripts.

**Why:** Canonical, cached loader for HotpotQA; keeps the corpus + question prep reproducible from a clean checkout. Only the thin `scripts/` wrappers touch it — the formatting logic in `helix.eval.corpus` is pure and dataset-agnostic.

## Frontend

### Next.js 14 + TypeScript + Tailwind + shadcn/ui

**Pick:** App Router. Server components where they earn their keep, TanStack Query for client-side data, shadcn for the component primitives, Recharts for trace visualizations.

**Why:** This stack ships. The dashboard is not the research contribution; the goal is to make traces, runs, and eval scores legible quickly. Familiar stack means fewer hours sunk into the dashboard and more hours on the runtime and the RAG loop.

## Observability

### OpenTelemetry as the trace standard

**Pick:** OTel SDKs in both Python and Go workers. OTel Collector deployed as a sidecar in dev, central deployment in prod.

**Why:** Industry standard. Adopting OTel means our traces are immediately compatible with Jaeger, Grafana Tempo, Datadog, and any other backend a user might want to point Helix at. We add a custom ClickHouse exporter as the primary sink.

### Prometheus + Grafana for metrics

**Pick:** Prometheus scrape of orchestrator, workers, and NATS. Grafana for the platform-health dashboard.

**Why:** Standard. The Helm chart ships pre-built dashboards.

## Local development and production

### Docker Compose for local dev

**Pick:** A single `docker-compose.yml` that spins up Postgres, ClickHouse, Qdrant, NATS, Redis, MinIO, and the OTel collector. `make dev` boots everything.

**Why:** One command from clone to running. Lowers the barrier for contributors and reviewers.

### Kubernetes + Helm for production

**Pick:** Helm chart that deploys all components, with values for SaaS providers (Anthropic API key, OpenAI API key) and resource sizing.

**Why:** Helm is the right deployment substrate for the system's component count, and shipping a Helm chart is a clear signal that the repo is production-real rather than a notebook.

### CI/CD

**Pick:** GitHub Actions. Per PR: lint (golangci-lint, ruff, mypy, prettier), unit tests, integration tests via testcontainers, eval-suite smoke test (HotpotQA dev, 100 questions). On main: full eval suite, publish container images.

## What we deliberately did not include

- **A graph DSL.** Helix workflows are Python functions decorated with `@helix.workflow`. No YAML, no JSON, no custom syntax. The DAG is materialized from the decorator metadata at registration time.
- **A managed UI for prompt editing.** Out of scope. Prompts live in source.
- **An agent framework.** Helix is the runtime; users bring their own agent code (or use the reference research agent as a starting point).
- **A streaming protocol for live token output.** Worth a v2; in v1, the dashboard polls for run state and uses SSE for run lifecycle events.
- **Multi-tenant isolation at the cluster level.** Helix runs single-tenant per deployment. Multi-tenant is post-v1.

## Versions of record

| Component | Version | Pinning |
|---|---|---|
| Go | 1.22+ | `go.mod` |
| Python | 3.12+ | `pyproject.toml`, uv lockfile |
| Postgres | 15 | Helm chart values |
| ClickHouse | 24 | Helm chart values |
| Qdrant | 1.10+ | Helm chart values |
| NATS | 2.10+ | Helm chart values |
| Redis | 7 | Helm chart values |
| MinIO | RELEASE.2024-* | Helm chart values |
| Nomic Embed | v1.5 | HuggingFace revision hash |
| BGE reranker | base, revision pinned | HuggingFace revision hash |
| Claude | `claude-sonnet-4` | LiteLLM config |
