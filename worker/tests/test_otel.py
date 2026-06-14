"""Unit tests for helix.otel — OTel span export integration."""

import sys
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


def test_configure_otel_idempotent() -> None:
    """Calling configure_otel twice with a valid endpoint only configures once."""
    mock_provider = MagicMock()
    mock_trace = MagicMock()

    otel_patches: dict[str, Any] = {
        "opentelemetry.trace": mock_trace,
        "opentelemetry.sdk.trace": MagicMock(TracerProvider=MagicMock(return_value=mock_provider)),
        "opentelemetry.sdk.trace.export": MagicMock(),
        "opentelemetry.sdk.resources": MagicMock(),
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter": MagicMock(),
    }

    with patch.dict(sys.modules, otel_patches):
        otel_module.configure_otel("localhost:4317")
        otel_module.configure_otel("localhost:4317")  # second call must be no-op

    assert mock_trace.set_tracer_provider.call_count == 1


def test_configure_otel_sets_configured_flag() -> None:
    mock_trace = MagicMock()
    otel_patches: dict[str, Any] = {
        "opentelemetry.trace": mock_trace,
        "opentelemetry.sdk.trace": MagicMock(),
        "opentelemetry.sdk.trace.export": MagicMock(),
        "opentelemetry.sdk.resources": MagicMock(),
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter": MagicMock(),
    }

    with patch.dict(sys.modules, otel_patches):
        otel_module.configure_otel("localhost:4317")

    assert otel_module._configured


def test_configure_otel_graceful_on_import_error() -> None:
    """configure_otel survives ImportError without raising."""

    def _raise_import(*_: Any, **__: Any) -> None:
        raise ImportError("not installed")

    with patch("builtins.__import__", side_effect=_raise_import):
        # Should not raise even when the import itself fails.
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

    mock_trace = MagicMock()
    mock_trace.get_tracer.return_value = mock_tracer
    mock_trace.StatusCode.OK = "OK"
    mock_trace.StatusCode.ERROR = "ERROR"
    mock_trace.Status = MagicMock()

    otel_module._configured = True  # pretend configure_otel was called
    try:
        with patch.dict(sys.modules, {"opentelemetry.trace": mock_trace}):
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

    mock_trace = MagicMock()
    mock_trace.get_tracer.return_value = mock_tracer

    otel_module._configured = True
    try:
        with patch.dict(sys.modules, {"opentelemetry.trace": mock_trace}):
            with pytest.raises(RuntimeError):
                with otel_module.OtelSpanExporter("failing-span"):
                    raise RuntimeError("boom")
    finally:
        otel_module._configured = False

    # Status should have been set (with ERROR) and span ended
    mock_span.set_status.assert_called_once()
    mock_span.end.assert_called_once()
