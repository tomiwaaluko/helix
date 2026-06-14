"""Unit tests for the Redis token-bucket rate limiter."""

from __future__ import annotations

from typing import Any

from helix.tools.rate_limit import RedisRateLimiter


class ScriptedEvalRedis:
    """Fake whose eval() returns a scripted sequence of [allowed, wait_ms]."""

    def __init__(self, results: list[list[int]]) -> None:
        self._results = results
        self.calls = 0

    async def eval(self, *_: Any) -> list[int]:
        result = self._results[min(self.calls, len(self._results) - 1)]
        self.calls += 1
        return result


# ── no-op paths ───────────────────────────────────────────────────────────────


async def test_acquire_noop_without_redis() -> None:
    limiter = RedisRateLimiter(None, key="m", rate=10)
    await limiter.acquire()  # returns immediately, no error


async def test_acquire_noop_when_rate_non_positive() -> None:
    limiter = RedisRateLimiter(ScriptedEvalRedis([[0, 100]]), key="m", rate=0)
    await limiter.acquire()  # rate<=0 → no-op, never calls eval


# ── token bucket ──────────────────────────────────────────────────────────────


async def test_acquire_returns_when_token_available() -> None:
    redis = ScriptedEvalRedis([[1, 0]])
    limiter = RedisRateLimiter(redis, key="m", rate=5)
    await limiter.acquire()
    assert redis.calls == 1


async def test_acquire_waits_then_succeeds() -> None:
    redis = ScriptedEvalRedis([[0, 1], [1, 0]])
    limiter = RedisRateLimiter(redis, key="m", rate=5)
    await limiter.acquire()
    assert redis.calls == 2  # blocked once, then got a token


async def test_acquire_fails_open_on_timeout() -> None:
    # Always denied; acquire must give up at the deadline rather than hang.
    redis = ScriptedEvalRedis([[0, 1]])
    limiter = RedisRateLimiter(redis, key="m", rate=5)
    await limiter.acquire(max_wait_s=0.01)
    assert redis.calls >= 1
