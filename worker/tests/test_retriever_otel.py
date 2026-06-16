"""Unit tests for the OTel dual-write in HybridRetriever._emit_retrieval_otel."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

import helix.otel as otel_module
from helix.rag.retriever import _emit_retrieval_otel
from helix.runtime.context import current_eval_run_id


@pytest.fixture(autouse=True)
def reset_otel_state() -> Any:
    otel_module._configured = False
    yield
    otel_module._configured = False


_SAMPLE_ATTRS: dict[str, Any] = {
    "query": "What is the capital of France?",
    "retriever": "hybrid+reranked",
    "top_k": 10,
    "results": [
        {"chunk_id": "doc1#c0", "doc_id": "doc1", "score": 0.9},
        {"chunk_id": "doc2#c0", "doc_id": "doc2", "score": 0.7},
    ],
}


def test_emit_retrieval_otel_noop_when_not_configured() -> None:
    assert not otel_module.is_configured()
    with patch("helix.rag.retriever.OtelSpanExporter") as mock_cls:
        _emit_retrieval_otel(_SAMPLE_ATTRS)
    mock_cls.assert_not_called()


def test_emit_retrieval_otel_emits_span_with_correct_attrs() -> None:
    otel_module._configured = True

    captured_args: list[Any] = []

    class _CaptureExporter:
        def __init__(self, name: str, attrs: dict[str, str]) -> None:
            captured_args.append((name, attrs))

        def __enter__(self) -> _CaptureExporter:
            return self

        def __exit__(self, *_: Any) -> None:
            pass

    with patch("helix.rag.retriever.OtelSpanExporter", _CaptureExporter):
        _emit_retrieval_otel(_SAMPLE_ATTRS)

    assert len(captured_args) == 1
    name, attrs = captured_args[0]
    assert name == "retrieve"
    assert attrs["kind"] == "retrieval"
    assert attrs["query"] == _SAMPLE_ATTRS["query"]
    assert attrs["retriever"] == _SAMPLE_ATTRS["retriever"]
    assert attrs["top_k"] == "10"
    parsed = json.loads(attrs["results"])
    assert len(parsed) == 2


def test_emit_retrieval_otel_includes_run_id_from_contextvar() -> None:
    otel_module._configured = True
    token = current_eval_run_id.set("run-test-99")

    captured_attrs: dict[str, str] = {}

    class _CaptureExporter:
        def __init__(self, name: str, attrs: dict[str, str]) -> None:
            captured_attrs.update(attrs)

        def __enter__(self) -> _CaptureExporter:
            return self

        def __exit__(self, *_: Any) -> None:
            pass

    try:
        with patch("helix.rag.retriever.OtelSpanExporter", _CaptureExporter):
            _emit_retrieval_otel(_SAMPLE_ATTRS)
    finally:
        current_eval_run_id.reset(token)

    assert captured_attrs.get("run_id") == "run-test-99"
