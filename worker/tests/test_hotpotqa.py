"""HotpotQA eval prep: schema, both shapes, slicing, referential integrity."""

from helix.eval.corpus import build_corpus, wiki_doc_id
from helix.eval.hotpotqa import build_questions, missing_doc_ids, supporting_doc_ids

_EXAMPLE = {
    "question": "Who is older, Alpha or Beta?",
    "answer": "Alpha",
    "type": "comparison",
    "supporting_facts": {"title": ["Alpha", "Beta"], "sent_id": [0, 2]},
    "context": {
        "title": ["Alpha", "Beta"],
        "sentences": [["A1. ", "A2."], ["B1. ", "B2. ", "B3."]],
    },
}
_RAW_SHAPE = {
    "question": "Where was Gamma born?",
    "answer": "Rome",
    "type": "bridge",
    "supporting_facts": [["Gamma", 1]],
}


def test_build_questions_matches_schema() -> None:
    (row,) = build_questions([_EXAMPLE])
    assert row["id"] == "hotpotqa_dev_001"
    assert row["input"] == {"question": "Who is older, Alpha or Beta?"}
    assert row["expected_output"]["answer"] == "Alpha"
    assert row["expected_output"]["supporting_facts"] == [
        {"doc_id": wiki_doc_id("Alpha"), "sent": 0},
        {"doc_id": wiki_doc_id("Beta"), "sent": 2},
    ]
    assert row["metadata"] == {"hops": 2, "type": "comparison"}


def test_build_questions_handles_raw_supporting_shape() -> None:
    (row,) = build_questions([_RAW_SHAPE])
    assert row["expected_output"]["supporting_facts"] == [
        {"doc_id": wiki_doc_id("Gamma"), "sent": 1}
    ]
    assert row["metadata"]["hops"] == 1


def test_build_questions_slices_and_numbers() -> None:
    examples = [dict(_EXAMPLE, question=f"q{i}") for i in range(5)]
    rows = build_questions(examples, start=2, count=2, id_start=3)
    assert [r["id"] for r in rows] == ["hotpotqa_dev_003", "hotpotqa_dev_004"]
    assert [r["input"]["question"] for r in rows] == ["q2", "q3"]


def test_build_questions_train_split_is_prefixed_and_disjoint() -> None:
    # The fine-tune mining split uses a distinct id prefix and a later offset, so
    # its ids never collide with the dev split's.
    examples = [dict(_EXAMPLE, question=f"q{i}") for i in range(5)]
    dev = build_questions(examples, start=0, count=2)
    train = build_questions(examples, start=2, count=2, id_prefix="hotpotqa_train")
    assert [r["id"] for r in train] == ["hotpotqa_train_001", "hotpotqa_train_002"]
    assert {r["input"]["question"] for r in train} == {"q2", "q3"}
    assert set(r["id"] for r in dev).isdisjoint(r["id"] for r in train)


def test_referential_integrity_with_corpus_builder() -> None:
    # The corpus built from the same example contains every supporting title.
    questions = build_questions([_EXAMPLE])
    corpus_ids = {doc.id for doc in build_corpus([_EXAMPLE])}
    assert missing_doc_ids(questions, corpus_ids) == set()


def test_missing_doc_ids_detects_gaps() -> None:
    questions = build_questions([_EXAMPLE])
    assert supporting_doc_ids(questions) == {wiki_doc_id("Alpha"), wiki_doc_id("Beta")}
    # Corpus missing "Beta" → it is reported missing.
    assert missing_doc_ids(questions, {wiki_doc_id("Alpha")}) == {wiki_doc_id("Beta")}
