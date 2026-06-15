"""Execution-mode context for the SDK.

The decorators detect local vs submit mode by reading ``current_engine``: it is
``None`` in local mode and set to the active :class:`~helix.runtime.engine.Engine`
while a submitted run executes. ``current_run`` carries the run-scoped node-id
counter and ``trace_id`` so the engine can attribute each dispatched task.

Both propagate via ``contextvars`` so they survive ``await`` and are copied into
each spawned asyncio task. This module deliberately imports nothing from the
engine or decorators at runtime, so either can read the contextvars without a
circular import.
"""

from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from helix.runtime.engine import Engine, RunContext

current_engine: contextvars.ContextVar[Engine | None] = contextvars.ContextVar(
    "helix_current_engine", default=None
)
current_run: contextvars.ContextVar[RunContext | None] = contextvars.ContextVar(
    "helix_current_run", default=None
)
current_eval_run_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "helix_eval_run_id", default=""
)
