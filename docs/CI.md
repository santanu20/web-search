# CI and dependency maintenance

## Tested environment

`requirements-ci.txt` is the fully resolved Python dependency snapshot produced
by the CI runner on `ubuntu-latest` with Python 3.13. `requirements-ci.in` is
the direct-dependency input list; the lock file includes transitive packages
so CI does not resolve floating dependency versions on every run.

The lock snapshot is platform/Python-version specific. When changing the
Python minor version or CI runner OS, regenerate and validate the lock in
that target environment rather than assuming the existing snapshot is
portable.

## Refreshing the lock

Run this from a clean Python 3.13 environment on the same Ubuntu runner
family used by CI:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements-ci.in
python -m pip freeze --all > requirements-ci.txt
python -m pip check
playwright install chromium --with-deps
python -m compileall -q web_search.py tests
WEB_SEARCH_NO_SKILL_VENV=1 python -m pytest tests/ -q --strict-markers
```

Review the lock diff before committing it. Do not hand-edit transitive
versions to satisfy a scanner or test without checking the dependency
constraints; upgrade the direct package in `requirements-ci.in`, regenerate
the full lock, and run the entire test suite.

## CI gates

- pip installs the committed fully pinned `requirements-ci.txt`
- `pip check` rejects incompatible dependency metadata
- `compileall` catches Python syntax/import-compilation errors early
- Chromium is installed for browser-related tests
- pytest runs with strict marker registration
- stale runs for the same branch/PR are cancelled to conserve runner time

Dependabot proposes weekly pip and GitHub Actions updates. Dependency update
PRs must regenerate the lock and pass this same CI workflow before merge.