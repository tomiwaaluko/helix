"""Hybrid retriever: dense (Qdrant) + sparse (BM25) → RRF → BGE rerank.

The full retrieval path. Dense and sparse candidates are fused with Reciprocal
Rank Fusion (RRF, ``k=60``) — which combines by *rank* so the two very different
score scales never need calibrating — then the fused candidates are reranked by
the cross-encoder and the top-k returned as ``Doc``s.

Each returned ``Doc`` carries ``metadata["doc_id"]`` so the workflow can populate
``Answer.metadata["retrieved_doc_ids"]`` for the recall scorer.
"""

from __future__ import annotations

from dataclasses import dataclass

from helix.logging import SpanLogger
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
        return docs
