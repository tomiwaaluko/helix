"""OrchestratorEvalReporter: env gating, POST shape, and error swallowing."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from helix.eval import reporter as reporter_mod
from helix.eval.reporter import OrchestratorEvalReporter, default_reporter


class _FakeClient:
    """Stand-in for httpx.AsyncClient that records POSTs."""

    def __init__(
        self, recorder: list[dict[str, Any]], *, status: int, exc: Exception | None
    ) -> None:
        self._recorder = recorder
        self._status = status
        self._exc = exc

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False

    async def post(self, url: str, *, json: Any, headers: dict[str, str]) -> httpx.Response:
        self._recorder.append({"url": url, "json": json, "headers": headers})
        if self._exc is not None:
            raise self._exc
        return httpx.Response(self._status, request=httpx.Request("POST", url))


def _patch_client(
    monkeypatch: pytest.MonkeyPatch,
    recorder: list[dict[str, Any]],
    *,
    status: int = 204,
    exc: Exception | None = None,
) -> None:
    def factory(*_: object, **__: object) -> _FakeClient:
        return _FakeClient(recorder, status=status, exc=exc)

    monkeypatch.setattr(reporter_mod.httpx, "AsyncClient", factory)


def test_default_reporter_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HELIX_ORCHESTRATOR_URL", raising=False)
    monkeypatch.delenv("HELIX_API_TOKEN", raising=False)
    assert default_reporter() is None


def test_default_reporter_none_when_token_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HELIX_ORCHESTRATOR_URL", "http://orch:8080")
    monkeypatch.delenv("HELIX_API_TOKEN", raising=False)
    assert default_reporter() is None


def test_default_reporter_built_when_both_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HELIX_ORCHESTRATOR_URL", "http://orch:8080")
    monkeypatch.setenv("HELIX_API_TOKEN", "secret")
    rep = default_reporter()
    assert isinstance(rep, OrchestratorEvalReporter)


async def test_record_posts_events(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder: list[dict[str, Any]] = []
    _patch_client(monkeypatch, recorder)
    rep = OrchestratorEvalReporter("http://orch:8080/", "secret")

    events = [{"example_id": "ex-1", "scorer": "answer_f1", "score": 0.5, "passed": False}]
    await rep.record("eval-7", events)

    assert len(recorder) == 1
    call = recorder[0]
    assert call["url"] == "http://orch:8080/api/v1/evals/eval-7/events"
    assert call["json"] == {"events": events}
    assert call["headers"]["Authorization"] == "Bearer secret"


async def test_record_noop_on_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder: list[dict[str, Any]] = []
    _patch_client(monkeypatch, recorder)
    rep = OrchestratorEvalReporter("http://orch:8080", "secret")
    await rep.record("eval-7", [])
    assert recorder == []


async def test_record_swallows_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder: list[dict[str, Any]] = []
    _patch_client(monkeypatch, recorder, exc=httpx.ConnectError("boom"))
    rep = OrchestratorEvalReporter("http://orch:8080", "secret")
    event = {"example_id": "ex-1", "scorer": "s", "score": 1.0, "passed": True}
    # Must not raise.
    await rep.record("eval-7", [event])
    assert len(recorder) == 1


async def test_record_swallows_503(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder: list[dict[str, Any]] = []
    _patch_client(monkeypatch, recorder, status=503)
    rep = OrchestratorEvalReporter("http://orch:8080", "secret")
    event = {"example_id": "ex-1", "scorer": "s", "score": 1.0, "passed": True}
    # 503 (ClickHouse not configured) is logged, not raised.
    await rep.record("eval-7", [event])
    assert len(recorder) == 1
