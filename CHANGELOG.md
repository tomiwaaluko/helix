## Unreleased

- Scaffold the vertical-slice worker package: root `Makefile`, `worker/pyproject.toml`
  (pinned deps + ruff/mypy/pytest config), the `helix/` package tree with empty
  `__init__.py` files, a `.gitignore` for `data/`/`*.db`, and a smoke test. `make lint`
  and `make test` pass on the empty package. (Task 1)
