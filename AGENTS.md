# AGENTS.md

Agents for this repository read this file. AGIW Suite is a menu-bar monitor for people who run local models: activity, memory pressure, worker status, and explicit load and unload. A missing or stale reading is unknown. It is never idle, zero, or none. Opening the app starts no coding task and loads no model. The observer listens on `127.0.0.1` only. The LAN peer link stays off until someone turns it on. Source tests use fixtures and mocked calls. A green suite is not a device pass, a notarized app, or a measured speedup. When a check did not run, write NOT_RUN.

## Repository map

| Tree | What it is |
| --- | --- |
| Mac | `Monitor.swift`, `JevKeychain.swift`, `Info.plist`, `build.sh`, `login-agent.sh`, `package-release.sh`. Menu-bar app for Apple silicon, macOS 13 or later. |
| Windows | `windows/`. Preview tray and loopback observer. Not a released installer. |
| CLI | `agiw/`, `pyproject.toml`, `peer_link.py`, `machine_state.py`, `tests_cli/`. The `agiw` command pairs a Mac and a Windows PC. |
| Web | `web/`. Overview, `/monitor`, setup, components, identity, machines. `server.py` serves them. |
| Android | `android/`. Read-only monitor, version 1.1.0. Not a store package. |
| Jaryo | `jaryo/` kit, `web/jaryo.css`, `web/jaryo.mjs`, and the marks in `brand/`. The UI language. |

Linux is not a product host. On Linux every `agiw` command except `--help` and `--version` exits 2 and changes nothing.

## Commands

Run from the repository root. Verified on Linux with Python 3.12.3 and Node 22.23.4. Python 3.9 is the supported minimum. There is no repo-wide Python or JavaScript linter. Android lint is the Gradle task in CI.

```sh
python3 -B server.py --port 8765
```

Prints a JSON line `{"port": 8765, "pid": ...}`. `GET /` and `GET /monitor` returned 200. An unauthenticated control POST to `/api/models/control` returned 403. Ctrl-C stops the process.

```sh
MONITOR_ALLOW_ROUTER_FIXTURE_SKIP=1 PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -p 'test_*.py' -q
node --test test_*.mjs
(cd windows && PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -p 'test_*.py' -q)
bash -n build.sh login-agent.sh package-release.sh
python3 -B bundle_nisi.py --verify
node --check web/components.js
node --check web/monitor.mjs
node vendor/nisi/package/bin/nisi.mjs --version
node vendor/nisi/package/bin/nisi.mjs demo
node jaryo/cli.mjs check
node jaryo/cli.mjs tokens
sh android/monitor-core/check.sh
python3 -m agiw --version
```

Results of that run: Python 1440 tests, OK, 103 skipped. Node 673 passed, 0 failed. Windows edition 570 tests, OK, 72 skipped. `bash -n` and both `node --check` exited 0. Nisi verify printed `PASS` and `0.2.0`. The demo printed `COMPLETED`, `reportStored: true`, and `modelCalls: 0`. Jaryo check printed `ok`. The Android script printed 57, 58, and 17 checks passed. `python3 -m agiw --version` printed `agiw 1.1.0 (agiw-peer/2)`. Skips are not acceptance. The router flag skips a dual-read case only when its writer is absent.

```sh
python3 -m agiw doctor
```

Exited 2. The message says Linux is not supported and nothing was changed.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests_cli -q
```

Exited 1: 206 tests, 46 failures. The failures are the Linux refusal. That run is not a CLI pass. CI runs this suite on macOS and Windows.

```sh
npm install --prefix /tmp/pw playwright@1.49.1
(cd /tmp/pw && npx playwright install chromium)
NODE_PATH=/tmp/pw/node_modules node checks/forced-colors-emulation.mjs
```

Printed `ok` for light and dark on `/` and `/monitor`. This is Chromium emulation, not Windows High Contrast on a machine. Do not run Playwright, headless Chrome, or Lighthouse on the Mac. Browser tests go to the box or the PC, and Chrome there uses `--use-mock-keychain --password-store=basic`.

### Not run here

`plutil -lint Info.plist` and `xcrun swiftc -typecheck` of `Monitor.swift` and `JevKeychain.swift` are the macOS CI steps. `build.sh` replaces `$HOME/Applications/Inference Monitor.app`. That is an install, not a source check. `package-release.sh` needs a Developer ID identity. `./gradlew :monitor-core:test :app:lintDebug :app:assembleDebug` and `sh android/ci/emulator-smoke.sh` run in CI. A physical Galaxy Z Flip6 is NOT_RUN. A Windows tray launch is NOT_RUN. Native macOS GUI of this source is NOT_RUN.

## CI

| Workflow | When | Where |
| --- | --- | --- |
| `.github/workflows/source-checks.yml` | every pull request, and push to `main` | GitHub-hosted `macos-latest`, Python 3.9 and 3.14, Node 22. The required check is named `monitor`. Ubuntu runs forced-colors. |
| `.github/workflows/cli.yml` | path-filtered | macOS and Windows, Python 3.9 and 3.12. |
| `.github/workflows/windows-edition.yml` | path-filtered | Windows, Python 3.9 and 3.12, plus `node --test test_*.mjs`. |
| `.github/workflows/android.yml` | path-filtered | Ubuntu unit tests, lint, a debug APK, and an emulator smoke. |

The Mac's own GitHub Actions runners stay off. Do not register one.

## Conventions

One small pull request per concern. Never push to `main`. Merge with a merge commit when the required checks are green. Docs stay plain and factual, with no hype. Python tests are unittest. JavaScript tests are `node --test`. Use `python3 -B` and `PYTHONDONTWRITEBYTECODE=1`. Keep Mac files at the root, Windows under `windows/`, the command under `agiw/` and `tests_cli/`, pages under `web/`, the phone under `android/`, and the kit under `jaryo/`.

## Hard rules

- NEVER edit `AGIW.md`.
- No secrets. No device identifiers: no computer name, user name, or serial number in docs, receipts, or commits.
- Honest NOT_RUN. A skip, an emulator, a Linux refusal, or a check that did not run is not a pass.
- No tags, GitHub releases, or publishing without Louis's yes. `package-release.sh` is not a release.
- 1.0.0 (7) is the installable app. It is Developer ID signed, Apple-notarized, and published on GitHub only. The v1.1.0 source tag is build 14 and has no app attached. Building this checkout does not reproduce the 1.0.0 ZIP.
- No device installs. A Galaxy Z Flip6 install needs Louis. Leave a debug APK off a phone.

## Jaryo

The UI language is ink and paper, booklet red `#C21A27`, IBM Plex Sans for text, and IBM Plex Mono for figures and code. On this checkout `node jaryo/cli.mjs tokens` still prints provisional Inter, surface `black-white`, and `appliedChoiceFile` null. Do not describe that file as already updated. D4 Split is rejected. Files in `jaryo/marks/` are studies, not the wordmark. `android/monitor-core/check.sh` allows only `#000000` and `#FFFFFF`.

## Deeper docs

`README.md`, `CONTRIBUTING.md`, `SECURITY.md`, `CHANGELOG.md`, `roadmap.md`, `docs/README.md`, `docs/development.md`, `docs/status.md`, `docs/architecture.md`, `docs/jaryo-identity.md`, `docs/cli.md`.

This repository has no `CLAUDE.md` and no Copilot instructions file. This file is the agent guide.
