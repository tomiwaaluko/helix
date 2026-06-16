"""BM25 index: ranking sanity, ordering, persistence, and empty corpus."""

from pathlib import Path

from helix.rag.chunker import Chunk
from helix.tools.bm25 import BM25Index, ScoredChunk


def _chunk(cid: str, text: str) -> Chunk:
    return Chunk(chunk_id=cid, doc_id=cid.split("#")[0], text=text, source="s", token_count=0)


_CORPUS = [
    _chunk("d0#chunk0", "the quick brown fox jumps"),
    _chunk("d1#chunk0", "apple banana cherry fruit"),
    _chunk("d2#chunk0", "quick apple pie recipe"),
]


def test_search_ranks_matching_chunk_first() -> None:
    index = BM25Index.build(_CORPUS)
    results = index.search("apple", top_k=3)

    assert results
    assert isinstance(results[0], ScoredChunk)
    assert "apple" in results[0].text.lower()
    assert results[0].score > 0.0


def test_results_sorted_by_score_descending() -> None:
    index = BM25Index.build(_CORPUS)
    results = index.search("quick apple", top_k=3)
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)


def test_top_k_limits_results() -> None:
    index = BM25Index.build(_CORPUS)
    assert len(index.search("quick", top_k=1)) == 1


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    index = BM25Index.build(_CORPUS)
    path = tmp_path / "bm25_index.pkl"
    index.save(path)

    loaded = BM25Index.load(path)
    before = index.search("apple pie", top_k=2)
    after = loaded.search("apple pie", top_k=2)
    assert [c.chunk_id for c in after] == [c.chunk_id for c in before]
    assert [c.score for c in after] == [c.score for c in before]


def test_empty_corpus_returns_no_results() -> None:
    index = BM25Index.build([])
    assert index.search("anything", top_k=5) == []
