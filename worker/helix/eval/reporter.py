"""Eval reporter — posts scored per-example results to the orchestrator.

The slice persists eval results to SQLite locally. When the orchestrator is
reachable (``HELIX_ORCHESTRATOR_URL`` + ``HELIX_API_TOKEN`` set), the harness
additionally posts each example's scores to ``POST /api/v1/evals/{eval_id}/events``
so they land in ClickHouse and surface in the dashboard ``/evals`` view.

Reporting is best-effort: a network failure or a 503 (ClickHouse not configured
on the orchestrator) is logged and swallowed — it must never abort an eval run,
since the SQLite store already holds the authoritative results.
"""

from __future__ import annotations

import logging
import os
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)


class EvalReporter(Protocol):
    """Sink for per-example eval scores."""

    async def record(self, eval_id: str, events: list[dict[str, object]]) -> None: ...


class OrchestratorEvalReporter:
    """Posts eval events to the orchestrator REST API.

    Failures are logged and swallowed so the eval run always completes.
    """

    def __init__(self, base_url: str, token: str, *, timeout: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    async def record(self, eval_id: str, events: list[dict[str, object]]) -> None:
        if not events:
            return
        url = f"{self._base_url}/api/v1/evals/{eval_id}/events"
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json={"events": events}, headers=headers)
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("eval reporter POST failed for eval %s: %s", eval_id, exc)


def default_reporter() -> OrchestratorEvalReporter | None:
    """Return a reporter when the orchestrator env vars are set, else ``None``.

    ``None`` is the local-mode default: ``make eval`` with no orchestrator
    configured runs entirely in-process and reports nothing remotely.
    """
    base_url = os.environ.get("HELIX_ORCHESTRATOR_URL", "")
    token = os.environ.get("HELIX_API_TOKEN", "")
    if not base_url or not token:
        return None
    return OrchestratorEvalReporter(base_url, token)
