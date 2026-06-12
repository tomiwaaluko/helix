# Issues Log

> A running record of non-trivial problems hit while building Helix and how they were
> resolved. The point is to save the next agent (and the maintainer) from re-debugging
> something we already understand.
>
> **Maintenance:** Every agent must append an entry here whenever it hits an issue worth
> remembering — an environment quirk, a third-party bug, a silent failure, a design trap.
> See the Session protocol in `AGENTS.md`. Latest entries at the top of each section.

## Entry template

```
### <short title>

- **Date / session:** YYYY-MM-DD, <tool>
- **Symptom:** what was observed (error text, wrong number, silent no-op)
- **Root cause:** what was actually wrong
- **Fix:** what changed (and where — file/commit)
- **Guard:** test / lint / CI that keeps it fixed (if any)
- **Watch for:** how to recognize a recurrence
```

---

## Environment & tooling

### `sqlite3` CLI is not installed

- **Date / session:** 2026-06-12, Claude Code
- **Symptom:** Shell `sqlite3 data/...db "SELECT ..."` returned nothing; under `2>/dev/null`
  the failure was invisible, so the DB looked empty when it was not.
- **Root cause:** No `sqlite3` binary in this remote execution environment (exit code 127).
- **Fix:** Inspect SQLite via the Python `sqlite3` module instead:
  `python -c "import sqlite3; print(sqlite3.connect('data/smoke/helix.db').execute('SELECT ...').fetchall())"`.
- **Guard:** none (environment fact). Don't pipe `sqlite3` through `2>/dev/null`.
- **Watch for:** "empty" query results that should have rows — check for exit 127 first.

### tiktoken `cl100k_base` unreachable (403)

- **Date / session:** earlier M0, Claude Code
- **Symptom:** `make index` failed; `403 Client Error: Forbidden` fetching
  `cl100k_base.tiktoken` from the OpenAI blob store.
- **Root cause:** Network-restricted environment can't reach the tokenizer blob store.
- **Fix:** `_tiktoken_counter` in `helix/rag/chunker.py` degrades to a deterministic
  char-based heuristic (emitting a `RuntimeWarning`) when the tokenizer can't be fetched.
- **Guard:** chunker unit tests pass with the heuristic counter (injectable token counter).
- **Watch for:** the `RuntimeWarning: tiktoken cl100k_base unavailable` line in test output —
  benign, expected in this environment.

### HotpotQA dataset moved namespace / format

- **Date / session:** earlier M0, Claude Code
- **Symptom:** `scripts/prepare_corpus.py` / `prepare_hotpotqa.py` failed to resolve the
  bare `hotpot_qa` dataset id on the Hub.
- **Root cause:** Dataset moved to the `hotpotqa/hotpot_qa` namespace and to Parquet.
- **Fix:** Updated the dataset id; the pure parsers already handled the Parquet
  `context` / `supporting_facts` shapes.
- **Guard:** referential-integrity check (`missing_doc_ids`) over dev/holdout/train splits.

### Missing transitive dependencies (`einops`, `accelerate`)

- **Date / session:** `einops` earlier M0; `accelerate` 2026-06-12, Claude Code
- **Symptom:** Clean checkout failed at runtime — `einops` at first `embed_documents`;
  `accelerate` (`ImportError: requires accelerate>=1.1.0`) at the `train_embedding` step.
- **Root cause:** Both are transitive requirements of the Nomic remote modeling code /
  `sentence-transformers` `.fit()` (which delegates to `transformers.Trainer`), but neither
  was declared in `worker/pyproject.toml`.
- **Fix:** Pinned both explicitly in `dependencies`. `accelerate>=1.1.0` is required even for
  single-device CPU training. `docs/tech-stack.md` updated to explain the `accelerate` pin.
- **Guard:** declared deps; surfaced only by running the real (non-stubbed) training path.
- **Watch for:** any runtime `ImportError` from a library that the unit-test stubs bypass —
  the stubbed backends hide real transitive deps until a real run exercises them.

---

## Third-party bugs / traps

### Nomic checkpoint save/reload silently drops fine-tuned weights

- **Date / session:** 2026-06-12, Claude Code
- **Symptom:** After fine-tuning, the promotion canary showed no change. Reload logged
  `_IncompatibleKeys(missing_keys=[encoder.layers.*], unexpected_keys=[encoder.encoder.layers.*])`.
- **Root cause:** The Nomic Embed v1.5 remote modeling code's `save_pretrained` writes the
  transformer weights under a **doubled** `encoder.encoder.layers.*` prefix, while reload
  expects `encoder.layers.*`. `load_state_dict(strict=False)` therefore drops all 108
  fine-tuned transformer tensors and the candidate falls back to base weights — a silent
  no-op fine-tune. Reproduced even on an *untrained* base model: save→reload perturbed
  embeddings by max-abs 3.29.
- **Fix:** `_normalize_nomic_checkpoint()` in `helix/rag/trainer/train.py` rewrites the saved
  `*.safetensors` after `model.save()`, de-doubling `encoder.encoder.` → `encoder.`. Idempotent
  and self-limiting (leaves canonical checkpoints untouched, so a future fixed library is safe).
  Post-fix, save→reload embeddings match the in-memory model exactly (max-abs 0.0).
- **Guard:** `test_normalize_nomic_checkpoint_dedoubles_prefix` and
  `test_normalize_nomic_checkpoint_is_noop_when_already_canonical` in `tests/test_trainer.py`.
- **Watch for:** any `_IncompatibleKeys` warning mentioning `encoder.encoder.*` during model
  load — means a checkpoint slipped through without normalization. Do not remove the helper
  unless the upstream `save_pretrained` is fixed.

---

## Pipeline / design traps

### Training OOM-killed — MultipleNegativesRankingLoss memory scales with batch_size

- **Date / session:** 2026-06-12, Claude Code
- **Symptom:** A real `finetune` run died with no Python traceback during the training phase
  (last log line `Computing widget examples`). `dmesg`: `Out of memory: Killed process
  (python) anon-rss:15953472kB` — ~15.2 GB, the full container budget. The candidate
  checkpoint dir was created but empty; the job row stayed stuck at `status=training`. RSS
  held steady at ~3.2 GB through all of mining, then spiked to 15 GB during the fit.
- **Root cause:** `MultipleNegativesRankingLoss` retains backprop activations for *every* text
  in each batch element — anchor + positive + N hard negatives (6 texts at
  `negatives_per_query=4`). Peak memory ≈ `batch_size * texts * seq_len * hidden * layers`.
  At the default `batch_size=16` that is ~16 GB and OOMs. **Not the model card** — disabling
  `create_model_card` (first attempt) did not help; the fit itself was the driver. The
  `Computing widget examples` line was just the last thing logged before the kill, not the
  cause. Confirmed by an isolated repro (no re-mining): batch 16 → OOM (15.9 GB), batch 4 →
  fit OK at **7.28 GB** peak. Nomic's `max_seq_length` defaults to 8192, but indexed passages
  are short (max ~317 tokens), so sequence length was not the lever here — batch size was.
- **Fix:** (`helix/cli.py`, `helix/rag/trainer/train.py`)
  1. `finetune` CLI `--batch-size` default 16 → **4** (peaks ~7 GB on the 15 GB box; raise on
     a GPU host).
  2. `_default_train_fn` caps `model.max_seq_length` to 512 (the corpus chunk size) — a no-op
     for in-distribution passages but bounds the worst case if a future corpus has long docs.
  3. Kept `create_model_card=False` (trims unused save work; corrected its comment so it no
     longer claims to be the OOM fix).
- **Guard:** none direct (the stub train backend used in unit tests bypasses
  `_default_train_fn` and never loads the real model). Covered operationally: the run monitor
  tracks process `VmRSS`; the isolated memory repro is the reference if defaults change.
- **Watch for:** a training-phase death with no traceback near `Computing widget examples`, or
  `status` stuck at `training` with an empty checkpoint dir → check `dmesg` for an OOM kill
  before assuming a code bug. Raising `--batch-size` or `negatives_per_query` re-inflates peak
  memory linearly. Re-mining is avoidable: mined `failure_cases` persist in SQLite and the LLM
  cache is warm, so a re-run's mining phase is cache-dominated and fast.


### Mining eval was all-or-nothing under provider errors

- **Date / session:** 2026-06-12, Claude Code
- **Symptom:** A single Gemini `503` ("model is currently experiencing high demand") during the
  mining run aborted the entire `finetune` job after exhausting retries.
- **Root cause:** `evaluate()`'s `asyncio.gather` re-raises on any per-example exception. Correct
  for a deterministic baseline `eval`, wrong for mining (partial results are better than none).
- **Fix:** Added `tolerate_failures: bool = False` to `evaluate()` (`helix/eval/harness.py`).
  When `True`, exhausted examples are skipped and counted in `EvalReport.examples_skipped`;
  `_finetune()` passes `True` for the mining eval only. Baseline `eval` stays all-or-nothing.
- **Guard:** harness tests (partial skip, all-fail, default-raises) + a CLI test asserting the
  "examples skipped" line. Commit `633f24a`.
- **Watch for:** if a future caller wants partial results, pass `tolerate_failures=True` rather
  than catching exceptions around `evaluate()`.

### Promotion canary decided on dense-only recall

- **Date / session:** 2026-06-11, Claude Code
- **Symptom:** A candidate could be promoted on a dense-recall gain that the reranker then
  washes out, so the promote/archive decision didn't reflect the end-to-end metric.
- **Root cause:** The Phase 3 gate scored dense retrieval in isolation, not the full pipeline.
- **Fix:** `_build_promotion_backends` (`helix/cli.py`) builds two `HybridRetriever`s sharing one
  BM25 index + reranker, differing only in the dense embedder + target collection. Added
  `QdrantAdapter.for_collection()` and `_build_candidate_embedder`. Commit `de5e697`.
- **Guard:** `test_promotion_hybrid_retrieve_fn_routes_and_dedupes` drives the real hybrid
  retrieve_fn over embedded Qdrant + BM25.

### Smoke corpus saturates recall — no measurable lift

- **Date / session:** 2026-06-12, Claude Code
- **Symptom:** The 419-doc smoke corpus mined zero failures at default top-k (perfect recall),
  and at top-k=3 the 7-question dev set still showed recall 1.0→1.0 — no headroom to measure
  a promotion lift.
- **Root cause:** A tiny corpus + tiny eval set saturate retrieval; there is nothing to mine
  and no room for a delta. This is a test-data limitation, not a bug.
- **Fix / next step:** Real measurement requires the full 15512-doc corpus (`make seed`) and a
  train split, with a top-k that leaves recall headroom. On the full corpus the dev_100 baseline
  is `retrieval_recall@10` = 0.69 — ample headroom (see the throughput note below for split size).
- **Watch for:** any "no_failures" / flat-delta result on a small corpus — it's expected; don't
  chase it as a code bug.

### Mining throughput is bound by the deep_research thinking-model latency

- **Date / session:** 2026-06-12, Claude Code
- **Symptom:** A full-corpus `finetune` over the **1000-question** train split projected to
  **~28 hours**. Measured rate: one batch of 4 questions (concurrency=4) took ~6.5 min
  (~1.7 min/question amortized) on `gemini/gemini-2.5-flash`.
- **Root cause:** Mining evaluates every train question through the *full* `deep_research`
  workflow (multi-hop: each hop is an LLM call). `gemini-2.5-flash` is a reasoning/"thinking"
  model, so each call spends 20–60s on reasoning tokens. The cost is intrinsic to the mining
  design: `mine_failures` only consumes the **retrieval** spans (which sub-queries surfaced which
  docs), but those sub-queries are themselves LLM-generated by deep_research, so you cannot skip
  the workflow without changing which (realistic, multi-hop) failures get mined.
- **Fix / decision:** Mining does **not** need all 1000 questions — at recall@10 = 0.69, ~31% fail,
  so ~150 questions already yields ~46 failures → plenty of contrastive triplets. The promotion
  canary still measures on the **full dev_100**, so the headline recall@10 delta is unaffected by
  the train-split size. Ran on a 150-question split (`hotpotqa_train_150.jsonl`, ~4h). Larger
  splits are strictly a "more triplets" lever, not a correctness one.
- **Watch for:** before launching a full `finetune`, estimate `train_size/concurrency × ~6.7 min`.
  If you need the full 1000, either raise `--concurrency` (watch Gemini rate limits) or accept the
  multi-hour wall time. Do not mistake slow-but-progressing mining for a hang — check that
  `eval_results` rows for `finetune-mining-<job_id>` are still climbing.

### Holdout dataset access is guarded — don't read it directly

- **Date / session:** standing constraint
- **Symptom:** N/A (preventive).
- **Root cause:** `evals/datasets/hotpotqa_dev_holdout_500.jsonl` is the sequestered final-measure
  set; reading it elsewhere leaks it into development.
- **Fix / rule:** `load_dataset` raises `HoldoutAccessError` unless `HELIX_HOLDOUT_UNLOCK=1`
  (only `make eval-final` sets it). The filename literal may appear only in
  `helix/eval/harness.py` and `scripts/prepare_hotpotqa.py`. A CI workflow
  (`holdout-guard.yml`) greps for unauthorized references.
- **Watch for:** any new file referencing the holdout filename — CI will reject it.
