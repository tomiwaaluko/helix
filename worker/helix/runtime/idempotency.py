"""Exactly-once execution sentinel backed by Redis.

NATS JetStream delivers task envelopes *at least once*; a worker can therefore see
the same ``(task_id, attempt_number)`` twice (redelivery after a slow ack, a
rebalance, etc.). The orchestrator's ``CompleteTask`` is already idempotent on that
pair, but the *handler body* would still run twice. This sentinel guards the body so
each attempt executes once.

Keying is on ``(task_id, attempt)``, not ``task_id`` alone: a genuine orchestrator
retry carries a new ``attempt_number`` and *must* run. Only duplicate deliveries of
the **same** attempt are deduplicated.

When Redis is not configured the sentinel is a no-op: ``begin`` always returns
``True`` and the system behaves exactly as it did in M2 (relying on orchestrator-side
idempotency). Local ``make eval`` is unaffected.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Imported only for type hints. `redis.asyncio` eagerly pulls in OpenTelemetry,
    # so we keep it out of the import path of `import helix` (the SDK surface).
    from redis.asyncio import Redis

_KEY_PREFIX = "helix:task"
_ONCE_PREFIX = "helix:once"


class Idempotency:
    """Redis ``SET NX PX`` sentinel for at-most-once handler execution."""

    def __init__(self, redis: Redis | None, *, ttl_s: int = 300) -> None:
        self._redis = redis
        self._ttl_ms = ttl_s * 1000

    @staticmethod
    def _key(task_id: str, attempt: int) -> str:
        return f"{_KEY_PREFIX}:{task_id}:{attempt}"

    async def begin(self, task_id: str, attempt: int, *, owner: str = "") -> bool:
        """Claim ``(task_id, attempt)``.

        Returns ``True`` if this caller acquired the claim (run the handler) or if
        Redis is disabled. Returns ``False`` if another delivery already holds it
        (skip — duplicate).
        """
        if self._redis is None:
            return True
        acquired = await self._redis.set(
            self._key(task_id, attempt),
            (owner or "1").encode(),
            nx=True,
            px=self._ttl_ms,
        )
        return acquired is True

    async def complete(self, task_id: str, attempt: int) -> None:
        """Mark the attempt done, preserving the TTL window.

        The key is left to expire naturally so a late duplicate within the window
        still sees the claim and skips.
        """
        if self._redis is None:
            return
        await self._redis.set(self._key(task_id, attempt), b"done", keepttl=True)

    async def release(self, task_id: str, attempt: int) -> None:
        """Drop the claim so the attempt can be redelivered and re-run.

        Called when the handler raises, so a transient failure does not wedge the
        attempt for the full TTL.
        """
        if self._redis is None:
            return
        await self._redis.delete(self._key(task_id, attempt))


# ── Public SDK helper ─────────────────────────────────────────────────────────
# A module-global client wired once at worker startup, mirroring configure_otel.

_global_redis: Redis | None = None


def configure_redis(redis: Redis | None) -> None:
    """Register the process-wide Redis client used by ``helix.exactly_once``.

    Call once at worker startup with the same client passed to the engine. Passing
    ``None`` (the default state) makes ``exactly_once`` a transparent pass-through.
    """
    global _global_redis
    _global_redis = redis


@asynccontextmanager
async def exactly_once(key: str, *, ttl_s: int = 86_400) -> AsyncIterator[bool]:
    """Guard a non-idempotent side effect so it runs once across retries.

    Yields ``True`` the first time *key* is seen (run the side effect) and ``False``
    on a subsequent attempt (skip it)::

        async with helix.exactly_once(f"send-welcome:{user_id}") as first:
            if first:
                await send_email(user_id)

    When Redis is not configured this always yields ``True`` (runs every time), which
    is the correct behavior for local development, tests, and ``make eval``. If the
    guarded block raises, the claim is released so a later attempt retries it.
    """
    redis = _global_redis
    if redis is None:
        yield True
        return

    full = f"{_ONCE_PREFIX}:{key}"
    acquired = await redis.set(full, b"1", nx=True, px=ttl_s * 1000)
    first = acquired is True
    try:
        yield first
    except BaseException:
        if first:
            await redis.delete(full)
        raise
    else:
        if first:
            await redis.set(full, b"done", keepttl=True)
