"""Indexer — end-to-end corpus ingestion into Qdrant.

Loads a JSONL corpus, chunks each document, embeds all chunks, and upserts them
through the Qdrant alias, then points ``corpus.active`` at the new collection.

Qdrant point ids must be unsigned ints or UUIDs, but chunk ids are human strings
like ``doc1#chunk0``. We derive a deterministic ``uuid5`` from the chunk id for
the point id (so re-indexing overwrites rather than duplicates) and keep the
readable ``chunk_id`` in the payload.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import Any

from helix.logging import SpanLogger
from helix.rag.chunker import TokenCounter, chunk_document
from helix.tools.embedder import Embedder
from helix.tools.qdrant_adapter import Point, QdrantAdapter

_POINT_NAMESPACE = uuid.UUID("a4f0c8d2-6b1e-4e3a-9c7d-0e1f2a3b4c5d")
_DEFAULT_LOGGER = SpanLogger()


@dataclass
class IndexResult:
    docs: int
    chunks: int
    elapsed_s: float
    collection: str
    alias: str


def _point_id(chunk_id: str) -> str:
    return str(uuid.uuid5(_POINT_NAMESPACE, chunk_id))


def _load_jsonl(path: str | PathLike[str]) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped:
                docs.append(json.loads(stripped))
    return docs


async def index_corpus(
    corpus_path: str | PathLike[str],
    collection: str,
    *,
    embedder: Embedder,
    adapter: QdrantAdapter,
    alias: str = "corpus.active",
    vector_size: int = 768,
    distance: str = "Cosine",
    max_tokens: int = 512,
    overlap_tokens: int = 64,
    token_counter: TokenCounter | None = None,
    span_logger: SpanLogger | None = None,
) -> IndexResult:
    """Chunk, embed, and index a JSONL corpus; point ``alias`` at ``collection``."""
    logger = span_logger or _DEFAULT_LOGGER
    start = time.perf_counter()
    docs = _load_jsonl(corpus_path)

    with logger.span("index_corpus", kind="internal") as attrs:
        await adapter.create_collection(collection, vector_size=vector_size, distance=distance)
        # Point the alias at the new collection up front so the alias-routed upsert
        # below resolves — on a fresh index there is no prior alias to write through.
        await adapter.set_alias(alias, collection)

        chunks = [
            chunk
            for doc in docs
            for chunk in chunk_document(
                str(doc["id"]),
                str(doc["text"]),
                str(doc["source"]),
                max_tokens=max_tokens,
                overlap_tokens=overlap_tokens,
                token_counter=token_counter,
            )
        ]

        vectors = embedder.embed_documents([c.text for c in chunks]) if chunks else []
        points = [
            Point(
                id=_point_id(chunk.chunk_id),
                vector=vector,
                payload={
                    "doc_id": chunk.doc_id,
                    "chunk_id": chunk.chunk_id,
                    "source": chunk.source,
                    "text": chunk.text,
                },
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        if points:
            await adapter.upsert(points)

        elapsed = time.perf_counter() - start
        attrs["docs"] = len(docs)
        attrs["chunks"] = len(chunks)
        attrs["elapsed_s"] = round(elapsed, 4)

    return IndexResult(
        docs=len(docs),
        chunks=len(chunks),
        elapsed_s=elapsed,
        collection=collection,
        alias=alias,
    )
