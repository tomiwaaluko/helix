"""Hybrid retriever integration: index → dense+sparse → RRF → rerank → Docs."""

import json
from pathlib import Path

import pytest

pytest.importorskip("qdrant_client")

from qdrant_client import AsyncQdrantClient  # noqa: E402

from helix.logging import SpanLogger  # noqa: E402
from helix.rag.chunker import chunk_document  # noqa: E402
from helix.rag.indexer import index_corpus  # noqa: E402
from helix.rag.retriever import HybridRetriever  # noqa: E402
from helix.tools.bm25 import BM25Index  # noqa: E402
from helix.tools.embedder import Embedder  # noqa: E402
from helix.tools.qdrant_adapter import QdrantAdapter  # noqa: E402
from helix.tools.reranker import Reranker  # noqa: E402

_DIM = 8

_DOCS = [
    {"id": "d0", "text": "the quick brown fox jumps over the lazy dog", "source": "s"},
    {"id": "d1", "text": "apple banana cherry orange fruit basket", "source": "s"},
    {"id": "d2", "text": "quick apple pie recipe with cinnamon", "source": "s"},
    {"id": "d3", "text": "the history of ancient rome and its emperors", "source": "s"},
]


def _words(text: str) -> int:
    return len(text.split())


def _encode(texts: list[str], batch_size: int) -> list[list[float]]:
    return [[float(sum(map(ord, t)) % 13 + 1), *([1.0] * (_DIM - 1))] for t in texts]


def _overlap(pairs: list[tuple[str, str]]) -> list[float]:
    # Term-overlap stand-in for the cross-encoder: shared lowercased words.
    return [float(len(set(q.lower().split()) & set(p.lower().split()))) for q, p in pairs]


async def test_hybrid_retrieve_end_to_end(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text("\n".join(json.dumps(d) for d in _DOCS), encoding="utf-8")
    logger = SpanLogger(tmp_path / "spans.jsonl")
    embedder = Embedder(span_logger=logger, encode_fn=_encode)
    adapter = QdrantAdapter(client=AsyncQdrantClient(location=":memory:"), span_logger=logger)
    reranker = Reranker(predict_fn=_overlap)

    async with adapter:
        await index_corpus(
            corpus,
            "corpus.base",
            embedder=embedder,
            adapter=adapter,
            vector_size=_DIM,
            token_counter=_words,
            span_logger=logger,
        )
        chunks = [
            chunk
            for doc in _DOCS
            for chunk in chunk_document(doc["id"], doc["text"], doc["source"], token_counter=_words)
        ]
        retriever = HybridRetriever(
            embedder, adapter, BM25Index.build(chunks), reranker, span_logger=logger
        )

        results = await retriever.retrieve("apple", top_k=2)

    # Reranker (term overlap) puts the two "apple" docs on top.
    assert 1 <= len(results) <= 2
    assert {r.metadata["doc_id"] for r in results} <= {"d1", "d2"}
    assert all(set(r.metadata) == {"doc_id", "chunk_id"} for r in results)
    # Doc.id is the corpus doc_id (not the chunk_id).
    assert all(r.id == r.metadata["doc_id"] for r in results)

    spans = [json.loads(line) for line in (tmp_path / "spans.jsonl").read_text().splitlines()]
    retrieve_span = next(s for s in spans if s["name"] == "retrieve")
    assert retrieve_span["kind"] == "retrieval"
    assert retrieve_span["attributes"]["retriever"] == "hybrid+reranked"
    assert retrieve_span["attributes"]["top_k"] == 2
    assert len(retrieve_span["attributes"]["results"]) == len(results)
