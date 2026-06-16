"""Hybrid retriever: dense (Qdrant) + sparse (BM25) → RRF → BGE rerank.

The full retrieval path. Dense and sparse candidates are fused with Reciprocal
Rank Fusion (RRF, ``k=60``) — which combines by *rank* so the two very different
score scales never need calibrating — then the fused candidates are reranked by
the cross-encoder and the top-k returned as ``Doc``s.

Each returned ``Doc`` carries ``metadata["doc_id"]`` so the workflow can populate
``Answer.metadata["retrieved_doc_ids"]`` for the recall scorer.

When the OTel SDK is configured (``OTEL_EXPORTER_OTLP_ENDPOINT`` is set), each
``retrieve`` call also emits a lightweight OTel span so the Go collector can fan
the data into the ClickHouse ``retrievals`` table for dashboard visibility and
offline mining via ``mine_from_clickhouse``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from helix.logging import SpanLogger
from helix.otel import OtelSpanExporter, is_configured
from helix.runtime.context import current_eval_run_id
from helix.tools.bm25 import BM25Index
from helix.tools.embedder import Embedder
from helix.tools.qdrant_adapter import QdrantAdapter
from helix.tools.reranker import Reranker
from helix.types import Doc

_DEFAULT_LOGGER = SpanLogger()


@dataclass
class _Candidate:
    chunk_id: str
    doc_id: str
    text: str
    source: str


def _rrf_fuse(
    dense: list[tuple[str, str, str, str]],
    sparse: list[tuple[str, str, str, str]],
    k: int,
) -> list[_Candidate]:
    """Fuse two ranked lists of (chunk_id, doc_id, text, source) by RRF."""
    scores: dict[str, float] = {}
    candidates: dict[str, _Candidate] = {}
    for ranked in (dense, sparse):
        for rank, (chunk_id, doc_id, text, source) in enumerate(ranked, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
            candidates.setdefault(chunk_id, _Candidate(chunk_id, doc_id, text, source))
    return sorted(candidates.values(), key=lambda c: scores[c.chunk_id], reverse=True)


def _emit_retrieval_otel(attrs: dict[str, Any]) -> None:
    """Emit a lightweight OTel span carrying finalized retrieval attrs.

    Called after the SpanLogger span closes so all attrs are populated.
    No-op when OTel is not configured (OTEL_EXPORTER_OTLP_ENDPOINT unset).
    The span duration is near-zero — it exists purely as a structured event for
    the collector's retrieval fan-out, not as a performance trace.
    """
    if not is_configured():
        return
    otel_attrs: dict[str, str] = {
        "kind": "retrieval",
        "query": str(attrs.get("query", "")),
        "retriever": str(attrs.get("retriever", "")),
        "top_k": str(attrs.get("top_k", 0)),
        "results": json.dumps(attrs.get("results", [])),
        "run_id": current_eval_run_id.get(),
    }
    with OtelSpanExporter("retrieve", otel_attrs):
        pass


class HybridRetriever:
    """Dense + sparse retrieval with RRF fusion and cross-encoder reranking."""

    def __init__(
        self,
        embedder: Embedder,
        adapter: QdrantAdapter,
        bm25: BM25Index,
        reranker: Reranker,
        *,
        span_logger: SpanLogger | None = None,
    ) -> None:
        self._embedder = embedder
        self._adapter = adapter
        self._bm25 = bm25
        self._reranker = reranker
        self._spans = span_logger or _DEFAULT_LOGGER

    async def retrieve(
        self,
        query: str,
        top_k: int = 10,
        *,
        dense_k: int = 50,
        sparse_k: int = 50,
        fuse_k: int = 50,
        rrf_k: int = 60,
    ) -> list[Doc]:
        with self._spans.span("retrieve", kind="retrieval") as attrs:
            query_vector = self._embedder.embed_queries([query])[0]
            dense_hits = await self._adapter.search(query_vector, top_k=dense_k)
            sparse_hits = self._bm25.search(query, top_k=sparse_k)

            dense = [
                (
                    str(h.payload.get("chunk_id")),
                    str(h.payload.get("doc_id")),
                    str(h.payload.get("text", "")),
                    str(h.payload.get("source", "")),
                )
                for h in dense_hits
            ]
            sparse = [(h.chunk_id, h.doc_id, h.text, h.source) for h in sparse_hits]

            fused = _rrf_fuse(dense, sparse, rrf_k)[:fuse_k]
            ranked = self._reranker.rerank(query, [c.text for c in fused], top_k=top_k)

            docs = [
                Doc(
                    # Doc.id is the corpus doc_id (recall is measured at doc level,
                    # and citations reference doc_id); chunk_id lives in metadata.
                    id=fused[r.index].doc_id,
                    text=fused[r.index].text,
                    source=fused[r.index].source,
                    score=r.score,
                    metadata={
                        "doc_id": fused[r.index].doc_id,
                        "chunk_id": fused[r.index].chunk_id,
                    },
                )
                for r in ranked
            ]

            attrs["query"] = query
            attrs["retriever"] = "hybrid+reranked"
            attrs["top_k"] = top_k
            attrs["results"] = [
                {
                    "chunk_id": d.metadata["chunk_id"],
                    "doc_id": d.metadata["doc_id"],
                    "score": d.score,
                }
                for d in docs
            ]

        # Dual-write: emit OTel span for collector fan-out after JSONL span closes.
        _emit_retrieval_otel(attrs)
        return docs
