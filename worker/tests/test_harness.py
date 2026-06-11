"""Eval harness: dataset loading, the holdout guard, scoring, and aggregation."""

import json
from pathlib import Path
from typing import Any

import pytest

from helix.eval.harness import (
    Example,
    HoldoutAccessError,
    ScoreResult,
    evaluate,
    load_dataset,
    sha256_file,
)
from helix.runtime.sqlite_store import SqliteStore


def _write_dataset(path: Path, n: int) -> None:
    rows = [
        {
            "id": f"ex_{i:03d}",
            "input": {"x": i},
            "expected_output": {"y": 2 * i},
            "metadata": {"k": i},
        }
        for i in range(n)
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


def test_load_dataset_parses_and_validates(tmp_path: Path) -> None:
    path = tmp_path / "data.jsonl"
    _write_dataset(path, 3)
    examples = load_dataset(path)
    assert [e.id for e in examples] == ["ex_000", "ex_001", "ex_002"]
    assert examples[1].input == {"x": 1}
    assert examples[1].expected_output == {"y": 2}
    assert examples[1].metadata == {"k": 1}


def test_load_dataset_rejects_missing_field(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps({"id": "x", "input": {}}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="expected_output"):
        load_dataset(path)


def test_holdout_guard_blocks_without_unlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HELIX_HOLDOUT_UNLOCK", raising=False)
    path = tmp_path / "mini_holdout.jsonl"  # 'holdout' substring triggers the guard
    _write_dataset(path, 1)
    with pytest.raises(HoldoutAccessError, match="eval-final"):
        load_dataset(path)


def test_holdout_guard_allows_with_unlock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HELIX_HOLDOUT_UNLOCK", "1")
    path = tmp_path / "mini_holdout.jsonl"
    _write_dataset(path, 2)
    assert len(load_dataset(path)) == 2


async def _double(example_input: Any) -> dict[str, int]:
    return {"y": example_input["x"] * 2}


def _exact(example: Example, output: Any) -> float:
    return 1.0 if output["y"] == example.expected_output["y"] else 0.0


def _half(example: Example, output: Any) -> float:
    return 0.5


def _varied(example: Example, output: Any) -> float:
    return float(example.input["x"])


def _detailed(example: Example, output: Any) -> ScoreResult:
    return ScoreResult(score=1.0, details={"x": example.input["x"]})


async def test_evaluate_aggregates_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "data.jsonl"
    _write_dataset(path, 10)
    dataset = load_dataset(path)
    scorers = {"exact": _exact, "half": _half, "varied": _varied, "detailed": _detailed}

    async with SqliteStore(tmp_path / "helix.db") as store:
        report = await evaluate(
            _double, dataset, scorers, concurrency=4, store=store, eval_id="eval-1"
        )

        # Aggregates.
        assert report.metrics["exact"]["mean"] == 1.0
        assert report.metrics["exact"]["n"] == 10
        assert report.metrics["exact"]["ci_low"] == report.metrics["exact"]["ci_high"] == 1.0
        assert report.metrics["half"]["mean"] == 0.5
        assert report.metrics["half"]["std"] == 0.0
        assert report.metrics["varied"]["mean"] == pytest.approx(4.5)
        v = report.metrics["varied"]
        assert v["ci_low"] <= v["mean"] <= v["ci_high"]

        # Per-example + persistence.
        assert len(report.per_example) == 10
        rows = await store.get_eval_results("eval-1")
        assert len(rows) == 40  # 10 examples × 4 scorers
        detailed = next(r for r in rows if r.scorer == "detailed" and r.example_id == "ex_003")
        assert detailed.details == {"x": 3}

    out = tmp_path / "report.json"
    report.to_json(out)
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded["eval_id"] == "eval-1"
    assert loaded["metrics"]["exact"]["mean"] == 1.0
    assert len(loaded["per_example"]) == 10


async def test_evaluate_is_deterministic(tmp_path: Path) -> None:
    path = tmp_path / "data.jsonl"
    _write_dataset(path, 10)
    dataset = load_dataset(path)
    scorers = {"varied": _varied}
    first = await evaluate(_double, dataset, scorers, seed=7)
    second = await evaluate(_double, dataset, scorers, seed=7)
    assert first.metrics == second.metrics  # same seed → identical bootstrap CI


async def test_evaluate_retries_transient_failures(tmp_path: Path) -> None:
    path = tmp_path / "data.jsonl"
    _write_dataset(path, 3)
    dataset = load_dataset(path)
    calls: dict[int, int] = {}

    async def flaky(example_input: Any) -> dict[str, int]:
        x = int(example_input["x"])
        calls[x] = calls.get(x, 0) + 1
        if calls[x] < 3:  # fail twice, succeed on the third attempt
            raise RuntimeError("transient 503")
        return {"y": 2 * x}

    report = await evaluate(
        flaky, dataset, {"exact": _exact}, max_attempts=3, retry_backoff=0.0
    )
    assert report.metrics["exact"]["mean"] == 1.0  # every example eventually succeeded
    assert all(c == 3 for c in calls.values())


async def test_evaluate_raises_after_exhausting_attempts(tmp_path: Path) -> None:
    path = tmp_path / "data.jsonl"
    _write_dataset(path, 2)
    dataset = load_dataset(path)

    async def always_fails(example_input: Any) -> dict[str, int]:
        raise RuntimeError("persistent outage")

    with pytest.raises(RuntimeError, match="persistent outage"):
        await evaluate(
            always_fails, dataset, {"exact": _exact}, max_attempts=2, retry_backoff=0.0
        )


def test_sha256_file_is_stable(tmp_path: Path) -> None:
    path = tmp_path / "f.bin"
    path.write_bytes(b"helix")
    assert sha256_file(path) == sha256_file(path)
    assert len(sha256_file(path)) == 64
