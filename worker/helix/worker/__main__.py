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

import click

from helix import configure_redis
from helix.logging import SpanLogger
from helix.rag.retriever import HybridRetriever
from helix.runtime.remote_engine import RemoteEngine
from helix.tools.blob import BlobStore
from helix.tools.bm25 import BM25Index
from helix.tools.llm_cache import LLMCache
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

    async with RemoteEngine(
        orchestrator_url=orchestrator,
        nats_url=nats_url,
        pool=pool,
        redis=redis,
    ) as engine:
        engine.register_workflow("deep_research", handle_deep_research)
        logger.info("worker ready — consuming from pool %s", pool)
        await engine.run()


if __name__ == "__main__":
    main()
