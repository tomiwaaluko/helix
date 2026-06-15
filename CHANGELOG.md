## Unreleased

- **M7: Retrievals fan-out + ClickHouse miner path + retrieval view.**
  Python: `current_eval_run_id` contextvar propagated from `evaluate()` to the retriever;
  `_emit_retrieval_otel()` dual-writes a lightweight OTel span for each `retrieve` call after
  the `SpanLogger` span closes (no-op when `OTEL_EXPORTER_OTLP_ENDPOINT` unset).
  `mine_from_clickhouse()` queries ClickHouse HTTP API (`CLICKHOUSE_HTTP_URL`) for workflow
  and retrieval spans, builds synthetic span dicts, and routes through the existing
  `mine_failures()` — no new deps (uses existing `httpx`). `_finetune` in `cli.py`
  routes to `mine_from_clickhouse` when `CLICKHOUSE_HTTP_URL` is set, else falls back to JSONL.
  7 new Python tests (`test_retriever_otel.py` × 3, `test_miner_clickhouse.py` × 4).
  Go: `internal/clickhouse/retrieval_writer.go` — `RetrievalWriter` (async buffered-channel,
  same pattern as `BatchWriter`), exported `ResultTuple`; `retrieval_reader.go` —
  `RetrievalReader.ListRetrievals` (parameterized vs. global, 500-row limit).
  `internal/otlp/server.go` gains `retrievalSink` interface, `NewServerWithRetrieval` constructor,
  fan-out logic in `Export` (gates on `name=="retrieve" && kind=="retrieval"`), and
  `parseRetrievalRow` (JSON results → `[]ResultTuple`, duration from nano timestamps).
  `internal/api/handler.go` gains `retrievalQuerier` interface, `WithRetrievals` setter,
  `GET /api/v1/retrievals` handler (503 on nil reader, `?run_id=` filter).
  `cmd/collector/main.go` wires `RetrievalWriter` + `NewServerWithRetrieval`.
  `cmd/orchestrator/main.go` wires `WithRetrievals`. 8 new Go tests.
  Dashboard: `/retrievals` page with `<RetrievalTable>` (query, retriever, top-k, recall@k,
  run ID, timestamp, duration; `staleTime: Infinity`, 503-tolerant error UX).
  BFF route `web/app/api/retrievals/route.ts` proxies `GET /api/v1/retrievals?run_id=…`.
  `eval-detail.tsx` gains "View retrievals →" link. Nav updated. `web/openapi.yaml`
  extended with `RetrievalRow` schema and `/retrievals` path; types regenerated.
  4 new vitest tests.

- **M6: Eval views slice landed.**
  Go: `POST /api/v1/evals/{eval_id}/events` persists per-example scores to ClickHouse
  synchronously (overrides connection-level `async_insert=1` with per-query `async_insert=0`);
  `GET /api/v1/evals` returns summaries (example count, scorer means); `GET /api/v1/evals/{eval_id}`
  returns per-example events. All three return 503 when ClickHouse is unset.
  New packages: `internal/clickhouse/eval_writer.go`, `internal/clickhouse/eval_reader.go`;
  handler gains `WithEvals(w, r)` setter; 11 new Go handler tests + 5 writer unit tests.
  Python: `helix/eval/reporter.py` adds `EvalReporter` Protocol + `OrchestratorEvalReporter`
  (best-effort POST, swallows `httpx.HTTPError`; `default_reporter()` factory reads env vars);
  `evaluate()` in `harness.py` gains `reporter` param; `cli.py` wires `default_reporter()`.
  7 new reporter tests + 2 harness reporter tests.
  Dashboard: `/evals` list page (`<EvalTable>` — dynamic scorer columns, 10 s refetch, 503 UX)
  and `/evals/[id]` detail page (`<EvalDetail>` — metric cards + per-example score table,
  green/red pass/fail coloring). BFF routes `web/app/api/evals/**` proxy with bearer token.
  Nav updated. `web/openapi.yaml` extended with `EvalSummary`, `EvalEvent`, `ScorerMean`,
  `RecordEvalEventsRequest` schemas; types regenerated. 7 new vitest tests.

- **M5: Trace endpoint + dashboard trace view + CI wiring.**
  Go: `GET /api/v1/runs/{run_id}/trace` reads spans from ClickHouse by `trace_id`
  and rewrites `s3://` blob attributes to presigned MinIO HTTPS URLs (1h expiry).
  Returns 503 with a clear message when `CLICKHOUSE_URL` is unset — no config required
  for running without ClickHouse. New packages: `internal/clickhouse/reader.go`,
  `internal/minio/presigner.go` (new dep: `minio/minio-go/v7`); handler gains
  `WithTrace(sr, presigner)` setter; `config.ClickHouseURL` optional field; 5 new
  Go handler tests + 7 presigner unit tests.
  Dashboard: `/runs/[id]/trace` page with `<SpanTree>` — collapsible parent-child
  tree built from `parent_span_id`; per-span attribute table; `_url` attributes show a
  lazy "Load payload" button (direct fetch of presigned URL, no BFF needed). "View trace"
  button added to run-detail. BFF route `web/app/api/runs/[id]/trace/route.ts` proxies
  with bearer token. 8 vitest tests. `web-gate` wired into root `make test` + `make lint`.

- **M4: Dashboard (thin Runs slice) landed.**
  New `web/` Next.js 14 (App Router) app: `/runs` (table + status filter + 3 s polling) and
  `/runs/[id]` (metadata, input/output JSON, task tree, cancel). Data flows through a
  server-side BFF proxy (`web/app/api/runs/**`) that attaches the bearer token, so it never
  reaches the browser and the orchestrator needs no CORS. TanStack Query + shadcn/ui +
  Tailwind; types generated from `web/openapi.yaml` via `openapi-typescript`. Backend change
  is additive: `store.ListTasksForRun` + `GET /api/v1/runs/{id}` now returns a `tasks` array.
  18 vitest tests + 3 new Go handler tests; `web` kept out of the root gate (run `make web-gate`).

- **M3: Redis + MinIO milestone landed.**
  Redis 7 and MinIO added to `docker-compose.yml` (MinIO API remapped to host 9100 to avoid
  ClickHouse's 9000). Redis: an exactly-once sentinel keyed on `(task_id, attempt)` wired into
  `RemoteEngine` (`worker/helix/runtime/idempotency.py`), the public `helix.exactly_once(...)`
  helper, and a distributed token-bucket LLM rate limiter (`worker/helix/tools/rate_limit.py`).
  MinIO: a `BlobStore` (`worker/helix/tools/blob.py`) implementing the
  `s3://helix-blobs/<y>/<m>/<d>/<span_id>.bin` scheme with bucket bootstrap, presigned URLs,
  and a `maybe_offload` helper that pushes >32 KB span payloads (`prompt_uri`/`completion_uri`)
  off-span — wired into the LLM adapter behind `HELIX_SPAN_PAYLOADS` (default off). All three are
  no-ops when `REDIS_URL`/`S3_ENDPOINT` are unset; `make eval` and determinism are unchanged.
  `make dev` now boots Redis + MinIO; new unit tests for the sentinel, blob store, and limiter.
  Made the `redis` import lazy across worker modules so `import helix` stays free of redis's
  transitive OpenTelemetry pull (see ISSUES.md); hardened `test_otel.py` accordingly.

- **M2: ClickHouse + OTel collector milestone landed.**
  ClickHouse 24 added to `docker-compose.yml`. Schema in `migrations/clickhouse/202606150001_initial_schema.sql`
  (`spans` with 90-day TTL + async inserts; shell tables `llm_calls`, `retrievals`, `eval_events` for M5).
  New Go binary `cmd/collector/`: OTLP/gRPC receiver → `internal/clickhouse.BatchWriter` (200 ms flush,
  500-row batches). New `worker/helix/otel.py`: `OtelSpanExporter` + `configure_otel()` — no-op when
  `OTEL_EXPORTER_OTLP_ENDPOINT` unset, dual-write alongside JSONL when set.
  `make build` now produces `bin/orchestrator` **and** `bin/collector`. `make collector` runs the collector.
  Unit tests: `internal/clickhouse/writer_test.go` (DDL + buffer), `internal/otlp/server_test.go`
  (proto translation), `worker/tests/test_otel.py` (no-op + configure path).

- **M1: Go orchestrator milestone landed.**
  Proto contracts (`proto/helix/v1/`), Postgres schema (`migrations/202606150001_initial_schema.sql`),
  Go orchestrator binary (`cmd/orchestrator/`) with gRPC (RegisterWorker, Heartbeat, CompleteTask, Checkpoint)
  and REST (`/api/v1/runs`), NATS JetStream dispatch, Python `RemoteEngine`
  (`worker/helix/runtime/remote_engine.py`), and worker entrypoint (`python -m helix.worker`).
  `make eval` continues unchanged in local mode. `make test-integration` exercises the full stack.
  `make dev` now boots Qdrant + Postgres + NATS.

- **BRIGHT multi-seed replication (B4 seed=1, B5 seed=2): lift reproduces across all seeds.**
  B4: 0.2528 → 0.3436 (Δ +0.0908), promoted. B5: 0.2528 → 0.3433 (Δ +0.0905), promoted.
  Cross-seed delta spread: 0.003 pp. Paired 95% CIs all exclude zero (P(Δ≤0) ≤ 0.013).
  Improvements dominate regressions ~15:5 in every run. Training randomness has no meaningful
  effect on the outcome. Artifacts: `evals/baselines/bright_b{4,5}_canary_flips.json`.

- **BRIGHT-B3 hardened: paired significance test confirms the +0.0886 lift is real.**
  New `scripts/analyze_canary_flips.py` re-runs the canary's exact hybrid retrieval per question
  and computes a paired test the canary omitted. Reconciles to 0.2528 → 0.3413 and adds:
  paired bootstrap 95% CI [+0.0111, +0.1716] (excludes 0), bootstrap P(Δ≤0)=0.012, sign-test
  p=0.041; 15 improved / 5 regressed / 31 unchanged (7 Miss→Hit vs 3 Hit→Miss). Improvements
  dominate regressions 15:5 — inverse of the net-negative HotpotQA pattern. Result artifact:
  `evals/baselines/bright_b3_canary_flips.json`.

- **BRIGHT-B3: thesis confirmed. Fine-tune lifts recall@10 by +8.86 pp on hard held-out data.**
  First valid BRIGHT biology canary measurement (stratified 52/51 split, fixed dispatch bug).
  92 failures mined from 52 training questions → 92 triplets → 3-epoch fine-tune (loss 0.7066).
  Canary recall@10: 0.2528 → 0.3413 (Δ +0.0886) → promoted to `corpus.bright.active`.
  35% relative improvement over base; 51 held-out questions with 0% training-set overlap.

- **Fixed critical canary dispatch bug (ACTIVE_ALIAS hardcode → Δ=0 on BRIGHT).**
  `_build_promotion_backends._retrieve()` in `helix/cli.py` compared `collection == ACTIVE_ALIAS`
  (hardcoded `"corpus.active"`). When `--promotion-alias corpus.bright.active`, neither the
  before-arm nor the after-arm matched, so both routed to the candidate retriever → Δ=0 by
  construction. BRIGHT-B1 and B2 were both corrupted by this bug. Fix: dispatch on
  `collection == candidate_collection` instead. Commit `d410252`. Logged in ISSUES.md.

- **BRIGHT biology stratified split (52 train / 51 canary, interleaved by base recall).**
  Replaced the random 80/23 B1 split (mean recall: train=0.1640, canary=0.5815 — skewed) with
  a stratified interleaved split: mean recall train=0.2615, canary=0.2528 (within 0.009 pp).

- **BRIGHT biology mini-experiment: base recall@10 = 0.2572 (headroom confirmed).**
  Indexed 10,372-doc corpus (372 gold + 10k sampled distractors), measured
  HybridRetriever recall@10 on all 103 biology queries. Full hits: 7, partial: 46,
  misses: 50. Dramatic contrast with HotpotQA (0.94): BRIGHT provides the headroom
  needed to validate the fine-tune thesis. Fine-tune run initiated.

- **`finetune` CLI parameterised for arbitrary base collection + promotion alias.**
  Added `--collection` (base arm for mining + canary before-arm) and `--promotion-alias`
  (alias to swap on promotion). Default is `corpus.active` for backward compat.
  `promote_candidate()` gains `active_alias` kwarg (same default). Enables BRIGHT
  experiment without touching the HotpotQA corpus or `corpus.active` alias.

- **BRIGHT biology data pipeline added.**
  `scripts/prepare_bright.py`: downloads xlangai/BRIGHT biology, produces
  `data/bright_corpus.jsonl` (gold + sampled distractors) and
  `evals/datasets/bright_biology_dev.jsonl` (103 queries in Helix eval format).
  `scripts/check_bright_recall.py`: direct HybridRetriever recall check (no LLM calls).
  Makefile: `seed-bright`, `check-bright`, `finetune-bright` targets.
  Splits: `bright_biology_train.jsonl` (80 q, mining) + `bright_biology_canary.jsonl` (23 q).

- **Per-question flip analysis for run 4 (fine-tune regression mechanism documented).**
  Full HybridRetriever comparison of base vs run-4 fine-tuned embedder on all 100 dev
  questions: 82 no-change, 6 hit→miss, 3 miss→hit, 9 both-miss. All 6 regressions follow
  the same pattern: recall 1.00→0.50 on 2-hop questions — the fine-tuned model finds one
  gold doc but drops the second. Mechanism documented in `docs/vertical-slice-plan.md`.

- **`make lint` fixed: now works without venv activated.**
  - `Makefile`: use `PYTHONPATH=worker python3.12` for holdout integrity check, `python3.12 -m mypy`
    for type-checking. No longer requires the active venv for these targets.
  - `scripts/check_holdout_integrity.py`: made standalone (no helix import, no transitive deps).
    Added to CI guard allowlist in `.github/workflows/holdout-guard.yml`.
  - `worker/pyproject.toml`: extended mypy `ignore_missing_imports` to cover `aiosqlite`,
    `qdrant_client`, `numpy`, `torch`, `safetensors`, `click`; added `disallow_untyped_decorators
    = false` override for `helix.cli`. Removes false positives when type-checking outside venv.
  - `worker/helix/rag/trainer/train.py`: removed now-redundant `# type: ignore[arg-type]` on
    `DataLoader` call (becomes unused when torch is in `ignore_missing_imports`).

- **Experimental findings written up in `docs/vertical-slice-plan.md`.**
  New section "Experimental results (M0 slice)" documents: authoritative baseline, all 4 run
  results, ceiling-effect analysis (0.94 base recall → no headroom), bottleneck diagnosis
  (LLM sub-question decomposition loses 0.29 recall, not the retriever), per-question flip
  breakdown, and path forward (BRIGHT corpus).

- **Fine-tune lift on the HotpotQA dev set is not statistically detectable (3-run summary).**
  After fixing the corpus mismatch (run 2) and the stale-alias bug (run 4 — the first
  fully valid before/after), the canary recall@10 deltas across all completed runs are:
  - Run 1 (train 150, confounded corpus): 291 triplets, 0.960 → 0.935, Δ −0.025
  - Run 2 (train 150): 19 triplets, 0.940 → 0.950, Δ +0.010 (promoted, then rolled back)
  - Run 4 (train 400, clean): 62 triplets, 0.940 → 0.925, Δ −0.015 (archived)
  All three deltas fall inside heavily overlapping 95% CIs (e.g. run 4: before [0.905, 0.970]
  vs after [0.885, 0.960]). **No run shows a statistically significant effect in either
  direction.** Root cause: the base retriever already scores 0.94–0.96 recall@10 on the dev
  set — there is essentially no headroom for mined-failure fine-tuning to demonstrate a
  measurable lift. Run 2's "promotion" was within noise (a coin-flip), not a real improvement.
  The thesis (mined-failure fine-tuning beats a strong baseline) needs a canary with actual
  headroom to be testable — likely the harder target-state corpus (BRIGHT), not HotpotQA.
  `corpus.active` left on `corpus.base` (run 2's noise-level promotion was reverted).

- **400-question train split runs to completion; 1000-question split does not.**
  `hotpotqa_train_1000.jsonl` mining was killed at ~4.5 h (container session ceiling) with
  zero failure_cases saved — `evaluate()` commits all-or-nothing at the end of the run. The
  400-question split (qs 1–150 warm in LLM cache, 151–400 cold) mines in ~1 h and produced
  62 failures. See ISSUES.md for the incremental-persistence follow-up.

- **First promoted fine-tune run (run 2) — clean apples-to-apples result.**
  Re-ran `helix.cli finetune` after rebuilding `corpus.base` from the full 15 568-chunk corpus
  so both arms searched identical-size indices. Results:
  - Mining: 19/150 train questions failed direct retrieval (vs 142/150 in run 1 — the larger
    corpus covers more training questions). 19 failure cases → 19 contrastive triplets.
  - Training: 3 epochs, train_loss=1.53 (92 s; fewer examples than run 1).
  - Canary eval: before recall@10 = 0.9400, after recall@10 = 0.9500, Δ = **+0.0100**.
  - Decision: **promoted**. `corpus.active` alias now points to the fine-tuned candidate
    collection (`b72904285aef4680b447728cbfb2ec05`).
  - Note: direct-retrieval recall (0.94/0.95) remains much higher than end-to-end eval recall
    (0.6500); the bottleneck is LLM sub-question decomposition, not the retriever.

- **New baseline on full 15 512-doc corpus** (`evals/baselines/hotpotqa_dev_100_baseline.json`).
  After rebuilding `corpus.base` from the complete 15 568-chunk index, re-ran the 100-question
  dev eval: `retrieval_recall@10 = 0.6500 [0.6050, 0.6950]`, `answer_f1 = 0.1492 [0.1277,
  0.1734]`, `citation_precision = 0.8915 [0.8475, 0.9357]`. The slight recall drop from the
  prior estimate (~0.69) is expected — the larger corpus introduces more distractors for
  the same set of gold documents. This is now the authoritative baseline for the fine-tune
  comparison.

- **Fix: cache `_tiktoken_counter()` result after first call** (`helix/rag/chunker.py`). With
  `token_counter=None` (the CLI default), every `chunk_document` call previously retried the
  tiktoken `cl100k_base` blob download, adding ~10 min of failing HTTP round-trips before
  embedding started on a 15 K-doc corpus. A module-level sentinel list now stores the resolved
  counter (tiktoken or heuristic fallback) after the first call; subsequent calls are O(1).
  139 tests, mypy --strict clean, no change to chunk counts.

- **First real end-to-end fine-tune measurement on the full corpus.**
  Ran `make finetune` (via `helix.cli finetune`) on `hotpotqa_train_150.jsonl` against the
  full 15 512-doc corpus and `hotpotqa_dev_100.jsonl`. Results:
  - Mining: 142/150 questions failed direct retrieval (recall@10 = 0.027 on train); 291 failure
    cases → 291 contrastive triplets.
  - Training: 3 epochs, batch_size=4, loss=0.336; checkpoint saved to
    `data/models/ae7289f02f69439080e17fd85babe9ae/`.
  - Canary eval: before recall@10 = 0.960 [CI 0.930–0.985] (base hybrid retriever, old
    5 937-point corpus); after recall@10 = 0.935 [CI 0.900–0.965] (fine-tuned hybrid retriever,
    new 15 568-point corpus); delta = −0.025.
  - Decision: **archived** (negative delta).
  - Important caveat: before and after arms searched indices of different sizes (5 937 vs
    15 568 chunks), confounding the delta. Next step: rebuild `corpus.base` with the full
    corpus before the next finetune run (see ISSUES.md "Corpus base index stale").

- **Fix the fine-tuned checkpoint save/reload round-trip.** The Nomic Embed v1.5 remote
  modeling code's `save_pretrained` writes transformer weights under a doubled
  `encoder.encoder.layers.*` prefix while reload expects `encoder.layers.*`, so a reloaded
  checkpoint silently dropped all 108 fine-tuned transformer tensors and fell back to base
  weights — making every promotion canary evaluate a no-op "candidate" (reproduced even on an
  untrained base model: save→reload perturbed embeddings by max-abs 3.29). `_default_train_fn`
  now calls `_normalize_nomic_checkpoint()` after `model.save()` to de-double the prefix in the
  saved `*.safetensors`; the fix is idempotent and self-limiting (a future fixed library leaves
  canonical checkpoints untouched). After the fix, save→reload embeddings match the in-memory
  model exactly (max-abs 0.0). 2 new trainer tests. Surfaced by the first real end-to-end run.
- Declare `accelerate>=1.1.0` in `worker/pyproject.toml`. `sentence-transformers` `.fit()`
  delegates to `transformers.Trainer`, which hard-requires `accelerate` even for single-device
  CPU training; without it the real fine-tune raised `ImportError` at the `train_embedding` step.
  Surfaced by the first real end-to-end `finetune` run (the mining-eval resilience fix let the
  job advance past mining into training, where the missing dep was hit). `docs/tech-stack.md`
  updated to note the pin.
- **Mining-eval resilience** (`633f24a`): `evaluate()` gains a `tolerate_failures=True` parameter
  that skips per-example failures (after all retries) instead of aborting the run. Skipped count
  is recorded in `EvalReport.examples_skipped`, propagated into `FinetuneResult.failures_skipped`,
  and printed by `make finetune` when > 0. `_finetune()` passes `tolerate_failures=True` for the
  mining eval; baseline `eval` command remains all-or-nothing. Three new harness tests (partial
  skips, all-fail, default-raises) + one CLI test. 136 total tests, mypy --strict clean.
- Make the promotion canary run the **full hybrid pipeline** (dense + BM25 + rerank) instead of
  dense-only retrieval, so the promote/archive decision reflects end-to-end recall: a candidate
  whose dense gain is washed out by the reranker is no longer promoted. The CLI's
  `_build_promotion_backends` now builds two `HybridRetriever`s sharing one BM25 index + reranker,
  differing only in the dense embedder and its target collection (base model over `corpus.active`,
  the fine-tuned checkpoint over `corpus.candidate.<job_id>`); doc-ids are deduped order-preserving
  to match how the workflow builds `retrieved_doc_ids`. Adds `QdrantAdapter.for_collection()` (a
  sibling adapter scoped to another collection over the same client) and a `_build_candidate_embedder`
  CLI seam. New integration test drives the real hybrid retrieve_fn over embedded Qdrant + BM25.
  132 total tests, mypy --strict clean.
- Add the `finetune` CLI command + `make finetune` (Phase 4): orchestrates the full
  mine → train → promote loop end-to-end against one `embedding_jobs` row.
  `python -m helix.cli finetune --train <split> --eval <split> --corpus <path>` evaluates the
  train split through the current pipeline (persisting results + spans), mines retrieval failures,
  builds contrastive triplets, fine-tunes the embedder, then canary-evaluates the candidate on the
  dev split and swaps `corpus.active` only on a positive recall@10 lift (otherwise archives).
  Heavy backends are injected via new CLI factory seams (`_build_store`, `_train_backend`,
  `_build_promotion_backends`) so the loop is unit-tested without a model or Qdrant server. Adds a
  `FinetuneResult` summary (status, failures mined, triplets built, before/after metrics) and a
  "Fine-tune loop (slice extension)" section to `docs/vertical-slice-plan.md`. 2 new CLI tests
  (full loop + no-failures archive); 131 total tests, mypy --strict clean on 35 files. (Phase 4)
- Add the promotion + canary-eval phase (Phase 3): `helix/rag/promotion/promote.py` —
  `promote_candidate()` re-indexes the full corpus with the candidate checkpoint into
  `corpus.candidate.<job_id>`, measures retrieval_recall@10 before (current `corpus.active`,
  baseline embedder) and after (candidate embedder, new collection) with 95% bootstrap CIs,
  then atomically swaps `corpus.active` to the candidate via `set_alias` only when
  `after.mean > before.mean`; otherwise archives. The job record in `embedding_jobs` is updated
  to `promoted` or `archived` with full CI metrics and `artifact_uri` regardless of outcome.
  Adds `upsert_to()` and `search_in()` to `QdrantAdapter` for explicit-collection access that
  bypasses the alias during indexing and retrieval. Both heavy operations (`index_fn`,
  `retrieve_fn`) are injectable so 12 new tests run without a model or Qdrant instance.
  129 total tests, mypy --strict clean on 35 files. (Phase 3)
- Add the embedding trainer (Phase 2): `helix/rag/trainer/` — `triplets.py` turns mined
  `FailureCase` records into prefixed `(query, gold_passage, hard_negatives)` contrastive
  triplets (Nomic `search_query:`/`search_document:` prefixes applied here, reused from
  `embedder.py`), dropping cases with no gold text or no resolvable hard negative. `train.py`
  fine-tunes Nomic Embed v1.5 with `MultipleNegativesRankingLoss` (InfoNCE) and saves the
  candidate checkpoint; the training backend is injectable (`train_fn`) so tests run without the
  0.5 GB model, and the default backend seeds torch/numpy/python RNGs for reproducible candidates.
  Adds an `embedding_jobs` SQLite table (`create/update/get_embedding_job`, status lifecycle with
  auto `promoted_at`) mirroring `data-model.md`. 12 new tests; 114 total, mypy --strict clean. (Phase 2)
- Add the failure miner (Phase 1): `helix/rag/miner/` — a four-signature rule classifier
  (`lexical_only`, `semantic_mismatch`, `multi_hop_miss`, `ambiguous`) in `signatures.py`, and
  `mine_failures` in `miner.py` which joins `data/spans.jsonl` retrieval spans to per-example
  eval results, identifies missed gold passages (recall < 1.0), classifies each failure, and
  returns `FailureCase` records carrying the representative query, missed gold doc, text, ranked
  retrieved set, and up to `max_hard_negatives` retrieved-but-wrong doc_ids for the trainer.
  Adds a `failure_cases` SQLite table to `sqlite_store.py` with idempotent `save_failure_cases`
  / `get_failure_cases` (filter by signature). Adds `load_corpus` to `helix/eval/corpus.py`.
  14 new unit tests; all 102 tests pass, mypy --strict clean on 30 files. (Phase 1)
- Seed a mining/train split for the embedding fine-tune loop (Phase 0): `scripts/prepare_train_split.py`
  writes `evals/datasets/hotpotqa_train_1000.jsonl` (HotpotQA distractor questions 600-1600), disjoint
  from the dev set (0-100) and the sequestered holdout (100-600). Failures mined from this split train
  the embedding model while `hotpotqa_dev_100` stays a fair held-out set for measuring recall@10 lift.
  `scripts/prepare_corpus.py` now indexes the first 1600 questions (was 600) so the corpus covers all
  three splits — referential integrity verified for dev/holdout/train (corpus grew 5911 → 15512 docs).
- Make the eval harness resilient to per-example failures: `evaluate` now retries each
  example up to `max_attempts` (default 3) with exponential backoff, so a transient provider
  error on one question no longer aborts the whole batch. The backoff sleeps outside the
  concurrency semaphore, and because workflow LLM calls are cached, a retried example reuses
  the sub-calls that already succeeded and only re-issues the failed one.
- Enable LiteLLM's provider retries in the LLM adapter: pass `num_retries` (default 4,
  override via `HELIX_LLM_NUM_RETRIES`) to `acompletion` so transient provider errors
  (503/429/timeouts) are retried with exponential backoff instead of aborting a 100-question
  eval on the first blip. The adapter still implements no retry loop of its own — LiteLLM owns
  the backoff, consistent with the cache/metering/replay design. `num_retries` is not part of
  the cache key (it does not affect output).
- Pin `einops` in the worker dependencies: the Nomic Embed v1.5 remote modeling code
  imports it, so a clean checkout failed at the first `embed_documents` without it.
- Make the chunker's token counter resilient: `cl100k_base` lives on a blob store that
  is unreachable in network-restricted environments (403), which previously failed the
  entire `index`. `_tiktoken_counter` now degrades to a deterministic char-based heuristic
  (with a `RuntimeWarning`) when the tokenizer can't be fetched. Behavior is unchanged when
  tiktoken is reachable; for the HotpotQA corpus the impact is negligible (99.8% of docs are
  under the chunk budget, so chunk boundaries are essentially identical either way).
- Scaffold the vertical-slice worker package: root `Makefile`, `worker/pyproject.toml`
  (pinned deps + ruff/mypy/pytest config), the `helix/` package tree with empty
  `__init__.py` files, a `.gitignore` for `data/`/`*.db`, and a smoke test. `make lint`
  and `make test` pass on the empty package. (Task 1)
- Add the SDK authoring surface for local execution: `@helix.task`, `@helix.workflow`
  (with `.local()`), and `helix.gather`, plus the `Doc`/`Citation`/`Answer` core types.
  Decorators record retry/timeout metadata (inert locally) and keep a single dispatch
  point for the engine to slot into later. (Task 2)
- Add `helix.logging`: a structured span logger writing JSONL to `data/spans.jsonl`
  with a `SpanLogger.span()` context manager and a `trace()` binder. The span stack and
  per-run `trace_id` propagate via `contextvars`, so nested spans get correct
  `parent_span_id` across `await`/`gather`. Field names match the target ClickHouse
  `spans` schema. (Task 3)
- Add `helix.runtime.sqlite_store.SqliteStore`: async CRUD over the slice's SQLite state
  (`runs`, `tasks`, `eval_results`, `datasets`) with `create_run`/`update_run`,
  `create_task`/`update_task`, `store_eval_result`/`get_eval_results`, and an idempotent
  `register_dataset`. Single connection, serialized writes; JSON state in TEXT columns. (Task 4)
- Add the asyncio engine (`helix.runtime.engine.Engine`): runs a submitted workflow as a
  coroutine and dispatches each `@task` call through an `asyncio.Queue`, returning a `Future`.
  Mode is detected via a `contextvars.ContextVar[Engine | None]` (new `helix.runtime.context`),
  so workflow code is identical in `.local()` and `submit()`. Persists run/task lifecycle to
  SQLite, emits a `kind="task"` span per dispatch, and retries once before failing. Also adds
  `SqliteStore.get_tasks`. Corrected the slice-plan span-kind contract from `workflow` to
  `task`. (Task 5)
- Add the LiteLLM tool adapter (`helix.tools.litellm_adapter.llm_call`) and disk-backed
  response cache (`helix.tools.llm_cache.LLMCache`). Async wrapper over `acompletion` that
  emits a `kind="llm"` span (model, token counts, `cost_usd`, `cache_hit`, `replayed`),
  defaults `temperature=0`, and caches by `sha256` of canonical generation params. Cache hits
  replay with `cost_usd=0`/`replayed=true`; on by default outside production, off via
  `HELIX_LLM_CACHE=0`. LiteLLM is lazily imported and injectable for testing. (Task 6)
- Add the embedding client (`helix.tools.embedder.Embedder`): Nomic Embed v1.5 via
  sentence-transformers, with `embed_queries`/`embed_documents` applying the `search_query:` /
  `search_document:` task prefixes, internal batching (default 64), and a `kind="internal"`
  span (`model`, `count`, `dimension`). The model is lazily loaded and the encode fn is
  injectable, so tests run against a stub. (Task 7)
- Add the Qdrant adapter (`helix.tools.qdrant_adapter.QdrantAdapter`): async `upsert` (batched,
  100), `search` (via `query_points`), `create_collection`, and `set_alias`. All reads/writes go
  through a collection alias (default `corpus.active`); `search` emits a `kind="retrieval"` span.
  Client is lazily imported and injectable, so tests run against Qdrant's in-memory local mode.
  Bumped the `qdrant-client` floor to `>=1.12` for the `query_points` API. (Task 8)
- Add the document chunker (`helix.rag.chunker.chunk_document`): structural splitting
  (paragraphs → sentences → word-window fallback), greedy packing to `max_tokens` (default 512)
  with `overlap_tokens` (default 64) carried across boundaries, deterministic `{doc_id}#chunk{N}`
  ids, and `tiktoken` (`cl100k_base`) sizing. The token counter is injectable for testing. Adds
  `tiktoken` as a dependency. (Task 9)
- Add the indexer (`helix.rag.indexer.index_corpus`): loads a JSONL corpus, chunks every doc,
  embeds all chunks, and upserts them into Qdrant with `{doc_id, chunk_id, source, text}`
  payloads, pointing the `corpus.active` alias at the new collection. Chunk ids map to
  deterministic `uuid5` point ids (re-index overwrites). Returns an `IndexResult` summary and
  wraps the run in a `kind="internal"` span. (Task 10)
- Add BM25 sparse retrieval (`helix.tools.bm25.BM25Index`): builds an in-memory
  `rank_bm25.BM25Okapi` index over chunks (lowercase whitespace tokenization), `search` returns
  ranked `ScoredChunk`s, and `save`/`load` pickle the index for reuse. Empty corpus yields no
  results. (Task 11)
- Add the hybrid retriever (`helix.rag.retriever.HybridRetriever`) and BGE reranker
  (`helix.tools.reranker.Reranker`): dense (Qdrant) + sparse (BM25) candidates fused by RRF
  (`k=60`), reranked by a cross-encoder, returned as top-k `Doc`s carrying
  `metadata["doc_id"]` for the recall scorer. Emits a `kind="retrieval"` span
  (`query`, `retriever`, `top_k`, `results`). Reranker model is lazily loaded and the predict
  fn injectable for testing. (Task 12)
- Add corpus preparation: `helix.eval.corpus` (pure logic — `wiki_doc_id` hashing, `build_corpus`
  dedup-by-title over both HotpotQA context shapes, `write_corpus` JSONL) plus a thin
  `scripts/prepare_corpus.py` that downloads HotpotQA distractor-dev via `datasets` and writes
  `data/corpus.jsonl`. The `wiki_<title_hash>` id scheme is shared so Task 14's `supporting_facts`
  resolve against the corpus. Adds `datasets` as a dependency. (Task 13)
- Add HotpotQA eval-dataset prep: `helix.eval.hotpotqa` (pure — `build_questions` to the
  `data-model.md` schema over both supporting-fact shapes, `write_examples`, and
  `missing_doc_ids`/`supporting_doc_ids` integrity checks) plus `scripts/prepare_hotpotqa.py`,
  which writes `evals/datasets/hotpotqa_dev_100.jsonl` and validates supporting-fact `doc_id`s
  against `data/corpus.jsonl`. Supporting-fact titles reuse `wiki_doc_id`. (Task 14)
- Add the `deep_research` workflow (`helix.workflows.deep_research`): `decompose` →
  parallel `retrieve` → `synthesize`, as `@helix.task`/`@helix.workflow`. LLM calls run at
  `temperature=0`; the workflow attaches the deduped union of retrieved `doc_id`s to
  `Answer.metadata["retrieved_doc_ids"]`. Dependencies are injected via a `contextvars`
  `ResearchDeps` (`using_research_deps`); system prompts live in `helix/workflows/prompts/`.
  Also changes `HybridRetriever` so `Doc.id` is the corpus `doc_id` (chunk_id stays in
  metadata), so recall and citations key off the doc. (Task 15)
- Add the eval harness (`helix.eval.harness`): `load_dataset` (JSONL + schema validation),
  `evaluate(workflow_fn, dataset, scorers, concurrency, store)` (semaphore-bounded async runs,
  per-scorer SQLite persistence, mean/std/seeded-95%-bootstrap-CI aggregation), and `EvalReport`
  (`.metrics`/`.per_example`/`.to_json`). (Task 16)
- Add the holdout access guard (Task 14b, code): `load_dataset` raises `HoldoutAccessError` for
  any `holdout` path unless `HELIX_HOLDOUT_UNLOCK=1`; the holdout filename lives only in
  `harness.py`. `scripts/prepare_hotpotqa.py` extended to emit the 500-question holdout + a
  `.sha256` lock; `scripts/check_holdout_integrity.py` (run in `make lint`, skips if absent); and
  a `holdout-guard.yml` CI workflow that rejects unauthorized holdout filename references. (Task 14b)
- Add the eval scorers (`helix.eval.scorers`): `answer_f1` (token-level F1 with canonical
  HotpotQA/SQuAD normalization — lowercase, punctuation-split, articles removed),
  `citation_precision` (fraction of cited doc_ids that are gold; vacuous 1.0 when no citations),
  and `retrieval_recall_at_k` (gold doc_ids found in the top-k `retrieved_doc_ids`), plus
  `default_scorers()` keyed for the harness. (Task 17)
- Add the slice CLI (`helix.cli`, `python -m helix.cli`): `index` (chunk + embed a corpus into
  Qdrant and write the BM25 sidecar), `eval` (run `deep_research` over a dataset, score with the
  selected scorers, write the baseline JSON report with bootstrap CIs + model/embedding/timestamp
  metadata), and `run` (answer one question). Heavy components (embedder, Qdrant adapter, reranker,
  LLM completion, token counter) come from monkeypatchable module-level factories so the end-to-end
  CLI test (`tests/test_cli.py`) drives the full pipeline with stubs and on-disk local Qdrant.
  `--no-cache` disables the LLM cache; adds `infra/compose/docker-compose.yml` so `make dev` boots
  Qdrant. This makes `make eval` real. (Task 18)
- Fix the HotpotQA download in `scripts/prepare_corpus.py` and `scripts/prepare_hotpotqa.py`: the
  dataset moved to the `hotpotqa/hotpot_qa` namespace (and to Parquet), so the bare `hotpot_qa` id
  no longer resolves on the Hub. The pure parsers already handle the Parquet `context` /
  `supporting_facts` shapes, so only the dataset id changed.
- Fix corpus coverage in `scripts/prepare_corpus.py`: build from the first 600 questions (not 500)
  so the corpus covers the union of the dev (0–100) and holdout (100–600) splits. Under-covering
  silently failed holdout referential integrity (the holdout's supporting paragraphs were absent).
- Add an embedded Qdrant mode to the CLI (`--qdrant-path` on `index`/`eval`/`run`): runs Qdrant
  on-disk in-process via `AsyncQdrantClient(path=...)`, so the pipeline runs with no Qdrant server
  or Docker daemon. Without it, `--qdrant-url` connects to a server as before. The CLI test now
  exercises the real embedded branch instead of stubbing the adapter.
