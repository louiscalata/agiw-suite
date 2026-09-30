# Contributing to AGIW Inference Monitor

This private repository contains the macOS Monitor source. Optional router, Windows worker and model services are managed separately. Access to the private repository is required to open an issue or pull request.

## Propose a focused change

Base source changes on `main`, preferably on a separate branch. Keep bug fixes, tests and documentation changes focused. Describe the observed problem, the proposed behavior and the relevant check results. Distinguish fixture coverage from live runtime observations and signed package acceptance. Do not include credentials, private machine names or unsanitized local state.

## Existing source checks

Run from the repository root on macOS with Python and Node.js available:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -p 'test_*.py' -q
node --test test_*.mjs
```

The R2.9 dual-read integration cases require state writers from the separate router project. If a required writer is missing, the test fails by default. For an isolated source checkout, use the explicit CI allowance:

```sh
MONITOR_ALLOW_ROUTER_FIXTURE_SKIP=1 PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -p 'test_*.py' -q
```

This flag permits a skip only when a required writer cannot be found; it does not disable cases whose writers are present. The current clean-runner workflow records 45 such skips. Report the actual skipped count alongside passes and failures. Skipped integration cases do not establish router acceptance.

Use existing focused tests appropriate to the change. The source suites use fixtures and mocked model calls; they do not establish live inference, native Windows behavior or clean-Mac distribution acceptance.

## Installation and release boundaries

`build.sh` installs and replaces the local application when its owner lock permits. Running it is an installation action. `package-release.sh` creates a separately signed DMG candidate and requires its documented release inputs. Neither source-test success nor a signed candidate establishes notarization or publication. Follow the README and canonical roadmap for those separate gates.

The owner has not chosen a license. No reuse terms are granted by this guide; resolve the license decision before public release.
