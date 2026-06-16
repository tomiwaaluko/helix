"""SDK decorators: ``@task``, ``@workflow``, and ``gather``.

This module implements the *local* execution path only — tasks are awaited
directly, in-process, with no runtime, queue, or state store. Retry and timeout
arguments are recorded as metadata (the decorator metadata *is* the DAG) but are
inert until the engine lands.

Engine-aware dispatch (submit mode) is introduced in Task 5. It detects mode via
a ``contextvars.ContextVar[Engine | None]`` and slots into ``Task.__call__`` —
the single dispatch point below. Workflow code is identical in both modes; do
not special-case the decorators per mode.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable

from helix.runtime.context import current_engine

# `helix.gather` is exactly asyncio.gather; re-exported so workflow code depends
# on the SDK surface rather than asyncio directly.
gather = asyncio.gather

_TIMEOUT_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0}


def _parse_timeout(spec: str | None) -> float | None:
    """Parse a timeout like ``"30s"`` / ``"2m"`` / ``"1h"`` into seconds."""
    if spec is None:
        return None
    unit = spec[-1:]
    if unit not in _TIMEOUT_UNITS:
        raise ValueError(f"invalid timeout {spec!r}: expected a suffix of s, m, or h")
    return float(spec[:-1]) * _TIMEOUT_UNITS[unit]


class Task[**P, R]:
    """An async function registered as a task.

    Calling a task returns the underlying coroutine, so it composes with
    ``gather`` and ``await`` exactly like a plain ``async def``.
    """

    def __init__(self, fn: Callable[P, Awaitable[R]], *, retries: int, timeout: str | None) -> None:
        self._fn = fn
        self.retries = retries
        self.timeout_s = _parse_timeout(timeout)
        functools.update_wrapper(self, fn)

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> Awaitable[R]:
        # Single dispatch point. Local mode (no active engine) awaits the
        # function directly; submit mode hands off to the engine, which returns
        # a Future. Workflow code is identical either way.
        engine = current_engine.get()
        if engine is None:
            return self._fn(*args, **kwargs)
        return engine.dispatch(self, args, kwargs)


class Workflow[**P, R]:
    """An async function registered as a workflow.

    Use ``.local(**kwargs)`` to run it in-process for development and tests.
    """

    def __init__(self, fn: Callable[P, Awaitable[R]], *, name: str, version: str) -> None:
        self._fn = fn
        self.name = name
        self.version = version
        functools.update_wrapper(self, fn)

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> Awaitable[R]:
        return self._fn(*args, **kwargs)

    async def local(self, *args: P.args, **kwargs: P.kwargs) -> R:
        """Run the workflow in-process, skipping the runtime entirely."""
        return await self._fn(*args, **kwargs)


def task[**P, R](
    *, retries: int = 0, timeout: str | None = None
) -> Callable[[Callable[P, Awaitable[R]]], Task[P, R]]:
    """Register an ``async def`` as a task. Retries/timeout are inert locally."""

    def decorate(fn: Callable[P, Awaitable[R]]) -> Task[P, R]:
        return Task(fn, retries=retries, timeout=timeout)

    return decorate


def workflow[**P, R](
    *, name: str, version: str
) -> Callable[[Callable[P, Awaitable[R]]], Workflow[P, R]]:
    """Register an ``async def`` as a workflow with a stable name and version."""

    def decorate(fn: Callable[P, Awaitable[R]]) -> Workflow[P, R]:
        return Workflow(fn, name=name, version=version)

    return decorate
