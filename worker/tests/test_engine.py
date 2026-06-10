"""Engine integration: lifecycle, parallelism, retries, spans, mode invariance."""

import asyncio
import json
from pathlib import Path

import pytest

import helix
from helix.logging import SpanLogger
from helix.runtime.engine import Engine
from helix.runtime.sqlite_store import SqliteStore, TaskRow


@helix.task()
async def double(x: int) -> int:
    return x * 2


@helix.task()
async def increment(x: int) -> int:
    return x + 1


@helix.workflow(name="toy", version="1.0.0")
async def toy(start: int) -> int:
    a, b = await helix.gather(double(start), increment(start))
    return a + b


async def _await_task_status(
    store: SqliteStore, run_id: str, status: str, timeout_s: float = 2.0
) -> TaskRow:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        for task in await store.get_tasks(run_id):
            if task.status == status:
                return task
        await asyncio.sleep(0.01)
    raise AssertionError(f"no task for run {run_id} reached status {status!r}")


async def test_task_lifecycle_through_engine(tmp_path: Path) -> None:
    gate = asyncio.Event()

    @helix.task()
    async def slow(x: int) -> int:
        await gate.wait()
        return x + 1

    @helix.workflow(name="wf_slow", version="1.0.0")
    async def wf(x: int) -> int:
        return await slow(x)

    async with (
        SqliteStore(tmp_path / "h.db") as store,
        Engine(store, span_logger=SpanLogger(tmp_path / "spans.jsonl")) as engine,
    ):
        run_id = await engine.submit(wf, {"x": 41})

        running = await _await_task_status(store, run_id, "running")
        assert running.node_id == "slow#0"
        assert running.attempts == 1
        assert running.input == {"args": [41], "kwargs": {}}  # slow(x) is positional
        assert running.output is None

        gate.set()
        result = await engine.run_until_complete(run_id)
        assert result == 42

        run = await store.get_run(run_id)
        assert run is not None
        assert run.status == "succeeded"
        assert run.output == {"result": 42}
        assert run.finished_at is not None

        done = await store.get_task(running.id)
        assert done is not None
        assert done.status == "succeeded"
        assert done.output == {"result": 42}


async def test_parallel_branches_get_their_own_rows(tmp_path: Path) -> None:
    async with (
        SqliteStore(tmp_path / "h.db") as store,
        Engine(store, span_logger=SpanLogger(tmp_path / "spans.jsonl")) as engine,
    ):
        run_id = await engine.submit(toy, {"start": 3})
        result = await engine.run_until_complete(run_id)
        assert result == 10  # double(3)=6 + increment(3)=4

        tasks = await store.get_tasks(run_id)
        assert {t.node_id for t in tasks} == {"double#0", "increment#1"}
        assert all(t.status == "succeeded" for t in tasks)
        assert all(t.attempts == 1 for t in tasks)


async def test_failure_retries_then_marks_failed(tmp_path: Path) -> None:
    calls = {"n": 0}

    @helix.task()
    async def boom(x: int) -> int:
        calls["n"] += 1
        raise ValueError("nope")

    @helix.workflow(name="wf_boom", version="1.0.0")
    async def wf(x: int) -> int:
        return await boom(x)

    async with (
        SqliteStore(tmp_path / "h.db") as store,
        Engine(store, span_logger=SpanLogger(tmp_path / "spans.jsonl")) as engine,
    ):
        run_id = await engine.submit(wf, {"x": 1})
        with pytest.raises(ValueError, match="nope"):
            await engine.run_until_complete(run_id)

        assert calls["n"] == 2  # initial attempt + one retry (budget=1)

        tasks = await store.get_tasks(run_id)
        assert len(tasks) == 1
        assert tasks[0].status == "failed"
        assert tasks[0].attempts == 2

        run = await store.get_run(run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error is not None
        assert run.error["type"] == "ValueError"


async def test_engine_emits_task_spans(tmp_path: Path) -> None:
    spans_path = tmp_path / "spans.jsonl"
    async with (
        SqliteStore(tmp_path / "h.db") as store,
        Engine(store, span_logger=SpanLogger(spans_path)) as engine,
    ):
        run_id = await engine.submit(toy, {"start": 2})
        await engine.run_until_complete(run_id)

    spans = [json.loads(line) for line in spans_path.read_text().splitlines()]
    assert len(spans) == 2
    assert all(s["kind"] == "task" for s in spans)
    assert {s["name"] for s in spans} == {"double#0", "increment#1"}
    assert len({s["trace_id"] for s in spans}) == 1  # one trace for the run


async def test_same_workflow_local_and_submit(tmp_path: Path) -> None:
    # Identical workflow code runs in both modes.
    assert await toy.local(start=3) == 10

    async with (
        SqliteStore(tmp_path / "h.db") as store,
        Engine(store, span_logger=SpanLogger(tmp_path / "spans.jsonl")) as engine,
    ):
        run_id = await engine.submit(toy, {"start": 3})
        assert await engine.run_until_complete(run_id) == 10
