"""Promotion + canary eval for a fine-tuned embedding candidate.

After the trainer produces a checkpoint, this module runs the promotion loop:

1. **Index** — re-embeds the full corpus with the candidate model into a new
   Qdrant collection ``corpus.candidate.<job_id>`` without touching the live
   ``corpus.active`` alias.
2. **Before recall** — measures retrieval_recall@10 over the dev examples using
   the *current* ``corpus.active`` collection (baseline embedder).
3. **After recall** — measures retrieval_recall@10 using the candidate embedder
   against the new collection.
4. **Decision** — bootstraps 95% CIs for both.  If ``after.mean > before.mean``
   the alias ``corpus.active`` is atomically swapped to the candidate collection
   (promoted); otherwise the collection is left in place and the job is archived.

Both heavy operations (indexing and retrieval) are injectable so tests run
without a model, a Qdrant instance, or gradient descent.  The default backends
build real ``Embedder`` instances lazily (model loading deferred to first call).
"""

from __future__ import annotations

import json
import random
import statistics
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import Any

from helix.eval.harness import Example
from helix.runtime.sqlite_store import SqliteStore
from helix.tools.qdrant_adapter import Point, QdrantAdapter

# (corpus_path, collection_name) -> None — create + populate a Qdrant collection
IndexFn = Callable[[str, str], Awaitable[None]]

# (query_text, collection_name, top_k) -> list[doc_ids]
RetrieveFn = Callable[[str, str, int], Awaitable[list[str]]]

_POINT_NAMESPACE = uuid.UUID("a4f0c8d2-6b1e-4e3a-9c7d-0e1f2a3b4c5d")
_ACTIVE_ALIAS = "corpus.active"


def _load_jsonl(path: str) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if stripped:
                docs.append(json.loads(stripped))
    return docs


@dataclass(frozen=True)
class PromoteConfig:
    """Hyper-parameters for the canary eval step."""

    vector_size: int = 768
    distance: str = "Cosine"
    top_k: int = 10
    bootstrap_samples: int = 1000
    seed: int = 0


@dataclass
class PromoteResult:
    """Outcome of a promotion run."""

    job_id: str
    status: str  # "promoted" | "archived"
    collection: str  # candidate collection name (whether promoted or archived)
    metrics: dict[str, Any]  # {"before": CI dict, "after": CI dict, "delta_mean": float}


def candidate_collection(job_id: str) -> str:
    """Canonical name for the candidate Qdrant collection."""
    return f"corpus.candidate.{job_id}"


def _gold_doc_ids(example: Example) -> set[str]:
    facts = example.expected_output.get("supporting_facts", [])
    return {str(fact["doc_id"]) for fact in facts}


def _recall_at_k(retrieved: list[str], gold: set[str], k: int) -> float:
    if not gold:
        return 0.0
    return len(gold & set(retrieved[:k])) / len(gold)


def _bootstrap_ci(
    values: list[float],
    *,
    samples: int,
    seed: int,
    alpha: float = 0.05,
) -> dict[str, float]:
    n = len(values)
    if n == 0:
        return {"mean": 0.0, "std": 0.0, "ci_low": 0.0, "ci_high": 0.0, "n": 0}
    mean = statistics.fmean(values)
    std = statistics.pstdev(values)
    if n == 1:
        return {"mean": mean, "std": std, "ci_low": mean, "ci_high": mean, "n": n}
    rng = random.Random(seed)
    boot_means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(samples))
    ci_low = boot_means[int((alpha / 2) * samples)]
    ci_high = boot_means[min(int((1 - alpha / 2) * samples), samples - 1)]
    return {"mean": mean, "std": std, "ci_low": ci_low, "ci_high": ci_high, "n": n}


def _make_default_index_fn(
    checkpoint_dir: str,
    adapter: QdrantAdapter,
    config: PromoteConfig,
) -> IndexFn:
    """Real backend: embed corpus with candidate model and upsert into collection."""

    async def _index(corpus_path: str, collection: str) -> None:
        from helix.rag.chunker import chunk_document
        from helix.tools.embedder import Embedder

        embedder = Embedder(model_name=checkpoint_dir)
        docs = _load_jsonl(corpus_path)

        await adapter.create_collection(
            collection, vector_size=config.vector_size, distance=config.distance
        )
        chunks = [
            chunk
            for doc in docs
            for chunk in chunk_document(str(doc["id"]), str(doc["text"]), str(doc["source"]))
        ]
        vectors = embedder.embed_documents([c.text for c in chunks]) if chunks else []
        points = [
            Point(
                id=str(uuid.uuid5(_POINT_NAMESPACE, c.chunk_id)),
                vector=v,
                payload={
                    "doc_id": c.doc_id,
                    "chunk_id": c.chunk_id,
                    "text": c.text,
                    "source": c.source,
                },
            )
            for c, v in zip(chunks, vectors, strict=True)
        ]
        if points:
            await adapter.upsert_to(collection, points)

    return _index


def _make_default_retrieve_fn(
    checkpoint_dir: str,
    adapter: QdrantAdapter,
) -> RetrieveFn:
    """Real backend: embed query with the appropriate model and search the collection."""
    from helix.tools.embedder import Embedder

    # Lazy-init: Embedder stores None for _encode until first call to embed_queries.
    baseline_embedder = Embedder()
    candidate_embedder = Embedder(model_name=checkpoint_dir)

    async def _retrieve(query: str, collection: str, k: int) -> list[str]:
        if collection == _ACTIVE_ALIAS:
            vec = baseline_embedder.embed_queries([query])[0]
            results = await adapter.search(vec, top_k=k)
        else:
            vec = candidate_embedder.embed_queries([query])[0]
            results = await adapter.search_in(collection, vec, top_k=k)
        return [str(r.payload.get("doc_id", "")) for r in results]

    return _retrieve


async def promote_candidate(
    checkpoint_dir: str | PathLike[str],
    corpus_path: str | PathLike[str],
    examples: list[Example],
    job_id: str,
    *,
    adapter: QdrantAdapter,
    store: SqliteStore,
    config: PromoteConfig | None = None,
    index_fn: IndexFn | None = None,
    retrieve_fn: RetrieveFn | None = None,
) -> PromoteResult:
    """Index the candidate checkpoint, canary-eval on dev examples, and promote if better.

    Raises ``ValueError`` when ``examples`` is empty — there is nothing to measure.

    The alias ``corpus.active`` is swapped to the candidate collection only when
    ``after.mean > before.mean``.  The job record in ``store`` is updated to
    ``promoted`` or ``archived`` with full CI metrics regardless of outcome.
    """
    if not examples:
        raise ValueError("promote_candidate requires at least one example")

    cfg = config or PromoteConfig()
    ckpt = str(checkpoint_dir)
    corpus = str(corpus_path)
    coll = candidate_collection(job_id)

    _index = index_fn or _make_default_index_fn(ckpt, adapter, cfg)
    _retrieve = retrieve_fn or _make_default_retrieve_fn(ckpt, adapter)

    await store.update_embedding_job(job_id, status="evaluating")

    # Step 1: index corpus with candidate model (does not touch corpus.active)
    await _index(corpus, coll)

    # Step 2: before recall — current corpus.active with baseline embedder
    before_scores: list[float] = []
    for ex in examples:
        gold = _gold_doc_ids(ex)
        retrieved = await _retrieve(str(ex.input.get("question", "")), _ACTIVE_ALIAS, cfg.top_k)
        before_scores.append(_recall_at_k(retrieved, gold, cfg.top_k))

    # Step 3: after recall — candidate collection with candidate embedder
    after_scores: list[float] = []
    for ex in examples:
        gold = _gold_doc_ids(ex)
        retrieved = await _retrieve(str(ex.input.get("question", "")), coll, cfg.top_k)
        after_scores.append(_recall_at_k(retrieved, gold, cfg.top_k))

    before_ci = _bootstrap_ci(before_scores, samples=cfg.bootstrap_samples, seed=cfg.seed)
    after_ci = _bootstrap_ci(after_scores, samples=cfg.bootstrap_samples, seed=cfg.seed)
    delta = after_ci["mean"] - before_ci["mean"]

    promoted = after_ci["mean"] > before_ci["mean"]
    status = "promoted" if promoted else "archived"

    if promoted:
        await adapter.set_alias(_ACTIVE_ALIAS, coll)

    metrics: dict[str, Any] = {
        "before": before_ci,
        "after": after_ci,
        "delta_mean": round(delta, 6),
    }
    artifact_uri = f"data/models/{job_id}" if promoted else None
    await store.update_embedding_job(
        job_id,
        status=status,
        metrics=metrics,
        artifact_uri=artifact_uri,
    )

    return PromoteResult(job_id=job_id, status=status, collection=coll, metrics=metrics)
