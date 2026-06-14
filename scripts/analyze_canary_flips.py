"""Per-question flip analysis + paired significance test for a promotion canary.

The promotion canary (`promote_candidate`) reports only bootstrapped CIs for the
*before* and *after* means — it does not persist per-question scores, and it tests
each arm independently rather than the paired delta. That makes a single positive
run hard to trust: a +0.09 mean lift could be a handful of lucky questions.

This script re-runs the **identical** hybrid retrieval the canary used (it mirrors
`helix.cli._build_promotion_backends`) over the canary split, scores both arms
per question, and reports:

  * the flip table (Miss→Hit, Hit→Miss, partial moves, unchanged),
  * a **paired** bootstrap 95% CI on the mean per-question delta, and
  * an exact sign test over the questions that moved.

It loads no LLM — retrieval only — so it is cheap and deterministic.

Usage:
    python3.12 scripts/analyze_canary_flips.py \
      --base-collection corpus.bright \
      --candidate-collection corpus.candidate.<job_id> \
      --checkpoint worker/data/models/<job_id> \
      --canary evals/datasets/bright_biology_canary.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "worker"))

QDRANT_PATH = ROOT / "data" / "qdrant"


def load_examples(path: Path) -> list[dict]:
    if not path.exists():
        sys.exit(f"ERROR: {path} not found.")
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def gold_ids(ex: dict) -> set[str]:
    return {f["doc_id"] for f in ex["expected_output"]["supporting_facts"]}


def recall_at_k(retrieved: list[str], gold: set[str], k: int) -> float:
    if not gold:
        return 0.0
    return len(gold & set(retrieved[:k])) / len(gold)


def paired_bootstrap_ci(
    deltas: list[float], *, samples: int, seed: int, alpha: float = 0.05
) -> dict[str, float]:
    """Bootstrap the mean of the *paired* per-question deltas (resample questions)."""
    n = len(deltas)
    mean = statistics.fmean(deltas)
    if n < 2:
        return {"mean": mean, "ci_low": mean, "ci_high": mean, "p_gt_0": 1.0, "n": n}
    rng = random.Random(seed)
    boot = sorted(
        sum(deltas[rng.randrange(n)] for _ in range(n)) / n for _ in range(samples)
    )
    ci_low = boot[int((alpha / 2) * samples)]
    ci_high = boot[min(int((1 - alpha / 2) * samples), samples - 1)]
    # Fraction of bootstrap resamples with mean delta <= 0 (one-sided bootstrap p-value).
    p_le_0 = sum(1 for b in boot if b <= 0.0) / samples
    return {
        "mean": mean,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "p_le_0": p_le_0,
        "n": n,
    }


def sign_test_p(improved: int, regressed: int) -> float:
    """Two-sided exact binomial sign test p-value over questions that moved."""
    n = improved + regressed
    if n == 0:
        return 1.0
    k = min(improved, regressed)

    def comb(a: int, b: int) -> int:
        from math import comb as _c

        return _c(a, b)

    tail = sum(comb(n, i) for i in range(0, k + 1)) / (2**n)
    return min(1.0, 2 * tail)


async def retrieve_scores(
    *,
    collection: str,
    model_name: str | None,
    examples: list[dict],
    bm25_path: str,
    top_k: int,
    label: str,
) -> list[float]:
    from helix.logging import SpanLogger
    from helix.rag.retriever import HybridRetriever
    from helix.tools.bm25 import BM25Index
    from helix.tools.embedder import Embedder
    from helix.tools.qdrant_adapter import QdrantAdapter
    from helix.tools.reranker import Reranker
    from qdrant_client import AsyncQdrantClient  # type: ignore[import-untyped]

    bm25 = BM25Index.load(bm25_path)
    reranker = Reranker()
    logger = SpanLogger()
    client = AsyncQdrantClient(path=str(QDRANT_PATH))
    embedder = Embedder(model_name=model_name) if model_name else Embedder()
    adapter = QdrantAdapter(
        client=client, collection_alias=collection, span_logger=logger
    )
    retriever = HybridRetriever(embedder, adapter, bm25, reranker, span_logger=logger)

    scores: list[float] = []
    for i, ex in enumerate(examples):
        q = str(ex["input"]["question"])
        docs = await retriever.retrieve(q, top_k=top_k)
        retrieved = list(dict.fromkeys(d.id for d in docs))
        scores.append(recall_at_k(retrieved, gold_ids(ex), top_k))
        if (i + 1) % 20 == 0:
            print(
                f"  [{label}] [{i + 1}/{len(examples)}] mean: {statistics.fmean(scores):.4f}"
            )
    await client.close()
    return scores


async def run(args: argparse.Namespace) -> None:
    examples = load_examples(Path(args.canary))
    print(f"Canary: {len(examples)} questions from {args.canary}\n")

    print("Scoring BASE arm...")
    base = await retrieve_scores(
        collection=args.base_collection,
        model_name=None,
        examples=examples,
        bm25_path=args.bm25,
        top_k=args.top_k,
        label="base",
    )
    print("\nScoring CANDIDATE arm...")
    cand = await retrieve_scores(
        collection=args.candidate_collection,
        model_name=args.checkpoint,
        examples=examples,
        bm25_path=args.bm25,
        top_k=args.top_k,
        label="cand",
    )

    deltas = [c - b for b, c in zip(base, cand, strict=True)]
    base_mean = statistics.fmean(base)
    cand_mean = statistics.fmean(cand)

    # Flip categories (recall is fractional for multi-gold questions).
    miss_to_hit = sum(
        1 for b, c in zip(base, cand, strict=True) if b == 0.0 and c > 0.0
    )
    hit_to_miss = sum(
        1 for b, c in zip(base, cand, strict=True) if b > 0.0 and c == 0.0
    )
    improved = sum(1 for d in deltas if d > 1e-9)
    regressed = sum(1 for d in deltas if d < -1e-9)
    unchanged = sum(1 for d in deltas if abs(d) <= 1e-9)

    ci = paired_bootstrap_ci(deltas, samples=args.bootstrap, seed=args.seed)
    sign_p = sign_test_p(improved, regressed)

    print(f"\n{'=' * 64}")
    print("  PER-QUESTION FLIP ANALYSIS — canary, paired")
    print(f"{'=' * 64}")
    print(f"  base collection:      {args.base_collection}")
    print(f"  candidate collection: {args.candidate_collection}")
    print(f"  checkpoint:           {args.checkpoint}")
    print(f"  questions:            {len(examples)}   top_k={args.top_k}")
    print(f"{'-' * 64}")
    print(f"  base recall@{args.top_k}:      {base_mean:.4f}")
    print(f"  candidate recall@{args.top_k}: {cand_mean:.4f}")
    print(f"  mean delta:           {ci['mean']:+.4f}")
    print(f"  paired 95% CI:        [{ci['ci_low']:+.4f}, {ci['ci_high']:+.4f}]")
    print(f"  bootstrap P(delta<=0):{ci.get('p_le_0', float('nan')):.4f}")
    print(f"{'-' * 64}")
    print(f"  improved (delta>0):   {improved}")
    print(f"  regressed (delta<0):  {regressed}")
    print(f"  unchanged:            {unchanged}")
    print(f"    of which Miss->Hit:  {miss_to_hit}")
    print(f"    of which Hit->Miss:  {hit_to_miss}")
    print(f"  sign-test p (2-sided):{sign_p:.4g}")
    print(f"{'=' * 64}")

    significant = ci["ci_low"] > 0.0
    if significant:
        print("  ✓ Paired 95% CI excludes zero — lift is statistically robust.")
    else:
        print("  ✗ Paired 95% CI includes zero — lift not distinguishable from noise.")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {
                    "base_collection": args.base_collection,
                    "candidate_collection": args.candidate_collection,
                    "checkpoint": args.checkpoint,
                    "n": len(examples),
                    "top_k": args.top_k,
                    "base_mean": base_mean,
                    "candidate_mean": cand_mean,
                    "delta_ci": ci,
                    "improved": improved,
                    "regressed": regressed,
                    "unchanged": unchanged,
                    "miss_to_hit": miss_to_hit,
                    "hit_to_miss": hit_to_miss,
                    "sign_test_p": sign_p,
                    "per_question": [
                        {"base": b, "candidate": c, "delta": d}
                        for b, c, d in zip(base, cand, deltas, strict=True)
                    ],
                },
                indent=2,
            )
        )
        print(f"\n  wrote {args.json_out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-collection", default="corpus.bright")
    parser.add_argument("--candidate-collection", required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument(
        "--canary", default="evals/datasets/bright_biology_canary.jsonl"
    )
    parser.add_argument("--bm25", default="data/bright_bm25_index.pkl")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--bootstrap", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--json-out", default=None)
    asyncio.run(run(parser.parse_args()))
