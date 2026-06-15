"""Python worker entrypoint for remote (M1) mode.

Usage:
    python -m helix.worker \\
      --orchestrator grpc://localhost:50051 \\
      --nats nats://localhost:4222 \\
      --pool research

The worker registers with the Go orchestrator, subscribes to NATS for task
envelopes, executes the deep_research workflow in local mode (no current_engine),
and reports completion via gRPC CompleteTask.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import click

from helix import configure_redis
from helix.eval.harness import load_dataset
from helix.logging import SpanLogger
from helix.rag.miner.miner import FailureCase, mine_from_clickhouse
from helix.rag.promotion.promote import promote_candidate
from helix.rag.retriever import HybridRetriever
from helix.rag.trainer.train import TrainConfig, TrainResult, train_embedding
from helix.rag.trainer.triplets import build_triplets
from helix.runtime.remote_engine import RemoteEngine
from helix.runtime.sqlite_store import SqliteStore
from helix.tools.blob import BlobStore
from helix.tools.bm25 import BM25Index
from helix.tools.llm_cache import LLMCache
from helix.tools.qdrant_adapter import QdrantAdapter
from helix.tools.rate_limit import RedisRateLimiter
from helix.tools.redis_conn import get_redis
from helix.workflows.deep_research import ResearchDeps, deep_research, using_research_deps

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("helix.worker")


def _build_deps(
    *,
    qdrant_url: str,
    qdrant_path: str | None,
    bm25_path: str,
    collection_alias: str,
    top_k: int,
    model: str | None,
    span_logger: SpanLogger,
    rate_limiter: RedisRateLimiter | None = None,
    blob_store: BlobStore | None = None,
) -> ResearchDeps:
    # Import here to avoid heavy init at module load
    from helix.cli import (
        _build_adapter,
        _build_completion_fns,
        _build_embedder,
        _build_reranker,
    )

    embedder = _build_embedder(span_logger)
    bm25 = BM25Index.load(bm25_path)
    adapter = _build_adapter(
        span_logger,
        url=qdrant_url,
        path=qdrant_path,
        collection_alias=collection_alias,
    )
    retriever = HybridRetriever(embedder, adapter, bm25, _build_reranker(), span_logger=span_logger)
    completion_fn, cost_fn = _build_completion_fns()
    return ResearchDeps(
        retriever=retriever,
        top_k=top_k,
        model=model,
        span_logger=span_logger,
        cache=LLMCache(),
        completion_fn=completion_fn,
        cost_fn=cost_fn,
        rate_limiter=rate_limiter,
        blob_store=blob_store,
    )


async def _run_finetune_job(
    *,
    job_id: str,
    train_split: str,
    eval_split: str,
    corpus_alias: str = "corpus.active",
    ch_http_url: str | None = None,
    models_dir: str = "data/models",
    span_logger: SpanLogger,
) -> dict[str, Any]:
    """Run the mine → train → promote pipeline for a production finetune job.

    Returns a dict compatible with FinetuneTaskOutput (Go store) so the gRPC
    CompleteTask hook can update finetune_jobs in Postgres.
    """
    output_dir = str(Path(models_dir) / job_id)

    # Phase 1: mine failures from ClickHouse.
    # train_dataset is omitted — mine_from_clickhouse uses all traces in ClickHouse.
    if not ch_http_url:
        raise RuntimeError("CLICKHOUSE_HTTP_URL must be set for production finetune jobs")

    cases: list[FailureCase] = await mine_from_clickhouse(
        train_dataset=[],
        ch_http_url=ch_http_url,
    )
    if not cases:
        logger.info("finetune_job %s: no failures mined — archived", job_id)
        return {
            "job_id": job_id,
            "outcome": "no_failures",
            "before_recall": 0.0,
            "after_recall": 0.0,
            "failures": 0,
            "triplets": 0,
        }

    # Phase 2: build triplets and train.
    triplets = build_triplets(cases, {})
    if not triplets:
        logger.info("finetune_job %s: no triplets — archived", job_id)
        return {
            "job_id": job_id,
            "outcome": "no_triplets",
            "before_recall": 0.0,
            "after_recall": 0.0,
            "failures": len(cases),
            "triplets": 0,
        }

    train_result: TrainResult = train_embedding(triplets, output_dir, config=TrainConfig())
    logger.info(
        "finetune_job %s: trained %d triplets → %s",
        job_id,
        train_result.triplets_count,
        output_dir,
    )

    # Phase 3: promote — use a throw-away SQLiteStore for the canary step.
    # The promotion outcome is returned; Postgres finetune_jobs is updated by
    # the orchestrator's CompleteTask hook, not from here.
    tmp_db = str(Path(tempfile.mkdtemp()) / f"finetune-{job_id}.db")
    async with SqliteStore(tmp_db) as store:
        await store.create_embedding_job("nomic-ai/nomic-embed-text-v1.5", {})
        qdrant_url = os.environ.get("QDRANT_URL", "http://localhost:6333")
        adapter = QdrantAdapter(
            url=qdrant_url, collection_alias=corpus_alias, span_logger=span_logger
        )
        eval_examples = load_dataset(eval_split)

        corpus_path = os.environ.get("HELIX_CORPUS_PATH", "data/corpus.jsonl")
        promote_result = await promote_candidate(
            checkpoint_dir=output_dir,
            corpus_path=corpus_path,
            examples=eval_examples,
            job_id=job_id,
            adapter=adapter,
            store=store,
        )

    before = promote_result.metrics.get("before", {}).get("mean", 0.0)
    after = promote_result.metrics.get("after", {}).get("mean", 0.0)
    logger.info(
        "finetune_job %s: %s (before=%.4f after=%.4f)",
        job_id,
        promote_result.status,
        before,
        after,
    )
    return {
        "job_id": job_id,
        "outcome": promote_result.status,
        "before_recall": before,
        "after_recall": after,
        "failures": len(cases),
        "triplets": train_result.triplets_count,
        "config": train_result.config.to_dict(),
    }


@click.command()
@click.option("--orchestrator", default="grpc://localhost:50051", help="Go orchestrator gRPC URL")
@click.option("--nats", "nats_url", default="nats://localhost:4222", help="NATS URL")
@click.option("--pool", default="research", help="Worker pool name")
@click.option("--qdrant-url", default="http://localhost:6333", envvar="QDRANT_URL")
@click.option("--qdrant-path", default=None, envvar="QDRANT_PATH")
@click.option("--bm25", "bm25_path", default="data/bright_bm25_index.pkl", envvar="BM25_PATH")
@click.option("--collection", default="corpus.active", envvar="HELIX_COLLECTION")
@click.option("--top-k", default=10, type=int, envvar="HELIX_TOP_K")
@click.option("--model", default=None, envvar="HELIX_DEFAULT_MODEL")
def main(
    orchestrator: str,
    nats_url: str,
    pool: str,
    qdrant_url: str,
    qdrant_path: str | None,
    bm25_path: str,
    collection: str,
    top_k: int,
    model: str | None,
) -> None:
    asyncio.run(
        _run(
            orchestrator=orchestrator,
            nats_url=nats_url,
            pool=pool,
            qdrant_url=qdrant_url,
            qdrant_path=qdrant_path,
            bm25_path=bm25_path,
            collection=collection,
            top_k=top_k,
            model=model,
        )
    )


async def _run(
    *,
    orchestrator: str,
    nats_url: str,
    pool: str,
    qdrant_url: str,
    qdrant_path: str | None,
    bm25_path: str,
    collection: str,
    top_k: int,
    model: str | None,
) -> None:
    span_logger = SpanLogger()

    # M3 infra: Redis (exactly-once + rate limit) and MinIO (blob offload).
    # All three are no-ops when their env vars are unset.
    redis = get_redis(os.environ.get("REDIS_URL"))
    configure_redis(redis)  # powers helix.exactly_once for workflow authors

    blob_store = BlobStore.from_env()
    if blob_store is not None:
        await blob_store.ensure_buckets()
        logger.info("blob store ready")

    rpm = int(os.environ.get("HELIX_LLM_RPM", "0"))
    rate_limiter = RedisRateLimiter(redis, key=model or "default", rate=rpm) if rpm > 0 else None

    logger.info("building research deps ...")
    deps = _build_deps(
        qdrant_url=qdrant_url,
        qdrant_path=qdrant_path,
        bm25_path=bm25_path,
        collection_alias=collection,
        top_k=top_k,
        model=model,
        span_logger=span_logger,
        rate_limiter=rate_limiter,
        blob_store=blob_store,
    )
    logger.info("deps ready")

    async def handle_deep_research(**kwargs: object) -> object:
        with using_research_deps(deps):
            return await deep_research(**kwargs)  # type: ignore[arg-type]

    ch_http_url = os.environ.get("CLICKHOUSE_HTTP_URL")

    async def handle_finetune_job(**kwargs: object) -> object:
        kw = dict(kwargs)
        return await _run_finetune_job(
            job_id=str(kw.get("job_id", "")),
            train_split=str(kw.get("train_split", "")),
            eval_split=str(kw.get("eval_split", "")),
            corpus_alias=str(kw.get("corpus_alias", "corpus.active")),
            ch_http_url=ch_http_url,
            span_logger=span_logger,
        )

    async with RemoteEngine(
        orchestrator_url=orchestrator,
        nats_url=nats_url,
        pool=pool,
        redis=redis,
    ) as engine:
        engine.register_workflow("deep_research", handle_deep_research)
        engine.register_workflow("finetune_job", handle_finetune_job)
        logger.info("worker ready — consuming from pool %s", pool)
        await engine.run()


if __name__ == "__main__":
    main()
