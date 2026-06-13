.PHONY: seed eval eval-full eval-final finetune test test-eval-smoke lint fmt dev dev-down

# Boot Qdrant (the only external dependency for the slice)
dev:
	docker compose -f infra/compose/docker-compose.yml up -d qdrant

dev-down:
	docker compose -f infra/compose/docker-compose.yml down -v

# Download corpus + datasets, embed, index into Qdrant
seed: data/corpus.jsonl evals/datasets/hotpotqa_dev_100.jsonl
	python -m helix.cli index --corpus data/corpus.jsonl --collection corpus.base

data/corpus.jsonl:
	python scripts/prepare_corpus.py

evals/datasets/hotpotqa_dev_100.jsonl:
	python scripts/prepare_hotpotqa.py

# Eval: 100-question smoke test (the deliverable command)
eval:
	python -m helix.cli eval \
	  --workflow deep_research \
	  --dataset evals/datasets/hotpotqa_dev_100.jsonl \
	  --scorers answer_f1,citation_precision,retrieval_recall@10 \
	  --concurrency 4 \
	  --output evals/baselines/hotpotqa_dev_100_baseline.json

# Fine-tune loop: mine retrieval failures on the train split, fine-tune the
# embedder, and canary-promote on the dev set (swaps corpus.active only on lift).
finetune:
	python -m helix.cli finetune \
	  --train evals/datasets/hotpotqa_train_1000.jsonl \
	  --eval evals/datasets/hotpotqa_dev_100.jsonl \
	  --corpus data/corpus.jsonl \
	  --concurrency 4

# Alias for CLAUDE.md compatibility
test-eval-smoke: eval

# Full eval (placeholder — same as eval in the slice)
eval-full: eval

# Held-out hard set — the ONLY command allowed to read the holdout file
eval-final:
	HELIX_HOLDOUT_UNLOCK=1 python -m helix.cli eval \
	  --workflow deep_research \
	  --dataset evals/datasets/hotpotqa_dev_holdout_500.jsonl \
	  --scorers answer_f1,citation_precision,retrieval_recall@10 \
	  --concurrency 4 \
	  --output evals/baselines/hotpotqa_holdout_500_result.json

test:
	cd worker && python3.12 -m pytest tests/ -x -q

lint:
	PYTHONPATH=worker python3.12 scripts/check_holdout_integrity.py
	cd worker && ruff check . && python3.12 -m mypy --strict helix/

fmt:
	cd worker && ruff format . && ruff check --fix .
