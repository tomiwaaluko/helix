"""Helix slice CLI — the end-to-end entry point.

Three commands wire the slice together:

* ``index`` chunks + embeds a JSONL corpus into Qdrant and writes the companion
  BM25 index to disk (the hybrid retriever needs both halves).
* ``eval`` runs the ``deep_research`` workflow over an eval dataset, scores it,
  and writes the baseline JSON report (the slice deliverable).
* ``run`` answers a single question — a convenience for spot-checking.

The heavy, model-backed components (the embedder, Qdrant adapter, reranker, and
LLM) are built by small module-level factories so tests can substitute stubs
without a GPU, a Qdrant server, or a network. Production runs use the real
defaults.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

from helix.eval.corpus import load_corpus
from helix.eval.harness import EvalReport, Example, Scorer, evaluate, load_dataset
from helix.eval.scorers import (
    answer_f1,
    citation_precision,
    default_scorers,
    retrieval_recall_scorer,
)
from helix.logging import SpanLogger
from helix.rag.chunker import Chunk, TokenCounter, chunk_document
from helix.rag.indexer import IndexResult, index_corpus
from helix.rag.miner.miner import mine_failures, to_store_row
from helix.rag.promotion.promote import (
    IndexFn,
    PromoteConfig,
    RetrieveFn,
    candidate_collection,
    promote_candidate,
)
from helix.rag.retriever import HybridRetriever
from helix.rag.trainer.train import TrainConfig, train_embedding
from helix.rag.trainer.train import TrainFn as _TrainFn
from helix.rag.trainer.triplets import build_triplets
from helix.runtime.sqlite_store import SqliteStore
from helix.tools.bm25 import BM25Index
from helix.tools.embedder import Embedder
from helix.tools.litellm_adapter import _DEFAULT_MODEL as ADAPTER_DEFAULT_MODEL
from helix.tools.llm_cache import LLMCache
from helix.tools.qdrant_adapter import QdrantAdapter
from helix.tools.reranker import Reranker
from helix.types import Answer
from helix.workflows.deep_research import ResearchDeps, deep_research, using_research_deps

DEFAULT_QDRANT_URL = "http://localhost:6333"
DEFAULT_BM25_PATH = "data/bm25_index.pkl"
DEFAULT_BASELINE = "evals/baselines/hotpotqa_dev_100_baseline.json"
DEFAULT_DB_PATH = "data/helix.db"
DEFAULT_MODELS_DIR = "data/models"
SUPPORTED_WORKFLOWS = ("deep_research",)


# --- Component factories (monkeypatched in tests) ----------------------------


def _build_embedder(span_logger: SpanLogger) -> Embedder:
    return Embedder(span_logger=span_logger)


def _build_candidate_embedder(checkpoint_dir: str, span_logger: SpanLogger) -> Embedder:
    """The fine-tuned candidate embedder, loaded from a checkpoint dir (stubbed in tests)."""
    return Embedder(model_name=checkpoint_dir, span_logger=span_logger)


def _build_adapter(
    span_logger: SpanLogger,
    *,
    url: str,
    path: str | None,
    collection_alias: str = "corpus.active",
) -> QdrantAdapter:
    """Build a Qdrant adapter.

    With ``path`` set, run Qdrant embedded in-process against an on-disk store
    (no server, no Docker daemon); otherwise connect to the server at ``url``.
    """
    if path:
        from qdrant_client import AsyncQdrantClient

        return QdrantAdapter(
            client=AsyncQdrantClient(path=path),
            collection_alias=collection_alias,
            span_logger=span_logger,
        )
    return QdrantAdapter(url=url, collection_alias=collection_alias, span_logger=span_logger)


def _build_reranker() -> Reranker:
    return Reranker()


def _build_completion_fns() -> tuple[
    Callable[..., Awaitable[Any]] | None, Callable[[Any], float] | None
]:
    """Return (completion_fn, cost_fn); ``(None, None)`` routes through LiteLLM."""
    return None, None


def _token_counter() -> TokenCounter | None:
    """``None`` lets the chunker use its tiktoken counter; tests override it."""
    return None


def _build_store(db_path: str) -> SqliteStore:
    return SqliteStore(db_path)


def _train_backend() -> _TrainFn | None:
    """``None`` runs the real sentence-transformers fit; tests inject a stub."""
    return None


def _build_promotion_backends(
    *,
    checkpoint_dir: str,
    adapter: QdrantAdapter,
    candidate_collection: str,
    bm25_path: str,
    top_k: int,
    span_logger: SpanLogger,
) -> tuple[IndexFn | None, RetrieveFn | None]:
    """Return (index_fn, retrieve_fn) for the promotion canary.

    The canary runs the *full hybrid pipeline* (dense + BM25 + rerank) so the
    promotion decision reflects end-to-end retrieval, not dense recall in
    isolation: a candidate that improves dense recall but is washed out by the
    reranker should not be promoted. Both arms share the BM25 index and reranker
    (model-independent); only the dense embedder and its target collection differ:

    * ``corpus.active`` → base embedder over the live alias (matches ``make eval``).
    * candidate collection → the fine-tuned checkpoint over its fresh collection.

    Returns ``None`` for ``index_fn`` so promotion re-embeds the corpus with the
    candidate model via its default indexer; only retrieval is overridden here.
    """
    bm25 = BM25Index.load(bm25_path)
    reranker = _build_reranker()
    base_retriever = HybridRetriever(
        _build_embedder(span_logger), adapter, bm25, reranker, span_logger=span_logger
    )
    candidate_retriever = HybridRetriever(
        _build_candidate_embedder(checkpoint_dir, span_logger),
        adapter.for_collection(candidate_collection),
        bm25,
        reranker,
        span_logger=span_logger,
    )

    async def _retrieve(query: str, collection: str, k: int) -> list[str]:
        retriever = candidate_retriever if collection == candidate_collection else base_retriever
        docs = await retriever.retrieve(query, top_k=k)
        # Doc-level, order-preserving dedup — matches how the workflow builds
        # Answer.metadata["retrieved_doc_ids"], so recall is measured identically.
        return list(dict.fromkeys(doc.id for doc in docs))

    return None, _retrieve


# --- Shared helpers ----------------------------------------------------------


def _resolve_scorers(spec: str) -> dict[str, Scorer]:
    """Parse a ``name,name,...`` spec into the harness scorer mapping."""
    standard = default_scorers()
    resolved: dict[str, Scorer] = {}
    for name in (part.strip() for part in spec.split(",")):
        if not name:
            continue
        if name in standard:
            resolved[name] = standard[name]
        elif name == "answer_f1":
            resolved[name] = answer_f1
        elif name == "citation_precision":
            resolved[name] = citation_precision
        elif name.startswith("retrieval_recall@"):
            try:
                k = int(name.split("@", 1)[1])
            except ValueError as exc:
                raise click.BadParameter(f"invalid recall scorer {name!r}") from exc
            resolved[name] = retrieval_recall_scorer(k)
        else:
            raise click.BadParameter(f"unknown scorer {name!r}")
    if not resolved:
        raise click.BadParameter("no scorers selected")
    return resolved


def _load_and_chunk(
    corpus_path: str, token_counter: TokenCounter | None, *, max_tokens: int, overlap_tokens: int
) -> list[Chunk]:
    chunks: list[Chunk] = []
    for line in Path(corpus_path).read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        doc = json.loads(stripped)
        chunks.extend(
            chunk_document(
                str(doc["id"]),
                str(doc["text"]),
                str(doc["source"]),
                max_tokens=max_tokens,
                overlap_tokens=overlap_tokens,
                token_counter=token_counter,
            )
        )
    return chunks


async def _run_workflow(inp: Mapping[str, Any]) -> Answer:
    return await deep_research.local(**inp)


def _check_workflow(name: str) -> None:
    if name not in SUPPORTED_WORKFLOWS:
        raise click.BadParameter(
            f"unknown workflow {name!r}; supported: {', '.join(SUPPORTED_WORKFLOWS)}"
        )


# --- Core async logic --------------------------------------------------------


async def _index(
    *,
    corpus: str,
    collection: str,
    qdrant_url: str,
    qdrant_path: str | None,
    alias: str,
    bm25_path: str,
    vector_size: int,
    max_tokens: int,
    overlap_tokens: int,
    span_logger: SpanLogger,
) -> IndexResult:
    counter = _token_counter()
    embedder = _build_embedder(span_logger)
    async with _build_adapter(span_logger, url=qdrant_url, path=qdrant_path) as adapter:
        result = await index_corpus(
            corpus,
            collection,
            embedder=embedder,
            adapter=adapter,
            alias=alias,
            vector_size=vector_size,
            max_tokens=max_tokens,
            overlap_tokens=overlap_tokens,
            token_counter=counter,
            span_logger=span_logger,
        )
    # Build the sparse half of the hybrid index from the same chunks and persist
    # it for the retriever (Qdrant holds only the dense vectors).
    chunks = _load_and_chunk(corpus, counter, max_tokens=max_tokens, overlap_tokens=overlap_tokens)
    BM25Index.build(chunks).save(bm25_path)
    return result


async def _open_retriever(
    *,
    qdrant_url: str,
    qdrant_path: str | None,
    bm25_path: str,
    span_logger: SpanLogger,
    collection_alias: str = "corpus.active",
) -> tuple[QdrantAdapter, HybridRetriever]:
    embedder = _build_embedder(span_logger)
    bm25 = BM25Index.load(bm25_path)
    adapter = _build_adapter(
        span_logger, url=qdrant_url, path=qdrant_path, collection_alias=collection_alias
    )
    retriever = HybridRetriever(embedder, adapter, bm25, _build_reranker(), span_logger=span_logger)
    return adapter, retriever


def _make_deps(
    retriever: HybridRetriever, *, top_k: int, model: str | None, span_logger: SpanLogger
) -> ResearchDeps:
    completion_fn, cost_fn = _build_completion_fns()
    return ResearchDeps(
        retriever=retriever,
        top_k=top_k,
        model=model,
        span_logger=span_logger,
        cache=LLMCache(),
        completion_fn=completion_fn,
        cost_fn=cost_fn,
    )


async def _eval(
    *,
    dataset_path: str,
    scorers: Mapping[str, Scorer],
    concurrency: int,
    qdrant_url: str,
    qdrant_path: str | None,
    bm25_path: str,
    top_k: int,
    model: str | None,
    span_logger: SpanLogger,
) -> tuple[EvalReport, list[Example], str, str]:
    dataset = load_dataset(dataset_path)
    adapter, retriever = await _open_retriever(
        qdrant_url=qdrant_url, qdrant_path=qdrant_path, bm25_path=bm25_path, span_logger=span_logger
    )
    deps = _make_deps(retriever, top_k=top_k, model=model, span_logger=span_logger)
    async with adapter:
        with using_research_deps(deps):
            report = await evaluate(_run_workflow, dataset, scorers, concurrency=concurrency)
    resolved_model = model or os.environ.get("HELIX_DEFAULT_MODEL", ADAPTER_DEFAULT_MODEL)
    embedder_name = retriever_embedder_name(retriever)
    return report, dataset, resolved_model, embedder_name


def retriever_embedder_name(retriever: HybridRetriever) -> str:
    embedder = getattr(retriever, "_embedder", None)
    return getattr(embedder, "model_name", "unknown")


@dataclass
class FinetuneResult:
    """Outcome of the end-to-end fine-tune loop (mine → train → promote)."""

    job_id: str
    status: str  # "promoted" | "archived" | "no_failures" | "no_triplets"
    failures_mined: int
    triplets_built: int
    output_dir: str | None
    metrics: dict[str, Any] | None
    failures_skipped: int = 0  # mining-eval examples skipped due to provider errors


async def _finetune(
    *,
    train_dataset_path: str,
    eval_dataset_path: str,
    corpus_path: str,
    scorers: Mapping[str, Scorer],
    concurrency: int,
    qdrant_url: str,
    qdrant_path: str | None,
    bm25_path: str,
    top_k: int,
    model: str | None,
    base_model: str,
    models_dir: str,
    db_path: str,
    spans_path: str,
    train_config: TrainConfig,
    promote_config: PromoteConfig,
    span_logger: SpanLogger,
    collection_alias: str = "corpus.active",
    promotion_alias: str = "corpus.active",
) -> FinetuneResult:
    """Run the full loop: mine failures on the train split, fine-tune, canary-promote.

    The mining run evaluates ``train_dataset_path`` through the *current* retrieval
    pipeline, persisting per-example results and emitting spans; failures (recall
    < 1.0) become contrastive triplets that fine-tune ``base_model``. The candidate
    is then canary-evaluated on ``eval_dataset_path`` and the ``corpus.active`` alias
    is swapped only on a positive recall@10 lift. State transitions are recorded on
    the ``embedding_jobs`` row throughout.
    """
    corpus = load_corpus(corpus_path)
    store = _build_store(db_path)
    async with store:
        job = await store.create_embedding_job(base_model, train_config.to_dict())
        job_id = job.id
        output_dir = str(Path(models_dir) / job_id)

        # --- Phase 1: mining run — eval the train split, persist results + spans ---
        await store.update_embedding_job(job_id, status="mining")
        train_dataset = load_dataset(train_dataset_path)
        adapter, retriever = await _open_retriever(
            qdrant_url=qdrant_url,
            qdrant_path=qdrant_path,
            bm25_path=bm25_path,
            span_logger=span_logger,
            collection_alias=collection_alias,
        )
        async with adapter:
            deps = _make_deps(retriever, top_k=top_k, model=model, span_logger=span_logger)
            with using_research_deps(deps):
                report = await evaluate(
                    _run_workflow,
                    train_dataset,
                    scorers,
                    concurrency=concurrency,
                    store=store,
                    eval_id=f"finetune-mining-{job_id}",
                    tolerate_failures=True,
                )
            skipped = report.examples_skipped
            eval_results = await store.get_eval_results(report.eval_id)
            cases = mine_failures(train_dataset, eval_results, corpus, spans_path=spans_path)
            await store.save_failure_cases([to_store_row(case) for case in cases])
            if not cases:
                await store.update_embedding_job(job_id, status="archived")
                return FinetuneResult(
                    job_id, "no_failures", 0, 0, None, None, failures_skipped=skipped
                )

            # --- Phase 2: train — triplets → fine-tuned candidate checkpoint ---
            await store.update_embedding_job(job_id, status="training")
            triplets = build_triplets(
                cases, corpus, negatives_per_query=train_config.negatives_per_query
            )
            if not triplets:
                await store.update_embedding_job(job_id, status="archived")
                return FinetuneResult(
                    job_id, "no_triplets", len(cases), 0, None, None, failures_skipped=skipped
                )
            train_embedding(
                triplets,
                output_dir,
                base_model=base_model,
                config=train_config,
                train_fn=_train_backend(),
            )
            await store.update_embedding_job(
                job_id, status="training", triplets_count=len(triplets)
            )

            # --- Phase 3: promote — canary-eval on the dev split, swap on lift ---
            eval_dataset = load_dataset(eval_dataset_path)
            index_fn, retrieve_fn = _build_promotion_backends(
                checkpoint_dir=output_dir,
                adapter=adapter,
                candidate_collection=candidate_collection(job_id),
                bm25_path=bm25_path,
                top_k=top_k,
                span_logger=span_logger,
            )
            promote_result = await promote_candidate(
                output_dir,
                corpus_path,
                eval_dataset,
                job_id,
                adapter=adapter,
                store=store,
                config=promote_config,
                index_fn=index_fn,
                retrieve_fn=retrieve_fn,
                active_alias=promotion_alias,
            )

        return FinetuneResult(
            job_id=job_id,
            status=promote_result.status,
            failures_mined=len(cases),
            triplets_built=len(triplets),
            output_dir=output_dir,
            metrics=promote_result.metrics,
            failures_skipped=skipped,
        )


async def _run_one(
    *,
    question: str,
    qdrant_url: str,
    qdrant_path: str | None,
    bm25_path: str,
    top_k: int,
    model: str | None,
    span_logger: SpanLogger,
) -> Answer:
    adapter, retriever = await _open_retriever(
        qdrant_url=qdrant_url, qdrant_path=qdrant_path, bm25_path=bm25_path, span_logger=span_logger
    )
    deps = _make_deps(retriever, top_k=top_k, model=model, span_logger=span_logger)
    async with adapter:
        with using_research_deps(deps):
            return await deep_research.local(question=question)


def _span_logger(spans: str | None) -> SpanLogger:
    return SpanLogger(spans) if spans else SpanLogger()


# --- Click commands ----------------------------------------------------------


@click.group()
def cli() -> None:
    """Helix vertical-slice command line."""


@cli.command()
@click.option("--corpus", required=True, help="JSONL corpus path.")
@click.option("--collection", default="corpus.base", show_default=True)
@click.option("--qdrant-url", default=DEFAULT_QDRANT_URL, show_default=True)
@click.option("--qdrant-path", default=None, help="Embedded on-disk Qdrant dir (no server/Docker).")
@click.option("--alias", default="corpus.active", show_default=True)
@click.option("--bm25", "bm25_path", default=DEFAULT_BM25_PATH, show_default=True)
@click.option("--vector-size", default=768, show_default=True, type=int)
@click.option("--max-tokens", default=512, show_default=True, type=int)
@click.option("--overlap-tokens", default=64, show_default=True, type=int)
@click.option("--spans", default=None, help="Span JSONL path (default data/spans.jsonl).")
def index(
    corpus: str,
    collection: str,
    qdrant_url: str,
    qdrant_path: str | None,
    alias: str,
    bm25_path: str,
    vector_size: int,
    max_tokens: int,
    overlap_tokens: int,
    spans: str | None,
) -> None:
    """Chunk, embed, and index a corpus into Qdrant (+ BM25 sidecar)."""
    result = asyncio.run(
        _index(
            corpus=corpus,
            collection=collection,
            qdrant_url=qdrant_url,
            qdrant_path=qdrant_path,
            alias=alias,
            bm25_path=bm25_path,
            vector_size=vector_size,
            max_tokens=max_tokens,
            overlap_tokens=overlap_tokens,
            span_logger=_span_logger(spans),
        )
    )
    click.echo(
        f"Indexed {result.docs} docs / {result.chunks} chunks into "
        f"{result.collection} (alias {result.alias}); BM25 -> {bm25_path}"
    )


@cli.command()
@click.option("--workflow", default="deep_research", show_default=True)
@click.option("--dataset", required=True, help="Eval dataset JSONL path.")
@click.option(
    "--scorers",
    default="answer_f1,citation_precision,retrieval_recall@10",
    show_default=True,
)
@click.option("--concurrency", default=4, show_default=True, type=int)
@click.option("--output", default=DEFAULT_BASELINE, show_default=True)
@click.option("--qdrant-url", default=DEFAULT_QDRANT_URL, show_default=True)
@click.option("--qdrant-path", default=None, help="Embedded on-disk Qdrant dir (no server/Docker).")
@click.option("--bm25", "bm25_path", default=DEFAULT_BM25_PATH, show_default=True)
@click.option("--top-k", default=10, show_default=True, type=int)
@click.option("--model", default=None, help="Override the LLM (default LiteLLM model).")
@click.option("--no-cache", is_flag=True, help="Disable the LLM response cache.")
@click.option("--spans", default=None, help="Span JSONL path (default data/spans.jsonl).")
def eval(
    workflow: str,
    dataset: str,
    scorers: str,
    concurrency: int,
    output: str,
    qdrant_url: str,
    qdrant_path: str | None,
    bm25_path: str,
    top_k: int,
    model: str | None,
    no_cache: bool,
    spans: str | None,
) -> None:
    """Run the workflow over a dataset and write the baseline JSON report."""
    _check_workflow(workflow)
    if no_cache:
        os.environ["HELIX_LLM_CACHE"] = "0"
    scorer_map = _resolve_scorers(scorers)
    report, examples, resolved_model, embedding_model = asyncio.run(
        _eval(
            dataset_path=dataset,
            scorers=scorer_map,
            concurrency=concurrency,
            qdrant_url=qdrant_url,
            qdrant_path=qdrant_path,
            bm25_path=bm25_path,
            top_k=top_k,
            model=model,
            span_logger=_span_logger(spans),
        )
    )
    payload: dict[str, Any] = {
        "eval_id": report.eval_id,
        "workflow": workflow,
        "dataset": Path(dataset).stem,
        "n": len(examples),
        "metrics": report.metrics,
        "per_example": report.per_example,
        "model": resolved_model,
        "embedding_model": embedding_model,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    click.echo(f"Wrote {output} ({len(examples)} examples)")
    for name, agg in report.metrics.items():
        click.echo(f"  {name}: {agg['mean']:.4f} [{agg['ci_low']:.4f}, {agg['ci_high']:.4f}]")


@cli.command()
@click.option(
    "--train", "train_dataset", required=True, help="Mining split (JSONL) to fine-tune on."
)
@click.option("--eval", "eval_dataset", required=True, help="Dev split (JSONL) for the canary.")
@click.option("--corpus", required=True, help="JSONL corpus path (doc_id → text lookup).")
@click.option(
    "--scorers",
    default="answer_f1,citation_precision,retrieval_recall@10",
    show_default=True,
)
@click.option("--concurrency", default=4, show_default=True, type=int)
@click.option("--qdrant-url", default=DEFAULT_QDRANT_URL, show_default=True)
@click.option("--qdrant-path", default=None, help="Embedded on-disk Qdrant dir (no server/Docker).")
@click.option("--bm25", "bm25_path", default=DEFAULT_BM25_PATH, show_default=True)
@click.option("--top-k", default=10, show_default=True, type=int)
@click.option("--model", default=None, help="Override the LLM (default LiteLLM model).")
@click.option("--base-model", default="nomic-ai/nomic-embed-text-v1.5", show_default=True)
@click.option("--models-dir", default=DEFAULT_MODELS_DIR, show_default=True)
@click.option("--db", "db_path", default=DEFAULT_DB_PATH, show_default=True)
@click.option("--epochs", default=3, show_default=True, type=int)
# batch_size=4 keeps MultipleNegativesRankingLoss training within the slice's
# 15 GB CPU box: peak RSS scales with batch_size * (anchor+positive+negatives) *
# seq_len * layers, and 16 OOM-killed the run mid-fit (~16 GB). 4 peaks ~7 GB.
# Raise it on a GPU / larger-memory host where headroom allows.
@click.option("--batch-size", default=4, show_default=True, type=int)
@click.option("--lr", default=2e-5, show_default=True, type=float)
@click.option("--seed", default=0, show_default=True, type=int)
@click.option("--no-cache", is_flag=True, help="Disable the LLM response cache.")
@click.option("--spans", default=None, help="Span JSONL path (default data/spans.jsonl).")
@click.option(
    "--collection",
    "collection_alias",
    default="corpus.active",
    show_default=True,
    help="Qdrant collection/alias for the base (before) arm of mining and canary.",
)
@click.option(
    "--promotion-alias",
    default="corpus.active",
    show_default=True,
    help="Alias to swap on promotion (default corpus.active; use corpus.bright.active for BRIGHT).",
)
def finetune(
    train_dataset: str,
    eval_dataset: str,
    corpus: str,
    scorers: str,
    concurrency: int,
    qdrant_url: str,
    qdrant_path: str | None,
    bm25_path: str,
    top_k: int,
    model: str | None,
    base_model: str,
    models_dir: str,
    db_path: str,
    epochs: int,
    batch_size: int,
    lr: float,
    seed: int,
    no_cache: bool,
    spans: str | None,
    collection_alias: str,
    promotion_alias: str,
) -> None:
    """Mine retrieval failures on --train, fine-tune the embedder, canary-promote on --eval."""
    if no_cache:
        os.environ["HELIX_LLM_CACHE"] = "0"
    scorer_map = _resolve_scorers(scorers)
    train_config = TrainConfig(lr=lr, batch_size=batch_size, epochs=epochs, seed=seed)
    promote_config = PromoteConfig(top_k=top_k, seed=seed)
    logger = _span_logger(spans)
    result = asyncio.run(
        _finetune(
            train_dataset_path=train_dataset,
            eval_dataset_path=eval_dataset,
            corpus_path=corpus,
            scorers=scorer_map,
            concurrency=concurrency,
            qdrant_url=qdrant_url,
            qdrant_path=qdrant_path,
            bm25_path=bm25_path,
            top_k=top_k,
            model=model,
            base_model=base_model,
            models_dir=models_dir,
            db_path=db_path,
            spans_path=spans or "data/spans.jsonl",
            train_config=train_config,
            promote_config=promote_config,
            span_logger=logger,
            collection_alias=collection_alias,
            promotion_alias=promotion_alias,
        )
    )
    click.echo(f"Fine-tune job {result.job_id}: {result.status}")
    click.echo(f"  failures mined: {result.failures_mined}")
    click.echo(f"  triplets built: {result.triplets_built}")
    if result.failures_skipped:
        click.echo(f"  examples skipped (provider errors): {result.failures_skipped}")
    if result.metrics is not None:
        before = result.metrics["before"]["mean"]
        after = result.metrics["after"]["mean"]
        delta = result.metrics["delta_mean"]
        click.echo(f"  recall@{top_k}: {before:.4f} → {after:.4f} (Δ {delta:+.4f})")
    if result.output_dir is not None:
        click.echo(f"  checkpoint: {result.output_dir}")


@cli.command()
@click.option("--workflow", default="deep_research", show_default=True)
@click.option("--input", "input_json", required=True, help='JSON, e.g. {"question": "..."}.')
@click.option("--qdrant-url", default=DEFAULT_QDRANT_URL, show_default=True)
@click.option("--qdrant-path", default=None, help="Embedded on-disk Qdrant dir (no server/Docker).")
@click.option("--bm25", "bm25_path", default=DEFAULT_BM25_PATH, show_default=True)
@click.option("--top-k", default=10, show_default=True, type=int)
@click.option("--model", default=None)
@click.option("--no-cache", is_flag=True)
@click.option("--spans", default=None)
def run(
    workflow: str,
    input_json: str,
    qdrant_url: str,
    qdrant_path: str | None,
    bm25_path: str,
    top_k: int,
    model: str | None,
    no_cache: bool,
    spans: str | None,
) -> None:
    """Answer a single question with the workflow."""
    _check_workflow(workflow)
    if no_cache:
        os.environ["HELIX_LLM_CACHE"] = "0"
    parsed = json.loads(input_json)
    question = parsed.get("question")
    if not isinstance(question, str):
        raise click.BadParameter("input must be a JSON object with a string 'question'")
    answer = asyncio.run(
        _run_one(
            question=question,
            qdrant_url=qdrant_url,
            qdrant_path=qdrant_path,
            bm25_path=bm25_path,
            top_k=top_k,
            model=model,
            span_logger=_span_logger(spans),
        )
    )
    click.echo(answer.text)
    for citation in answer.citations:
        click.echo(f"  [{citation.doc_id}]")


if __name__ == "__main__":
    cli()
