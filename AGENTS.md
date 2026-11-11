# AGIW Suite

Agents working in this repository follow this file. AGIW is a menu-bar monitor for people who run local models: activity, memory pressure, worker status, and explicit load and unload.

## Ethos

A missing or stale reading is unknown. It is never idle, zero, quiet, or none. Opening the app starts no coding task and loads no model. The observer listens on `127.0.0.1` only. The LAN peer link stays off until someone turns it on. Source tests use fixtures and mocked calls. A green suite is not a device pass, a notarized app, or a measured speedup. When a check did not run, say NOT_RUN.

## Repository map

| Tree | What it is |
| --- | --- |
| Mac | `Monitor.swift`, `JevKeychain.swift`, `Info.plist`, `build.sh`, `login-agent.sh`, `package-release.sh`. Menu-bar app for Apple silicon, macOS 13 or later. |
| Windows | `windows/`. Preview tray and loopback observer. Not a released installer. |
| CLI | `agiw/`, `pyproject.toml`, `peer_link.py`, `machine_state.py`, `tests_cli/`. The `agiw` command pairs a Mac and a Windows PC. |
| Web | `web/`. Overview, `/monitor`, setup, components, identity, machines. `server.py` serves them. |
| Android | `android/`. Read-only monitor, version 1.1.0. Not a store package. |
| Jaryo | `jaryo/` kit, `web/jaryo.css`, `web/jaryo.mjs`, and the marks in `brand/`. |

Linux is retired as a product machine. On Linux every `agiw` command refuses and changes nothing. The version flag still prints.

## Commands

Run these from the repository root. They were run on Linux with Python 3.12 and Node 22. Python 3.9 is the supported minimum. CI also runs the Python suites on 3.14.

```sh
python3 -B server.py --port 8765
```

Stdout is one JSON line, `{"port": 8765, "pid": ...}`. `http://127.0.0.1:8765/` and `/monitor` return HTTP 200. A control POST returns HTTP 403 until the Mac app supplies the per-launch capability. Ctrl-C stops the process.

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
python3 -B -m agiw --version
```

Expect exit 0. The Python runs print a skip count: router-writer cases, and on Linux the native Windows cases. Report that count. Skips are not acceptance. `MONITOR_ALLOW_ROUTER_FIXTURE_SKIP=1` skips a router dual-read case only when its writer is absent. Nisi verify prints `PASS` and `0.2.0`. The demo prints `COMPLETED`, `reportStored: true`, and `modelCalls: 0`. Jaryo check prints `ok`. `agiw --version` prints `agiw 1.1.0 (agiw-peer/2)`.

```sh
python3 -B -m agiw doctor
```

On Linux this exits 2. The message says Linux is not supported and nothing was changed.

### Mac

`bash -n` on `build.sh`, `login-agent.sh`, and `package-release.sh` ran here. The Xcode steps in `.github/workflows/source-checks.yml` are NOT_RUN on Linux. `build.sh` replaces `$HOME/Applications/Inference Monitor.app` on an Apple silicon Mac. That is an install, not a source check.

### Windows

The `windows/` unittest above is the part that runs on Linux, with native cases skipped. The Windows edition workflow runs those cases on Windows. A PowerShell tray launch on a PC is NOT_RUN here.

### CLI

The CLI workflow runs the `tests_cli` suite on macOS and Windows. On this Linux host `PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests_cli -q` exited 1. The failures are the Linux refusal (exit 2). That run is not a CLI pass.

### Android

`sh android/monitor-core/check.sh` is the check that ran here. Gradle lint, the debug APK, and the emulator smoke are NOT_RUN here. They run in `.github/workflows/android.yml`. A physical Galaxy Z Flip6 is NOT_RUN.

### Web

`node --test test_*.mjs` and the two `node --check` lines cover the pages. Forced-colors emulation is the Ubuntu job in source checks. Windows High Contrast on hardware is NOT_RUN.

## CI

| Workflow | When | Where |
| --- | --- | --- |
| `.github/workflows/source-checks.yml` | push to `main`, every pull request | GitHub-hosted `macos-latest`, Python 3.9 and 3.14, Node 22. The required check is named `monitor`. Ubuntu emulates forced-colors. The Mac's own Actions runner stays off. |
| `.github/workflows/cli.yml` | path-filtered | macOS and Windows, Python 3.9 and 3.12. |
| `.github/workflows/windows-edition.yml` | path-filtered | Windows, Python 3.9 and 3.12, plus the root Node tests. |
| `.github/workflows/android.yml` | path-filtered | Ubuntu unit tests, lint, debug APK, and an emulator smoke. |

## Conventions

Python tests are unittest. JavaScript tests are `node --test`. Use `python3 -B` and `PYTHONDONTWRITEBYTECODE=1`. Keep a change in its tree: Mac files at the root, Windows under `windows/`, the command under `agiw/` and `tests_cli/`, pages under `web/`, the phone under `android/`, the kit under `jaryo/`. The license is Apache-2.0. Leave credentials, private hostnames, and unsanitized machine state out of commits and issues. Longer notes: `CONTRIBUTING.md`, `docs/README.md`, `docs/development.md`, `docs/status.md`.

## Hard rules

- NEVER edit `AGIW.md`.
- 1.0.0 (7) is the installable app. It is Developer ID signed, Apple-notarized, and published on GitHub only. Later versions, including the 1.1.0 (14) source in this tree, are source-only. Building this checkout does not reproduce the 1.0.0 ZIP.
- No device installs. A Galaxy Z Flip6 install needs Louis. Leave the debug APK off a phone.
- No tags and no releases. Leave tags, GitHub releases, and publication alone. `package-release.sh` is not a release.
- Honest NOT_RUN. A skip, an emulator, a Linux refusal, or a check that did not run stays NOT_RUN or a reported skip. It is not hardware acceptance.
- The Mac is on macOS 27.2.1. Codex on that Mac does the heavy Mac work.
- The Mac never launches headless Chrome, Playwright, or Lighthouse. Browser tests run on the box or the PC, and Chrome there uses `--use-mock-keychain --password-store=basic`.
- The Mac's GitHub Actions runners stay off. Do not register or start one.

## Jaryo

Ink on paper. The accent is red `#C21A27`. Text is Plex Sans. Code and figures are Plex Mono.

`node jaryo/cli.mjs tokens` prints `jaryo/tokens.json`. That file still records provisional Inter, surface `black-white`, and `appliedChoiceFile` null. The Android color check allows `#000000` and `#FFFFFF` only. An accent or type change lands in the token file and that check together. D4 Split is rejected. Files in `jaryo/marks/` are studies, not the wordmark.
