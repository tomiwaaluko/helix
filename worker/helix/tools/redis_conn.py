"""Shared async Redis client factory.

Redis is *ephemeral* infrastructure (rate-limit counters, exactly-once sentinels) —
never a system of record. Every consumer accepts ``Redis | None`` and degrades to a
no-op when Redis is not configured, so local ``make eval`` and unit tests never touch
a server.

Call ``get_redis(os.environ.get("REDIS_URL"))`` once at worker startup and pass the
result down explicitly (the ``configure_otel`` pattern — no global singletons).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis


def get_redis(url: str | None) -> Redis | None:
    """Build an async Redis client from a connection URL.

    Returns ``None`` when *url* is falsy, which every consumer treats as
    "Redis disabled — degrade gracefully". ``decode_responses=False`` keeps
    values as bytes so callers control encoding. The ``redis`` import is local
    because it eagerly pulls in OpenTelemetry; deferring it keeps that off the
    import path of code that never configures Redis.
    """
    if not url:
        return None
    from redis.asyncio import Redis

    return Redis.from_url(url, decode_responses=False)
