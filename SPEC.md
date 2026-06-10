# Helix

> A distributed agent runtime with first-class evaluation, observability, and self-improving retrieval.

**Tagline:** Run agents reliably. Measure them rigorously. Make them better automatically.

## Problem

Production agents fail in ways traditional software does not. They retry the wrong tool, retrieve irrelevant context, hallucinate citations, and recover from upstream errors silently. Most agent frameworks today optimize for the happy path: chain a few LLM calls, hit a vector store, return an answer. They treat failure as an exception rather than a signal.

The gap between "agent demo" and "agent that works at the 95th percentile" is closed by three capabilities that current frameworks bolt on at best:

1. A durable execution layer that survives worker crashes, broker outages, and provider rate limits without losing state.
2. An evaluation and observability layer that captures traces, scores outcomes, and surfaces regressions before they ship.
3. A retrieval subsystem that learns from its own failure cases instead of decaying as the corpus and the query distribution drift.

Helix is one platform that does all three. The thesis is that an agent runtime designed from the ground up around evaluation produces measurably better agents than one where evaluation is grafted on after the fact.

## Research contribution

**Claim:** Embedding models fine-tuned on automatically mined failure cases from agent traces produce measurable retrieval improvement on a held-out hard set.

**Method:**

1. Run the reference agent over a benchmark workload; collect retrieval traces with success labels (was the cited gold passage actually in the retrieved set?).
2. Cluster failures by error signature: lexical-only mismatch, semantic mismatch, multi-hop miss, ambiguous query.
3. Mine hard negatives from in-cluster failures, producing contrastive triplets of `(query, gold_passage, near_miss_passage)`.
4. Fine-tune the base embedding model (Nomic Embed v1.5) on the mined triplets with InfoNCE loss.
5. Evaluate the fine-tuned model against a held-out hard set that is sequestered at project start and never touched by the miner.

**Headline metric:** Recall@10 on the held-out hard set, before and after fine-tuning, with bootstrap confidence intervals.

**Secondary contributions:**

- A retrieval failure taxonomy derived empirically from the trace corpus.
- A reusable failure-mining pipeline open-sourced as part of the platform.
- An eval harness compatible with the Inspect AI and OpenAI Evals trace formats.

**Risk:** The fine-tune may produce no measurable lift. If so, the framework, taxonomy, and methodology remain the contribution, and the negative result is itself publishable.

## Goals

- **Durable agent execution.** Workflows survive worker crashes, broker outages, and provider failures with at-least-once execution and idempotency hooks.
- **Lossless observability.** Every LLM call, tool call, and retrieval is captured as a structured span. Replay must produce a byte-identical trace given the same inputs.
- **Evaluation as a first-class workflow.** Evals are not scripts you run on the side; they are workflows the runtime schedules, traces, and stores alongside production runs.
- **Self-improving retrieval.** Failure mining and embedding fine-tuning are continuous background workflows, not one-off scripts.
- **Open by default.** Apache 2.0, reproducible builds, public benchmarks.

## Non-goals

- A new agent framework. Helix wraps existing agent code (Python functions that call LLMs and tools). It does not prescribe a graph DSL or a chain abstraction.
- A managed cloud offering. The reference deployment is single-tenant Kubernetes.
- A general-purpose workflow engine. Helix optimizes for LLM-and-tool workloads; it will not compete with Temporal on financial transaction durability.
- Real-time streaming agents at high QPS. Helix targets batch and interactive workloads up to roughly 100 concurrent runs per cluster.

## System overview

Three planes:

- **Control plane.** Go service that owns workflow definitions, run state, scheduling, and the public API. Backed by Postgres.
- **Data plane.** Python workers that execute agent code. Communicate with the control plane over gRPC, receive task dispatches over NATS. Stateless and horizontally scalable.
- **Observability plane.** OpenTelemetry collector ingests spans from workers, persists to ClickHouse, exposes them to the dashboard and eval harness.

Five subsystems sit on top:

- **Scheduler** — DAG resolver, worker assignment, retry policy enforcement.
- **State store** — Postgres tables for workflows, runs, tasks, checkpoints, dead-letter queue.
- **Event bus** — NATS JetStream for control-plane → worker dispatch and worker → control-plane completion events.
- **RAG subsystem** — Indexer, retriever, reranker, failure miner, embedding trainer. Backed by Qdrant.
- **Eval harness** — Dataset registry, scorer registry, evaluation runs as first-class workflows.

Detailed architecture in `docs/architecture.md`.

## Reference workload

A multi-hop deep-research agent over a fixed corpus:

- **Corpus:** BRIGHT plus a 50k-document slice of arXiv cs.AI/cs.CL abstracts and selected full texts.
- **Task:** Given a question requiring 2–4 hops across documents, return an answer with citations.
- **Eval sets:** HotpotQA distractor (dev), MuSiQue (dev), BRIGHT, and a 500-question hard set authored against known-hard retrieval edge cases.
- **Metrics:** Answer F1, citation precision, citation recall, retrieval recall@k, hop accuracy.
- **Failure modes captured:** Missing evidence, hallucinated citation, wrong hop, premature termination, context-window overflow.

The hard set is sequestered at project start. It is not seen by the failure miner or the embedding trainer; it is used only to measure the final lift.

## Success criteria

The project succeeds when all of the following are demonstrably true on the public benchmark:

1. The runtime executes 10,000 agent runs end-to-end with under 0.1% lost-run rate (runs that disappear without entering the dead-letter queue).
2. The replay layer reproduces a sample of 100 runs byte-identically.
3. The eval harness completes the full benchmark suite (HotpotQA dev + MuSiQue dev + BRIGHT + hard set) in under one hour on a single worker pool with eight workers.
4. The fine-tuned embedding model shows at least 5% relative recall@10 improvement on the held-out hard set, statistically significant at p < 0.05 over 1000 bootstrap samples. If this threshold is not met, the negative result is documented with the methodology intact.
5. The dashboard renders a run, its trace, and its eval scores within 2 seconds on a corpus of 1M spans.

## Milestones

- **M1 — Runtime skeleton (weeks 1–3).** Go control plane, Python worker SDK, Postgres state, gRPC API. Run a hello-world workflow end-to-end.
- **M2 — Observability (weeks 4–5).** OTel ingestion, ClickHouse schema, basic dashboard rendering runs and spans.
- **M3 — RAG subsystem (weeks 6–7).** Qdrant integration, baseline retriever, ingestion pipeline for the corpus.
- **M4 — Reference agent + first eval (weeks 8–9).** Deep-research agent code, eval harness, HotpotQA + MuSiQue + BRIGHT baselines.
- **M5 — Failure mining + fine-tuning loop (weeks 10–12).** Trace mining, hard-negative miner, embedding trainer, end-to-end fine-tune workflow.
- **M6 — Hard set + headline result (weeks 13–14).** Author hard set, run full eval suite, write up the research result.
- **M7 — Polish + launch (weeks 15–16).** Helm chart, docs site, blog post, public benchmark results.

## Open questions

- Should the worker SDK ship a TypeScript variant in v1, or wait until post-launch? Python-only keeps scope tight.
- Is NATS JetStream the right choice given Helix's at-least-once semantics, or should the control plane handle dispatch directly over gRPC? Re-evaluate after M1.
- For the hard set: hand-authored or adversarially mined from base agent failures? Both have biases; pick one and document it.
- License for the fine-tuned embedding weights — match the base model (Apache 2.0) or release under a more permissive license?

## License

Apache 2.0. All eval datasets are either redistributed under their original licenses or pointed to via reproducible download scripts.
