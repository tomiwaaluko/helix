"""Unit tests for the OTel dual-write in llm_call._emit_llm_call_otel."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

import helix.otel as otel_module
from helix.runtime.context import current_eval_run_id
from helix.tools.litellm_adapter import _emit_llm_call_otel, _provider_from_model


@pytest.fixture(autouse=True)
def reset_otel_state() -> Any:
    otel_module._configured = False
    yield
    otel_module._configured = False


_SAMPLE_ATTRS: dict[str, Any] = {
    "model": "claude-sonnet-4-20250514",
    "prompt_tokens": 512,
    "completion_tokens": 128,
    "cost_usd": 0.003,
    "cache_hit": False,
    "replayed": False,
}


def test_provider_from_model_anthropic() -> None:
    assert _provider_from_model("claude-sonnet-4-20250514") == "anthropic"


def test_provider_from_model_openai() -> None:
    assert _provider_from_model("gpt-4o") == "openai"
    assert _provider_from_model("o1-preview") == "openai"


def test_provider_from_model_google() -> None:
    assert _provider_from_model("gemini-pro") == "google"


def test_provider_from_model_unknown() -> None:
    assert _provider_from_model("llama-3") == "unknown"


def test_emit_llm_call_otel_noop_when_not_configured() -> None:
    assert not otel_module.is_configured()
    with patch("helix.tools.litellm_adapter.OtelSpanExporter") as mock_cls:
        _emit_llm_call_otel(_SAMPLE_ATTRS)
    mock_cls.assert_not_called()


def test_emit_llm_call_otel_emits_span_with_correct_attrs() -> None:
    otel_module._configured = True

    captured: list[tuple[str, dict[str, str]]] = []

    class _CaptureExporter:
        def __init__(self, name: str, attrs: dict[str, str]) -> None:
            captured.append((name, attrs))

        def __enter__(self) -> _CaptureExporter:
            return self

        def __exit__(self, *_: Any) -> None:
            pass

    with patch("helix.tools.litellm_adapter.OtelSpanExporter", _CaptureExporter):
        _emit_llm_call_otel(_SAMPLE_ATTRS)

    assert len(captured) == 1
    name, attrs = captured[0]
    assert name == "llm_call"
    assert attrs["kind"] == "llm"
    assert attrs["model"] == "claude-sonnet-4-20250514"
    assert attrs["provider"] == "anthropic"
    assert attrs["prompt_tokens"] == "512"
    assert attrs["completion_tokens"] == "128"
    assert attrs["cache_hit"] == "false"


def test_emit_llm_call_otel_includes_run_id_from_contextvar() -> None:
    otel_module._configured = True
    token = current_eval_run_id.set("run-m8-test")

    captured_attrs: dict[str, str] = {}

    class _CaptureExporter:
        def __init__(self, name: str, attrs: dict[str, str]) -> None:
            captured_attrs.update(attrs)

        def __enter__(self) -> _CaptureExporter:
            return self

        def __exit__(self, *_: Any) -> None:
            pass

    try:
        with patch("helix.tools.litellm_adapter.OtelSpanExporter", _CaptureExporter):
            _emit_llm_call_otel(_SAMPLE_ATTRS)
    finally:
        current_eval_run_id.reset(token)

    assert captured_attrs.get("run_id") == "run-m8-test"
