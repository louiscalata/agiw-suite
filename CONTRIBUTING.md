# Contributing to AGIW Inference Monitor

This repository contains the macOS Monitor source. Optional router, Windows worker and model services are managed separately. Use issues for focused bug reports and pull requests for proposed changes.

## Propose a focused change

Base source changes on `main`, preferably on a separate branch. Keep bug fixes, tests and documentation changes focused. Describe the observed problem, the proposed behavior and the relevant check results. Distinguish fixture coverage from live runtime observations and signed package acceptance. Do not include credentials, private machine names or unsanitized local state.

## Existing source checks

Run from the repository root on macOS with Python and Node.js available:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -p 'test_*.py' -q
node --require ./test_support/warnings-as-errors.cjs --test test_*.mjs
python3 -B -m unittest discover -s tests_cli -q                            # the agiw command-line tool
(cd windows && python3 -B -m unittest discover -s tests -p 'test_*.py' -q)  # the Windows edition
```

On macOS the Windows edition's native cases skip; the `Windows edition` workflow runs them on Windows. A change to
`agiw/`, `pyproject.toml`, `machine_state.py` or `docs/cli.md` is also checked by the `CLI` workflow on macOS and Windows.

The R2.9 dual-read integration cases require state writers from the separate router project. If a required writer is missing, the test fails by default. For an isolated source checkout, use the explicit CI allowance:

```sh
MONITOR_ALLOW_ROUTER_FIXTURE_SKIP=1 PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -p 'test_*.py' -q
```

This flag permits a skip only when a required writer cannot be found; it does not disable cases whose writers are present. The current clean-runner workflow records 45 such skips. Report the actual skipped count alongside passes and failures. Skipped integration cases do not establish router acceptance. `test_router_contract.py` runs the same cases against public fixture writers, with no router installed; see the [router state contract](docs/router-state-contract.md). A pass there is contract coverage for the Monitor, not router acceptance.

Use existing focused tests appropriate to the change. The source suites use fixtures and mocked model calls; they do not establish live inference, native Windows behavior or clean-Mac distribution acceptance.

## Golden fixture tests

Recent public golden corpora pin current collector and projection behavior with synthetic inputs. Expectations were recorded from executing the then-current functions. A fixture pass is characterization of current source, not device acceptance, native GUI acceptance or live inference.

These corpora are included in the source-check commands above (`unittest discover` from the repository root, and the Windows edition discover under `windows/`). Changing collector or projection behavior may require updating the matching corpus; see each corpus README. Do not copy real machine state into a corpus.

- `test_support/activity_collect_golden/` pins `activity.collect_activity` (261 cases). Replay modules: `test_activity_collect_golden` and `test_activity_collect_active_golden`.
- `test_support/collect_nisi_v02_golden/` pins `nisi_v02.collect_nisi_v02` (608 cases). Replay modules: `test_collect_nisi_v02_golden` and `test_collect_nisi_v02_generation_golden`.
- `test_support/fleet_state_golden/` pins `build_fleet_state`. Replay modules: `test_fleet_state_golden` and `test_fleet_state_golden_protocol`. A later change made these cases portable across Python versions.
- `test_support/mac_presence_golden/` pins Mac presence projection (308 synthetic snapshots). Replay module: `test_mac_presence_golden`.
- Windows collector goldens live under `windows/tests/fixtures/windows_collect_golden/` (330 cases), with replay tests `windows/tests/test_collect_golden.py`, `windows/tests/test_collect_golden_errors.py` and `windows/tests/test_collect_golden_discovery.py` (portable discovery).

## Installation and release boundaries

`build.sh` installs and replaces the local application when its owner lock permits. Running it is an installation action. `package-release.sh` creates a separately signed DMG candidate and requires its documented release inputs. Neither source-test success nor a signed candidate establishes notarization or publication. Follow the README and canonical roadmap for those separate gates.

This source is licensed under [Apache-2.0](LICENSE). Unless explicitly stated otherwise, intentionally submitted contributions are provided under those terms. Include the origin and required attribution of any third-party material in a proposed change.
