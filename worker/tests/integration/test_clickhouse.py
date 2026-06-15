"""Integration tests for the ClickHouse OTel dual-write path.

Skipped unless HELIX_INTEGRATION=1 and CLICKHOUSE_HTTP_URL is set.
These tests exercise the mine_from_clickhouse path end-to-end against a real
ClickHouse instance (seeded with synthetic span data) and the miner's classification
logic against real query results.

    HELIX_INTEGRATION=1 \
    CLICKHOUSE_HTTP_URL=http://localhost:8123 \
    python -m pytest tests/integration/test_clickhouse.py -v
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from helix.rag.miner.miner import mine_from_clickhouse

pytestmark = pytest.mark.skipif(
    not os.environ.get("HELIX_INTEGRATION"),
    reason="integration test; set HELIX_INTEGRATION=1 with CLICKHOUSE_HTTP_URL set",
)

CH_HTTP_URL = os.environ.get("CLICKHOUSE_HTTP_URL", "http://localhost:8123")


def _ch_exec(sql: str) -> None:
    """Execute a DDL or INSERT directly against ClickHouse HTTP interface."""
    resp = httpx.post(CH_HTTP_URL, content=sql.encode())
    resp.raise_for_status()


def _seed_spans(run_id: str, trace_id: str) -> None:
    """Insert synthetic workflow + retrieval spans for a test run."""
    now_ns = int(time.time() * 1e9)
    wf_row = {
        "trace_id": trace_id,
        "span_id": f"span-wf-{run_id}",
        "parent_span_id": "",
        "run_id": run_id,
        "task_id": "",
        "attempt_number": 0,
        "name": "deep_research",
        "kind": "workflow",
        "start_time": now_ns,
        "end_time": now_ns + 1_000_000_000,
        "status": "ok",
        "status_message": "",
        "service_name": "helix-worker",
        "worker_id": "",
        "attributes": json.dumps({"workflow": "deep_research"}),
    }
    ret_row = {
        "trace_id": trace_id,
        "span_id": f"span-ret-{run_id}",
        "parent_span_id": f"span-wf-{run_id}",
        "run_id": run_id,
        "task_id": "",
        "attempt_number": 0,
        "name": "retrieve",
        "kind": "retrieval",
        "start_time": now_ns + 100_000_000,
        "end_time": now_ns + 200_000_000,
        "status": "ok",
        "status_message": "",
        "service_name": "helix-worker",
        "worker_id": "",
        "attributes": json.dumps(
            {
                "kind": "retrieval",
                "query": "What is the capital of France?",
                "retriever": "hybrid+reranked",
                "top_k": "10",
                "results": json.dumps([["doc-paris", 0.95, 1], ["doc-london", 0.80, 2]]),
            }
        ),
    }
    for row in (wf_row, ret_row):
        _ch_exec(f"INSERT INTO spans FORMAT JSONEachRow {json.dumps(row)}")


@pytest.mark.asyncio
async def test_mine_from_clickhouse_returns_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """mine_from_clickhouse finds failures when gold doc is not in retrieved results."""
    run_id = f"itest-{uuid.uuid4().hex[:8]}"
    trace_id = f"trace-{uuid.uuid4().hex[:8]}"
    _seed_spans(run_id, trace_id)

    # Fake dataset row: gold doc is "doc-berlin" (not in the retrieved docs above).
    dataset_row: dict[str, Any] = {
        "id": "ex-001",
        "input": {"question": "What is the capital of France?"},
        "expected_output": {
            "answer": "Paris",
            "supporting_facts": [{"doc_id": "doc-berlin", "sent": 0}],
        },
    }

    cases = await mine_from_clickhouse(
        train_dataset=[dataset_row],
        eval_results={},
        corpus=MagicMock(),
        ch_http_url=CH_HTTP_URL,
    )

    # doc-berlin is not in the retrieved results → should produce a failure case.
    assert len(cases) >= 1
    assert cases[0].query == "What is the capital of France?"
    assert cases[0].gold_passage_id == "doc-berlin"


@pytest.mark.asyncio
async def test_mine_from_clickhouse_no_failure_when_gold_retrieved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mine_from_clickhouse produces no failure when gold doc is retrieved."""
    run_id = f"itest-{uuid.uuid4().hex[:8]}"
    trace_id = f"trace-{uuid.uuid4().hex[:8]}"
    _seed_spans(run_id, trace_id)

    # Gold doc IS in the retrieved results.
    dataset_row: dict[str, Any] = {
        "id": "ex-001",
        "input": {"question": "What is the capital of France?"},
        "expected_output": {
            "answer": "Paris",
            "supporting_facts": [{"doc_id": "doc-paris", "sent": 0}],
        },
    }

    cases = await mine_from_clickhouse(
        train_dataset=[dataset_row],
        eval_results={},
        corpus=MagicMock(),
        ch_http_url=CH_HTTP_URL,
    )

    assert cases == []
