"""Embedding client — Nomic Embed v1.5 via sentence-transformers.

Nomic ships task-prefix tokens that materially change the embedding space:
queries are embedded as ``search_query: <text>`` and corpus passages as
``search_document: <text>``. Mixing them up silently degrades retrieval, so the
prefixes live here and nowhere else — callers pass raw text.

The sentence-transformers model (~0.5 GB download) is loaded lazily on first
encode and is injectable, so unit tests run with a stub instead of the model.
Encoding is synchronous; per repo conventions sync code is fine inside async
tasks, and the heavy work is in native code that releases the GIL.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import cast

from helix.logging import SpanLogger

QUERY_PREFIX = "search_query: "
DOCUMENT_PREFIX = "search_document: "

# encode(texts, batch_size) -> sequence of vectors
EncodeFn = Callable[[list[str], int], Sequence[Sequence[float]]]

_DEFAULT_LOGGER = SpanLogger()


class Embedder:
    """Batched text embedding with Nomic task prefixes."""

    def __init__(
        self,
        model_name: str = "nomic-ai/nomic-embed-text-v1.5",
        *,
        batch_size: int = 64,
        span_logger: SpanLogger | None = None,
        encode_fn: EncodeFn | None = None,
    ) -> None:
        self.model_name = model_name
        self._batch_size = batch_size
        self._spans = span_logger or _DEFAULT_LOGGER
        self._encode = encode_fn

    def _load(self) -> EncodeFn:
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer(self.model_name, trust_remote_code=True)

        def encode(texts: list[str], batch_size: int) -> Sequence[Sequence[float]]:
            vectors = model.encode(texts, batch_size=batch_size, show_progress_bar=False)
            return cast("Sequence[Sequence[float]]", vectors.tolist())

        return encode

    def _embed(self, texts: list[str], prefix: str, span_name: str) -> list[list[float]]:
        if self._encode is None:
            self._encode = self._load()
        prefixed = [prefix + t for t in texts]
        with self._spans.span(span_name, kind="internal") as attrs:
            attrs["model"] = self.model_name
            attrs["count"] = len(texts)
            vectors: list[list[float]] = []
            for start in range(0, len(prefixed), self._batch_size):
                batch = prefixed[start : start + self._batch_size]
                vectors.extend([list(v) for v in self._encode(batch, self._batch_size)])
            attrs["dimension"] = len(vectors[0]) if vectors else 0
        return vectors

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        """Embed search queries (``search_query:`` prefix)."""
        return self._embed(texts, QUERY_PREFIX, "embed_queries")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed corpus passages (``search_document:`` prefix)."""
        return self._embed(texts, DOCUMENT_PREFIX, "embed_documents")
