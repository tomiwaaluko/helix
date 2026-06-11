"""Eval harness — dataset loading, the runner loop, and aggregation.

Loads a JSONL eval dataset, runs a workflow over each example under a concurrency
limit, scores each output, persists per-example results to SQLite, and aggregates
mean / std / 95% bootstrap CI per scorer.

Holdout guard: ``load_dataset`` refuses any path containing ``holdout`` unless
``HELIX_HOLDOUT_UNLOCK=1`` is set (only ``make eval-final`` does). The holdout
filename literal lives *only* in this module so the CI guard can whitelist it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import statistics
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from os import PathLike, fspath
from pathlib import Path
from typing import Any

from helix.logging import new_id
from helix.runtime.sqlite_store import SqliteStore

HOLDOUT_DATASET_PATH = "evals/datasets/hotpotqa_dev_holdout_500.jsonl"
HOLDOUT_SHA256_PATH = "evals/datasets/hotpotqa_dev_holdout_500.sha256"
HOLDOUT_UNLOCK_ENV = "HELIX_HOLDOUT_UNLOCK"


class HoldoutAccessError(RuntimeError):
    """Raised when a holdout dataset is read without the unlock env var."""


@dataclass(frozen=True)
class Example:
    id: str
    input: dict[str, Any]
    expected_output: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScoreResult:
    """A scorer result with optional subscore details."""

    score: float
    details: dict[str, Any] = field(default_factory=dict)


Scorer = Callable[[Example, Any], "float | ScoreResult"]
WorkflowFn = Callable[[Mapping[str, Any]], Awaitable[Any]]


@dataclass
class EvalReport:
    eval_id: str
    metrics: dict[str, dict[str, float]]
    per_example: list[dict[str, Any]]
    examples_skipped: int = 0

    def to_json(self, path: str | PathLike[str]) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(
                {
                    "eval_id": self.eval_id,
                    "metrics": self.metrics,
                    "per_example": self.per_example,
                },
                indent=2,
            ),
            encoding="utf-8",
        )


def sha256_file(path: str | PathLike[str]) -> str:
    """SHA-256 hex digest of a file's bytes."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def load_dataset(path: str | PathLike[str]) -> list[Example]:
    """Read and validate a JSONL eval dataset; enforces the holdout guard."""
    if "holdout" in fspath(path) and os.environ.get(HOLDOUT_UNLOCK_ENV) != "1":
        raise HoldoutAccessError(
            f"Refusing to read holdout dataset {fspath(path)!r}: holdout reads are "
            f"restricted to `make eval-final` (set {HOLDOUT_UNLOCK_ENV}=1)."
        )
    examples: list[Example] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            row = json.loads(stripped)
            for required in ("id", "input", "expected_output"):
                if required not in row:
                    raise ValueError(f"{fspath(path)}:{line_no} missing field {required!r}")
            examples.append(
                Example(
                    id=str(row["id"]),
                    input=dict(row["input"]),
                    expected_output=dict(row["expected_output"]),
                    metadata=dict(row.get("metadata", {})),
                )
            )
    return examples


def _as_score(result: float | ScoreResult) -> tuple[float, dict[str, Any] | None]:
    if isinstance(result, ScoreResult):
        return result.score, result.details
    return float(result), None


def _bootstrap_ci(
    values: Sequence[float], *, samples: int, seed: int, alpha: float = 0.05
) -> tuple[float, float]:
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(samples))
    low = means[int((alpha / 2) * samples)]
    high = means[min(int((1 - alpha / 2) * samples), samples - 1)]
    return low, high


def _aggregate(values: Sequence[float], *, bootstrap_samples: int, seed: int) -> dict[str, float]:
    if not values:
        return {"mean": 0.0, "std": 0.0, "ci_low": 0.0, "ci_high": 0.0, "n": 0}
    mean = statistics.fmean(values)
    std = statistics.pstdev(values)
    if len(values) == 1:
        ci_low, ci_high = values[0], values[0]
    else:
        ci_low, ci_high = _bootstrap_ci(values, samples=bootstrap_samples, seed=seed)
    return {
        "mean": mean,
        "std": std,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "n": len(values),
    }


async def evaluate(
    workflow_fn: WorkflowFn,
    dataset: Sequence[Example],
    scorers: Mapping[str, Scorer],
    *,
    concurrency: int = 4,
    store: SqliteStore | None = None,
    eval_id: str | None = None,
    bootstrap_samples: int = 1000,
    seed: int = 0,
    max_attempts: int = 3,
    retry_backoff: float = 2.0,
    tolerate_failures: bool = False,
) -> EvalReport:
    """Run ``workflow_fn`` over the dataset, score, persist, and aggregate.

    Each example is attempted up to ``max_attempts`` times with exponential
    backoff (``retry_backoff`` seconds, doubling) so a transient provider error
    on one example does not abort the whole batch. Workflow LLM calls are cached,
    so a retried example reuses sub-calls that already succeeded and only re-issues
    the one that failed. The backoff sleep happens outside the concurrency
    semaphore so a stalled example does not block the others.

    When ``tolerate_failures=True``, examples that exhaust all retry attempts are
    silently skipped instead of aborting the run; the count is recorded in
    ``EvalReport.examples_skipped``. This is intended for mining runs where partial
    results are preferable to a full abort. Leave it ``False`` (the default) for
    baseline eval commands where every example must succeed.
    """
    run_id = eval_id or new_id()
    semaphore = asyncio.Semaphore(concurrency)

    async def _run_workflow_with_retries(example: Example) -> Any:
        for attempt in range(1, max_attempts + 1):
            try:
                async with semaphore:
                    return await workflow_fn(example.input)
            except Exception:
                if attempt == max_attempts:
                    raise
                await asyncio.sleep(retry_backoff * 2 ** (attempt - 1))
        raise AssertionError("unreachable")  # pragma: no cover

    async def run_one(example: Example) -> dict[str, Any] | None:
        try:
            output = await _run_workflow_with_retries(example)
        except Exception:
            if not tolerate_failures:
                raise
            return None
        scores: dict[str, float] = {}
        for name, scorer in scorers.items():
            score, details = _as_score(scorer(example, output))
            scores[name] = score
            if store is not None:
                await store.store_eval_result(run_id, example.id, name, score, details)
        return {"example_id": example.id, "scores": scores}

    raw: list[dict[str, Any] | None] = list(
        await asyncio.gather(*(run_one(example) for example in dataset))
    )
    per_example: list[dict[str, Any]] = [r for r in raw if r is not None]
    skipped = len(raw) - len(per_example)

    metrics = {
        name: _aggregate(
            [row["scores"][name] for row in per_example],
            bootstrap_samples=bootstrap_samples,
            seed=seed,
        )
        for name in scorers
    }
    return EvalReport(
        eval_id=run_id,
        metrics=metrics,
        per_example=per_example,
        examples_skipped=skipped,
    )
