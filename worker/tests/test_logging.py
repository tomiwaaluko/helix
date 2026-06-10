"""Span logger: schema, parent-child nesting, and contextvar propagation."""

import json
from pathlib import Path

import helix
import helix.logging as hlog

_SCHEMA_FIELDS = {
    "trace_id",
    "span_id",
    "parent_span_id",
    "name",
    "kind",
    "start_time",
    "end_time",
    "attributes",
}


def _read(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_span_record_schema(tmp_path: Path) -> None:
    logger = hlog.SpanLogger(tmp_path / "spans.jsonl")
    with logger.span("solo", "task"):
        pass
    (record,) = _read(tmp_path / "spans.jsonl")
    assert set(record) == _SCHEMA_FIELDS
    assert record["name"] == "solo"
    assert record["kind"] == "task"
    assert record["start_time"] and record["end_time"]


def test_nested_spans_parent_child(tmp_path: Path) -> None:
    logger = hlog.SpanLogger(tmp_path / "spans.jsonl")
    with hlog.trace("trace-1"):
        with logger.span("outer", "workflow") as attrs:
            attrs["k"] = "v"
            with logger.span("inner", "task"):
                pass

    by_name = {s["name"]: s for s in _read(tmp_path / "spans.jsonl")}
    assert by_name["outer"]["parent_span_id"] is None
    assert by_name["inner"]["parent_span_id"] == by_name["outer"]["span_id"]
    assert by_name["outer"]["trace_id"] == by_name["inner"]["trace_id"] == "trace-1"
    assert by_name["outer"]["attributes"] == {"k": "v"}


def test_auto_trace_id_is_shared(tmp_path: Path) -> None:
    logger = hlog.SpanLogger(tmp_path / "spans.jsonl")
    with logger.span("a"):
        with logger.span("b"):
            pass
    spans = _read(tmp_path / "spans.jsonl")
    assert spans[0]["trace_id"] == spans[1]["trace_id"]
    assert spans[0]["trace_id"]  # non-empty


async def test_spans_propagate_across_gather(tmp_path: Path) -> None:
    import asyncio

    logger = hlog.SpanLogger(tmp_path / "spans.jsonl")

    async def child(n: int) -> None:
        with logger.span(f"child-{n}", "task"):
            await asyncio.sleep(0)

    with hlog.trace("t"):
        with logger.span("parent", "workflow"):
            await helix.gather(child(1), child(2))

    spans = _read(tmp_path / "spans.jsonl")
    parent = next(s for s in spans if s["name"] == "parent")
    children = [s for s in spans if str(s["name"]).startswith("child")]
    assert len(children) == 2
    for c in children:
        assert c["parent_span_id"] == parent["span_id"]
        assert c["trace_id"] == "t"
