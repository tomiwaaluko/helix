"""Distributed token-bucket rate limiter backed by Redis.

LiteLLM owns per-call retries, but it cannot coordinate a *global* request rate
across many workers hitting the same provider. This limiter does: a token bucket
keyed by ``(provider, model)`` lives in Redis, so N workers share one budget.

The refill + take is a single Lua script so the check-and-decrement is atomic. When
Redis is not configured, or the rate is non-positive, ``acquire`` returns immediately —
local ``make eval`` and unit tests never block.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only: `redis.asyncio` eagerly imports OpenTelemetry; keep it lazy.
    from redis.asyncio import Redis

# Atomically refill the bucket and try to take one token.
# KEYS[1] = bucket key; ARGV = rate (tokens/period), period_ms, now_ms.
# Returns {allowed (0|1), wait_ms}.
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local period = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local capacity = rate
local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then
  tokens = capacity
  ts = now
end
local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + elapsed * rate / period)
local allowed = 0
local wait = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  wait = math.ceil((1 - tokens) * period / rate)
end
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', key, math.ceil(period * 2))
return {allowed, wait}
"""


class RedisRateLimiter:
    """A shared token bucket. ``rate`` tokens refill over ``period_s`` seconds."""

    def __init__(self, redis: Redis | None, *, key: str, rate: int, period_s: float = 60.0) -> None:
        self._redis = redis
        self._key = f"helix:ratelimit:{key}"
        self._rate = rate
        self._period_ms = period_s * 1000.0

    async def acquire(self, *, max_wait_s: float = 30.0) -> None:
        """Block until a token is available, or until *max_wait_s* elapses.

        No-op when Redis is unconfigured or the rate is non-positive. On timeout it
        fails open (returns without a token) rather than stalling a worker forever.
        """
        if self._redis is None or self._rate <= 0:
            return
        deadline = time.monotonic() + max_wait_s
        while True:
            allowed, wait_ms = await self._take()
            if allowed:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(wait_ms / 1000.0, remaining))

    async def _take(self) -> tuple[int, int]:
        now_ms = int(time.time() * 1000)
        result = await self._redis.eval(  # type: ignore[union-attr]
            _TOKEN_BUCKET_LUA, 1, self._key, self._rate, int(self._period_ms), now_ms
        )
        allowed, wait = result[0], result[1]
        return int(allowed), int(wait)
