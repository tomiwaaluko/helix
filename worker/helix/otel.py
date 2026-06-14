"""OTel span export integration for the Helix worker.

When configure_otel() is called with a non-empty endpoint, spans emitted via
OtelSpanExporter are forwarded to cmd/collector via OTLP/gRPC in addition to
the existing JSONL path.  When configure_otel() receives None or an empty
string, the module is a no-op: all classes and functions are importable but
produce no output.

Usage::

    # at worker startup (RemoteEngine.__init__ or __main__):
    from helix.otel import configure_otel
    configure_otel(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"))

    # around a unit of work:
    from helix.otel import OtelSpanExporter
    with OtelSpanExporter("deep_research", {"run_id": run_id, "task_id": task_id}):
        result = await workflow(...)
"""

from __future__ import annotations

__all__ = ["OtelSpanExporter", "configure_otel"]

import logging
from types import TracebackType
from typing import Any

_log = logging.getLogger(__name__)

# Set to True once configure_otel() wires up a real provider.
_configured: bool = False


def configure_otel(endpoint: str | None) -> None:
    """Wire up the global OTel TracerProvider.

    No-op when *endpoint* is falsy or when already configured.
    Catches ImportError gracefully so the worker starts even when the
    optional opentelemetry packages are not installed.
    """
    global _configured

    if not endpoint or _configured:
        return

    try:
        from opentelemetry import trace as otel_trace  # type: ignore[import-untyped]
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # type: ignore[import-untyped]
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource  # type: ignore[import-untyped]
        from opentelemetry.sdk.trace import TracerProvider  # type: ignore[import-untyped]
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,  # type: ignore[import-untyped]
        )

        resource = Resource.create({"service.name": "helix-worker"})
        exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        otel_trace.set_tracer_provider(provider)
        _configured = True
        _log.info("OTel configured endpoint=%s", endpoint)
    except ImportError:
        _log.warning(
            "opentelemetry packages not installed — OTel export disabled. "
            "Install opentelemetry-sdk and opentelemetry-exporter-otlp-proto-grpc."
        )


class OtelSpanExporter:
    """Context manager that records one span via the OTel SDK when configured.

    When the module has not been configured (configure_otel() was not called
    with an endpoint), this class is a silent no-op: entering and exiting the
    context manager do nothing.
    """

    def __init__(
        self,
        name: str,
        attributes: dict[str, str] | None = None,
    ) -> None:
        self._name = name
        self._attributes: dict[str, str] = attributes or {}
        self._span: Any = None

    def __enter__(self) -> OtelSpanExporter:
        if not _configured:
            return self
        try:
            import opentelemetry.trace as otel_trace  # type: ignore[import-untyped]

            tracer = otel_trace.get_tracer("helix.worker")
            self._span = tracer.start_span(self._name, attributes=self._attributes)
        except Exception:
            self._span = None
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._span is None:
            return
        try:
            import opentelemetry.trace as otel_trace  # type: ignore[import-untyped]

            if exc_type is not None:
                self._span.set_status(otel_trace.Status(otel_trace.StatusCode.ERROR, str(exc_val)))
            else:
                self._span.set_status(otel_trace.Status(otel_trace.StatusCode.OK))
            self._span.end()
        except Exception:
            pass
