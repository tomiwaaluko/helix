"""Embedder: task prefixes, batching, dimensions, and span attributes."""

import json
from pathlib import Path

from helix.logging import SpanLogger
from helix.tools.embedder import DOCUMENT_PREFIX, QUERY_PREFIX, Embedder


class StubEncoder:
    """Records the (prefixed) texts it sees and returns fixed 768-dim vectors."""

    def __init__(self, dimension: int = 768) -> None:
        self.seen: list[str] = []
        self.batch_sizes: list[int] = []
        self._dim = dimension

    def __call__(self, texts: list[str], batch_size: int) -> list[list[float]]:
        self.seen.extend(texts)
        self.batch_sizes.append(len(texts))
        return [[float(i)] * self._dim for i in range(len(texts))]


def test_embed_queries_prefixes_and_dimension(tmp_path: Path) -> None:
    stub = StubEncoder()
    emb = Embedder(span_logger=SpanLogger(tmp_path / "s.jsonl"), encode_fn=stub)

    vectors = emb.embed_queries([f"q{i}" for i in range(10)])

    assert len(vectors) == 10
    assert all(len(v) == 768 for v in vectors)
    assert stub.seen == [f"{QUERY_PREFIX}q{i}" for i in range(10)]


def test_embed_documents_uses_document_prefix(tmp_path: Path) -> None:
    stub = StubEncoder()
    emb = Embedder(span_logger=SpanLogger(tmp_path / "s.jsonl"), encode_fn=stub)

    emb.embed_documents(["a", "b"])

    assert stub.seen == [f"{DOCUMENT_PREFIX}a", f"{DOCUMENT_PREFIX}b"]


def test_batches_respect_batch_size(tmp_path: Path) -> None:
    stub = StubEncoder()
    emb = Embedder(batch_size=4, span_logger=SpanLogger(tmp_path / "s.jsonl"), encode_fn=stub)

    vectors = emb.embed_documents([f"d{i}" for i in range(10)])

    assert len(vectors) == 10
    assert stub.batch_sizes == [4, 4, 2]  # 10 texts in batches of 4


def test_emits_internal_span_with_attributes(tmp_path: Path) -> None:
    spans_path = tmp_path / "s.jsonl"
    emb = Embedder(span_logger=SpanLogger(spans_path), encode_fn=StubEncoder())

    emb.embed_queries(["x", "y", "z"])

    (span,) = [json.loads(line) for line in spans_path.read_text().splitlines()]
    assert span["kind"] == "internal"
    assert span["name"] == "embed_queries"
    assert span["attributes"]["count"] == 3
    assert span["attributes"]["dimension"] == 768
    assert span["attributes"]["model"] == "nomic-ai/nomic-embed-text-v1.5"
