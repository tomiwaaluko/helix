"""Unit tests for the exactly-once sentinel and helix.exactly_once helper."""

from __future__ import annotations

from typing import Any

import pytest

import helix
from helix.runtime.idempotency import Idempotency


class FakeRedis:
    """Minimal in-memory async stand-in for redis.asyncio.Redis."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def set(
        self,
        key: str,
        value: bytes,
        *,
        nx: bool = False,
        px: int | None = None,
        keepttl: bool = False,
    ) -> bool | None:
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        n = 0
        for k in keys:
            if k in self.store:
                del self.store[k]
                n += 1
        return n


# ── Idempotency (engine sentinel) ─────────────────────────────────────────────


async def test_begin_is_noop_true_without_redis() -> None:
    idem = Idempotency(None)
    assert await idem.begin("task-1", 1) is True
    # complete/release must also be safe no-ops
    await idem.complete("task-1", 1)
    await idem.release("task-1", 1)


async def test_begin_dedups_same_attempt() -> None:
    idem = Idempotency(FakeRedis())
    assert await idem.begin("task-1", 1) is True
    assert await idem.begin("task-1", 1) is False  # duplicate delivery → skip


async def test_different_attempt_runs() -> None:
    idem = Idempotency(FakeRedis())
    assert await idem.begin("task-1", 1) is True
    assert await idem.begin("task-1", 2) is True  # genuine retry → new attempt


async def test_release_allows_rerun() -> None:
    idem = Idempotency(FakeRedis())
    assert await idem.begin("task-1", 1) is True
    await idem.release("task-1", 1)
    assert await idem.begin("task-1", 1) is True  # claim dropped → can re-run


async def test_complete_keeps_claim() -> None:
    redis = FakeRedis()
    idem = Idempotency(redis)
    assert await idem.begin("task-1", 1) is True
    await idem.complete("task-1", 1)
    # claim still present → a duplicate is still skipped
    assert await idem.begin("task-1", 1) is False


# ── helix.exactly_once (public helper) ────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_global_redis() -> Any:
    helix.configure_redis(None)
    yield
    helix.configure_redis(None)


async def test_exactly_once_passthrough_without_redis() -> None:
    runs = 0
    for _ in range(3):
        async with helix.exactly_once("k") as first:
            assert first is True  # always runs when Redis is unconfigured
            runs += 1
    assert runs == 3


async def test_exactly_once_runs_once_with_redis() -> None:
    helix.configure_redis(FakeRedis())
    runs = 0
    for _ in range(3):
        async with helix.exactly_once("send-email:user-42") as first:
            if first:
                runs += 1
    assert runs == 1


async def test_exactly_once_releases_on_exception() -> None:
    helix.configure_redis(FakeRedis())
    with pytest.raises(ValueError):
        async with helix.exactly_once("k") as first:
            assert first is True
            raise ValueError("boom")
    # claim released → next attempt runs again
    async with helix.exactly_once("k") as first:
        assert first is True
