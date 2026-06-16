"""Qdrant adapter — async wrapper for upsert and vector search.

All reads and writes go through a collection *alias* (default ``corpus.active``)
rather than a concrete collection name. Embedding promotions flip the alias to a
new collection, so code that talks to the alias keeps working across promotions
without change. ``create_collection`` and ``set_alias`` manage the indirection.

The Qdrant client is lazily imported and injectable, so unit tests can drive a
local in-memory client (``AsyncQdrantClient(location=":memory:")``) with no
Docker or network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import TracebackType
from typing import TYPE_CHECKING, Any

from helix.logging import SpanLogger

if TYPE_CHECKING:
    from qdrant_client import AsyncQdrantClient
    from qdrant_client.models import Filter

_DEFAULT_LOGGER = SpanLogger()


@dataclass
class Point:
    """A vector to upsert, with its id and payload."""

    id: str | int
    vector: list[float]
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScoredPoint:
    """A search hit: id, similarity score, and stored payload."""

    id: str | int
    score: float
    payload: dict[str, Any] = field(default_factory=dict)


class QdrantAdapter:
    """Async upsert/search over a Qdrant collection alias."""

    def __init__(
        self,
        url: str | None = None,
        *,
        collection_alias: str = "corpus.active",
        client: AsyncQdrantClient | None = None,
        span_logger: SpanLogger | None = None,
    ) -> None:
        self._alias = collection_alias
        self._spans = span_logger or _DEFAULT_LOGGER
        if client is None:
            from qdrant_client import AsyncQdrantClient

            if url is None:
                raise ValueError("QdrantAdapter requires either a url or an injected client")
            client = AsyncQdrantClient(url=url)
        self._client = client

    def for_collection(self, collection_alias: str) -> QdrantAdapter:
        """A sibling adapter over the same client, scoped to a different collection/alias.

        Used by the promotion canary to query a specific ``corpus.candidate.<job_id>``
        collection through the same hybrid retrieval code that talks to ``corpus.active``.
        """
        return QdrantAdapter(
            client=self._client, collection_alias=collection_alias, span_logger=self._spans
        )

    async def create_collection(
        self, name: str, vector_size: int, distance: str = "Cosine"
    ) -> None:
        from qdrant_client import models

        await self._client.create_collection(
            collection_name=name,
            vectors_config=models.VectorParams(
                size=vector_size, distance=models.Distance[distance.upper()]
            ),
        )

    async def set_alias(self, alias: str, collection: str) -> None:
        from qdrant_client import models

        await self._client.update_collection_aliases(
            change_aliases_operations=[
                models.CreateAliasOperation(
                    create_alias=models.CreateAlias(collection_name=collection, alias_name=alias)
                )
            ]
        )

    async def upsert(self, points: list[Point], *, batch_size: int = 100) -> None:
        from qdrant_client import models

        with self._spans.span("qdrant_upsert", kind="internal") as attrs:
            attrs["alias"] = self._alias
            attrs["count"] = len(points)
            for start in range(0, len(points), batch_size):
                chunk = points[start : start + batch_size]
                await self._client.upsert(
                    collection_name=self._alias,
                    points=[
                        models.PointStruct(id=p.id, vector=p.vector, payload=p.payload)
                        for p in chunk
                    ],
                )

    async def search(
        self,
        query_vector: list[float],
        top_k: int = 10,
        filters: Filter | None = None,
    ) -> list[ScoredPoint]:
        with self._spans.span("qdrant_search", kind="retrieval") as attrs:
            attrs["alias"] = self._alias
            attrs["top_k"] = top_k
            response = await self._client.query_points(
                collection_name=self._alias,
                query=query_vector,
                limit=top_k,
                query_filter=filters,
            )
            results = [
                ScoredPoint(
                    id=hit.id if isinstance(hit.id, int | str) else str(hit.id),
                    score=hit.score,
                    payload=hit.payload or {},
                )
                for hit in response.points
            ]
            attrs["count"] = len(results)
        return results

    async def upsert_to(
        self, collection_name: str, points: list[Point], *, batch_size: int = 100
    ) -> None:
        """Upsert directly into a named collection, bypassing the alias."""
        from qdrant_client import models

        for start in range(0, len(points), batch_size):
            chunk = points[start : start + batch_size]
            await self._client.upsert(
                collection_name=collection_name,
                points=[
                    models.PointStruct(id=p.id, vector=p.vector, payload=p.payload) for p in chunk
                ],
            )

    async def search_in(
        self,
        collection_name: str,
        query_vector: list[float],
        top_k: int = 10,
    ) -> list[ScoredPoint]:
        """Search a named collection directly, bypassing the alias."""
        response = await self._client.query_points(
            collection_name=collection_name,
            query=query_vector,
            limit=top_k,
        )
        return [
            ScoredPoint(
                id=hit.id if isinstance(hit.id, int | str) else str(hit.id),
                score=hit.score,
                payload=hit.payload or {},
            )
            for hit in response.points
        ]

    async def aclose(self) -> None:
        await self._client.close()

    async def __aenter__(self) -> QdrantAdapter:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()
