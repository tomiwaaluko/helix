# CLAUDE.md

@AGENTS.md

## Claude Code specifics

This file is intentionally thin. The operating manual is `AGENTS.md`, imported above; it governs every agent (Claude Code and Codex alike). Edit `AGENTS.md` for anything that should apply to all agents — do not re-add the manual here.

- The maintainer runs Claude Code with `--dangerously-skip-permissions` only after reviewing a plan. Default to plan-first for schema, proto, event-bus, or public API (REST/SDK/CLI) changes; ship-first for self-contained changes. See "How the maintainer works with agents" in `AGENTS.md`.
- Reserve Claude Code for the load-bearing tasks — engine/contextvars (Task 5), `deep_research` prompt tuning (Task 15), eval-harness aggregation (Task 16). Mechanical tasks go to Codex.
- We are in slice phase. The authoritative scope is `docs/vertical-slice-plan.md`. Anything about Go, NATS, Postgres, ClickHouse, or proto is target state, not on disk yet.
- Before ending a session, update `HANDOFF.md` per the Session protocol in `AGENTS.md`.
