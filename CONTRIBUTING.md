# Contributing

Thanks for helping improve InvoiceDock. Contributions should preserve the project's central distinction between provider-backed verification, model consistency, and human review.

## Before coding

1. Search existing issues and the roadmap.
2. Open an issue for a new integration, database change, or user-visible workflow before a large implementation.
3. Never include real invoices, email credentials, tax IDs, access tokens, or model keys in issues, tests, screenshots, or commits.

## Development workflow

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
pytest
ruff check app tests
```

Use synthetic fixtures. Add tests for parsing variations, security limits, conflict semantics, and regressions. Keep browser behavior usable with keyboard navigation and at narrow widths.

## Pull requests

- Keep each PR focused and explain the user-visible outcome.
- Update README/docs and `.env.example` for configuration changes.
- Add or update tests; describe how the change was manually verified.
- State any new dependency's license and why it is needed.
- Do not silently change the meaning of `verified`, `consistent`, or `reviewed`.
- Use clear commit messages; maintainers may squash on merge.

By contributing, you agree that your contribution is licensed under Apache-2.0.

