"""Chunker: size bounds, structural splitting, overlap, and edge cases.

Uses a whitespace word-count stub for the token counter so tests need no
tokenizer; ``max_tokens`` is therefore measured in words here.
"""

from helix.rag.chunker import chunk_document


def _words(text: str) -> int:
    return len(text.split())


def test_large_document_chunks_within_budget() -> None:
    paragraphs = [" ".join(f"p{i}w{j}" for j in range(50)) for i in range(40)]  # ~2000 words
    text = "\n\n".join(paragraphs)

    chunks = chunk_document(
        "doc1", text, "corpus", max_tokens=200, overlap_tokens=0, token_counter=_words
    )

    assert len(chunks) > 1
    assert all(c.token_count <= 200 for c in chunks)
    assert [c.chunk_id for c in chunks] == [f"doc1#chunk{i}" for i in range(len(chunks))]
    assert all(c.doc_id == "doc1" and c.source == "corpus" for c in chunks)


def test_tiny_document_is_one_chunk() -> None:
    chunks = chunk_document("d", "just a few words", "src", max_tokens=512, token_counter=_words)
    assert len(chunks) == 1
    assert chunks[0].chunk_id == "d#chunk0"
    assert chunks[0].text == "just a few words"
    assert chunks[0].token_count == 4


def test_empty_document_yields_no_chunks() -> None:
    assert chunk_document("d", "   \n\n  ", "src", token_counter=_words) == []


def test_oversized_paragraph_splits_on_sentences() -> None:
    # One paragraph (no blank lines), several sentences, over the budget.
    para = " ".join(f"Sentence number {i} has some filler words here." for i in range(20))
    chunks = chunk_document("d", para, "src", max_tokens=30, overlap_tokens=0, token_counter=_words)
    assert len(chunks) > 1
    assert all(c.token_count <= 30 for c in chunks)


def test_oversized_sentence_splits_on_words() -> None:
    # A single "sentence" with no punctuation, longer than the budget.
    sentence = " ".join(f"w{i}" for i in range(100))
    chunks = chunk_document(
        "d", sentence, "src", max_tokens=25, overlap_tokens=0, token_counter=_words
    )
    assert len(chunks) >= 4
    assert all(c.token_count <= 25 for c in chunks)


def test_overlap_duplicates_tokens_across_chunks() -> None:
    paragraphs = [" ".join(f"p{i}w{j}" for j in range(20)) for i in range(20)]
    text = "\n\n".join(paragraphs)
    doc_tokens = _words(text)

    no_overlap = chunk_document(
        "d", text, "src", max_tokens=100, overlap_tokens=0, token_counter=_words
    )
    with_overlap = chunk_document(
        "d", text, "src", max_tokens=100, overlap_tokens=40, token_counter=_words
    )

    # No overlap: chunks partition the words exactly.
    assert sum(c.token_count for c in no_overlap) == doc_tokens
    # Overlap: tail segments are repeated, so total exceeds the document size.
    assert sum(c.token_count for c in with_overlap) > doc_tokens
    assert all(c.token_count <= 100 for c in with_overlap)
