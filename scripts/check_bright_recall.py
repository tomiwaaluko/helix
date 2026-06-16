"""Direct HybridRetriever recall check for the BRIGHT biology dev set.

Measures retrieval_recall@10 without running the full deep_research workflow
(no LLM calls, no cost). Uses the same hybrid pipeline as the finetune canary:
  dense (Qdrant) + BM25 + BGE reranker → top-10.

The collection to query is controlled by --collection (default corpus.bright).
Run against both corpus.bright (base) and a fine-tuned candidate to compare.

Usage:
    python3.12 scripts/check_bright_recall.py [--collection corpus.bright] [--top-k 10]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "worker"))

DEV_PATH = ROOT / "evals" / "datasets" / "bright_biology_dev.jsonl"
BM25_PATH = ROOT / "data" / "bright_bm25_index.pkl"
QDRANT_PATH = ROOT / "data" / "qdrant"


def load_examples() -> list[dict]:
    if not DEV_PATH.exists():
        sys.exit(f"ERROR: {DEV_PATH} not found. Run scripts/prepare_bright.py first.")
    return [json.loads(l) for l in DEV_PATH.read_text().splitlines() if l.strip()]


def gold_ids(ex: dict) -> set[str]:
    return {f["doc_id"] for f in ex["expected_output"]["supporting_facts"]}


def recall_at_k(retrieved: list[str], gold: set[str], k: int) -> float:
    if not gold:
        return 0.0
    return len(gold & set(retrieved[:k])) / len(gold)


async def run(collection: str, top_k: int) -> None:
    from helix.logging import SpanLogger
    from helix.rag.retriever import HybridRetriever
    from helix.tools.bm25 import BM25Index
    from helix.tools.embedder import Embedder
    from helix.tools.qdrant_adapter import QdrantAdapter
    from helix.tools.reranker import Reranker
    from qdrant_client import AsyncQdrantClient  # type: ignore[import-untyped]

    if not BM25_PATH.exists():
        sys.exit(f"ERROR: {BM25_PATH} not found. Run 'make seed-bright' first.")

    examples = load_examples()
    bm25 = BM25Index.load(str(BM25_PATH))
    reranker = Reranker()
    logger = SpanLogger()
    client = AsyncQdrantClient(path=str(QDRANT_PATH))
    embedder = Embedder()
    adapter = QdrantAdapter(client=client, collection_alias=collection, span_logger=logger)
    retriever = HybridRetriever(embedder, adapter, bm25, reranker, span_logger=logger)

    scores: list[float] = []
    for i, ex in enumerate(examples):
        q = str(ex["input"]["question"])
        docs = await retriever.retrieve(q, top_k=top_k)
        retrieved = list(dict.fromkeys(d.id for d in docs))
        r = recall_at_k(retrieved, gold_ids(ex), top_k)
        scores.append(r)
        if (i + 1) % 20 == 0:
            print(f"  [{i+1}/{len(examples)}] running mean: {sum(scores)/len(scores):.4f}")

    await client.close()

    mean = sum(scores) / len(scores)
    hits = sum(1 for s in scores if s >= 1.0)
    partial = sum(1 for s in scores if 0 < s < 1.0)
    misses = sum(1 for s in scores if s == 0.0)

    print(f"\n{'='*60}")
    print(f"  Collection:        {collection}")
    print(f"  Questions:         {len(scores)}")
    print(f"  retrieval_recall@{top_k}: {mean:.4f}")
    print(f"  Full hits:         {hits}")
    print(f"  Partial hits:      {partial}")
    print(f"  Total misses:      {misses}")
    print(f"{'='*60}")

    if mean <= 0.70:
        print("  ✓ Headroom confirmed (recall ≤ 0.70) — fine-tune experiment is valid here.")
    elif mean <= 0.85:
        print("  ~ Some headroom (0.70 < recall ≤ 0.85) — fine-tune may show measurable lift.")
    else:
        print("  ✗ Low headroom (recall > 0.85) — ceiling effect, same problem as HotpotQA.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection", default="corpus.bright")
    parser.add_argument("--top-k", type=int, default=10)
    args = parser.parse_args()
    asyncio.run(run(args.collection, args.top_k))
