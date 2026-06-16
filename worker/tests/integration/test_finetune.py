"""Integration test for the M9b production finetune-job path.

Tests the full end-to-end path: POST /api/v1/finetune-jobs creates a row and
dispatches a NATS task, a worker on the ``finetune_job`` pool runs the
mine → train → promote pipeline, completes via gRPC, and the orchestrator's
CompleteTask hook drives the finetune_jobs row to a terminal status readable
via GET /api/v1/finetune-jobs/{job_id}.

Requires ``make dev`` (Postgres + NATS + ClickHouse), a running orchestrator,
and a worker subscribed to the ``finetune_job`` pool:

    python -m helix.worker --pool finetune_job

Run via ``make test-integration``. Skipped automatically when HELIX_INTEGRATION
is not set, so the unit suite (``make test``) runs without the live stack.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import httpx

pytestmark = pytest.mark.skipif(
    not os.environ.get("HELIX_INTEGRATION"),
    reason="set HELIX_INTEGRATION=1 to run integration tests",
)

ORCHESTRATOR_HTTP = os.environ.get("ORCHESTRATOR_HTTP", "http://localhost:8080")
API_TOKEN = os.environ.get("HELIX_API_TOKEN", "dev-token")

# Terminal outcomes the worker can drive a job to. With an empty ClickHouse the
# miner finds no failures and the job lands on "no_failures" without training.
_TERMINAL = {"promoted", "archived", "no_failures", "no_triplets", "failed", "done"}


@pytest.fixture()
def headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {API_TOKEN}",
        "Content-Type": "application/json",
    }


def _create_job(client: httpx.Client, headers: dict[str, str]) -> dict[str, object]:
    resp = client.post(
        "/api/v1/finetune-jobs",
        headers=headers,
        json={
            "train_split": "evals/datasets/hotpotqa_train_1000.jsonl",
            "eval_split": "evals/datasets/hotpotqa_dev_100.jsonl",
        },
    )
    if resp.status_code == 503:
        pytest.skip("orchestrator built without a finetune store (503)")
    assert resp.status_code == 201, f"create finetune job failed: {resp.text}"
    body: dict[str, object] = resp.json()
    return body


def test_create_and_get_finetune_job(headers: dict[str, str]) -> None:
    """POST creates a job + dispatches; GET by id and list return it."""
    import httpx

    client = httpx.Client(base_url=ORCHESTRATOR_HTTP, timeout=10)

    job = _create_job(client, headers)
    job_id = job["id"]
    assert job_id, "expected id in response"
    assert job["run_id"], "expected run_id linking the dispatched run"
    assert job["status"] == "pending"

    r = client.get(f"/api/v1/finetune-jobs/{job_id}", headers=headers)
    assert r.status_code == 200, f"get finetune job failed: {r.text}"
    assert r.json()["id"] == job_id

    listing = client.get("/api/v1/finetune-jobs", headers=headers)
    assert listing.status_code == 200
    ids = {j["id"] for j in listing.json()}
    assert job_id in ids, "created job should appear in the list"


def test_finetune_job_reaches_terminal_state(headers: dict[str, str]) -> None:
    """Full path: worker on the finetune_job pool drives the job to terminal.

    Requires a worker subscribed to the ``finetune_job`` pool. With an empty
    ClickHouse the job lands on ``no_failures`` quickly (no training).
    """
    import httpx

    client = httpx.Client(base_url=ORCHESTRATOR_HTTP, timeout=10)

    job = _create_job(client, headers)
    job_id = job["id"]

    deadline = time.time() + 120
    status = "pending"
    while time.time() < deadline:
        r = client.get(f"/api/v1/finetune-jobs/{job_id}", headers=headers)
        assert r.status_code == 200
        status = r.json().get("status", "pending")
        if status in _TERMINAL:
            break
        time.sleep(2)

    assert status in _TERMINAL, f"job did not reach a terminal state within 120s; status={status}"


def test_create_finetune_job_requires_splits(headers: dict[str, str]) -> None:
    """POST with missing train/eval split is rejected with 400 (or 503)."""
    import httpx

    client = httpx.Client(base_url=ORCHESTRATOR_HTTP, timeout=10)
    resp = client.post("/api/v1/finetune-jobs", headers=headers, json={})
    if resp.status_code == 503:
        pytest.skip("orchestrator built without a finetune store (503)")
    assert resp.status_code == 400, f"expected 400 for missing splits, got {resp.status_code}"


def test_finetune_auth_required() -> None:
    import httpx

    client = httpx.Client(base_url=ORCHESTRATOR_HTTP, timeout=10)
    resp = client.get("/api/v1/finetune-jobs")
    assert resp.status_code == 401
