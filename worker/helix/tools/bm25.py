"""BM25 sparse retrieval over the chunk corpus.

An in-memory `rank_bm25.BM25Okapi` index that complements dense Qdrant search in
the hybrid retriever. Tokenization is deliberately simple — lowercase whitespace
split — which is adequate for the slice; production would use tantivy.

The built index pickles to ``data/bm25_index.pkl`` so it can be reused across
runs without re-tokenizing the corpus.
"""

from __future__ import annotations

import pickle
from dataclasses import dataclass
from os import PathLike
from pathlib import Path

from rank_bm25 import BM25Okapi

from helix.rag.chunker import Chunk


@dataclass
class ScoredChunk:
    chunk_id: str
    doc_id: str
    text: str
    source: str
    score: float


def _tokenize(text: str) -> list[str]:
    return text.lower().split()


class BM25Index:
    """In-memory BM25 index over chunks, with pickle persistence."""

    def __init__(self) -> None:
        self._chunks: list[Chunk] = []
        self._tokenized: list[list[str]] = []
        self._bm25: BM25Okapi | None = None

    @classmethod
    def build(cls, chunks: list[Chunk]) -> BM25Index:
        index = cls()
        index._chunks = list(chunks)
        index._tokenized = [_tokenize(c.text) for c in index._chunks]
        # BM25Okapi divides by the average document length, so an empty corpus
        # has no valid index; searches return nothing.
        index._bm25 = BM25Okapi(index._tokenized) if index._tokenized else None
        return index

    def search(self, query: str, top_k: int = 10) -> list[ScoredChunk]:
        if self._bm25 is None:
            return []
        scores = self._bm25.get_scores(_tokenize(query))
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]
        return [
            ScoredChunk(
                chunk_id=self._chunks[i].chunk_id,
                doc_id=self._chunks[i].doc_id,
                text=self._chunks[i].text,
                source=self._chunks[i].source,
                score=float(scores[i]),
            )
            for i in order
        ]

    def save(self, path: str | PathLike[str]) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as handle:
            pickle.dump((self._chunks, self._tokenized, self._bm25), handle)

    @classmethod
    def load(cls, path: str | PathLike[str]) -> BM25Index:
        with Path(path).open("rb") as handle:
            chunks, tokenized, bm25 = pickle.load(handle)  # noqa: S301 — local cache we wrote
        index = cls()
        index._chunks = chunks
        index._tokenized = tokenized
        index._bm25 = bm25
        return index
