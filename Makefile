.PHONY: seed eval eval-full eval-final test test-eval-smoke lint fmt dev dev-down

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
	cd worker && python -m pytest tests/ -x -q

lint:
	cd worker && ruff check . && mypy --strict helix/

fmt:
	cd worker && ruff format . && ruff check --fix .
