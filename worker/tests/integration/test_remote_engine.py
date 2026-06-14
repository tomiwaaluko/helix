"""Integration test for the M1 remote engine.

Tests the full end-to-end path: submit a run via REST, worker picks it up
via NATS, completes via gRPC, result readable in Postgres.

Requires ``make dev`` (Postgres + NATS) and a running orchestrator binary.
Run via ``make test-integration``.

Skipped automatically when HELIX_INTEGRATION is not set, so the unit test
suite (``make test``) can run without the live stack.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("HELIX_INTEGRATION"),
    reason="set HELIX_INTEGRATION=1 to run integration tests",
)

ORCHESTRATOR_HTTP = os.environ.get("ORCHESTRATOR_HTTP", "http://localhost:8080")
API_TOKEN = os.environ.get("HELIX_API_TOKEN", "dev-token")


@pytest.fixture()
def headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {API_TOKEN}",
        "Content-Type": "application/json",
    }


def test_submit_and_poll_run(headers: dict[str, str]) -> None:
    """POST /api/v1/runs → poll until succeeded or timeout 30s."""
    import httpx

    client = httpx.Client(base_url=ORCHESTRATOR_HTTP, timeout=10)

    resp = client.post(
        "/api/v1/runs",
        headers=headers,
        json={
            "workflow_name": "deep_research",
            "input": {"question": "What is the capital of France?"},
            "submitted_by": "integration-test",
        },
    )
    assert resp.status_code == 201, f"create run failed: {resp.text}"
    body = resp.json()
    run_id = body["id"]
    assert run_id, "expected run_id in response"

    deadline = time.time() + 30
    status = "pending"
    while time.time() < deadline:
        r = client.get(f"/api/v1/runs/{run_id}", headers=headers)
        assert r.status_code == 200
        status = r.json().get("status", "pending")
        if status in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(1)

    assert status == "succeeded", f"run did not succeed within 30s; final status: {status}"


def test_list_runs(headers: dict[str, str]) -> None:
    import httpx

    client = httpx.Client(base_url=ORCHESTRATOR_HTTP, timeout=10)
    resp = client.get("/api/v1/runs", headers=headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_cancel_run(headers: dict[str, str]) -> None:
    """Create a run and immediately cancel it."""
    import httpx

    client = httpx.Client(base_url=ORCHESTRATOR_HTTP, timeout=10)

    resp = client.post(
        "/api/v1/runs",
        headers=headers,
        json={
            "workflow_name": "deep_research",
            "input": {"question": "cancellation test " + uuid.uuid4().hex},
            "submitted_by": "integration-test",
        },
    )
    assert resp.status_code == 201
    run_id = resp.json()["id"]

    cancel_resp = client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers)
    assert cancel_resp.status_code == 200

    r = client.get(f"/api/v1/runs/{run_id}", headers=headers)
    assert r.json()["status"] == "cancelled"


def test_auth_required() -> None:
    import httpx

    client = httpx.Client(base_url=ORCHESTRATOR_HTTP, timeout=10)
    resp = client.get("/api/v1/runs")
    assert resp.status_code == 401
