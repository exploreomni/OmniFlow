# Contributing To OmniFlow

Thanks for helping make semantic-layer development safer.

## Development Setup

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade "pip==26.1.2" "setuptools==83.0.0"
python -m pip install -e ".[dev]"
pytest
ruff check .
bandit -c pyproject.toml -r src
python scripts/simulate_alpha.py
```

## Pull Requests

- Keep changes focused and include tests for changed behavior.
- Never commit API keys, customer payloads, model IDs from private tenants, or raw query results.
- Update documentation when public behavior changes.
- Treat API response examples as metadata-only fixtures.
- Run unit, lint, security, package, and simulation checks before requesting review.

By participating, you agree to follow the project Code of Conduct.
