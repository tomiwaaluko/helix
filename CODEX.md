# CODEX.md

Codex CLI reads `AGENTS.md` natively — that is the operating manual. This file exists for symmetry with `CLAUDE.md` and as a note to the maintainer; Codex does not auto-read it, so keep nothing load-bearing here.

## Codex CLI specifics

- The operating manual is `AGENTS.md`. Codex picks it up automatically. Do not duplicate its contents here.
- The maintainer runs Codex with `--yolo` (`--dangerously-bypass-approvals-and-sandbox`) only after reviewing a plan.
- Codex handles mechanical implementation — embedder (Task 7), Qdrant adapter (Task 8), chunker (Task 9), BM25 (Task 11). Design-heavy tasks go to Claude Code.
- We are in slice phase. The authoritative scope is `docs/vertical-slice-plan.md`.
- Before ending a session, update `HANDOFF.md` per the Session protocol in `AGENTS.md`.
