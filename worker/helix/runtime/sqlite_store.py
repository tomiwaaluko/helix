"""SQLite state store for the vertical slice.

Implements the subset of the target Postgres schema the slice needs: ``runs``,
``tasks``, ``eval_results``, and ``datasets`` (see ``docs/data-model.md`` for the
production shape). State is JSON-encoded into TEXT columns.

Concurrency note (slice gotcha): ``aiosqlite`` does not support concurrent
writers cleanly. This store holds a *single* connection, whose background thread
serializes every statement. The engine (Task 5) owns the store and routes all
writes through its single writer task; do not open a second writing connection.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from os import PathLike, fspath
from types import TracebackType
from typing import Any, cast

import aiosqlite

_TERMINAL_RUN_STATUSES = frozenset({"succeeded", "failed", "cancelled"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  workflow_name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  input TEXT NOT NULL,
  output TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  finished_at TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  node_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  input TEXT NOT NULL,
  output TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  UNIQUE(run_id, node_id)
);
CREATE INDEX IF NOT EXISTS tasks_run_idx ON tasks (run_id);

CREATE TABLE IF NOT EXISTS eval_results (
  id TEXT PRIMARY KEY,
  eval_id TEXT NOT NULL,
  example_id TEXT NOT NULL,
  scorer TEXT NOT NULL,
  score REAL NOT NULL,
  details TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS eval_results_eval_idx ON eval_results (eval_id);

CREATE TABLE IF NOT EXISTS datasets (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  version INTEGER NOT NULL,
  path TEXT NOT NULL,
  size INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  UNIQUE(name, version)
);

CREATE TABLE IF NOT EXISTS failure_cases (
  id TEXT PRIMARY KEY,
  example_id TEXT NOT NULL,
  span_id TEXT NOT NULL,
  query TEXT NOT NULL,
  gold_doc_id TEXT NOT NULL,
  retrieved_top_k TEXT NOT NULL,
  signature TEXT NOT NULL,
  mined_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS failure_cases_signature_idx ON failure_cases (signature);
CREATE INDEX IF NOT EXISTS failure_cases_mined_idx ON failure_cases (mined_at DESC);
"""


@dataclass(frozen=True)
class RunRow:
    id: str
    workflow_name: str
    status: str
    input: dict[str, Any]
    output: dict[str, Any] | None
    error: dict[str, Any] | None
    created_at: str
    finished_at: str | None


@dataclass(frozen=True)
class TaskRow:
    id: str
    run_id: str
    node_id: str
    status: str
    input: dict[str, Any]
    output: dict[str, Any] | None
    attempts: int


@dataclass(frozen=True)
class EvalResultRow:
    id: str
    eval_id: str
    example_id: str
    scorer: str
    score: float
    details: dict[str, Any] | None
    created_at: str


@dataclass(frozen=True)
class DatasetRow:
    id: str
    name: str
    version: int
    path: str
    size: int
    content_hash: str


@dataclass(frozen=True)
class FailureCaseRow:
    id: str
    example_id: str
    span_id: str
    query: str
    gold_doc_id: str
    retrieved_top_k: list[dict[str, Any]]
    signature: str
    mined_at: str


def _new_id() -> str:
    return uuid.uuid4().hex


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _loads(text: Any) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(text))


def _loads_opt(text: Any) -> dict[str, Any] | None:
    return None if text is None else _loads(text)


class SqliteStore:
    """Async CRUD over the slice's SQLite state. One connection, serialized."""

    def __init__(self, path: str | PathLike[str] = "data/helix.db") -> None:
        self._path = fspath(path)
        self._conn: aiosqlite.Connection | None = None

    @classmethod
    async def connect(cls, path: str | PathLike[str] = "data/helix.db") -> SqliteStore:
        store = cls(path)
        await store._open()
        return store

    async def _open(self) -> None:
        conn = await aiosqlite.connect(self._path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.executescript(_SCHEMA)
        await conn.commit()
        self._conn = conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> SqliteStore:
        if self._conn is None:
            await self._open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SqliteStore is not connected; call connect() first")
        return self._conn

    # ---- runs ---------------------------------------------------------------

    async def create_run(
        self,
        workflow_name: str,
        input: dict[str, Any],
        *,
        run_id: str | None = None,
        status: str = "pending",
    ) -> RunRow:
        rid = run_id or _new_id()
        created_at = _now()
        await self._db.execute(
            "INSERT INTO runs (id, workflow_name, status, input, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (rid, workflow_name, status, json.dumps(input), created_at),
        )
        await self._db.commit()
        return RunRow(
            id=rid,
            workflow_name=workflow_name,
            status=status,
            input=input,
            output=None,
            error=None,
            created_at=created_at,
            finished_at=None,
        )

    async def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        output: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        finished_at: str | None = None,
    ) -> None:
        """Partial update; ``None`` arguments leave the column unchanged.

        Sets ``finished_at`` automatically when ``status`` becomes terminal.
        """
        sets: list[str] = []
        params: list[Any] = []
        if status is not None:
            sets.append("status = ?")
            params.append(status)
            if status in _TERMINAL_RUN_STATUSES and finished_at is None:
                finished_at = _now()
        if output is not None:
            sets.append("output = ?")
            params.append(json.dumps(output))
        if error is not None:
            sets.append("error = ?")
            params.append(json.dumps(error))
        if finished_at is not None:
            sets.append("finished_at = ?")
            params.append(finished_at)
        if not sets:
            return
        params.append(run_id)
        await self._db.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id = ?", params)
        await self._db.commit()

    async def get_run(self, run_id: str) -> RunRow | None:
        async with self._db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_run(row) if row is not None else None

    # ---- tasks --------------------------------------------------------------

    async def create_task(
        self,
        run_id: str,
        node_id: str,
        input: dict[str, Any],
        *,
        task_id: str | None = None,
        status: str = "pending",
    ) -> TaskRow:
        tid = task_id or _new_id()
        await self._db.execute(
            "INSERT INTO tasks (id, run_id, node_id, status, input) VALUES (?, ?, ?, ?, ?)",
            (tid, run_id, node_id, status, json.dumps(input)),
        )
        await self._db.commit()
        return TaskRow(
            id=tid,
            run_id=run_id,
            node_id=node_id,
            status=status,
            input=input,
            output=None,
            attempts=0,
        )

    async def update_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        output: dict[str, Any] | None = None,
        attempts: int | None = None,
    ) -> None:
        """Partial update; ``None`` arguments leave the column unchanged."""
        sets: list[str] = []
        params: list[Any] = []
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if output is not None:
            sets.append("output = ?")
            params.append(json.dumps(output))
        if attempts is not None:
            sets.append("attempts = ?")
            params.append(attempts)
        if not sets:
            return
        params.append(task_id)
        await self._db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", params)
        await self._db.commit()

    async def get_task(self, task_id: str) -> TaskRow | None:
        async with self._db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_task(row) if row is not None else None

    async def get_tasks(self, run_id: str) -> list[TaskRow]:
        async with self._db.execute(
            "SELECT * FROM tasks WHERE run_id = ? ORDER BY node_id", (run_id,)
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_task(row) for row in rows]

    # ---- eval results -------------------------------------------------------

    async def store_eval_result(
        self,
        eval_id: str,
        example_id: str,
        scorer: str,
        score: float,
        details: dict[str, Any] | None = None,
        *,
        result_id: str | None = None,
    ) -> EvalResultRow:
        rid = result_id or _new_id()
        created_at = _now()
        await self._db.execute(
            "INSERT INTO eval_results "
            "(id, eval_id, example_id, scorer, score, details, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                rid,
                eval_id,
                example_id,
                scorer,
                score,
                None if details is None else json.dumps(details),
                created_at,
            ),
        )
        await self._db.commit()
        return EvalResultRow(
            id=rid,
            eval_id=eval_id,
            example_id=example_id,
            scorer=scorer,
            score=score,
            details=details,
            created_at=created_at,
        )

    async def get_eval_results(self, eval_id: str) -> list[EvalResultRow]:
        async with self._db.execute(
            "SELECT * FROM eval_results WHERE eval_id = ? ORDER BY created_at, id",
            (eval_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_eval_result(row) for row in rows]

    # ---- failure cases ------------------------------------------------------

    async def save_failure_cases(self, cases: list[FailureCaseRow]) -> None:
        """Persist a batch of mined failure cases (idempotent on id)."""
        for case in cases:
            await self._db.execute(
                "INSERT INTO failure_cases "
                "(id, example_id, span_id, query, gold_doc_id,"
                " retrieved_top_k, signature, mined_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
                (
                    case.id,
                    case.example_id,
                    case.span_id,
                    case.query,
                    case.gold_doc_id,
                    json.dumps(case.retrieved_top_k),
                    case.signature,
                    case.mined_at,
                ),
            )
        await self._db.commit()

    async def get_failure_cases(self, *, signature: str | None = None) -> list[FailureCaseRow]:
        """Return all failure cases, optionally filtered by signature."""
        if signature is not None:
            async with self._db.execute(
                "SELECT * FROM failure_cases WHERE signature = ? ORDER BY mined_at DESC",
                (signature,),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with self._db.execute(
                "SELECT * FROM failure_cases ORDER BY mined_at DESC"
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_failure_case(row) for row in rows]

    # ---- datasets -----------------------------------------------------------

    async def register_dataset(
        self,
        name: str,
        version: int,
        path: str,
        size: int,
        content_hash: str,
        *,
        dataset_id: str | None = None,
    ) -> DatasetRow:
        """Idempotent on ``(name, version)``: returns the existing row if present."""
        did = dataset_id or _new_id()
        await self._db.execute(
            "INSERT INTO datasets (id, name, version, path, size, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(name, version) DO NOTHING",
            (did, name, version, path, size, content_hash),
        )
        await self._db.commit()
        async with self._db.execute(
            "SELECT * FROM datasets WHERE name = ? AND version = ?", (name, version)
        ) as cur:
            row = await cur.fetchone()
        assert row is not None  # noqa: S101 — just inserted or already present
        return _row_to_dataset(row)


def _row_to_run(row: aiosqlite.Row) -> RunRow:
    return RunRow(
        id=row["id"],
        workflow_name=row["workflow_name"],
        status=row["status"],
        input=_loads(row["input"]),
        output=_loads_opt(row["output"]),
        error=_loads_opt(row["error"]),
        created_at=row["created_at"],
        finished_at=row["finished_at"],
    )


def _row_to_task(row: aiosqlite.Row) -> TaskRow:
    return TaskRow(
        id=row["id"],
        run_id=row["run_id"],
        node_id=row["node_id"],
        status=row["status"],
        input=_loads(row["input"]),
        output=_loads_opt(row["output"]),
        attempts=row["attempts"],
    )


def _row_to_eval_result(row: aiosqlite.Row) -> EvalResultRow:
    return EvalResultRow(
        id=row["id"],
        eval_id=row["eval_id"],
        example_id=row["example_id"],
        scorer=row["scorer"],
        score=row["score"],
        details=_loads_opt(row["details"]),
        created_at=row["created_at"],
    )


def _row_to_dataset(row: aiosqlite.Row) -> DatasetRow:
    return DatasetRow(
        id=row["id"],
        name=row["name"],
        version=row["version"],
        path=row["path"],
        size=row["size"],
        content_hash=row["content_hash"],
    )


def _row_to_failure_case(row: aiosqlite.Row) -> FailureCaseRow:
    return FailureCaseRow(
        id=row["id"],
        example_id=row["example_id"],
        span_id=row["span_id"],
        query=row["query"],
        gold_doc_id=row["gold_doc_id"],
        retrieved_top_k=json.loads(row["retrieved_top_k"]),
        signature=row["signature"],
        mined_at=row["mined_at"],
    )
