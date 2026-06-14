"""Unit tests for helix.otel — OTel span export integration."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import helix.otel as otel_module


def _reset() -> None:
    otel_module._configured = False


@pytest.fixture(autouse=True)
def reset_state() -> Any:
    _reset()
    yield
    _reset()


# ── configure_otel ────────────────────────────────────────────────────────────


def test_configure_otel_no_op_when_none() -> None:
    otel_module.configure_otel(None)
    assert not otel_module._configured


def test_configure_otel_no_op_when_empty_string() -> None:
    otel_module.configure_otel("")
    assert not otel_module._configured


# configure_otel does `from opentelemetry... import X` *inside* the function. We patch
# the attributes on the real opentelemetry modules rather than swapping sys.modules:
# once any dependency (redis, litellm) genuinely imports opentelemetry, a sys.modules
# fake is bypassed by `from pkg import attr`. Patching the real target intercepts the
# in-function import regardless of import order, and mocking set_tracer_provider keeps
# the OTel global provider untouched. See ISSUES.md.
_OTEL_TARGETS = (
    "opentelemetry.sdk.trace.export.BatchSpanProcessor",
    "opentelemetry.sdk.trace.TracerProvider",
    "opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter",
    "opentelemetry.trace.set_tracer_provider",
)


def test_configure_otel_idempotent() -> None:
    """Two calls configure the global provider exactly once."""
    with (
        patch(_OTEL_TARGETS[0]),
        patch(_OTEL_TARGETS[1]),
        patch(_OTEL_TARGETS[2]),
        patch(_OTEL_TARGETS[3]) as mock_set,
    ):
        otel_module.configure_otel("localhost:4317")
        otel_module.configure_otel("localhost:4317")  # second call must be a no-op
    assert mock_set.call_count == 1


def test_configure_otel_sets_configured_flag() -> None:
    with (
        patch(_OTEL_TARGETS[0]),
        patch(_OTEL_TARGETS[1]),
        patch(_OTEL_TARGETS[2]),
        patch(_OTEL_TARGETS[3]),
    ):
        otel_module.configure_otel("localhost:4317")
    assert otel_module._configured


def test_configure_otel_graceful_on_import_error() -> None:
    """configure_otel survives ImportError without raising."""

    def _raise_import(*_: Any, **__: Any) -> None:
        raise ImportError("not installed")

    with patch("builtins.__import__", side_effect=_raise_import):
        try:
            otel_module.configure_otel("localhost:4317")
        except ImportError:
            pytest.fail("configure_otel must not propagate ImportError")


# ── OtelSpanExporter ──────────────────────────────────────────────────────────


def test_span_exporter_no_op_when_not_configured() -> None:
    """Context manager must be a no-op when _configured is False."""
    with otel_module.OtelSpanExporter("test-span", {"key": "value"}) as exp:
        assert exp._span is None


def test_span_exporter_no_op_exit_does_not_raise_on_exception() -> None:
    """__exit__ must not swallow the inner exception but must not raise its own."""
    with pytest.raises(ValueError):
        with otel_module.OtelSpanExporter("test-span"):
            raise ValueError("expected propagation")


def test_span_exporter_calls_start_and_end_when_configured() -> None:
    mock_span = MagicMock()
    mock_tracer = MagicMock()
    mock_tracer.start_span.return_value = mock_span

    otel_module._configured = True  # pretend configure_otel was called
    try:
        with patch("opentelemetry.trace.get_tracer", return_value=mock_tracer):
            with otel_module.OtelSpanExporter("my-span", {"k": "v"}):
                pass
    finally:
        otel_module._configured = False

    mock_tracer.start_span.assert_called_once_with("my-span", attributes={"k": "v"})
    mock_span.set_status.assert_called_once()
    mock_span.end.assert_called_once()


def test_span_exporter_sets_error_status_on_exception_when_configured() -> None:
    mock_span = MagicMock()
    mock_tracer = MagicMock()
    mock_tracer.start_span.return_value = mock_span

    otel_module._configured = True
    try:
        with patch("opentelemetry.trace.get_tracer", return_value=mock_tracer):
            with pytest.raises(RuntimeError):
                with otel_module.OtelSpanExporter("failing-span"):
                    raise RuntimeError("boom")
    finally:
        otel_module._configured = False

    # Status should have been set (with ERROR) and span ended
    mock_span.set_status.assert_called_once()
    mock_span.end.assert_called_once()
