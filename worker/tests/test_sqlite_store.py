"""CRUD coverage for the SQLite state store."""

from pathlib import Path

from helix.runtime.sqlite_store import SqliteStore


async def test_run_lifecycle(tmp_path: Path) -> None:
    async with SqliteStore(tmp_path / "helix.db") as store:
        run = await store.create_run("deep_research", {"query": "q"})
        assert run.status == "pending"
        assert run.finished_at is None

        fetched = await store.get_run(run.id)
        assert fetched is not None
        assert fetched.input == {"query": "q"}

        await store.update_run(run.id, status="running")
        running = await store.get_run(run.id)
        assert running is not None
        assert running.status == "running"
        assert running.finished_at is None  # not terminal yet

        await store.update_run(run.id, status="succeeded", output={"answer": "42"})
        done = await store.get_run(run.id)
        assert done is not None
        assert done.status == "succeeded"
        assert done.output == {"answer": "42"}
        assert done.finished_at is not None  # auto-set on terminal status


async def test_update_run_error_and_noop(tmp_path: Path) -> None:
    async with SqliteStore(tmp_path / "helix.db") as store:
        run = await store.create_run("wf", {})
        await store.update_run(run.id, status="failed", error={"type": "Boom"})
        failed = await store.get_run(run.id)
        assert failed is not None
        assert failed.error == {"type": "Boom"}

        # No-op update must not raise or change anything.
        await store.update_run(run.id)
        assert await store.get_run("missing") is None


async def test_task_lifecycle(tmp_path: Path) -> None:
    async with SqliteStore(tmp_path / "helix.db") as store:
        run = await store.create_run("wf", {})
        task = await store.create_task(run.id, "retrieve", {"subquery": "x"})
        assert task.attempts == 0
        assert task.status == "pending"

        await store.update_task(task.id, status="running", attempts=1)
        await store.update_task(task.id, status="succeeded", output={"docs": [1, 2]})
        fetched = await store.get_task(task.id)
        assert fetched is not None
        assert fetched.status == "succeeded"
        assert fetched.attempts == 1
        assert fetched.output == {"docs": [1, 2]}


async def test_eval_results(tmp_path: Path) -> None:
    async with SqliteStore(tmp_path / "helix.db") as store:
        await store.store_eval_result("eval-1", "ex-1", "answer_f1", 0.5)
        await store.store_eval_result("eval-1", "ex-1", "citation_precision", 1.0, {"matched": 3})
        await store.store_eval_result("eval-2", "ex-9", "answer_f1", 0.1)

        results = await store.get_eval_results("eval-1")
        assert len(results) == 2
        assert {r.scorer for r in results} == {"answer_f1", "citation_precision"}
        cp = next(r for r in results if r.scorer == "citation_precision")
        assert cp.score == 1.0
        assert cp.details == {"matched": 3}


async def test_register_dataset_is_idempotent(tmp_path: Path) -> None:
    async with SqliteStore(tmp_path / "helix.db") as store:
        first = await store.register_dataset("hotpotqa_dev_100", 1, "data/x.jsonl", 100, "abc")
        again = await store.register_dataset(
            "hotpotqa_dev_100", 1, "data/other.jsonl", 999, "different"
        )
        # Same (name, version) returns the original row; the second call is a no-op insert.
        assert again.id == first.id
        assert again.path == "data/x.jsonl"
        assert again.size == 100
