"""Indexer integration: JSONL corpus → chunk → embed → Qdrant (in-memory)."""

import json
from pathlib import Path

import pytest

pytest.importorskip("qdrant_client")

from qdrant_client import AsyncQdrantClient  # noqa: E402

from helix.logging import SpanLogger  # noqa: E402
from helix.rag.indexer import index_corpus  # noqa: E402
from helix.tools.embedder import Embedder  # noqa: E402
from helix.tools.qdrant_adapter import QdrantAdapter  # noqa: E402

_DIM = 8


def _words(text: str) -> int:
    return len(text.split())


def _stub_encode(texts: list[str], batch_size: int) -> list[list[float]]:
    # Deterministic, non-zero, slightly varied vectors keyed off text length.
    return [[float(len(t) % 7 + 1)] + [1.0] * (_DIM - 1) for t in texts]


def _write_corpus(path: Path, n: int) -> None:
    lines = [
        json.dumps({"id": f"doc{i}", "text": f"This is document number {i}.", "source": "test"})
        for i in range(n)
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


async def test_index_corpus_is_searchable_via_alias(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus.jsonl"
    _write_corpus(corpus, 100)
    logger = SpanLogger(tmp_path / "spans.jsonl")
    embedder = Embedder(span_logger=logger, encode_fn=_stub_encode)
    adapter = QdrantAdapter(
        client=AsyncQdrantClient(location=":memory:"),
        collection_alias="corpus.active",
        span_logger=logger,
    )

    async with adapter:
        result = await index_corpus(
            corpus,
            "corpus.base",
            embedder=embedder,
            adapter=adapter,
            vector_size=_DIM,
            token_counter=_words,
            span_logger=logger,
        )

        assert result.docs == 100
        assert result.chunks == 100  # each short doc is a single chunk
        assert result.collection == "corpus.base"
        assert result.alias == "corpus.active"

        # Searchable through the alias, with the expected payload fields.
        hits = await adapter.search([1.0] * _DIM, top_k=5)
        assert len(hits) == 5
        payload = hits[0].payload
        assert set(payload) == {"doc_id", "chunk_id", "source", "text"}
        assert payload["chunk_id"].endswith("#chunk0")
        assert payload["source"] == "test"

    # Parent index span recorded the summary, and the embed/upsert spans nest under it.
    spans = [json.loads(line) for line in (tmp_path / "spans.jsonl").read_text().splitlines()]
    index_span = next(s for s in spans if s["name"] == "index_corpus")
    assert index_span["attributes"]["docs"] == 100
    assert index_span["attributes"]["chunks"] == 100
    children = [s for s in spans if s["parent_span_id"] == index_span["span_id"]]
    assert {"embed_documents", "qdrant_upsert"} <= {c["name"] for c in children}


async def test_index_empty_corpus(tmp_path: Path) -> None:
    corpus = tmp_path / "empty.jsonl"
    corpus.write_text("", encoding="utf-8")
    logger = SpanLogger(tmp_path / "spans.jsonl")
    adapter = QdrantAdapter(client=AsyncQdrantClient(location=":memory:"), span_logger=logger)
    async with adapter:
        result = await index_corpus(
            corpus,
            "corpus.base",
            embedder=Embedder(span_logger=logger, encode_fn=_stub_encode),
            adapter=adapter,
            vector_size=_DIM,
            token_counter=_words,
            span_logger=logger,
        )
        assert result.docs == 0
        assert result.chunks == 0
        # Collection exists and alias resolves even with no points.
        assert await adapter.search([1.0] * _DIM, top_k=5) == []
