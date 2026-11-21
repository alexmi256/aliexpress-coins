# AGENTS.md

Rules for an autonomous agent working in this repository. They are not suggestions: a change that
breaks one of them is wrong even if its tests pass.

## The gate

- **`just check` is the only gate.** It runs ruff lint, ruff format check, mypy strict and pytest
  with the coverage ratchet. CI runs the same set; do not add a second definition of "passing".
- Run `just check` before you claim a task is done. A green test run without lint, format and types
  is not green.
- `just setup` creates `.venv` with plain `python3 -m venv` and pip. Never introduce uv, poetry,
  hatch, pdm or any other resolver: the target machine has none of them.

## Where things live

- **`pyproject.toml` is the single configuration home** for ruff, mypy and pytest. Do not add
  `setup.cfg`, `pytest.ini`, `mypy.ini`, `.ruff.toml` or equivalent.
- The package lives at `src/aliexpress_coins/`; tests live at `tests/`, mirroring the package.
- Runtime and development dependencies are pinned in `requirements.txt` and
  `requirements-dev.txt`. That is the one source of truth for versions; setuptools reads the
  runtime file for package metadata. Do not duplicate pins in `pyproject.toml`.
- Python is **3.14**. `requires-python` and ruff's `target-version` stay at 3.14.

## Changing the rules

- Ruff is line length 120 with single quotes, and the formatter owns wrapping (`E501` is off).
- Mypy is strict over **both** `src/` and `tests/`. A stale `# type: ignore` fails the build.
- Coverage is a **ratchet**: the floor in `pyproject.toml` only goes up. Never lower it to make a
  change pass; add tests instead.
- Do not weaken a gate, skip a test, or narrow the checked paths to get green.

## Scope discipline

- **Product behaviour belongs in an issue, not a drive-by commit.** If a task needs behaviour that
  is not in its issue, write the issue (or stop and ask) rather than smuggling it into a scaffold
  or refactor commit.
- The documents in `docs/` are authoritative where they disagree with a prompt. Read
  `docs/00-brief.md`, `docs/03-conventions.md` and `docs/04-delivery-plan.md` before changing the
  toolchain, layout or conventions.
- Do not delete or rewrite `docs/`. Corrections are made by editing the document that owns the
  claim, and only when the task says so.
- Do not implement an issue's neighbours. The dependency graph in `docs/04-delivery-plan.md` says
  what is allowed to land together.

## Testing and artifacts

- Tests never spawn a process or open a socket; a test that reaches the real phone "passes" while
  testing nothing.
- Time is injected (`clock` + `sleep`) rather than slept through.
- Screenshots, OCR dumps, run directories (`runs/`), database files and local state stay out of
  git. The `.gitignore` rules for them are not negotiable: they contain account data.
- Do not touch a physical device unless the issue explicitly requires it.
