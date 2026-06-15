.PHONY: seed eval eval-full eval-final finetune test test-eval-smoke lint fmt dev dev-down \
        seed-bright check-bright proto build orchestrator collector worker test-integration \
        web web-install web-build web-lint web-test web-gate

# Boot Qdrant + Postgres + NATS (M1 stack)
dev:
	docker compose -f infra/compose/docker-compose.yml up -d

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

# Regenerate protobuf stubs (Go + Python)
proto:
	protoc \
	  -I proto \
	  --go_out=gen/go --go_opt=paths=source_relative \
	  --go-grpc_out=gen/go --go-grpc_opt=paths=source_relative \
	  proto/helix/v1/types.proto proto/helix/v1/orchestrator.proto
	cd worker && python3.12 -m grpc_tools.protoc \
	  -I ../proto \
	  --python_out=. \
	  --grpc_python_out=. \
	  ../proto/helix/v1/types.proto ../proto/helix/v1/orchestrator.proto

# Build Go binaries (orchestrator + collector)
build:
	go build -o bin/orchestrator ./cmd/orchestrator/
	go build -o bin/collector ./cmd/collector/

# Run orchestrator against local dev stack
orchestrator: build
	DATABASE_URL=postgres://helix:helix@localhost:5432/helix?sslmode=disable \
	NATS_URL=nats://localhost:4222 \
	HELIX_API_TOKEN=dev-token \
	./bin/orchestrator

# Run OTel collector against local dev stack
collector: build
	CLICKHOUSE_URL=clickhouse://helix:helix@localhost:9000/default \
	OTLP_GRPC_PORT=4317 \
	./bin/collector

# Run Python worker against local dev stack (remote mode)
worker:
	cd worker && python3.12 -m helix.worker \
	  --orchestrator grpc://localhost:50051 \
	  --nats nats://localhost:4222 \
	  --pool research

test: web-test
	cd worker && python3.12 -m pytest tests/ -x -q --ignore=tests/integration
	go test ./cmd/... ./internal/... ./gen/... -count=1

# End-to-end integration test (requires make dev running)
test-integration:
	cd worker && python3.12 -m pytest tests/integration/ -x -q -v

# ── Dashboard (web/) ──────────────────────────────────────────────────────────
# Kept out of the node-free `make test`/`make lint` gate; run `make web-gate`
# explicitly (CI wiring lands in M5). `make web` runs the dev server, which needs
# a running orchestrator (ORCHESTRATOR_URL, HELIX_API_TOKEN).
web: web-install
	cd web && npm run dev

web-install:
	cd web && npm install

web-build: web-install
	cd web && npm run gen:types && npm run build

web-lint: web-install
	cd web && npm run lint && npm run format:check && npm run typecheck

web-test: web-install
	cd web && npm run test

# Full dashboard gate: types, lint, typecheck, unit tests, production build.
web-gate: web-lint web-test web-build

# BRIGHT biology mini-experiment
# Downloads BRIGHT, builds corpus (~10.5k docs) and BM25 index, indexes into Qdrant.
seed-bright: evals/datasets/bright_biology_dev.jsonl
	python3.12 -m helix.cli index \
	  --corpus data/bright_corpus.jsonl \
	  --collection corpus.bright \
	  --alias corpus.bright.active \
	  --bm25 data/bright_bm25_index.pkl

evals/datasets/bright_biology_dev.jsonl data/bright_corpus.jsonl:
	python3.12 scripts/prepare_bright.py

# Measure direct-retrieval recall@10 on BRIGHT biology (no LLM calls).
check-bright:
	PYTHONPATH=worker python3.12 scripts/check_bright_recall.py

# Mine failures on biology train split, fine-tune, canary-promote on biology canary split.
# Uses corpus.bright as the base arm; swaps corpus.bright.active alias on promotion.
finetune-bright:
	python3.12 -m helix.cli finetune \
	  --train evals/datasets/bright_biology_train.jsonl \
	  --eval evals/datasets/bright_biology_canary.jsonl \
	  --corpus data/bright_corpus.jsonl \
	  --scorers retrieval_recall@10 \
	  --bm25 data/bright_bm25_index.pkl \
	  --qdrant-path data/qdrant \
	  --collection corpus.bright \
	  --promotion-alias corpus.bright.active \
	  --concurrency 4

lint: web-lint
	PYTHONPATH=worker python3.12 scripts/check_holdout_integrity.py
	cd worker && ruff check . && python3.12 -m mypy --strict helix/
	golangci-lint run ./...

fmt:
	cd worker && ruff format . && ruff check --fix .
	gofmt -w cmd/ internal/ gen/
