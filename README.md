# Helix

A distributed agent runtime with first-class evaluation, observability, and a self-improving RAG loop.

The central thesis: systematically mining retrieval failures from production traces and using them as training signal for embedding fine-tuning yields measurable recall improvements over a strong baseline. Helix is the infrastructure that makes that experiment reproducible at scale — and a useful agent execution platform in its own right.

**Demonstrated result on BRIGHT biology:** recall@10 lifted from **0.253 → 0.343** (+0.090, 95% CI [0.010, 0.175], sign-test p=0.041) after one mine→train→promote cycle.

---

## Architecture

```
                        ┌──────────────────────────────────────────┐
                        │  Control Plane (Go)                      │
  Client ─── gRPC/REST ─▶  Orchestrator + Scheduler + Postgres    │
                        └──────────────┬───────────────────────────┘
                                       │ NATS JetStream (task dispatch)
               ┌───────────────────────┼───────────────────────┐
               ▼                       ▼                       ▼
        Worker: research        Worker: finetune_job    Worker: indexer
        (Python)                (Python)                (Python)
               │                       │
               └───────────┬───────────┘
                           │ OTel spans
                           ▼
                    OTel Collector ──▶ ClickHouse
                                           │
                                     Dashboard (Next.js)
```

Three planes:

- **Control plane (Go):** Orchestrator owns all durable run/task state in Postgres, dispatches work onto NATS, and exposes the REST + gRPC API.
- **Data plane (Python):** Stateless workers pull tasks from NATS, execute agent code (LiteLLM, Qdrant, tools), and post completion via gRPC.
- **Observability plane:** Every tool call emits an OTel span → OTel Collector → ClickHouse. The dashboard queries ClickHouse directly.

Full architecture details: [`docs/architecture.md`](docs/architecture.md).

---

## The RAG loop

```
Query ──▶ Retriever (dense+sparse+RRF) ──▶ Reranker ──▶ Agent
                                                          │
                                                       Eval
                                                          │ failures
                                                    Failure Miner
                                                          │ triplets
                                               Embedding Trainer (InfoNCE)
                                                          │
                                              Canary eval on held-out set
                                                          │
                                           ┌─ promote? ──┤
                                           ▼ yes         ▼ no
                                    Qdrant alias swap   archive
```

1. **Mine:** The failure miner queries ClickHouse for recent retrieval spans, joins against eval outcomes, and extracts (query, gold passage, hard negative) triplets from cases where the gold passage was not in the top-10.
2. **Train:** Fine-tune [Nomic Embed v1.5](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5) with InfoNCE loss using sentence-transformers. Checkpoint saved to MinIO.
3. **Promote:** Index the corpus into a new Qdrant collection with the candidate model, run a canary eval against the held-out hard set, and atomically swap the `corpus.active` alias if recall@10 improves with statistical significance.

---

## Stack

| Layer | Technology |
|-------|-----------|
| Control plane | Go 1.22+, gRPC, Postgres 15 |
| Event bus | NATS JetStream |
| Workers | Python 3.12+, LiteLLM, sentence-transformers |
| Telemetry | OpenTelemetry → ClickHouse 24 |
| Vectors | Qdrant 1.9+ |
| Blob storage | MinIO (S3-compatible) |
| Exactly-once / rate limiting | Redis 7 |
| Dashboard | Next.js 14 (App Router), TanStack Query, shadcn/ui, Tailwind |
| Deployment | Helm 3 + Kubernetes 1.25+ |

Full rationale for every choice: [`docs/tech-stack.md`](docs/tech-stack.md).

---

## Prerequisites

- Docker + Docker Compose
- Go 1.22+
- Python 3.12+ with [uv](https://github.com/astral-sh/uv)
- Node.js 20+ (dashboard only)

---

## Quick start (local dev)

### 1. Boot the stack

```bash
make dev
```

Starts Postgres, ClickHouse, NATS, Redis, MinIO, Qdrant, and the OTel Collector via Docker Compose (~30s).

### 2. Seed the corpus and build the Qdrant index

```bash
make seed
```

Downloads a ~1k document corpus and 100 HotpotQA distractor-dev questions, embeds them with Nomic Embed v1.5, and upserts into Qdrant (~10 min on first run; subsequent runs hit the local cache).

### 3. Run the eval smoke test

```bash
make eval
```

Runs the `deep_research` workflow over 100 questions and writes a scored baseline to `evals/baselines/hotpotqa_dev_100_baseline.json`. Takes ~3 min.

### 4. Run the fine-tune loop

```bash
make finetune
```

Mines retrieval failures from the eval run, fine-tunes the embedding model, runs a canary eval, and promotes if the recall lifts significantly. Requires Qdrant to be running.

### 5. Start the dashboard

```bash
make web
```

Opens the Next.js dashboard at `http://localhost:3000`. Pages: runs, traces, evals, retrievals, LLM calls, finetune jobs, embeddings.

---

## Running Go services

```bash
# Build orchestrator + collector
make build

# Run orchestrator against the local dev stack
make orchestrator

# Run OTel collector
make collector

# Run a Python worker against the local dev stack
make worker
```

---

## Commands reference

| Command | What it does | Time |
|---------|-------------|------|
| `make dev` | Boot all backing services via Docker Compose | ~30s |
| `make dev-down` | Tear down dev stack and volumes | ~5s |
| `make seed` | Prepare corpus + HotpotQA, embed, index | ~10 min |
| `make eval` | 100-question smoke eval, write baseline JSON | ~3 min |
| `make eval-full` | Alias for `make eval` (full-corpus eval runs on cluster) | ~3 min |
| `make eval-final` | Run on the sequestered 500-question holdout. **Only run when measuring final results.** | ~15 min |
| `make finetune` | Full mine→train→promote cycle | varies |
| `make test` | Unit tests (Go + Python) | ~30s |
| `make test-integration` | Integration tests via testcontainers (Postgres, ClickHouse, NATS) | ~5 min |
| `make lint` | ruff, mypy --strict, golangci-lint, prettier | ~30s |
| `make fmt` | ruff format, gofmt, prettier | ~5s |
| `make proto` | Regenerate gRPC stubs from `proto/helix/v1/` | ~3s |
| `make build` | Build Go binaries to `bin/` | ~20s |
| `make web` | Run Next.js dashboard in dev mode | foreground |

---

## Eval results

**HotpotQA distractor-dev (100 questions, base model)**

| Metric | Mean | 95% CI |
|--------|------|--------|
| answer_f1 | 0.149 | [0.128, 0.173] |
| citation_precision | 0.892 | [0.848, 0.936] |
| retrieval_recall@10 | 0.650 | [0.605, 0.695] |

**BRIGHT biology (51 questions, mine→train→promote)**

| | Base | After fine-tune | Δ |
|--|------|----------------|---|
| recall@10 | 0.253 | 0.343 | **+0.090** (95% CI [0.010, 0.175], p=0.041) |

The lift reproduces across multiple seeds (B3, B4, B5 — see `evals/baselines/`).

---

## Repo layout

```
cmd/
  orchestrator/      Go control plane: REST + gRPC + migration runner
  collector/         Go OTel collector: OTLP → ClickHouse

worker/
  helix/             Public Python SDK (workflow, task decorators)
  helix/v1/          Generated gRPC stubs (do not edit by hand)
  helix/rag/         Indexer, retriever, reranker, miner, trainer, promotion
  helix/workflows/   Reference deep_research agent
  helix/runtime/     Engine, RemoteEngine (gRPC client), NATS consumer

web/                 Next.js 14 dashboard
proto/               Protobuf definitions (single source of truth for gRPC)
migrations/          Up-only SQL migrations (Postgres)
evals/
  datasets/          HotpotQA dev (100) + holdout (500, locked)
  baselines/         Scored eval outputs
infra/
  compose/           docker-compose.yml for local dev
  helm/helix/        Production Helm chart (all 9 components)
docs/                Architecture, data model, API, tech stack, milestone plans
```

---

## Production deployment (Kubernetes)

The `infra/helm/helix/` chart deploys the full stack — orchestrator, collector, two worker pools, Postgres, NATS, ClickHouse, Redis, MinIO, Qdrant.

```bash
# Dev / local cluster
helm install helix infra/helm/helix \
  -n helix --create-namespace \
  --set secrets.apiToken=<your-token>

# Production
helm install helix infra/helm/helix \
  -n helix --create-namespace \
  -f infra/helm/helix/values-prod.yaml \
  --set secrets.apiToken=<your-token> \
  --set secrets.postgresPassword=<password> \
  --set secrets.minioRootPassword=<password>
```

See [`infra/helm/README.md`](infra/helm/README.md) for the full values reference, upgrade instructions, and port-forwarding commands.

---

## Development

```bash
# All checks before opening a PR
make fmt && make lint && make test

# With schema or proto changes
make fmt && make lint && make test-integration
```

Conventions are in [`AGENTS.md`](AGENTS.md) (language style, SQL rules, proto rules, definition of done).

---

## License

See [`LICENSE`](LICENSE).
