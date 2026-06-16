"""Single-process asyncio engine for the vertical slice.

Replaces the Go orchestrator + NATS. The engine runs a submitted workflow as a
coroutine; each ``@task`` call inside it is dispatched through an
``asyncio.Queue`` and returns a ``Future`` that resolves once a worker has run
the handler and persisted the result. Because the workflow coroutine drives
execution, dependencies resolve by ``await`` order and the run succeeds exactly
when the workflow returns (and fails when it raises).

State is persisted through a single :class:`~helix.runtime.sqlite_store.SqliteStore`.
aiosqlite serializes every statement on its connection thread, so the store *is*
the single writer; the engine does not need a separate writer task.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, is_dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Any

from helix.logging import SpanLogger, new_id, trace
from helix.runtime.context import current_engine, current_run
from helix.runtime.sqlite_store import SqliteStore

if TYPE_CHECKING:
    from helix.decorators import Task, Workflow


@dataclass
class RunContext:
    """Per-run state visible to ``dispatch`` via the ``current_run`` contextvar."""

    run_id: str
    trace_id: str
    _counter: int = 0

    def next_node_id(self, name: str) -> str:
        node_id = f"{name}#{self._counter}"
        self._counter += 1
        return node_id


@dataclass
class _WorkItem:
    run_id: str
    task_id: str
    node_id: str
    fn: Callable[..., Awaitable[Any]]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    future: asyncio.Future[Any]
    trace_id: str
    attempt: int = 1


def _jsonable(obj: Any) -> Any:
    """Best-effort JSON-safe snapshot for persistence (telemetry, not data flow).

    Dataclasses become dicts; lists/dicts recurse; anything else falls back to
    ``str``. The real Python objects always travel in-memory via the work item;
    this is only what lands in the ``tasks`` columns.
    """
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, list | tuple):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if obj is None or isinstance(obj, str | int | float | bool):
        return obj
    return str(obj)


def _input_snapshot(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    return {"args": [_jsonable(a) for a in args], "kwargs": _jsonable(kwargs)}


class Engine:
    """Runs workflows by dispatching ``@task`` calls through an asyncio queue."""

    def __init__(
        self,
        store: SqliteStore,
        *,
        max_concurrency: int = 4,
        span_logger: SpanLogger | None = None,
        retry_budget: int = 1,
    ) -> None:
        self._store = store
        self._spans = span_logger or SpanLogger()
        self._retry_budget = retry_budget
        self._queue: asyncio.Queue[_WorkItem] = asyncio.Queue()
        self._sem = asyncio.Semaphore(max_concurrency)
        self._runs: dict[str, asyncio.Task[Any]] = {}
        self._worker: asyncio.Task[None] | None = None
        self._inflight: set[asyncio.Task[None]] = set()
        self._bg: set[asyncio.Task[None]] = set()

    # ---- public API ---------------------------------------------------------

    async def submit(self, workflow: Workflow[..., Any], input: dict[str, Any]) -> str:
        """Create a run and start the workflow coroutine; returns the run id."""
        run = await self._store.create_run(workflow.name, input, status="running")
        runctx = RunContext(run_id=run.id, trace_id=new_id())
        self._ensure_worker()
        self._runs[run.id] = asyncio.create_task(self._run_workflow(workflow, input, runctx))
        return run.id

    async def run_until_complete(self, run_id: str) -> Any:
        """Block until the run's workflow finishes; persist and return its output."""
        try:
            output = await self._runs[run_id]
        except Exception as exc:
            await self._store.update_run(
                run_id,
                status="failed",
                error={"type": type(exc).__name__, "message": str(exc)},
            )
            raise
        await self._store.update_run(
            run_id, status="succeeded", output={"result": _jsonable(output)}
        )
        return output

    def dispatch(
        self, task: Task[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> asyncio.Future[Any]:
        """Enqueue a task call and return a Future that resolves with its result.

        Synchronous (called from the decorator). ``node_id`` is minted here with
        no ``await``, so the per-run counter is race-free.
        """
        runctx = current_run.get()
        if runctx is None:
            raise RuntimeError("Engine.dispatch called outside an active run")
        node_name: str = getattr(task, "__name__", "task")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        item = _WorkItem(
            run_id=runctx.run_id,
            task_id=new_id(),
            node_id=runctx.next_node_id(node_name),
            fn=task._fn,
            args=args,
            kwargs=kwargs,
            future=future,
            trace_id=runctx.trace_id,
        )
        bg = loop.create_task(self._enqueue(item))
        self._bg.add(bg)
        bg.add_done_callback(self._bg.discard)
        return future

    # ---- lifecycle ----------------------------------------------------------

    async def __aenter__(self) -> Engine:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        for pending in (*self._inflight, *self._bg):
            pending.cancel()
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None

    # ---- internals ----------------------------------------------------------

    def _ensure_worker(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._worker_loop())

    async def _run_workflow(
        self, workflow: Workflow[..., Any], input: dict[str, Any], runctx: RunContext
    ) -> Any:
        current_engine.set(self)
        current_run.set(runctx)
        with trace(runctx.trace_id):
            return await workflow(**input)

    async def _worker_loop(self) -> None:
        while True:
            item = await self._queue.get()
            await self._sem.acquire()
            handler = asyncio.create_task(self._run_item(item))
            self._inflight.add(handler)
            handler.add_done_callback(self._on_item_done)

    def _on_item_done(self, handler: asyncio.Task[None]) -> None:
        self._inflight.discard(handler)
        self._sem.release()

    async def _enqueue(self, item: _WorkItem) -> None:
        await self._store.create_task(
            item.run_id,
            item.node_id,
            _input_snapshot(item.args, item.kwargs),
            task_id=item.task_id,
            status="ready",
        )
        await self._queue.put(item)

    async def _run_item(self, item: _WorkItem) -> None:
        await self._store.update_task(item.task_id, status="running", attempts=item.attempt)
        try:
            with trace(item.trace_id), self._spans.span(item.node_id, kind="task"):
                result = await item.fn(*item.args, **item.kwargs)
        except Exception as exc:  # noqa: BLE001 - engine boundary records every task failure
            if item.attempt <= self._retry_budget:
                item.attempt += 1
                await self._queue.put(item)
                return
            await self._store.update_task(item.task_id, status="failed")
            if not item.future.done():
                item.future.set_exception(exc)
            return
        await self._store.update_task(
            item.task_id, status="succeeded", output={"result": _jsonable(result)}
        )
        if not item.future.done():
            item.future.set_result(result)
