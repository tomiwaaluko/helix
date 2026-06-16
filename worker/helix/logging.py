"""Structured span logger — minimal JSONL observability for the slice.

Not OpenTelemetry, but the same shape: each span is one JSON line carrying
``trace_id``, ``span_id``, ``parent_span_id``, ``name``, ``kind``,
``start_time``, ``end_time``, and ``attributes``. These field names match the
target ClickHouse ``spans`` schema so the future miner can consume slice traces
unchanged — do not rename or invent fields.

The current span stack and per-run ``trace_id`` propagate via ``contextvars``,
so nested spans get the correct ``parent_span_id`` even across ``await`` and
``gather`` (each asyncio task copies the context at creation).
"""

from __future__ import annotations

import contextvars
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from os import PathLike
from pathlib import Path
from typing import Any

DEFAULT_SPANS_PATH = Path("data/spans.jsonl")

_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "helix_trace_id", default=None
)
_span_stack: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "helix_span_stack", default=()
)


def new_id() -> str:
    """Return a fresh hex id for a trace or span."""
    return uuid.uuid4().hex


def _now() -> str:
    return datetime.now(UTC).isoformat()


@contextmanager
def trace(trace_id: str | None = None) -> Iterator[str]:
    """Bind a ``trace_id`` for the duration of a run.

    All spans opened within share this id. If omitted, one is generated.
    """
    tid = trace_id or new_id()
    token = _trace_id.set(tid)
    try:
        yield tid
    finally:
        _trace_id.reset(token)


class SpanLogger:
    """Appends spans as JSON lines to a file (default ``data/spans.jsonl``)."""

    def __init__(self, path: str | PathLike[str] = DEFAULT_SPANS_PATH) -> None:
        self._path = Path(path)

    def _write(self, record: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    @contextmanager
    def span(
        self,
        name: str,
        kind: str = "internal",
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Open a span; yields its mutable ``attributes`` dict.

        The span is written on exit (so nested spans appear before their
        parents). Add attributes during the body, e.g.
        ``with logger.span("llm", "llm") as attrs: attrs["cache_hit"] = True``.
        """
        trace_id = _trace_id.get()
        trace_token = None
        if trace_id is None:
            trace_id = new_id()
            trace_token = _trace_id.set(trace_id)

        stack = _span_stack.get()
        span_id = new_id()
        parent_span_id = stack[-1] if stack else None
        attrs: dict[str, Any] = dict(attributes or {})
        stack_token = _span_stack.set((*stack, span_id))

        record: dict[str, Any] = {
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "name": name,
            "kind": kind,
            "start_time": _now(),
            "end_time": None,
            "attributes": attrs,
        }
        try:
            yield attrs
        finally:
            record["end_time"] = _now()
            self._write(record)
            _span_stack.reset(stack_token)
            if trace_token is not None:
                _trace_id.reset(trace_token)
