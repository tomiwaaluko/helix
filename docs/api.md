# API Surfaces

Helix exposes four surfaces. Each is small on purpose; the platform is opinionated about what belongs at the edge versus inside the workflow.

| Surface | Audience | Transport | Stability |
|---|---|---|---|
| gRPC | Workers ↔ control plane | gRPC over mTLS | Internal. Breaking changes allowed with proto version bump. |
| REST + SSE | Dashboard, external tooling | HTTPS, JSON, ISO-8601 UTC | Public. Semver under `/api/v1/`. |
| Python SDK | Workflow authors | In-process + REST/gRPC underneath | Public. Semver. |
| CLI | Operators, demos | Wraps REST | Public. Semver. |

All public surfaces use a single error envelope:

```json
{ "error": { "code": "RUN_NOT_FOUND", "message": "no run with id=run_01H...", "details": {} } }
```

Codes are stable enums documented per endpoint. Lists are cursor-paginated with `?cursor=` and `?limit=` (max 200). Timestamps are RFC 3339 UTC. IDs are ULIDs prefixed by entity (`run_`, `task_`, `wf_`, `ds_`, `eval_`, `emb_`).

## gRPC (workers ↔ control plane)

Defined in `proto/helix/v1/`. Workers dial the orchestrator over mTLS using a worker certificate issued at provisioning time. Generated Python stubs live under `worker/helix_proto/`.

### Orchestrator service

```proto
service Orchestrator {
  rpc RegisterWorker(RegisterWorkerRequest) returns (RegisterWorkerResponse);
  rpc Heartbeat(stream HeartbeatRequest) returns (stream HeartbeatResponse);
  rpc CompleteTask(CompleteTaskRequest) returns (CompleteTaskResponse);
  rpc Checkpoint(CheckpointRequest) returns (CheckpointResponse);
  rpc GetWorkflow(GetWorkflowRequest) returns (Workflow);
}
```

- **RegisterWorker** returns a worker_id, the pools the worker is assigned to, and a lease duration.
- **Heartbeat** is bidi-streaming. Workers push load metrics every 5s; the orchestrator returns dispatch hints and drain signals. Connection loss past `lease_duration` marks the worker dead and reassigns its in-flight tasks.
- **CompleteTask** is **idempotent on `(task_id, attempt_number)`**. Workers may retry it freely after partial network failure. The response indicates whether the result was accepted, superseded, or dropped (e.g., the run was already canceled).
- **Checkpoint** persists progress for a long-running task. Stored to Postgres; visibility timeout extends automatically.
- **GetWorkflow** returns the materialized DAG for the run a task belongs to, used by workers that branch on prior task outputs.

### EvalRunner service

```proto
service EvalRunner {
  rpc SubmitEval(SubmitEvalRequest) returns (SubmitEvalResponse);
  rpc PublishExampleResult(PublishExampleResultRequest) returns (PublishExampleResultResponse);
}
```

`PublishExampleResult` writes one row per example per scorer to ClickHouse `eval_events` synchronously. This is the only synchronous ClickHouse write path. Eval workers batch results in groups of 50.

### Embeddings service

```proto
service Embeddings {
  rpc StartFineTuneJob(StartFineTuneJobRequest) returns (StartFineTuneJobResponse);
  rpc PromoteModel(PromoteModelRequest) returns (PromoteModelResponse);
}
```

`PromoteModel` flips the `corpus.active` Qdrant alias from the current collection to a candidate collection. Atomic on Qdrant's side. Records the promotion in `embedding_jobs` with the previous collection name for one-call rollback.

### Proto conventions

- One service per `.proto` file.
- Field numbers never reused. Reserved numbers documented in the proto.
- `optional` for nullable scalars. Empty repeated fields are unset, not absent.
- Breaking changes go to `proto/helix/v2/` rather than mutating v1. Both versions can run in parallel during transitions.

## REST + SSE (`/api/v1/`)

JSON over HTTPS. Authenticated via bearer token in `Authorization: Bearer <token>` for v1. SSE endpoints use the same auth header.

### Runs

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/runs` | Submit a run. Body: `{workflow_id, input, idempotency_key?}` |
| GET | `/api/v1/runs/{run_id}` | Fetch run state including task tree |
| GET | `/api/v1/runs` | List runs, filterable by `workflow_id`, `status`, `created_after` |
| POST | `/api/v1/runs/{run_id}/cancel` | Cancel a running run. Idempotent. |
| GET | `/api/v1/runs/{run_id}/events` | SSE stream of lifecycle events (`task.started`, `task.completed`, `task.failed`, `run.completed`, `run.failed`, `checkpoint.created`) |
| POST | `/api/v1/runs/{run_id}/replay` | Create a new run that replays this one. Body: `{mode: "deterministic"|"live", overrides?}` |

`idempotency_key` is honored for 24h; resubmitting the same key returns the original run_id.

Replay modes:
- `deterministic` short-circuits every recorded LLM call, retrieval, and tool call by `(input_hash, model_id, tool_name)`. Used for debugging and regression. Bytewise-identical output is the contract.
- `live` re-executes against current models and indices. Used for comparison runs after a model promotion or prompt change.

### Workflows

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/workflows` | List registered workflows |
| GET | `/api/v1/workflows/{workflow_id}` | Get workflow definition |
| GET | `/api/v1/workflows/{workflow_id}/runs` | Recent runs for this workflow |

Workflows are registered at worker startup by importing the module containing `@helix.workflow` decorators. There is no REST endpoint to create workflows; that path goes through code.

### Traces

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/runs/{run_id}/trace` | Full trace for a run in OTel JSON format. Large blobs (prompts, completions, retrieval payloads) are returned as signed MinIO URLs. |
| GET | `/api/v1/spans/{span_id}` | Single span detail, with blobs inlined when under 64KB. |

The dashboard hydrates blobs lazily on click.

### Datasets

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/datasets` | List datasets |
| GET | `/api/v1/datasets/{dataset_id}` | Dataset metadata + content_hash + row count |
| POST | `/api/v1/datasets` | Create dataset. Multipart upload of JSONL. Server computes SHA-256 and rejects on collision with a different name. |
| GET | `/api/v1/datasets/{dataset_id}/rows` | Paginated row access for the dashboard preview |

Datasets are immutable. New versions are new datasets, conventionally named `name@v2`, `name@v3`.

### Evals

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/evals` | Submit an eval. Body: `{workflow_id, dataset_id, scorers, concurrency}` |
| GET | `/api/v1/evals/{eval_id}` | Eval status + aggregate metrics |
| GET | `/api/v1/evals/{eval_id}/results` | Per-example results, paginated |
| GET | `/api/v1/evals/{eval_id}/compare?baseline={eval_id}` | Diff against a baseline eval: per-example deltas and bootstrap CIs on each metric |

### Retrievals

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/retrievals` | Search retrievals across runs. Filters: `run_id`, `query_contains`, `recall_at_10__lt`, `failure_signature`, `created_after` |
| GET | `/api/v1/retrievals/{retrieval_id}` | One retrieval with query, top-k, scores, labels |
| POST | `/api/v1/retrievals/mine` | Trigger an ad-hoc failure mine over a time window. Body: `{since, until, signatures}` |

### Embeddings

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/embeddings/jobs` | List fine-tune jobs |
| GET | `/api/v1/embeddings/jobs/{job_id}` | Job status, training curves, eval deltas |
| POST | `/api/v1/embeddings/jobs` | Start a fine-tune job. Body: `{base_model, training_set_id, eval_set_id, hyperparams}` |
| POST | `/api/v1/embeddings/jobs/{job_id}/promote` | Flip `corpus.active` alias to this job's collection. Records previous for rollback. |
| POST | `/api/v1/embeddings/rollback` | Flip alias back to the previous active collection. |

### Workers

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/workers` | List workers with pool, status, current load |
| POST | `/api/v1/workers/{worker_id}/drain` | Mark a worker for drain. It finishes in-flight tasks and stops accepting new ones. |

## Python SDK

The SDK is the primary authoring surface. Workflow code is plain Python; decorators register it with the runtime.

### Defining a workflow

```python
import helix

@helix.task(retries=3, timeout="30s")
async def decompose(query: str) -> list[str]:
    ...

@helix.task(retries=2, timeout="2m")
async def retrieve(subquery: str) -> list[helix.Doc]:
    ...

@helix.task(retries=1, timeout="3m")
async def synthesize(query: str, evidence: list[helix.Doc]) -> helix.Answer:
    ...

@helix.workflow(name="deep_research", version="1.0.0")
async def deep_research(query: str) -> helix.Answer:
    subqueries = await decompose(query)
    evidence = await helix.gather(*(retrieve(sq) for sq in subqueries))
    flat = [doc for sublist in evidence for doc in sublist]
    return await synthesize(query, flat)
```

Decorator metadata is the DAG. `helix.gather` is the parallel construct; control flow (if/else, loops) materializes as dynamic edges at runtime. There is no separate graph language.

### Submitting a run

```python
client = helix.Client(endpoint="https://helix.example.com", token=os.environ["HELIX_TOKEN"])

run = client.submit("deep_research", input={"query": "..."})
result = run.wait(timeout=300)

# Or stream events
for event in run.stream():
    print(event.type, event.data)
```

`client.submit` accepts `idempotency_key` to dedupe across crashes.

### Eval harness

```python
from helix.eval import evaluate, scorers

result = evaluate(
    workflow="deep_research",
    dataset="hotpotqa_distractor_dev@v1",
    scorers=[scorers.answer_f1, scorers.citation_precision, scorers.retrieval_recall_at_k(k=10)],
    concurrency=8,
)

print(result.metrics)        # aggregate
print(result.per_example)    # rows
```

Custom scorers:

```python
@helix.scorer(name="my_metric")
def my_metric(example, prediction) -> float:
    ...
```

Scorers receive the example dict and the workflow's output. They return either a float or a `helix.eval.ScoreResult` with subscores.

### Local execution

For development, workflows run in-process without the runtime:

```python
result = await deep_research.local(query="...")
```

This path skips Postgres, NATS, and ClickHouse. Useful for unit tests. Behavior diverges from the runtime only in retry semantics (none) and telemetry (none).

## CLI

A single `helix` binary, distributed via `pip install helix-cli` and a GitHub release tarball. Config at `~/.helix/config.yaml`:

```yaml
endpoint: https://helix.example.com
token: hex_...
default_workflow: deep_research
```

Top-level verbs:

```
helix run submit <workflow> --input '{"query":"..."}'
helix run get <run_id>
helix run replay <run_id> [--live]
helix run cancel <run_id>

helix workflow list
helix workflow show <workflow_id>

helix eval submit --workflow <name> --dataset <id> --scorers answer_f1,citation_precision
helix eval get <eval_id>
helix eval compare <eval_id> --baseline <eval_id>

helix dataset list
helix dataset push <path/to/data.jsonl> --name <name>

helix mine --since 7d --signatures lexical_only,semantic_mismatch

helix embed train --base nomic-embed-text-v1.5 --training-set <id> --eval-set <id>
helix embed promote <job_id>
helix embed rollback

helix worker list
helix worker drain <worker_id>
```

All commands accept `--json` for machine-readable output and `--endpoint` / `--token` to override config.

## Authentication

**v1:**
- REST: bearer token in `Authorization` header. Tokens are issued via `helix-admin token create` (a separate binary that talks to Postgres directly). Tokens are opaque, scoped to the cluster, and have a `created_by`, `name`, and `expires_at`.
- gRPC: mTLS. Worker certs are issued at provisioning time by a private CA whose root is bundled with the orchestrator.
- SSE: same bearer token as REST, passed in the `Authorization` header.

**v2 (post-launch):**
- OIDC / OAuth2 with per-user tokens.
- Token scopes (`runs:read`, `runs:write`, `embeddings:promote`, etc.).
- Optional audit log of mutating REST calls.

The v1 model is intentionally minimal. Helix runs single-tenant per deployment, and the dashboard sits behind a reverse proxy that handles user-facing auth.
