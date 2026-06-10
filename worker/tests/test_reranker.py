"""Reranker: scoring order, top_k limit, and empty input."""

from helix.tools.reranker import RankedPassage, Reranker


def _by_length(pairs: list[tuple[str, str]]) -> list[float]:
    return [float(len(passage.split())) for _, passage in pairs]


def test_rerank_orders_by_score_and_limits_top_k() -> None:
    reranker = Reranker(predict_fn=_by_length)
    out = reranker.rerank("q", ["a", "b b", "c c c"], top_k=2)
    assert [p.passage for p in out] == ["c c c", "b b"]
    assert out[0] == RankedPassage(index=2, passage="c c c", score=3.0)


def test_rerank_empty_passages() -> None:
    reranker = Reranker(predict_fn=lambda pairs: [])
    assert reranker.rerank("q", [], top_k=5) == []
