# Handoff

> Latest at top. Both agents update this before ending a session, per the Session protocol in `AGENTS.md`.
> If you're picking up work, read the top entry and `docs/vertical-slice-plan.md`.

## Entry template

```
## YYYY-MM-DD HH:MM TZ — <Tool> → next session

**Last commit:** `<hash>` on `<branch>`
**Working tree:** <clean | dirty: files>
**Task plan position:** <task N — status>

**What shipped this session**
- ...

**What's next**
1. ...

**Open questions / decisions pending**
- ...

**Gotchas hit**
- ...
```

---

## 2026-06-10 13:35 UTC — Claude Code → next session

**Last commit:** `34228f5` on `claude/current-phase-gotchas-tjkwsu`
**Working tree:** clean
**Task plan position:** Task 1 (scaffold) — DONE. Task 2 (SDK decorators) next.

**What shipped this session**
- Task 1 scaffold: root `Makefile` (faithful to the slice-plan spec), `worker/pyproject.toml`
  (pinned runtime deps + `dev` extras, with ruff/mypy/pytest config), the `helix/` package
  tree with empty `__init__.py` files (`runtime`, `tools`, `rag`, `workflows`, `eval`),
  `.gitignore` for `data/`/`*.db`, and a `tests/test_smoke.py` import check.
- Verified `make lint` (ruff clean, `mypy --strict helix/` clean on 6 files) and `make test`
  (1 passed) both green, invoked exactly as the Makefile defines them.

**What's next**
1. Task 2: `@helix.workflow`, `@helix.task`, `helix.gather` for local execution only, plus
   `types.py` (`Doc`, `Answer`, `Citation`) and the public exports in `helix/__init__.py`.

**Open questions / decisions pending**
- None blocking Task 2.

**Gotchas hit**
- Local interpreter is **Python 3.11.15**, but `pyproject` pins `requires-python >=3.12`
  per the spec. ruff/mypy/pytest all run fine under 3.11, so lint/test were verified, but a
  full `pip install -e '.[dev]'` (which pulls litellm/sentence-transformers/qdrant-client)
  was **not** run here — those heavy deps + the version pin make it a CI/3.12 concern, not a
  scaffold blocker. `import helix` itself needs none of them.
- `pytest` was not preinstalled; installed `pytest`/`pytest-asyncio` ad hoc to run the suite.
  `make test` relies on pytest's rootdir insertion for `import helix` (no `PYTHONPATH` set) —
  confirmed working.

---

## 2026-06-10 — Cowork (Claude) → next session

**Last commit:** operating-docs commit on `master` (run `git log -1 --oneline`; parent is `7dd58e8`)
**Working tree:** clean after this commit
**Task plan position:** Bootstrap finishing. Task 1 (scaffold) NOT started.

**What shipped this session**
- Added `.gitattributes` (`* text=auto eol=lf`) and cleared the phantom CRLF drift that had made README/CHANGELOG/`vertical-slice-plan.md` show as fully rewritten. The committed state was always LF; an editor was re-saving working copies as CRLF.
- Replaced the monolithic `CLAUDE.md` with a thin stub that imports `AGENTS.md`. Added a symmetric `CODEX.md`. `AGENTS.md` (the real manual, phase-aware) was already correct and is now committed.
- Reset this handoff log. The prior entries were placeholder/future-dated (06-11, 06-12) and described Tasks 7–8 as done against a commit `a3f81c2` that does not exist. Real HEAD before this session was `7dd58e8`.
- Verified the three pending edits to `docs/vertical-slice-plan.md` are already applied: LLM cache key includes `max_tokens`/`top_p`/`stop`; cache-hit spans set `cost_usd=0` + `replayed=true`; risks table notes near-zero rerun cost.

**What's next**
1. Task 1 (assigned to Claude Code): `Makefile` + `worker/pyproject.toml` + empty `worker/helix/` package; pass `make lint` on the empty package.
2. Verify the `@AGENTS.md` import: in a fresh Claude Code session, ask "what phase are we in and what gotchas apply today?" Expect "slice phase" plus the slice-phase gotchas (engine contextvars detection, LLM cache key, holdout unlock, `retrieved_doc_ids`, `temperature=0`, SQLite single-writer). A vague answer means the import is not resolving.

**Open questions / decisions pending**
- None blocking Task 1.

**Gotchas hit**
- The repo lives on a Windows filesystem. When operating on it from a Linux shell, the mount permits create and rename but **not unlink** — `git checkout`/index writes that delete-and-replace fail and can corrupt `.git/index`. Recovered by routing git through an index on a native fs and renaming a clean index back into place. If `.git/index` ever reports "corrupt," that is the cause. Prefer running git from Windows-side tooling.
- An editor is rewriting tracked files to CRLF on save. `.gitattributes` now pins LF; if CRLF diffs reappear, check the editor's line-ending setting.
