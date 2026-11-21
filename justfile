# The single entry point for this repository.
# `just check` must stay identical to the CI gate: lint, format check, mypy, tests.
# `.venv` is the only interpreter used after `setup`; the bootstrap below is the
# sole exception, because a virtualenv has to be created before it can be used.

venv := ".venv"
python := ".venv/bin/python"

# List the available recipes.
default:
    @just --list

# Create .venv with plain Python 3.14 + pip and install pinned dependencies.
setup:
    python3 -m venv {{venv}}
    {{python}} -m pip install --requirement requirements-dev.txt
    {{python}} -m pip install --editable . --no-build-isolation

# Lint with ruff.
lint:
    {{python}} -m ruff check .

# Format in place.
fmt:
    {{python}} -m ruff format .

# Fail if formatting would change anything.
fmt-check:
    {{python}} -m ruff format --check .

# Type-check src/ and tests/ in strict mode.
typecheck:
    {{python}} -m mypy

# Run the test suite. Coverage is measured and the ratchet is enforced.
test:
    {{python}} -m pytest

# Run the suite again with HTML and XML coverage reports for inspection.
cov:
    {{python}} -m pytest --cov-report=html --cov-report=xml

# The gate. A developer runs this and gets the verdict CI will give.
check: lint fmt-check typecheck test
    @echo "All quality gates passed."

# Remove the virtualenv, caches, coverage output and build artefacts.
clean:
    rm -rf {{venv}} .mypy_cache .pytest_cache .ruff_cache .coverage coverage.xml htmlcov
    rm -rf build dist *.egg-info src/*.egg-info
    find . -type d -name __pycache__ -prune -exec rm -rf {} +
