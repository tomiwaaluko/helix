# Helix — Session Handoff / New-Session Context

Paste this into a fresh session so it has full context. It covers what was done,
the current state, the one remaining blocker, and the exact steps to finish.

---

## 1. What this project is, right now

Helix is a distributed agent runtime with a self-improving RAG loop. We are in the
**M0 vertical-slice phase** (single Python process: SQLite + asyncio + JSONL spans +
real Qdrant + LiteLLM). Authoritative scope: `docs/vertical-slice-plan.md`. Operating
manual: `AGENTS.md` (read it first).

**All 18 slice implementation tasks are code-complete.** The only thing left is the
**headline deliverable: real baseline eval numbers** (`answer_f1`,
`citation_precision`, `retrieval_recall@10` over 100 HotpotQA dev questions), written
to `evals/baselines/hotpotqa_dev_100_baseline.json`.

## 2. Branch and remote state

- **Branch (work only here):** `claude/current-phase-gotchas-tjkwsu`
- **Remote HEAD:** `00d44f4` — `feat(cli): embedded Qdrant mode via --qdrant-path`
- **NEVER push to another branch. Do NOT open a PR unless explicitly asked.**

Recent commits (all pushed):
```
00d44f4 feat(cli): embedded Qdrant mode via --qdrant-path (no server/Docker)
2ed865d fix(scripts): build corpus over first 600 questions to cover holdout
87c27b6 fix(scripts): load HotpotQA from hotpotqa/hotpot_qa namespace
2ac9e22 docs: record Task 18 completion in HANDOFF
cd96f54 feat(cli): index/eval/run end-to-end CLI + integration test (Task 18)
```

**⚠️ The eval datasets are NOT on the remote.** A parallel session generated
`evals/datasets/hotpotqa_dev_100.jsonl`, `hotpotqa_dev_holdout_500.jsonl`, and
`hotpotqa_dev_holdout_500.sha256` but **did not push them** — they exist only in that
container. They are fully regenerable (the prep scripts are deterministic), so just
re-run the two prep scripts below rather than trying to recover them.

## 3. What this session accomplished

1. **Task 18 — the CLI (`worker/helix/cli.py`)**: `index`, `eval`, `run` behind
   `python -m helix.cli`, plus the end-to-end integration test (`worker/tests/test_cli.py`).
   This makes `make eval` real. Heavy components (embedder, Qdrant adapter, reranker,
   LLM, token counter) come from monkeypatchable module-level factories.
2. **Fixed two real data-prep bugs** found while trying to run for real:
   - HotpotQA moved on HuggingFace: `load_dataset("hotpot_qa", ...)` →
     `load_dataset("hotpotqa/hotpot_qa", ...)` (now Parquet; the parsers already handle
     both shapes).
   - Corpus under-coverage: `prepare_corpus.py` now indexes the first **600** questions
     (was 500) so it covers the union of dev (0–100) + holdout (100–600); otherwise the
     holdout failed referential integrity (197 supporting doc_ids missing).
3. **Added embedded Qdrant mode**: `--qdrant-path <dir>` on `index`/`eval`/`run` runs
   Qdrant on-disk in-process (`AsyncQdrantClient(path=...)`) — **no Qdrant server, no
   Docker daemon required**. `--qdrant-url` still connects to a server when no path is
   given. The CLI test exercises the real embedded branch.
4. Verified throughout: `ruff check .` clean, `mypy --strict helix/` clean (27 files),
   **84 tests pass**.

## 4. The one remaining blocker: HuggingFace network access

The pipeline downloads from HuggingFace in two places: the **HotpotQA dataset** (data
prep) and the **embedding/reranker models** (Nomic Embed v1.5 + BGE reranker, ~0.5 GB,
on first `index`). For a session to run end-to-end it needs:

- **HuggingFace allowlisted** in the environment's Network access. In the environment
  edit dialog: **Network access → Custom**, add (one per line) and keep the default
  package-manager list checked:
  ```
  huggingface.co
  *.huggingface.co
  *.hf.co
  ```
  (Or use **Full**.) Note: `*.googleapis.com` is already in the default Trusted list,
  so the **Gemini API works without any change** — only HuggingFace needs adding.
- **`GEMINI_API_KEY`** set as an environment variable (`.env` format, no quotes).
  Optionally also `HELIX_DEFAULT_MODEL=gemini/gemini-2.5-flash`.

Network/env edits only apply to **new** sessions started after saving. Verify at the
start of the run with the probe in §6.

## 5. Environment / model decisions already made

- **LLM: Gemini via LiteLLM.** Model: `gemini/gemini-2.5-flash` (good balance for a
  100-question / ~200-call baseline; `gemini/gemini-2.5-pro` if you want higher quality).
  Set via `HELIX_DEFAULT_MODEL` or `--model gemini/gemini-2.5-flash`. LiteLLM reads
  `GEMINI_API_KEY`. The LLM cache key includes the model name, so switching models
  re-pays for calls (expected).
- **Qdrant: use embedded mode** (`--qdrant-path data/qdrant`) since the cloud container
  has no Docker daemon. No `make dev` needed.
- **Python 3.12** required (PEP 695 generics; deps lack 3.14 wheels).

## 6. Exact steps to finish (run in the configured session)

```bash
# 0. Probe first — confirm the env is actually configured (don't start the long run blind)
printenv GEMINI_API_KEY >/dev/null && echo "key set" || echo "KEY MISSING"
curl -sS -o /dev/null -w "HF -> %{http_code}\n" https://huggingface.co/api/datasets/hotpotqa/hotpot_qa
# want: "key set" and an HF code that is NOT 403 (200/401 ok). 403 = HF still blocked.

# 1. Install deps (skip if a setup script already did it)
cd /home/user/helix/worker && pip install -e ".[dev]" && cd /home/user/helix

# 2. Env
export HELIX_DEFAULT_MODEL=gemini/gemini-2.5-flash   # GEMINI_API_KEY comes from env config

# 3. Data prep (regenerates corpus + datasets; deterministic)
python scripts/prepare_corpus.py        # expect ~5911 docs from the first 600 questions
python scripts/prepare_hotpotqa.py      # expect "Referential integrity OK" for BOTH dev and holdout

# 4. Index (first run downloads Nomic + BGE from HF, ~0.5 GB — slow once; embedded Qdrant)
python -m helix.cli index --corpus data/corpus.jsonl --collection corpus.base --qdrant-path data/qdrant

# 5. Eval (the deliverable; ~200 Gemini calls, cached after)
python -m helix.cli eval \
  --workflow deep_research \
  --dataset evals/datasets/hotpotqa_dev_100.jsonl \
  --scorers answer_f1,citation_precision,retrieval_recall@10 \
  --concurrency 4 \
  --qdrant-path data/qdrant \
  --no-cache \
  --output evals/baselines/hotpotqa_dev_100_baseline.json
```

If Gemini returns 429 (rate limit), lower `--concurrency` to 1.

## 7. Verify, then commit

- Open `evals/baselines/hotpotqa_dev_100_baseline.json`. Confirm: `n` = 100, `model` =
  `gemini/gemini-2.5-flash`, and a `metrics` block with all three scorers, each having
  `mean` / `ci_low` / `ci_high`.
- **Sanity check `retrieval_recall@10` is NOT 0.0.** A zero means the corpus doc_ids
  and the dataset's `supporting_facts` doc_ids aren't lining up (or
  `Answer.metadata["retrieved_doc_ids"]` isn't populated) — investigate before trusting
  the run. Non-zero confirms the corpus↔supporting-fact wiring end to end.
- Run the gates: `python scripts/check_holdout_integrity.py` (from repo root, with the
  package importable — `PYTHONPATH=worker` if not installed), then
  `cd worker && ruff check . && mypy --strict helix/ && python -m pytest tests/ -q`.
- Commit the eval datasets + the baseline JSON (and update `HANDOFF.md` per the Session
  protocol in `AGENTS.md`). Push to `claude/current-phase-gotchas-tjkwsu`. Do NOT open a PR.

## 8. Hard rules / gotchas (do not violate)

- **Holdout is sequestered.** Never read, print, or open
  `evals/datasets/hotpotqa_dev_holdout_500.jsonl`. Only `make eval-final` may, and it
  needs `HELIX_HOLDOUT_UNLOCK=1`. Do NOT run `make eval-final`. The filename literal may
  only appear in `helix/eval/harness.py` and `scripts/prepare_hotpotqa.py`.
- **Never bypass LiteLLM** for model calls (breaks cache/metering/replay).
- **Never commit** secrets, dataset blobs > 1 MB, or model weights. `data/` is
  gitignored and holds the corpus, the embedded Qdrant store, the BM25 sidecar, the LLM
  cache, and spans.
- **Don't sum `cost_usd` across runs without filtering `cache_hit=false`** — cache hits
  report `cost_usd=0`.
- **`temperature=0` is the default** and eval determinism depends on it — don't override.
- **One driver session only.** Two sessions committing to this branch will diverge.
  Pick the configured session as the single driver; the other should stop committing.
- **Embedded Qdrant local mode** persists to the `--qdrant-path` dir on disk and is
  released on close, so `index` (one process) then `eval` (another process) works
  sequentially against the same path. Don't open two clients on the same path at once.

## 9. Decision: which session to use

Use the session whose environment has **both** HuggingFace allowlisted **and**
`GEMINI_API_KEY` set (verify with the §6 probe). The data is regenerable, so a fresh
session is fine — just run §6 from the top. Abandon any session that has HF blocked
(it can't download the models).
