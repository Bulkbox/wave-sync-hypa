### Wave Sync Hypa

Syncs Hypa with Wave

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch develop
bench install-app wave_sync_hypa
```

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/wave_sync_hypa
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### Tests

The suite is split into two subpackages:

- `tests/unit/` — pure-mock tests (~1.5s). The dev-loop runner.
- `tests/integration/` — handler-driven / DB-touching tests (~10 min). Run before push.

```bash
# Dev loop — fast feedback while iterating
bench --site dev.bulkbox.cloud run-tests --app wave_sync_hypa \
    --module wave_sync_hypa.wave_sync_hypa.tests.unit

# Pre-push — full sweep across both subpackages
bench --site dev.bulkbox.cloud run-tests --app wave_sync_hypa
```

See `CONTRIBUTING.md` for the classification rule (when a new test belongs in `unit/` vs `integration/`) and the one-line `__init__.py` step required after adding any test file.

### CI

This app can use GitHub Actions for CI. The following workflows are configured:

- CI: Installs this app and runs unit tests on every push to `develop` branch.
- Linters: Runs [Frappe Semgrep Rules](https://github.com/frappe/semgrep-rules) and [pip-audit](https://pypi.org/project/pip-audit/) on every pull request.


### License

mit
