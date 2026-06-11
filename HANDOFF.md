# Handoff

> Latest at top. Both agents update this before ending a session, per the Session protocol in `AGENTS.md`.
> If you're picking up work, read the top entry and `docs/vertical-slice-plan.md`.

## Entry template

```
## YYYY-MM-DD HH:MM TZ — <Tool> → next session

**Last commit:** `<hash>` on `<branch>`
**Working tree:** <clean | dirty: files>
**Task plan position:** <task N — status>

**What shipped this session**
- ...

**What's next**
1. ...

**Open questions / decisions pending**
- ...

**Gotchas hit**
- ...
```

---

## 2026-06-11 23:00 UTC — Claude Code → next session

**Last commit:** `633f24a` on `claude/eloquent-clarke-qiha1x`
**Working tree:** clean (after this HANDOFF commit)
**Task plan position:** Fine-tune loop complete (Phases 0–4) + hybrid canary + mining resilience.

```
$ git log -1 --oneline
633f24a feat(harness): tolerate per-example LLM failures in mining eval
$ git status --short
(clean)
```

**What shipped this session**
- **Mining-eval resilience** (`633f24a`) — `evaluate()` now accepts `tolerate_failures=True`,
  catching per-example exceptions after all retries and skipping instead of aborting the run.
  - `worker/helix/eval/harness.py`: `EvalReport.examples_skipped: int = 0`; `run_one` catches
    and returns `None` when `tolerate_failures=True`; gather filters `None`s.
  - `worker/helix/cli.py`: `FinetuneResult.failures_skipped: int = 0`; `_finetune()` passes
    `tolerate_failures=True` to the mining eval and threads `skipped` through all early returns;
    `finetune` CLI prints "examples skipped (provider errors): N" when N > 0.
  - 3 new harness tests + 1 CLI test. 136 total tests, mypy --strict clean on 35 files.

**What's next**
1. **Run the loop for real**: `make seed` then `make finetune`. The resilience fix means the
   mining eval will now survive Gemini 503s — it'll skip failed examples and continue.
   If the small corpus (420 docs) again mines zero failures (perfect recall), switch to the
   full 15512-doc corpus with `--top-k 3` or `--top-k 5` to force missed gold docs.
2. Both prior canary limitations still stand (noted in the previous HANDOFF entry below):
   - Single-query retrieval proxy in the canary: the promotion score is based on per-example
     single-query recall, not full multi-hop deep_research output. Acceptable for now.
   - Chained promotions: each `make finetune` run uses the current `corpus.active` as base.
     If back-to-back runs are needed, the second run must re-index on the promoted checkpoint.

**Open questions / decisions pending**
- None new. Full seed + real finetune run is the only outstanding work.

**Gotchas hit**
- Gemini 503 hit twice during smoke runs (see previous entry). Now resilient.
- `evaluate()` return type change: `run_one` now returns `dict | None`; mypy needed the
  explicit `raw: list[dict[str, Any] | None]` annotation on the gather result.

---

## 2026-06-11 22:30 UTC — Claude Code → next session

**Last commit:** `de5e697` on `claude/eloquent-clarke-qiha1x` (HANDOFF commit follows)
**Working tree:** clean (after this HANDOFF commit)
**Task plan position:** Fine-tune loop complete (Phases 0–4) **+ hybrid canary**.

```
$ git log -1 --oneline
de5e697 feat(promotion): make the canary run the full hybrid pipeline
$ git status --short
(clean)
```

**What shipped this session**
- **Hybrid canary** (`de5e697`) — the promotion gate now decides on full hybrid
  (dense + BM25 + rerank) recall instead of dense-only, so a candidate whose dense
  gain is washed out by the reranker is correctly *not* promoted.
  - `worker/helix/cli.py`: `_build_promotion_backends` builds two `HybridRetriever`s
    sharing one BM25 + reranker; only the dense embedder + target collection differ
    (base→`corpus.active`, candidate→`corpus.candidate.<job_id>`). Returns
    `(None, retrieve_fn)` so promotion still re-embeds via its default indexer.
    New `_build_candidate_embedder` seam; doc-ids deduped order-preserving.
  - `worker/helix/tools/qdrant_adapter.py`: `for_collection()` — sibling adapter
    over the same client scoped to another collection.
  - `promote.py`: `ACTIVE_ALIAS` made public.
  - New integration test exercises the real hybrid retrieve_fn over embedded Qdrant+BM25.
  - 132 tests, mypy --strict clean on 35 files.

**What's next**
- **Run the loop for real** (unchanged from below): `make seed` then `make finetune`.
  First run downloads Nomic (~500 MB) + real gradient descent + mining eval over 1000
  train questions (cached after first run).
- **Two known canary limitations** (both documented in the slice plan / commit):
  1. *Single-query vs decomposed.* The canary queries with the raw question, not the
     workflow's decomposed sub-queries, so its absolute recall is a conservative proxy
     for the `make eval` 0.69 — the before/after **delta** is the trustworthy signal.
     To make it exact, run the full `deep_research` workflow over dev with deps pointed
     at the candidate retriever (needs an `evaluate_fn`-shaped seam in promotion; bigger).
  2. *Chained promotions.* The "before" arm uses the base embedder against `corpus.active`.
     After a prior promotion, `corpus.active` was indexed with a *previously-promoted*
     model, so the base embedder would mis-query it. Fine for the first finetune; for
     repeated promotions, track the active embedder (e.g. on the `embedding_jobs` row).

**Open questions / decisions pending**
- (carried) `answer_f1` normalization is articles-only (canonical SQuAD), not general stopwords.
- (carried) Should `make finetune` write a JSON run-summary report like `make eval` does?

**Gotchas hit**
- `ruff` ASYNC240 flags `pathlib.Path` I/O inside `async def` test bodies. Fixed by
  making the hybrid-canary test sync and driving the awaits via `asyncio.run` on an
  inner `_drive()` helper.

---

## 2026-06-11 21:40 UTC — Claude Code → next session

**Last commit:** `a3c5e08` on `claude/eloquent-clarke-qiha1x` (HANDOFF commit follows)
**Working tree:** clean (after this HANDOFF commit)
**Task plan position:** **Fine-tune loop complete (Phases 0–4).** The self-improving
RAG loop deferred in the original M0 plan is now built and orchestrated end-to-end.

```
$ git log -1 --oneline
a3c5e08 feat(cli): add finetune command orchestrating the full loop (Phase 4)
$ git status --short
(clean)
```

**What shipped this session**
- **Phase 4 — CLI orchestration** (`a3c5e08`)
  - `worker/helix/cli.py`: new `finetune` command + `_finetune()` async core wiring
    mine → train → promote against one `embedding_jobs` row; `FinetuneResult` summary.
  - Factory seams `_build_store`, `_train_backend`, `_build_promotion_backends` (mirror
    the existing `_build_*` pattern) keep the loop model-free in tests.
  - `Makefile`: `make finetune` target (train=train_1000, eval=dev_100, corpus=data/corpus.jsonl).
  - `docs/vertical-slice-plan.md`: un-deferred the loop, added "Fine-tune loop (slice extension)".
  - `worker/tests/test_cli.py`: 2 end-to-end tests (full promoted loop + no-failures archive),
    real embedded Qdrant + BM25, stubbed LLM/train/promotion backends.
  - 131 tests, mypy --strict clean on 35 files.

**Full loop now on disk (Phases 0-4)**
| Phase | Commit | Description |
|-------|--------|-------------|
| 0 | `9d8c8dd` | Disjoint train split `hotpotqa_train_1000.jsonl` (q 600-1600) |
| 1 | `a80b06f` | Failure miner: 4-signature classifier + `mine_failures()` + `failure_cases` |
| 2 | `c397646` | Embedding trainer: `build_triplets()` + `train_embedding()` + `embedding_jobs` |
| 3 | `07546a9` | Promotion: `promote_candidate()` + CI comparison + alias swap |
| 4 | `a3c5e08` | CLI `finetune` + `make finetune` — orchestrates 1→3 |

**What's next**
- **Run the loop for real.** `make finetune` has only been exercised with stubbed backends.
  A real run needs: `make seed` (corpus + dev split indexed), then `make finetune`. First run
  downloads Nomic (~500 MB) and does real gradient descent — budget GPU time + LLM cost for the
  mining eval over 1000 train questions (cached after first run).
- **Faithfulness note on the canary:** the promotion canary measures *dense-only* recall@10
  (`adapter.search` / `search_in`), isolating the embedding's contribution but NOT running the
  full hybrid (BM25 + dense + rerank) pipeline. So canary "before" recall won't equal the headline
  0.69 hybrid baseline. If the next agent wants a hybrid canary, swap promotion's default
  `retrieve_fn` for one that drives `HybridRetriever` against the candidate collection.

**Open questions / decisions pending**
- (carried) `answer_f1` normalization is articles-only (canonical SQuAD), not general stopwords.
- Should `make finetune` write a JSON summary report (like `make eval` does) for the run record?
  Currently it only echoes to stdout and persists to the `embedding_jobs` row.

**Gotchas hit**
- Promotion's alias swap (`set_alias`) fails if the candidate collection doesn't exist. In the
  full-loop test the stubbed `index_fn` must still `create_collection` (empty) so the real swap
  resolves — production `index_fn` creates it as part of indexing.
- The mining eval and `mine_failures` must share the same `--spans` path; `spans.jsonl` is
  append-only, so a fresh file per finetune run avoids stale question→trace joins.

---

## 2026-06-11 21:00 UTC — Claude Code → next session

**Last commit:** `07546a9` on `claude/eloquent-clarke-qiha1x`
**Working tree:** clean (after CHANGELOG + HANDOFF commit)
**Task plan position:** Phases 0–3 of the fine-tune loop complete.

```
$ git log -1 --oneline
07546a9 feat(promotion): add canary-eval promotion phase (Phase 3)
$ git status --short
(clean)
```

**What shipped this session**
- **Phase 3 — Promotion + canary eval** (`07546a9`)
  - `worker/helix/rag/promotion/promote.py`: `promote_candidate()` — re-indexes corpus
    with candidate checkpoint into `corpus.candidate.<job_id>`, measures recall@10
    before/after with bootstrap 95% CIs, alias-swaps `corpus.active` only on strict lift
    (after.mean > before.mean), updates `embedding_jobs` status to promoted/archived.
  - `worker/helix/tools/qdrant_adapter.py`: added `upsert_to()` and `search_in()` for
    explicit-collection access (bypasses alias during indexing and canary retrieval).
  - `worker/tests/test_promotion.py`: 12 tests (promoted / archived / tied / empty guard /
    job status transitions / metrics structure / evaluating-before-index ordering).
  - 129 tests, mypy --strict clean on 35 files.

**Loop components shipped (Phases 0-3)**
| Phase | Commit | Description |
|-------|--------|-------------|
| 0 | `9d8c8dd` | Disjoint train split: `hotpotqa_train_1000.jsonl` (questions 600-1600) |
| 1 | `a80b06f` | Failure miner: 4-signature classifier + `mine_failures()` + SQLite `failure_cases` |
| 2 | `c397646` | Embedding trainer: `build_triplets()` + `train_embedding()` + SQLite `embedding_jobs` |
| 3 | `07546a9` | Promotion: `promote_candidate()` + CI comparison + alias swap |

**What's next**
- **Phase 4 — CLI orchestration**: `python -m helix.cli finetune --train <split> --eval <split>`
  command wiring Phases 1→3 into one job; `make finetune` target. This closes the loop end-to-end
  and lets the user run the full fine-tune experiment from the command line.

**Open questions / decisions pending**
- (carried) `answer_f1` normalization is articles-only (canonical SQuAD), not general stopwords.
- (carried) Retrieval ran with heuristic token counter (cl100k blocked); production seeding may
  produce slightly different chunk boundaries.

**Gotchas**
- `ASYNC230` lint rule fires when `open()` is called inline in an async function. Fixed by
  extracting to `_load_jsonl()` sync helper (same pattern as `indexer.py`).
- `_bootstrap_ci` and `_aggregate` in `harness.py` are private; re-implemented in `promote.py`
  rather than coupling to internals.

---

## 2026-06-11 18:20 UTC — Claude Code → next session

**Last commit:** (see below) on `claude/eloquent-clarke-qiha1x`
**Working tree:** clean
**Task plan position:** **SLICE COMPLETE.** Canonical baseline on `gemini/gemini-2.5-flash`
committed. All 18 tasks done + both baseline runs complete.

```
$ git log -1 --oneline
6558e52 feat(eval): update baseline to canonical gemini-2.5-flash run (n=100)
$ git status --short
(clean)
```

**What shipped this session**
- **Canonical baseline** (`evals/baselines/hotpotqa_dev_100_baseline.json`, n=100,
  model=`gemini/gemini-2.5-flash`, embedding=`nomic-ai/nomic-embed-text-v1.5`):
  - `answer_f1`: 0.1554 [0.1327, 0.1781]
  - `citation_precision`: 0.9123 [0.8690, 0.9507]
  - `retrieval_recall@10`: 0.6900 [0.6450, 0.7400]
  - This supersedes the flash-lite run (0.176 f1, 0.660 recall) from the prior session.
  - `retrieval_recall@10` rose from 0.66 → 0.69 with the stronger model's sub-queries.

**Prior session shipped (for record)**
- **Flash-lite baseline** (superseded): answer_f1=0.176, citation_precision=0.919, recall=0.660.
- Four resilience fixes: chunker heuristic fallback (`983ae14`), einops dep (`4368a59`),
  LiteLLM `num_retries` (`39c6595`), harness example-level retry (`97c7626`).
- Index: 5911 docs / 5937 chunks into `corpus.base` (embedded Qdrant at `data/qdrant`).
- 87 tests pass, ruff clean, mypy --strict clean (27 files).

**What's next**
- **M0 slice is done.** Baseline numbers are on disk, all 18 tasks shipped, harness + pipeline
  proven end-to-end. Next milestone is M1 (Go orchestrator + NATS + Postgres).
- **`answer_f1` is low (0.155)** — expected: extractive SQuAD-style F1 normalization is strict;
  the deep_research synthesizer produces prose answers, not span extracts. The fine-tune loop
  will lift this. `citation_precision` (0.91) and `recall@10` (0.69) are healthy starting points.

**Open questions / decisions pending**
- (carried) `answer_f1` normalization is articles-only (canonical SQuAD), not general stopwords.
- The retrieval pipeline ran with the heuristic token counter (cl100k blocked in this env). For
  a canonical run, allowlist `openaipublic.blob.core.windows.net`; impact is negligible.

**Gotchas hit**
- **Gemini "high demand" 503s**: flash and pro were throttled in the prior session; this session
  flash was available. LiteLLM prints "If you need to debug this error" banners on every retry —
  these are informational, not fatal. The three-layer resilience (LiteLLM retries × 6, harness
  example retry × 3, disk cache) handled all transient failures.
- `data/` is gitignored: `corpus.jsonl`, `data/qdrant`, `bm25_index.pkl`, `llm_cache`, spans all
  live there and must be regenerated on a fresh clone (`prepare_corpus.py` → `prepare_hotpotqa.py`
  → `index`). Only the datasets and the baseline JSON are committed.

---

## 2026-06-11 07:00 UTC — Claude Code → next session

**Last commit:** (see below) on `claude/eloquent-clarke-qiha1x`
**Working tree:** clean after commit
**Task plan position:** Task 14b — DONE (data generated + committed). All 18 slice tasks complete.

**What shipped this session**
- `evals/datasets/hotpotqa_dev_100.jsonl`: 100 HotpotQA distractor-dev questions (questions 0–99).
- `evals/datasets/hotpotqa_dev_holdout_500.jsonl`: 500 sequestered holdout questions (100–599), disjoint.
- `evals/datasets/hotpotqa_dev_holdout_500.sha256`: SHA-256 hash-lock (`e4b7c5a564ad…`).
- All generated from `hotpotqa/hotpot_qa distractor/validation` via the fixed scripts on this branch.
- Referential integrity verified against `data/corpus.jsonl` (5911 docs from 600 questions) for both sets.
- `scripts/check_holdout_integrity.py` passes: `Holdout integrity OK`.
- 84 tests pass, ruff clean, mypy --strict clean (27 files).

**What's next**
1. **Real baseline numbers**: on a machine with Qdrant + LLM key run `make dev && make seed && make eval`
   to produce `evals/baselines/hotpotqa_dev_100_baseline.json` with real metrics + CIs. This container
   lacks Nomic Embed download + LLM network access.
2. `data/corpus.jsonl` is gitignored (data/ is in .gitignore) — the seeded corpus must be regenerated
   on each fresh clone via `make seed` / `python scripts/prepare_corpus.py`.

**Open questions / decisions pending**
- (carried) `answer_f1` normalization is articles-only (canonical SQuAD), not general stopwords.
- CLI `eval` does not persist per-example scores to SQLite; the JSON report is the artifact.

**Gotchas hit**
- `data/corpus.jsonl` produced 5911 docs (all unique paragraphs from 600 questions) — more than the
  1–2k the spec estimated, but correct: the spec estimate assumed ~2 gold paragraphs/question × 500.
  The actual context is 10 distractor paragraphs/question × 600 questions, deduped. Fine for eval.

---

## 2026-06-11 05:30 UTC — Claude Code → next session

**Last commit:** `cd96f54` on `claude/current-phase-gotchas-tjkwsu`
**Working tree:** clean (`git status` → nothing to commit)
**Task plan position:** Task 18 (CLI + end-to-end) — DONE. **All 18 slice implementation tasks are
now complete.** Remaining slice gap: Task 14b holdout DATA generation (needs a seeded env with
`datasets` + the real HotpotQA download — see below).

```
$ git log -1 --oneline
cd96f54 feat(cli): index/eval/run end-to-end CLI + integration test (Task 18)
$ git status --short
(clean)
```

**What shipped this session** (ship-first; self-contained)
- `helix/cli.py` (`python -m helix.cli`), three Click commands:
  - `index --corpus --collection [--vector-size --max-tokens --overlap-tokens --bm25 --qdrant-url
    --alias --spans]`: runs `index_corpus` (Qdrant dense) and writes the BM25 sidecar to
    `data/bm25_index.pkl` from the *same* chunks (same token counter + chunk params, so chunk_ids
    line up for RRF fusion).
  - `eval --workflow --dataset --scorers --concurrency --output [--qdrant-url --bm25 --top-k
    --model --no-cache --spans]`: loads dataset → builds `HybridRetriever` + `ResearchDeps` →
    `using_research_deps` around `evaluate(_run_workflow, ...)` → writes baseline JSON
    (`eval_id, workflow, dataset, n, metrics, per_example, model, embedding_model, timestamp`).
    `--no-cache` sets `HELIX_LLM_CACHE=0`. Echoes mean + [ci_low, ci_high] per scorer.
  - `run --input '{"question": "..."}'`: single question, prints answer text + cited doc_ids.
- **Factory hooks** (`_build_embedder/_build_adapter/_build_reranker/_build_completion_fns/
  _token_counter`) are module-level so tests monkeypatch them — the only seam needed to run the
  whole pipeline with stubs and no GPU/server/network.
- `tests/test_cli.py`: end-to-end `index`→`eval` through `CliRunner` with on-disk **local** Qdrant
  (`AsyncQdrantClient(path=...)`) so the collection survives between the two invocations; plus an
  unknown-workflow rejection test and a `run` test. Suite now **84 tests**.
- `infra/compose/docker-compose.yml`: the Qdrant service `make dev` already referenced but that was
  missing on disk (`qdrant/qdrant:v1.12.0`, ports 6333/6334, named volume, TCP healthcheck).
- Verified on Python 3.12 (`/tmp/helixvenv`): `ruff check .` clean, `mypy --strict helix/` clean
  (27 files), 84 tests pass. No stray `data/` written by the suite.

**What's next**
1. **Task 14b holdout DATA** (still deferred — needs network + `datasets`): run
   `python scripts/prepare_hotpotqa.py` in a seeded env to emit + commit
   `evals/datasets/hotpotqa_dev_100.jsonl`, the `hotpotqa_dev_holdout_500.jsonl`, and its `.sha256`
   lock. Until then `make lint`'s holdout integrity check skips (by design) and `make eval` has no
   dataset to read.
2. **Real baseline numbers** (the actual slice deliverable): on a machine with Qdrant + an LLM key,
   `make dev && make seed && make eval` to produce `evals/baselines/hotpotqa_dev_100_baseline.json`
   with real `answer_f1 / citation_precision / retrieval_recall@10` + CIs. Sandbox can't (no
   Nomic/reranker download, no LLM network).

**Open questions / decisions pending**
- (carried) **answer_f1 normalization** is articles-only (canonical SQuAD), not general stopwords —
  see prior entry. Still flagged.
- CLI `eval` does **not** persist per-example scores to SQLite (`evaluate(store=...)` left `None`);
  the JSON report is the artifact. Wire a `SqliteStore` through if you want the `eval_results` table
  populated for the dashboard later.

**Gotchas hit**
- **In-memory Qdrant is per-client**: `index` and `eval` are separate `AsyncQdrantClient` instances,
  so `:memory:` would lose the collection between them. The CLI test uses on-disk **local mode**
  (`path=...`), which persists across the two sequential commands (each opens→writes/reads→closes).
  In production this is moot — Qdrant is a shared server behind `--qdrant-url`.
- `index` chunks the corpus **twice** (once inside `index_corpus` for Qdrant, once in the CLI for
  BM25). Intentional, to keep `index_corpus` (Task 10) untouched; both use the same params so the
  chunk_ids match. If this ever bothers you, have `index_corpus` return its chunks.
- `make lint` runs `scripts/check_holdout_integrity.py` from the **repo root**, which imports
  `helix` — that only works if the package is importable (editable install or `PYTHONPATH=worker`).
  The ad-hoc venv here needs `PYTHONPATH=worker`; CI/the maintainer's env has it installed.

---

## 2026-06-11 04:38 UTC — Claude Code → next session

**Last commit:** `c67ccaf` on `claude/current-phase-gotchas-tjkwsu`
**Working tree:** clean
**Task plan position:** Task 17 (scorers) — DONE. Task 18 (CLI + end-to-end) next — the last
slice task. (Task 14b holdout DATA still deferred — needs a seeded env; see prior entry.)

**What shipped this session** (ship-first; pure functions)
- `helix/eval/scorers.py`: `answer_f1(example, prediction)` (token F1, canonical normalization),
  `citation_precision(example, prediction)` → `ScoreResult` (vacuous 1.0 + `vacuous=True` when no
  citations), `retrieval_recall_at_k(example, prediction, k=10)` → `ScoreResult` (gold doc_ids ∩
  top-k `retrieved_doc_ids`), `retrieval_recall_scorer(k)` (binds k → 2-arg scorer), and
  `default_scorers()` → `{"answer_f1", "citation_precision", "retrieval_recall@10"}`. Scorers read
  the workflow `Answer` via `getattr` (text/citations/metadata), so they're decoupled from the type.
- `tests/test_scorers.py`: exact/partial/zero F1, article+case normalization, citation
  all/partial/vacuous, recall full/partial/k-cutoff/no-gold, and a harness-integration test that
  runs all three default scorers through `evaluate`. Suite now 81 tests.

**What's next**
1. Task 18: `helix/cli.py` (the `python -m helix.cli ...` entry the Makefile already calls) —
   `index` (wire `index_corpus`) and `eval` (load dataset → build retriever + `ResearchDeps` →
   `using_research_deps` around `evaluate(lambda inp: deep_research.local(**inp), ..., default_scorers())`
   → write baseline JSON). `--no-cache` sets `HELIX_LLM_CACHE=0`. This finally makes `make eval`
   real. Then the end-to-end integration test.

**Open questions / decisions pending**
- **answer_f1 normalization:** I removed **articles only** (a/an/the), NOT general stopwords,
  despite the spec text saying "articles and stopwords". The parenthetical "(standard HotpotQA
  normalization)" is canonical SQuAD `normalize_answer`, which is articles-only; adding stopword
  removal would break comparability with published HotpotQA baselines (the research thesis needs
  comparable F1). Flagging in case you intended literal stopword stripping.
- `retrieval_recall@10` scorer name contains `@` (matches the Makefile `--scorers` list and the
  spec); it's a dict key/`eval_results.scorer` string, so no identifier issues.

**Gotchas hit**
- None new. Verified via `/tmp/helixvenv` (3.12): ruff clean, `mypy --strict helix/` clean
  (26 files), 81 pytest pass.

---

## 2026-06-10 23:03 UTC — Claude Code → next session

**Last commit:** `cb5150e` on `claude/current-phase-gotchas-tjkwsu`
**Working tree:** clean
**Task plan position:** Task 16 (eval harness) — DONE. Task 14b — CODE done, holdout DATA
generation deferred (needs dataset/network + Task 18 CLI). Task 17 (scorers) next.

**What shipped this session** (plan-first; maintainer briefed)
- `helix/eval/harness.py`: `load_dataset` (JSONL + schema validation), `evaluate(workflow_fn,
  dataset, scorers, *, concurrency, store, eval_id, bootstrap_samples, seed)` — semaphore-bounded
  async runs, scores via `scorers: Mapping[str, Scorer]` (`Scorer = (Example, output) -> float |
  ScoreResult`), persists each result to SQLite (`store_eval_result`), aggregates mean/std/95%
  **seeded** bootstrap CI. `EvalReport` (`.metrics`, `.per_example`, `.to_json`). `Example`,
  `ScoreResult`, `sha256_file`.
- **Task 14b access guard:** `HoldoutAccessError` raised by `load_dataset` when `"holdout"` is in
  the path and `HELIX_HOLDOUT_UNLOCK != "1"`. The holdout filename literal lives ONLY in
  `harness.py` (constants `HOLDOUT_DATASET_PATH`/`HOLDOUT_SHA256_PATH`); `prepare_hotpotqa.py` and
  `check_holdout_integrity.py` import it.
- `scripts/prepare_hotpotqa.py`: extended to also build the holdout (`build_questions(start=100,
  count=500, id_prefix="hotpotqa_holdout")`) and write its `.sha256`.
- `scripts/check_holdout_integrity.py`: verifies the holdout hash; **skips (exit 0) if absent** so
  `make lint` stays green pre-seed. Wired into the Makefile `lint` target.
- `.github/workflows/holdout-guard.yml`: fails if any `*.py` outside the two whitelisted files
  references the holdout filename.
- `tests/test_harness.py`: load/validate, holdout guard (block without unlock / allow with),
  aggregation + SQLite persistence (40 rows, details), bootstrap determinism, sha256. Suite 70.

**Deferred (cannot complete here)**
- The actual `evals/datasets/hotpotqa_dev_holdout_500.jsonl` + `.sha256` files — no
  dataset/network to generate them. Run `python scripts/prepare_hotpotqa.py` (after
  `prepare_corpus.py`) in an env with `datasets` to produce + commit them.
- 14b Done-when items 1–2 (`make eval-final` succeeds; CLI eval on the holdout raises
  `HoldoutAccessError`) need the CLI (Task 18). The guard itself is done and unit-tested.

**What's next**
1. Task 17: `helix/eval/scorers.py` — `answer_f1`, `citation_precision`,
   `retrieval_recall_at_k(k=10)` matching the `Scorer = (Example, output) -> float | ScoreResult`
   shape. `retrieval_recall_at_k` reads `output.metadata["retrieved_doc_ids"]` (populated by
   deep_research) vs `example.expected_output["supporting_facts"][].doc_id`.

**Open questions / decisions pending**
- Holdout ids use `hotpotqa_holdout_001..500` (disjoint prefix from the dev set). Easy to change
  in the script if you'd prefer continued `hotpotqa_dev_101..600` numbering.
- `scorers` is a `{name: fn}` mapping (not api.md's list + `@helix.scorer`). Cleaner for the slice
  harness; Task 17 scorers are plain functions registered by the CLI under their names.

**Gotchas hit**
- **Test cache isolation:** the Task 15 `deep_research` e2e test passed `cache=None`, so `llm_call`
  used the on-disk default `data/llm_cache/`; the first run populated it and the *second* run got
  cache hits that bypassed the fake LLM (and wrote a `data/` dir into the repo). Fixed by passing a
  tmp `LLMCache` in the test. **Workflow/LLM tests must isolate the cache** (tmp `LLMCache` or
  `HELIX_LLM_CACHE=0`).
- `make lint` now runs `python scripts/check_holdout_integrity.py` from the repo root, which
  imports `helix` — so it requires `pip install -e worker` (the maintainer's env has it). Verified
  here via `PYTHONPATH=worker`. Verified via `/tmp/helixvenv` (3.12): ruff clean, `mypy --strict
  helix/` clean (25 files), 70 pytest pass, CI grep guard passes.

---

## 2026-06-10 22:43 UTC — Claude Code → next session

**Last commit:** `f9dcdce` on `claude/current-phase-gotchas-tjkwsu`
**Working tree:** clean
**Task plan position:** Task 15 (deep_research) — DONE. Task 16 (eval harness) + Task 14b
(holdout) next, as one unit.

**What shipped this session** (plan-first; maintainer signed off in chat)
- `helix/workflows/deep_research.py`: `decompose` → parallel `retrieve` → `synthesize`
  (`@helix.task`/`@helix.workflow`). LLM calls via `llm_call` (temp 0); `retrieve` via the hybrid
  retriever. Workflow attaches the deduped union of retrieved `doc.id`s to
  `Answer.metadata["retrieved_doc_ids"]` (the recall gotcha). Deps (retriever, top_k, model,
  span_logger, cache, injectable completion/cost fns) come from a `contextvars` `ResearchDeps` via
  `using_research_deps(...)`. One `kind="workflow"` span wraps the run; llm/retrieval spans nest.
- Parsing: `_parse_subqueries` (JSON array, markdown-fence/prose-tolerant, cap 4, fallback to
  `[question]`); `_parse_synthesis` (answer text + trailing `CITATIONS: <doc_ids>` line, citations
  filtered to the evidence set; "I don't know" → empty citations).
- `helix/workflows/prompts/decompose.txt`, `synthesize.txt` (loaded via `Path(__file__).parent`).
- **`helix/rag/retriever.py`: `Doc.id` is now the corpus `doc_id`** (was `chunk_id`; chunk_id
  stays in `metadata`) — decision #2, approved. Updated `tests/test_retriever.py` to assert it.
- `tests/test_deep_research.py`: parser units + end-to-end `.local()` against in-memory Qdrant
  with a fake LLM (asserts answer, deduped doc-level `retrieved_doc_ids`, citations ⊆ retrieved,
  2 LLM calls, span tree). Suite now 63 tests.

**What's next**
1. Task 16 + Task 14b together: `helix/eval/harness.py` (dataset loading + eval runner + the
   **holdout access guard** `HoldoutAccessError` keyed on the substring `holdout` +
   `HELIX_HOLDOUT_UNLOCK=1`), then 14b's holdout extraction (`build_questions(start=100,
   count=500)`), `.sha256` lock, `scripts/check_holdout_integrity.py`, and
   `.github/workflows/holdout-guard.yml`. The harness runs `deep_research` per question — wire
   `using_research_deps` around the run loop (deps are run-wide constants; one binding before the
   concurrent gather propagates to every question's local execution).

**Open questions / decisions pending**
- **Submit-mode deps:** `ResearchDeps` is a contextvar set in the workflow's context, so it's
  visible in `.local()` but NOT to engine handlers (worker-loop context). Eval uses `.local()`,
  so fine for now; revisit if eval ever dispatches via the engine (same nested-context item as
  Task 5).
- **Prompt packaging:** prompts are read via `Path(__file__).parent` — works for editable installs
  (the slice). A wheel build would need `package_data`/`importlib.resources`. Note for M1+.

**Gotchas hit**
- mypy invariance: LLM `messages` must be annotated `list[dict[str, Any]]` (a `list[dict[str,str]]`
  literal won't pass to the `list[dict[str, Any]]` param). Verified via `/tmp/helixvenv` (3.12):
  ruff clean, `mypy --strict helix/` clean (24 files), 63 pytest pass.
- Could not real-LLM spot-check the 3 questions from the Done-when (no network/API key here); the
  mechanics are covered by the fake-LLM e2e test. Real spot-check happens on `make eval`.

---

## 2026-06-10 22:17 UTC — Claude Code → next session

**Last commit:** `d612d3d` on `claude/current-phase-gotchas-tjkwsu`
**Working tree:** clean
**Task plan position:** Task 14 (HotpotQA 100-question prep) — DONE. Task 14b (holdout) next,
but it's blocked on Task 16's harness (see below). Task 15 (deep_research) is the bigger next item.

**What shipped this session** (ship-first; logic in package, thin script wrapper)
- `helix/eval/hotpotqa.py`: `build_questions(examples, start, count, id_prefix, id_start)` →
  rows in the `data-model.md` schema (`id`, `input.question`, `expected_output.answer` +
  `supporting_facts[{doc_id, sent}]`, `metadata.hops/type`). `hops` = unique supporting doc_ids;
  handles both supporting-fact shapes (HF `{title, sent_id}` and raw `[title, sent]`). Plus
  `write_examples`, `supporting_doc_ids`, `missing_doc_ids` (referential-integrity check).
- `helix/eval/corpus.py`: added `load_corpus_ids(path)`.
- `scripts/prepare_hotpotqa.py`: downloads `hotpot_qa/distractor/validation`, writes the first 100
  to `evals/datasets/hotpotqa_dev_100.jsonl`, and validates supporting doc_ids against
  `data/corpus.jsonl` if present (raises `SystemExit` on a gap).
- `tests/test_hotpotqa.py`: schema, raw shape, slicing/numbering, and integrity (consistent with
  `build_corpus` from the same example; gap detection). Suite now 58 tests. No new dependency.

**What's next**
1. Task 15 (Claude Code — load-bearing per CLAUDE.md): `helix/workflows/deep_research.py` — the
   reference agent (`decompose` → `gather(retrieve)` → `synthesize`) as `@helix.task`/
   `@helix.workflow`. Must populate `Answer.metadata["retrieved_doc_ids"]` (union of retrieved
   doc_ids across subqueries) or recall silently zeros. Uses `llm_call` (temp 0) + `HybridRetriever`.
2. Task 14b (holdout) is partially blocked: its access guard lives in `helix/eval/harness.py`
   (Task 16, not built yet). The independent parts (holdout extraction via `build_questions(start=100,
   count=500)`, the `.sha256` lock, `scripts/check_holdout_integrity.py`, and
   `.github/workflows/holdout-guard.yml`) can be done now, but the `HoldoutAccessError` raise +
   `make eval-final` Done-when need the harness + CLI. Recommend doing 14b alongside/after Task 16.

**Open questions / decisions pending**
- Holdout id numbering for 14b is unspecified (`hotpotqa_dev_101..600`? `hotpotqa_holdout_001..500`?).
  `build_questions` already supports `id_prefix`/`id_start` either way — pick when starting 14b.

**Gotchas hit**
- None new. Verified via `/tmp/helixvenv` (3.12): ruff clean (worker + both scripts), `mypy
  --strict helix/` clean (23 files), 58 pytest pass.

---

## 2026-06-10 22:13 UTC — Claude Code → next session

**Last commit:** `f9be5ef` on `claude/current-phase-gotchas-tjkwsu`
**Working tree:** clean
**Task plan position:** Task 13 (corpus prep) — DONE. Task 14 (HotpotQA question prep) next.

**What shipped this session** (ship-first; logic in package, thin script wrapper)
- `helix/eval/corpus.py`: pure corpus-prep logic — `wiki_doc_id(title)` (`wiki_` + sha1[:16] of
  the stripped title), `CorpusDoc`, `build_corpus(examples, limit=None)` (dedup by title, handles
  **both** HotpotQA context shapes — HF parallel `title`/`sentences` lists and raw `[title,
  [sentences]]` pairs), and `write_corpus` → JSONL.
- `scripts/prepare_corpus.py`: thin wrapper — lazily imports `datasets`, loads
  `hotpot_qa/distractor/validation`, builds the corpus from the first 500 questions, writes
  `data/corpus.jsonl`. (Couldn't run end-to-end here — no network/dataset — but the logic it calls
  is fully unit-tested.)
- `worker/pyproject.toml`: added `datasets>=2.0`. `docs/tech-stack.md`: datasets rationale.
- `tests/test_corpus.py`: id determinism/stripping, dedup-by-title across both shapes + a
  shared "Beta" title, limit, JSONL roundtrip. Suite now 53 tests.

**Key cross-task contract**
- **Task 14 must import `wiki_doc_id` from `helix.eval.corpus`** and apply it to each
  `supporting_facts` title, so `doc_id`s match the corpus. Do not re-implement the hash.

**What's next**
1. Task 14: `scripts/prepare_hotpotqa.py` (+ pure logic in `helix.eval.corpus` or a sibling) —
   extract 100 questions into `evals/datasets/hotpotqa_dev_100.jsonl` with the `data-model.md`
   schema (`id`, `input.question`, `expected_output.answer` + `supporting_facts[{doc_id, sent}]`,
   `metadata.hops/type`). Done-when includes referential-integrity validation against the corpus.

**Open questions / decisions pending**
- `scripts/` lives outside `worker/`, so the Makefile's `ruff`/`mypy` (which run in `worker/`)
  don't cover it. I ruff-checked the wrapper manually (clean). The real logic is in `helix` and is
  covered. If we want scripts linted in CI, the lint target needs to include the repo root.
- `print()` in the script is intentional (CLI seed feedback); the no-print rule targets library
  code in `helix`, not entry-point scripts.

**Gotchas hit**
- None new. `datasets` is imported lazily inside the script's `main()` so neither the tests nor
  `helix` import it. Verified via `/tmp/helixvenv` (3.12): ruff clean, `mypy --strict helix/`
  clean (22 files), 53 pytest pass.

---

## 2026-06-10 19:50 UTC — Claude Code → next session

**Last commit:** `d9c5438` on `claude/current-phase-gotchas-tjkwsu`
**Working tree:** clean
**Task plan position:** Task 12 (hybrid retriever + reranker) — DONE. Task 13 (corpus prep script) next.

**What shipped this session** (ship-first; composes Task 7/8/11 + new reranker)
- `helix/tools/reranker.py`: `Reranker` (BGE cross-encoder). `rerank(query, passages, top_k)` →
  `list[RankedPassage]` (`index, passage, score`). `CrossEncoder` lazily loaded; `predict_fn`
  injectable so tests use a stub.
- `helix/rag/retriever.py`: `HybridRetriever`. `retrieve(query, top_k=10, dense_k=50,
  sparse_k=50, fuse_k=50, rrf_k=60)` → embeds query, Qdrant dense + BM25 sparse, **RRF fusion**
  (combines by rank so the two score scales don't need calibrating), cross-encoder rerank, top-k
  `Doc`s. Each `Doc` carries `metadata["doc_id"]`/`["chunk_id"]` (recall scorer needs doc_id).
  One `kind="retrieval"` span: `query`, `retriever="hybrid+reranked"`, `top_k`, `results`.
- `tests/test_reranker.py` (order/top_k/empty) + `tests/test_retriever.py` (end-to-end against
  in-memory Qdrant: index → retrieve "apple" → the two apple docs rank top via the term-overlap
  stub reranker; span asserted). Suite now 49 tests.

**What's next**
1. Task 13: `scripts/prepare_corpus.py` — download + format a 1–2k doc subset into
   `data/corpus.jsonl` (`{id, text, source}`). First file under `scripts/`. Check the spec for
   the source corpus (likely a HotpotQA-derived or wiki subset) and the `doc_id` hashing scheme
   (Task 14 maps `supporting_facts` titles to these `doc_id`s with the same hash).

**Open questions / decisions pending**
- Retriever defaults `fuse_k=50` (fused candidates sent to the reranker) matching the spec's
  "fused top-50"; `dense_k=sparse_k=50`. Easy knobs if eval tuning wants different candidate
  counts.

**Gotchas hit**
- None new. The retriever test re-chunks the same docs (deterministic `chunk_document`) to build
  the BM25 index so its `chunk_id`s match what `index_corpus` put in Qdrant — that alignment is
  what makes RRF fuse the two sides correctly. Verified via `/tmp/helixvenv` (3.12): ruff clean,
  `mypy --strict helix/` clean (21 files), 49 pytest pass.

---

## 2026-06-10 19:38 UTC — Claude Code → next session

**Last commit:** `0561308` on `claude/current-phase-gotchas-tjkwsu`
**Working tree:** clean
**Task plan position:** Task 11 (BM25 sparse retrieval) — DONE. Task 12 (hybrid retriever + reranker) next.

**What shipped this session** (ship-first; pure-Python component)
- `helix/tools/bm25.py`: `BM25Index` over `rank_bm25.BM25Okapi`. `build(chunks)` classmethod
  (lowercase whitespace tokenization), `search(query, top_k)` → ranked `ScoredChunk`
  (`chunk_id, doc_id, text, source, score`), `save`/`load` pickle the
  `(chunks, tokenized, bm25)` tuple. Empty corpus → `_bm25 = None` → `search` returns `[]`
  (BM25Okapi divides by avg doc length, so it can't index an empty corpus).
- `worker/pyproject.toml`: added `rank_bm25` to the mypy override (untyped).
- `tests/test_bm25.py`: matching-term ranks first, score-descending order, top_k limit,
  save/load round-trip (same ids + scores), empty corpus. Suite now 46 tests.

**What's next**
1. Task 12: `helix/rag/retriever.py` — hybrid dense (Qdrant) + sparse (BM25) with RRF fusion,
   then BGE cross-encoder rerank. This is where `ScoredPoint` (dense) and `ScoredChunk` (sparse)
   get fused by `chunk_id` rank into `Doc`s. Check the spec for RRF k constant, candidate counts,
   and the reranker model/return shape. BGE reranker is a heavyweight dep — use the same
   lazy-import + injectable pattern so tests stay light.

**Open questions / decisions pending**
- `BM25Index.search` returns the full `top_k` even when some scores are 0 (no term overlap). The
  hybrid retriever fuses by rank, so this is intended; flag if zero-score filtering is wanted.
- No span emitted by BM25 (spec is silent). The hybrid retriever (Task 12) will own the
  `kind="retrieval"` span over the whole dense+sparse+rerank path.

**Gotchas hit**
- None new. `rank_bm25.get_scores` returns a numpy array; sort indices by score and `float()` the
  result. Verified via `/tmp/helixvenv` (3.12): ruff clean, `mypy --strict helix/` clean
  (19 files), 46 pytest pass.

---

## 2026-06-10 18:43 UTC — Claude Code → next session

**Last commit:** `05f5e17` on `claude/current-phase-gotchas-tjkwsu`
**Working tree:** clean
**Task plan position:** Task 10 (indexer) — DONE. Task 11 (BM25 sparse retrieval) next.

**What shipped this session** (ship-first; composes the Task 7–9 tools)
- `helix/rag/indexer.py`: `index_corpus(...)` → `IndexResult`. Loads JSONL `{id,text,source}`,
  chunks every doc, embeds all chunk texts, upserts `Point`s with `{doc_id, chunk_id, source,
  text}` payloads, and points the `corpus.active` alias at the collection. Chunk ids map to a
  deterministic `uuid5` for the Qdrant point id (re-index overwrites instead of duplicating);
  the readable `chunk_id` lives in the payload. Whole run wrapped in a `kind="internal"` span
  (docs/chunks/elapsed_s); embedder + adapter are injected so their child spans nest under it.
- `tests/test_indexer.py`: 100-doc corpus is searchable via the alias with the expected payload,
  span nesting (embed/upsert under index_corpus), and an empty-corpus case. Suite now 41 tests.

**What's next**
1. Task 11: `helix/tools/bm25.py` — sparse retrieval with `rank_bm25`. Pure-Python, ship-first.
   Check the spec for tokenization and whether it indexes chunks (shares the chunk corpus) or
   docs, and the score/return shape the hybrid retriever (Task 12) will expect.

**Open questions / decisions pending**
- `index_corpus` calls `create_collection` unconditionally, so re-indexing the *same* collection
  name raises (collection exists). Fine for the slice's fresh-index flow; a real re-index would
  need a recreate/delete step or a new collection name + alias flip. Flag if needed sooner.

**Gotchas hit**
- **Alias bootstrap ordering.** The adapter always writes through the alias (production
  invariant), but on a *fresh* index there is no alias yet — upserting before `set_alias` raised
  "Collection corpus.active not found". Fixed by creating the alias right after the collection,
  before upsert (so the alias-routed write resolves). Reordered vs the spec's step list; behavior
  is equivalent and correct.
- Verified via `/tmp/helixvenv` (3.12): ruff clean, `mypy --strict helix/` clean (18 files),
  41 pytest pass.

---

## 2026-06-10 18:36 UTC — Claude Code → next session

**Last commit:** `d05c0a2` on `claude/current-phase-gotchas-tjkwsu`
**Working tree:** clean
**Task plan position:** Task 9 (chunker) — DONE. Task 10 (indexer workflow) next.

**What shipped this session** (ship-first; pure-Python, no service)
- `helix/rag/chunker.py`: `chunk_document(...)` → `list[Chunk]`. Structural splitting:
  paragraphs (blank-line) → sentences (for oversized paragraphs) → word-window fallback (for a
  sentence still over budget). Greedy packing to `max_tokens` (512) with `overlap_tokens` (64)
  carried from each chunk's tail into the next (capped so overlap + next segment still fits).
  Deterministic `{doc_id}#chunk{N}` ids; `tiktoken` `cl100k_base` sizing via an injectable
  counter.
- `worker/pyproject.toml`: added `tiktoken>=0.7` dependency + mypy override.
- `docs/tech-stack.md`: added the tiktoken rationale (DoD item 7 — new dependency).
- `tests/test_chunker.py`: ~2000-word doc within budget + sequential ids, tiny/empty docs,
  oversized-paragraph→sentences, oversized-sentence→words, and overlap duplication
  (sum of chunk tokens == doc tokens at overlap 0, > doc tokens at overlap 40). Suite now 39.

**What's next**
1. Task 10: `helix/rag/indexer.py` — the indexer workflow that ties chunker → embedder →
   Qdrant upsert (embed_documents on chunk text, upsert Points with payload). Likely a
   `@helix.workflow`/`@helix.task` composition. Check the spec for payload shape (doc_id,
   chunk_id, text, source) and collection/vector-size config (768 from Nomic).

**Open questions / decisions pending**
- Chunks are joined with a single space (segments lose original blank-line breaks). Fine for
  retrieval; flag if faithful reconstruction of original formatting is ever needed.

**Gotchas hit**
- None new. `tiktoken` counter is injectable so tests use a word-count stub (no tokenizer
  download). Verified via `/tmp/helixvenv` (3.12): ruff clean, `mypy --strict helix/` clean
  (17 files), 39 pytest pass.

---

## 2026-06-10 18:30 UTC — Claude Code → next session

**Last commit:** `65c1bf8` on `claude/current-phase-gotchas-tjkwsu`
**Working tree:** clean
**Task plan position:** Task 8 (Qdrant adapter) — DONE. Task 9 (chunker) next.

**What shipped this session** (ship-first; self-contained tool adapter)
- `helix/tools/qdrant_adapter.py`: `QdrantAdapter` with async `create_collection`, `set_alias`,
  `upsert` (batched at 100, `kind="internal"` span), and `search` (`kind="retrieval"` span).
  `Point`/`ScoredPoint` dataclasses keep the surface independent of qdrant's types. All
  reads/writes target the **alias** (default `corpus.active`), never a concrete collection — the
  production alias-swap invariant, honored from day one. Client is lazily imported and injectable.
- `worker/pyproject.toml`: bumped `qdrant-client` floor `>=1.9` → `>=1.12`.
- `tests/test_qdrant_adapter.py`: real integration test against `AsyncQdrantClient(location=
  ":memory:")` — create/alias/upsert(100)/search, batch boundaries (250 @ 100), and the
  url-or-client guard. Suite now 33 tests.

**What's next**
1. Task 9: `helix/rag/chunker.py` — semantic + structural splitting. Pure-Python, no external
   service, so straightforward ship-first. Check the spec for chunk size/overlap and whether it
   needs the embedder (semantic splitting) or is purely structural for the slice.

**Open questions / decisions pending**
- Upsert span is `kind="internal"` (it's a write, not a retrieval); only `search` is
  `kind="retrieval"`. Consistent with the embedder using `internal` for index-side work. Flag if
  you'd rather every Qdrant op be `retrieval`.

**Gotchas hit**
- **qdrant-client 1.18 removed `AsyncQdrantClient.search`** — use `query_points(collection_name,
  query=vector, limit, query_filter)`, which returns a `QueryResponse` with `.points`. Hence the
  `>=1.12` floor bump.
- mypy: a qdrant point `id` is `int | str | UUID`; coerce the non-`int`/`str` case to `str` when
  building `ScoredPoint`.
- Qdrant **local in-memory mode supports aliases**, so the alias path is genuinely covered by the
  test (verified, not assumed). Verified via `/tmp/helixvenv` (3.12), qdrant-client 1.18:
  ruff clean, `mypy --strict helix/` clean (16 files), 33 pytest pass.

---

## 2026-06-10 16:23 UTC — Claude Code → next session

**Last commit:** `77eeda5` on `claude/current-phase-gotchas-tjkwsu`
**Working tree:** clean
**Task plan position:** Task 7 (embedding client) — DONE. Task 8 (Qdrant adapter) next.

**What shipped this session** (ship-first; self-contained tool adapter)
- `helix/tools/embedder.py`: `Embedder` over Nomic Embed v1.5 (sentence-transformers).
  `embed_queries`/`embed_documents` apply the `search_query: ` / `search_document: ` task
  prefixes (kept here and nowhere else — mixing them silently hurts recall). Internal batching
  (default 64), one `kind="internal"` span per call with `model`/`count`/`dimension`. The
  SentenceTransformer is lazily loaded (`trust_remote_code=True`) on first encode; the encode
  fn is injectable (`encode_fn=`) so tests use a stub and never download the model.
- `worker/pyproject.toml`: extended the mypy `ignore_missing_imports` override to
  `sentence_transformers`.
- `tests/test_embedder.py`: prefix correctness for both methods, batch-size boundaries
  (10 texts @ batch 4 → [4,4,2]), 768-dim output, internal-span attributes. Suite now 30 tests.

**What's next**
1. Task 8: `helix/tools/qdrant_adapter.py` — Qdrant client wrapper (upsert + search). This is
   the first adapter against a *real* external service (Qdrant in Docker via `make dev`). Decide
   how to unit-test without a live Qdrant: same injectable-client pattern, or qdrant's in-memory
   mode (`QdrantClient(":memory:")`). Check the spec for collection/vector config and whether it
   wants the `corpus.active` alias indirection (that's a production gotcha; confirm slice scope).

**Open questions / decisions pending**
- None blocking Task 8.

**Gotchas hit**
- None new. Verified via `/tmp/helixvenv` (3.12): ruff clean, `mypy --strict helix/` clean
  (15 files), 30 pytest pass. sentence-transformers is a pre-existing pinned dep (no tech-stack
  change); it's untyped, hence the mypy override.

---

## 2026-06-10 15:40 UTC — Claude Code → next session

**Last commit:** `be53d15` on `claude/current-phase-gotchas-tjkwsu`
**Working tree:** clean
**Task plan position:** Task 6 (LiteLLM tool adapter) — DONE. Task 7 (embedding client) next.

**What shipped this session** (ship-first; self-contained tool adapter)
- `helix/tools/llm_cache.py`: `LLMCache` (JSON files under `data/llm_cache/`, keyed by
  `sha256` of canonical `{model, messages, temperature, max_tokens, top_p, stop}` with `None`
  values dropped) + `cache_enabled()` (on unless `HELIX_ENV=production` or `HELIX_LLM_CACHE=0`).
- `helix/tools/litellm_adapter.py`: `llm_call(...)` async wrapper. Emits a `kind="llm"` span
  with `model`/`prompt_tokens`/`completion_tokens`/`cost_usd`/`cache_hit`/`replayed`. Cache hit
  → returns immediately with `cost_usd=0`, `cache_hit=replayed=True`, tokens replayed. Miss →
  LiteLLM, write cache. `temperature=0` default; model from `HELIX_DEFAULT_MODEL`
  (default `claude-sonnet-4-20250514`). LiteLLM + cost fns are lazily imported and injectable,
  so tests need neither the dep nor a network/API key.
- `worker/pyproject.toml`: mypy override `ignore_missing_imports` for `litellm`.
- `tests/test_litellm_adapter.py`: key stability/sensitivity, miss→hit (LiteLLM called once),
  `--no-cache` bypass, `temperature=0` default, env model resolution, span attributes. 26 tests.

**What's next**
1. Task 7: `helix/tools/embedder.py` — Nomic Embed v1.5 wrapper (sentence-transformers).
   Likely the first real heavyweight dep; consider the same lazy-import + injectable pattern so
   tests don't pull the model. Check the spec for batching/normalization specifics.

**Open questions / decisions pending**
- None blocking Task 7.

**Gotchas hit**
- mypy `--strict` + untyped third-party: added a `[[tool.mypy.overrides]]` block for `litellm`
  (`ignore_missing_imports`). Keep LiteLLM usage behind `Any` (cast the lazy handles) so the
  adapter never depends on its annotations.
- Verified via `/tmp/helixvenv` (3.12): ruff clean, `mypy --strict helix/` clean (14 files),
  26 pytest pass. `data/llm_cache/` is already gitignored via the `data/` rule.

---

## 2026-06-10 15:01 UTC — Claude Code → next session

**Last commit:** `34d6464` on `claude/current-phase-gotchas-tjkwsu`
**Working tree:** clean
**Task plan position:** Task 5 (asyncio engine) — DONE. Task 6 (LiteLLM tool adapter) next.

**What shipped this session** (plan-first; maintainer signed off in chat)
- `helix/runtime/context.py`: `current_engine` / `current_run` contextvars. Separate module so
  `decorators.py` reads them without importing `engine.py` (no import cycle).
- `helix/runtime/engine.py`: `Engine` runs a submitted workflow as a coroutine; each `@task`
  call dispatches through an `asyncio.Queue` and returns a `Future`. Run success/failure is
  driven by the workflow coroutine returning/raising (dynamic DAG — no static graph). Per-task
  lifecycle persisted (`ready`→`running`→`succeeded`/`failed`, `attempts` bumped per dispatch,
  insert-before-enqueue so the `running` update never races a missing row). One `kind="task"`
  span per dispatch under the run's `trace_id`. Retry budget 1. `asyncio.Semaphore` concurrency
  (default 4). Background `_enqueue`/handler tasks are tracked in sets so they aren't GC'd.
- `helix/decorators.py`: wired the single dispatch point — `current_engine.get()` is `None`
  (local → direct await) or the engine (submit → `engine.dispatch`).
- `helix/runtime/sqlite_store.py`: added `get_tasks(run_id)`.
- `docs/vertical-slice-plan.md`: corrected the span-kind contract cell from `workflow` to
  `task` (decision #4, approved).
- `tests/test_engine.py`: gated lifecycle (ready→running→succeeded asserted mid-flight),
  parallel branches get distinct `node_id`s, retry-then-fail (`attempts==2`, run `failed`),
  task spans, and local-vs-submit invariance. Suite now 21 tests; ran 3× for flakiness — stable.

**Decisions (approved by maintainer)**
- Single writer = the `SqliteStore`'s one aiosqlite connection (already serializes); no separate
  writer task.
- Task spans are `kind="task"`, not `kind="workflow"` as the spec table originally read; the
  doc was corrected to match.
- Non-JSON task inputs (e.g. `list[Doc]`) travel in-memory; the `tasks.input/output` columns get
  a best-effort JSON snapshot (`{"args": [...], "kwargs": {...}}`, dataclasses→asdict, else str).
- Slice `tasks` table has no error column, so a failed task is `status='failed'` only; the error
  rides the Future up to the `runs.error` row.

**What's next**
1. Task 6: `helix/tools/litellm_adapter.py` + `llm_cache.py` — LiteLLM call path with the
   disk-backed cache. Mind the gotchas: cache key = `model+messages+temperature+max_tokens`
   (+`top_p`,`stop`); cache hits emit `cache_hit=true`/`cost_usd=0`; `temperature=0` default;
   never bypass LiteLLM.

**Open questions / decisions pending**
- Nested `@task`-from-`@task` isn't wired (handlers run in the worker-loop context without
  `current_engine`/`current_run` set). Not needed for `deep_research`'s leaf tasks; revisit if
  a future workflow dispatches tasks from inside a task.

**Gotchas hit**
- ruff `ASYNC109`: an async helper with a `timeout` param trips it; renamed to `timeout_s`.
- Same 3.12 toolchain requirement (PEP 695). Verified via `/tmp/helixvenv`: ruff clean,
  `mypy --strict helix/` clean (12 files), 21 pytest pass.

---

## 2026-06-10 14:19 UTC — Claude Code → next session

**Last commit:** `722d69c` on `claude/current-phase-gotchas-tjkwsu`
**Working tree:** clean
**Task plan position:** Task 4 (SQLite state store) — DONE. Task 5 (asyncio engine) next.

**What shipped this session**
- `helix/runtime/sqlite_store.py`: `SqliteStore` with async CRUD over `runs`, `tasks`,
  `eval_results`, `datasets` (simplified slice schema; JSON in TEXT columns). Methods:
  `create_run`/`update_run`/`get_run`, `create_task`/`update_task`/`get_task`,
  `store_eval_result`/`get_eval_results`, `register_dataset` (idempotent on `(name,version)`).
  Partial updates leave `None` columns unchanged; `update_run` auto-sets `finished_at` on
  terminal status. Frozen `*Row` dataclasses for reads (named with a `Row` suffix to avoid
  colliding with the SDK `Task`). Single connection + WAL + `foreign_keys=ON`; the connection's
  background thread serializes writes (the single-writer gotcha — engine owns this in Task 5).
- `tests/test_sqlite_store.py`: run/task lifecycles, error + no-op updates, eval result
  grouping/details, dataset idempotency. Suite now 16 tests.
- Added `aiosqlite` to the 3.12 verify venv (it's already a pinned runtime dep in pyproject).

**What's next**
1. Task 5 (Claude Code — load-bearing per CLAUDE.md): the asyncio engine + `contextvars`
   mode detection. Wires `Task.__call__` dispatch (the single point left in `decorators.py`)
   to the engine in submit mode while local mode stays a direct call. Route ALL state writes
   through the store via a single writer task.

**Open questions / decisions pending**
- `update_run`/`update_task` treat `None` as "unchanged", so a column can't be nulled back
  out once set. Fine for the slice's forward-only state machine; revisit if the engine ever
  needs to clear `output`/`error`.

**Gotchas hit**
- mypy: `aiosqlite.connect` wants `str | Path`, not `PathLike`. Coerced the stored path with
  `os.fspath(...)` in `__init__`.
- Same 3.12 toolchain requirement (PEP 695). Verified via `/tmp/helixvenv`: ruff clean,
  `mypy --strict helix/` clean (10 files), 16 pytest pass.

---

## 2026-06-10 13:57 UTC — Claude Code → next session

**Last commit:** `590b856` on `claude/current-phase-gotchas-tjkwsu`
**Working tree:** clean
**Task plan position:** Task 3 (structured span logger) — DONE. Task 4 (SQLite state store) next.

**What shipped this session**
- `helix/logging.py`: `SpanLogger` appending JSONL to `data/spans.jsonl`; a `span(name,
  kind, attributes)` context manager that yields the mutable attributes dict and writes the
  record on exit; a `trace(trace_id=None)` binder. Span stack + per-run `trace_id` propagate
  via `contextvars`, so `parent_span_id` is correct across `await`/`gather` (each asyncio
  task copies the context). Record fields are exactly `trace_id, span_id, parent_span_id,
  name, kind, start_time, end_time, attributes` — matching the target ClickHouse `spans`
  schema (`docs/data-model.md`); times are ISO-8601 UTC strings (simplified vs DateTime64).
- `tests/test_logging.py`: schema shape, nested parent-child, shared/auto trace_id, and
  cross-`gather` propagation. Full suite now 11 tests.

**What's next**
1. Task 4: `helix/runtime/sqlite_store.py` — the SQLite state store (runs/tasks). Mind the
   slice gotcha: `aiosqlite` has no clean concurrent writers, so all writes route through a
   single engine writer task (that engine arrives in Task 5).

**Open questions / decisions pending**
- None blocking Task 4.

**Gotchas hit**
- Same 3.12 toolchain requirement as the prior entry (PEP 695). Verified via the
  `/tmp/helixvenv` 3.12 venv: ruff clean, `mypy --strict helix/` clean (9 files), 11 pytest
  pass. Default sandbox `python3`/`mypy` are 3.11 and will choke on the syntax.
- ruff `UP017` under py312 wants `datetime.UTC`, not `timezone.utc`.

---

## 2026-06-10 13:45 UTC — Claude Code → next session

**Last commit:** `9699e4d` (Task 2 code) on `claude/current-phase-gotchas-tjkwsu`
**Working tree:** clean
**Task plan position:** Task 2 (SDK decorators, local execution) — DONE. Task 3 (span logger) next.

**What shipped this session**
- `helix/types.py`: `Doc`, `Citation`, `Answer` dataclasses (spec-exact fields/defaults).
- `helix/decorators.py`: `@task(retries=, timeout=)`, `@workflow(name=, version=)` with
  `.local(**kwargs)`, and `gather = asyncio.gather`. Tasks/workflows are thin generic
  wrappers that call through directly in local mode; retry/timeout are parsed into metadata
  but inert. `Task.__call__` is the single dispatch point Task 5's engine will hook.
- `helix/__init__.py`: public exports (`Doc`, `Citation`, `Answer`, `Task`, `Workflow`,
  `task`, `workflow`, `gather`).
- `tests/test_decorators.py`: toy two-task workflow via `.local()`, gather ordering, metadata,
  timeout parsing, type defaults. 7 tests pass.

**What's next**
1. Task 3: `helix/logging.py` — `SpanLogger` writing JSONL to `data/spans.jsonl`, with a
   `span(name, kind, attributes)` context manager and a contextvar span stack for
   `parent_span_id`. Schema fields are fixed (`trace_id`, `span_id`, `parent_span_id`,
   `name`, `kind`, `start_time`, `end_time`, `attributes`) — do not rename.

**Open questions / decisions pending**
- None blocking Task 3.

**Gotchas hit**
- **Toolchain must be Python 3.12+.** The code uses PEP 695 generics (`class Task[**P, R]`),
  which is a `SyntaxError` on 3.11. This sandbox's default `python3`/`mypy`/`ruff` are tied
  to **3.11**, so `make lint`/`make test` fail there with a misleading "Invalid syntax" from
  mypy. Verified instead via a 3.12 venv (`python3.12 -m venv`): ruff clean, `mypy --strict
  helix/` clean (8 files), 7 pytest pass. The Makefile is correct for the maintainer's 3.12
  env and was left unchanged. `python3.12`/`python3.13` are both present at `/usr/bin`.
- ParamSpec lives in the PEP 695 form (`[**P, R]`); ruff's UP046 rejects the old
  `Generic[P, R]` subclass form under `target-version = py312`.

---

## 2026-06-10 13:35 UTC — Claude Code → next session

**Last commit:** `34228f5` on `claude/current-phase-gotchas-tjkwsu`
**Working tree:** clean
**Task plan position:** Task 1 (scaffold) — DONE. Task 2 (SDK decorators) next.

**What shipped this session**
- Task 1 scaffold: root `Makefile` (faithful to the slice-plan spec), `worker/pyproject.toml`
  (pinned runtime deps + `dev` extras, with ruff/mypy/pytest config), the `helix/` package
  tree with empty `__init__.py` files (`runtime`, `tools`, `rag`, `workflows`, `eval`),
  `.gitignore` for `data/`/`*.db`, and a `tests/test_smoke.py` import check.
- Verified `make lint` (ruff clean, `mypy --strict helix/` clean on 6 files) and `make test`
  (1 passed) both green, invoked exactly as the Makefile defines them.

**What's next**
1. Task 2: `@helix.workflow`, `@helix.task`, `helix.gather` for local execution only, plus
   `types.py` (`Doc`, `Answer`, `Citation`) and the public exports in `helix/__init__.py`.

**Open questions / decisions pending**
- None blocking Task 2.

**Gotchas hit**
- Local interpreter is **Python 3.11.15**, but `pyproject` pins `requires-python >=3.12`
  per the spec. ruff/mypy/pytest all run fine under 3.11, so lint/test were verified, but a
  full `pip install -e '.[dev]'` (which pulls litellm/sentence-transformers/qdrant-client)
  was **not** run here — those heavy deps + the version pin make it a CI/3.12 concern, not a
  scaffold blocker. `import helix` itself needs none of them.
- `pytest` was not preinstalled; installed `pytest`/`pytest-asyncio` ad hoc to run the suite.
  `make test` relies on pytest's rootdir insertion for `import helix` (no `PYTHONPATH` set) —
  confirmed working.

---

## 2026-06-10 — Cowork (Claude) → next session

**Last commit:** operating-docs commit on `master` (run `git log -1 --oneline`; parent is `7dd58e8`)
**Working tree:** clean after this commit
**Task plan position:** Bootstrap finishing. Task 1 (scaffold) NOT started.

**What shipped this session**
- Added `.gitattributes` (`* text=auto eol=lf`) and cleared the phantom CRLF drift that had made README/CHANGELOG/`vertical-slice-plan.md` show as fully rewritten. The committed state was always LF; an editor was re-saving working copies as CRLF.
- Replaced the monolithic `CLAUDE.md` with a thin stub that imports `AGENTS.md`. Added a symmetric `CODEX.md`. `AGENTS.md` (the real manual, phase-aware) was already correct and is now committed.
- Reset this handoff log. The prior entries were placeholder/future-dated (06-11, 06-12) and described Tasks 7–8 as done against a commit `a3f81c2` that does not exist. Real HEAD before this session was `7dd58e8`.
- Verified the three pending edits to `docs/vertical-slice-plan.md` are already applied: LLM cache key includes `max_tokens`/`top_p`/`stop`; cache-hit spans set `cost_usd=0` + `replayed=true`; risks table notes near-zero rerun cost.

**What's next**
1. Task 1 (assigned to Claude Code): `Makefile` + `worker/pyproject.toml` + empty `worker/helix/` package; pass `make lint` on the empty package.
2. Verify the `@AGENTS.md` import: in a fresh Claude Code session, ask "what phase are we in and what gotchas apply today?" Expect "slice phase" plus the slice-phase gotchas (engine contextvars detection, LLM cache key, holdout unlock, `retrieved_doc_ids`, `temperature=0`, SQLite single-writer). A vague answer means the import is not resolving.

**Open questions / decisions pending**
- None blocking Task 1.

**Gotchas hit**
- The repo lives on a Windows filesystem. When operating on it from a Linux shell, the mount permits create and rename but **not unlink** — `git checkout`/index writes that delete-and-replace fail and can corrupt `.git/index`. Recovered by routing git through an index on a native fs and renaming a clean index back into place. If `.git/index` ever reports "corrupt," that is the cause. Prefer running git from Windows-side tooling.
- An editor is rewriting tracked files to CRLF on save. `.gitattributes` now pins LF; if CRLF diffs reappear, check the editor's line-ending setting.
