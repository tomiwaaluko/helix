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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

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
from helix.rag.retriever import HybridRetriever
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
SUPPORTED_WORKFLOWS = ("deep_research",)


# --- Component factories (monkeypatched in tests) ----------------------------


def _build_embedder(span_logger: SpanLogger) -> Embedder:
    return Embedder(span_logger=span_logger)


def _build_adapter(url: str, span_logger: SpanLogger) -> QdrantAdapter:
    return QdrantAdapter(url=url, span_logger=span_logger)


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
    alias: str,
    bm25_path: str,
    vector_size: int,
    max_tokens: int,
    overlap_tokens: int,
    span_logger: SpanLogger,
) -> IndexResult:
    counter = _token_counter()
    embedder = _build_embedder(span_logger)
    async with _build_adapter(qdrant_url, span_logger) as adapter:
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
    *, qdrant_url: str, bm25_path: str, span_logger: SpanLogger
) -> tuple[QdrantAdapter, HybridRetriever]:
    embedder = _build_embedder(span_logger)
    bm25 = BM25Index.load(bm25_path)
    adapter = _build_adapter(qdrant_url, span_logger)
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
    bm25_path: str,
    top_k: int,
    model: str | None,
    span_logger: SpanLogger,
) -> tuple[EvalReport, list[Example], str, str]:
    dataset = load_dataset(dataset_path)
    adapter, retriever = await _open_retriever(
        qdrant_url=qdrant_url, bm25_path=bm25_path, span_logger=span_logger
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


async def _run_one(
    *,
    question: str,
    qdrant_url: str,
    bm25_path: str,
    top_k: int,
    model: str | None,
    span_logger: SpanLogger,
) -> Answer:
    adapter, retriever = await _open_retriever(
        qdrant_url=qdrant_url, bm25_path=bm25_path, span_logger=span_logger
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
@click.option("--workflow", default="deep_research", show_default=True)
@click.option("--input", "input_json", required=True, help='JSON, e.g. {"question": "..."}.')
@click.option("--qdrant-url", default=DEFAULT_QDRANT_URL, show_default=True)
@click.option("--bm25", "bm25_path", default=DEFAULT_BM25_PATH, show_default=True)
@click.option("--top-k", default=10, show_default=True, type=int)
@click.option("--model", default=None)
@click.option("--no-cache", is_flag=True)
@click.option("--spans", default=None)
def run(
    workflow: str,
    input_json: str,
    qdrant_url: str,
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
