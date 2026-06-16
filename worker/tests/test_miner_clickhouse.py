"""Unit tests for mine_from_clickhouse — mocks httpx, no real ClickHouse needed."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from helix.eval.harness import Example
from helix.rag.miner.miner import mine_from_clickhouse
from helix.runtime.sqlite_store import EvalResultRow

_GOLD_DOC = "wiki_gold"
_WRONG_DOC = "wiki_wrong"
_CORPUS = {_GOLD_DOC: "Gold passage text.", _WRONG_DOC: "Wrong passage text."}
_QUESTION = "Who was the first president of the United States?"

_EXAMPLE = Example(
    id="ex_001",
    input={"question": _QUESTION},
    expected_output={
        "answer": "George Washington",
        "supporting_facts": [{"doc_id": _GOLD_DOC, "sent": 0}],
    },
    metadata={},
)

_EVAL_FAIL = EvalResultRow(
    id="r1",
    eval_id="eval-1",
    example_id="ex_001",
    scorer="retrieval_recall@10",
    score=0.0,
    details=None,
    created_at="2026-06-15T00:00:00+00:00",
)


def _make_httpx_client(wf_rows: list[dict[str, Any]], ret_rows: list[dict[str, Any]]) -> Any:
    """Return a mock httpx.AsyncClient whose post() returns pre-canned JSONEachRow text."""
    call_count = 0

    async def _post(url: str, *, content: bytes, headers: dict[str, str]) -> MagicMock:
        nonlocal call_count
        call_count += 1
        sql = content.decode()
        if "deep_research" in sql:
            rows = wf_rows
        else:
            rows = ret_rows
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.text = "\n".join(json.dumps(r) for r in rows)
        return resp

    client = AsyncMock()
    client.post = _post
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


async def test_mine_from_clickhouse_returns_empty_when_no_workflow_spans() -> None:
    wf_rows: list[dict[str, Any]] = []
    ret_rows: list[dict[str, Any]] = []

    mock_client = _make_httpx_client(wf_rows, ret_rows)
    with patch("helix.rag.miner.miner.httpx.AsyncClient", return_value=mock_client):
        cases = await mine_from_clickhouse(
            [_EXAMPLE],
            [_EVAL_FAIL],
            _CORPUS,
            ch_http_url="http://localhost:8123",
        )

    assert cases == []


async def test_mine_from_clickhouse_mines_failure_when_gold_missed() -> None:
    trace_id = "trace-ch-001"
    wf_rows = [{"trace_id": trace_id, "question": _QUESTION}]
    ret_rows = [
        {
            "trace_id": trace_id,
            "span_id": "sp-ret-1",
            "query": _QUESTION,
            "results": [[_WRONG_DOC, 0.8, 1]],
        }
    ]

    mock_client = _make_httpx_client(wf_rows, ret_rows)
    with patch("helix.rag.miner.miner.httpx.AsyncClient", return_value=mock_client):
        cases = await mine_from_clickhouse(
            [_EXAMPLE],
            [_EVAL_FAIL],
            _CORPUS,
            ch_http_url="http://localhost:8123",
        )

    assert len(cases) == 1
    assert cases[0].gold_doc_id == _GOLD_DOC
    assert cases[0].example_id == "ex_001"


async def test_mine_from_clickhouse_no_failure_when_gold_retrieved() -> None:
    """If the gold doc is in the retrieved set, no FailureCase is emitted."""
    trace_id = "trace-ch-002"
    wf_rows = [{"trace_id": trace_id, "question": _QUESTION}]
    ret_rows = [
        {
            "trace_id": trace_id,
            "span_id": "sp-ret-2",
            "query": _QUESTION,
            "results": [[_GOLD_DOC, 0.95, 1], [_WRONG_DOC, 0.6, 2]],
        }
    ]

    eval_pass = EvalResultRow(
        id="r2",
        eval_id="eval-1",
        example_id="ex_001",
        scorer="retrieval_recall@10",
        score=1.0,
        details=None,
        created_at="2026-06-15T00:00:00+00:00",
    )

    mock_client = _make_httpx_client(wf_rows, ret_rows)
    with patch("helix.rag.miner.miner.httpx.AsyncClient", return_value=mock_client):
        cases = await mine_from_clickhouse(
            [_EXAMPLE],
            [eval_pass],
            _CORPUS,
            ch_http_url="http://localhost:8123",
        )

    assert cases == []


async def test_mine_from_clickhouse_parses_tuple_results() -> None:
    """Array(Tuple) result rows like [passage_id, score, rank] are parsed to doc_id + score."""
    trace_id = "trace-ch-003"
    wf_rows = [{"trace_id": trace_id, "question": _QUESTION}]
    # ClickHouse returns tuples as lists in JSONEachRow.
    ret_rows = [
        {
            "trace_id": trace_id,
            "span_id": "sp-ret-3",
            "query": _QUESTION,
            "results": [[_WRONG_DOC, 0.77, 1]],
        }
    ]

    mock_client = _make_httpx_client(wf_rows, ret_rows)
    with patch("helix.rag.miner.miner.httpx.AsyncClient", return_value=mock_client):
        cases = await mine_from_clickhouse(
            [_EXAMPLE],
            [_EVAL_FAIL],
            _CORPUS,
            ch_http_url="http://localhost:8123",
        )

    assert len(cases) == 1
    # retrieved_top_k is built from the rep_span results
    assert cases[0].hard_negatives == [_WRONG_DOC]
