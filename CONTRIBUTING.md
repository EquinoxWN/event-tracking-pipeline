# Contributing

Thanks for helping. This is a Python and JavaScript project; issues and pull requests are welcome.

## Set up and check your change

Needs Python 3.11+ and Node.js 24+. Caches and virtual environments stay in git-ignored folders inside the repo.

```bash
make setup   # install the collector and dev tools
make lint    # ruff, ruff format --check, mypy --strict, node --check
make test    # pytest (collector, including the e2e run) and node --test (tracker)
make e2e     # the tracker sends 200 events to a local collector
make audit   # pip-audit and npm audit (as in CI)
```

A pull request is ready when `make lint` and `make test` pass and CI is green.

## Guidelines

- Keep each change focused; explain the problem it solves in the pull request.
- Add or update a test for every behaviour change, and update `docs/results/` when numbers change.
- Schema changes add optional fields only; anything else needs a new schema version and an ADR.
- Significant design changes need an ADR in `docs/adr/` (copy `0001` for the format).
- Keep function comments to one short phrase; let names and tests explain the rest.
- Report security problems privately, as described in [SECURITY.md](SECURITY.md).

By contributing you agree that your work is released under the [MIT License](LICENSE).
