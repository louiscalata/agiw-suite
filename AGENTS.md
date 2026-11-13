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

Linux is retired as a product machine. On Linux product commands exit 2 and change nothing. Argument-parser `--help` and `--version` still work.

## Commands

Run these from the repository root. Python 3.9 is the supported minimum. CI also runs the observer suites on 3.14 and uses Node 22. Record the actual command, version, result and skip count for the revision being checked; historical counts are not verification of a new change.

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

The Xcode steps in `.github/workflows/source-checks.yml` require macOS. `build.sh` replaces `$HOME/Applications/Inference Monitor.app` on an Apple silicon Mac. That is an install, not a source check. `package-release.sh` requires a Developer ID identity; neither script is a routine verification command.

### Windows

The `windows/` unittest above is the part that runs on Linux, with native cases skipped. The Windows edition workflow runs those cases on Windows. A PowerShell tray launch on a PC is NOT_RUN here.

### CLI

The CLI workflow runs `PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests_cli -q` on macOS, Windows and hosted Ubuntu. Tests inject the supported host kind on Linux; a Linux CI pass does not make Linux a supported product host.

### Android

`sh android/monitor-core/check.sh` checks source contracts and requires a Java runtime for its Java stage. Gradle lint, the debug APK and emulator smoke run in `.github/workflows/android.yml`. Report each stage separately. Emulator results do not establish physical-device acceptance.

### Web

`node --test test_*.mjs` and the two `node --check` lines cover the pages. Forced-colors emulation is the Ubuntu job in source checks. Windows High Contrast on hardware is NOT_RUN.

## CI

| Workflow | When | Where |
| --- | --- | --- |
| `.github/workflows/source-checks.yml` | push to `main`, every pull request | Same-repository Mac source checks use the existing `mac-m5` runner; fork PRs use hosted macOS. Python 3.9/3.14 and Node 22. Hosted Ubuntu runs forced-colors, overview ink and the kit smoke. The combined required check is `monitor`. |
| `.github/workflows/cli.yml` | path-filtered | Python 3.9/3.12: existing `mac-m5` and `pc-win` runners for same-repository Mac/Windows jobs, hosted Mac/Windows for fork PRs, hosted Ubuntu 24.04 for Linux fixture checks. |
| `.github/workflows/windows-edition.yml` | path-filtered | Existing `pc-win` runner for same-repository events; hosted Windows for fork PRs. Python 3.9/3.12 plus the root Node tests. |
| `.github/workflows/android.yml` | path-filtered | Hosted Ubuntu unit tests, lint, debug APK and an emulator smoke. |

The kit smoke uses Ubuntu 22.04 for compatibility with the pinned Chromium sandbox. Other hosted Ubuntu jobs keep their workflow-selected image. Inspect the workflow and current job receipts before claiming a runner or check passed. Registration and an online runner do not prove execution.

## Conventions

Python tests are unittest. JavaScript tests are `node --test`. Use `python3 -B` and `PYTHONDONTWRITEBYTECODE=1`. Keep a change in its tree: Mac files at the root, Windows under `windows/`, the command under `agiw/` and `tests_cli/`, pages under `web/`, the phone under `android/`, the kit under `jaryo/`. The license is Apache-2.0. Leave credentials, private hostnames, and unsanitized machine state out of commits and issues. Use the existing `roadmap.md` as the canonical plan. Preserve source ownership and unrelated work. Push reviewed feature branches, then merge the exact reviewed head with a merge commit after the required checks pass; never bypass them. Longer notes: `CONTRIBUTING.md`, `docs/README.md`, `docs/development.md`, `docs/status.md`.

## Hard rules

- NEVER edit `AGIW.md`.
- 1.0.0 (7) is the installable app. It is Developer ID signed, Apple-notarized, and published on GitHub only. Later versions, including the 1.1.0 (14) source in this tree, are source-only. Building this checkout does not reproduce the 1.0.0 ZIP.
- No device installs. A Galaxy Z Flip6 install needs Louis. Leave the debug APK off a phone.
- No tags and no releases. Leave tags, GitHub releases, and publication alone. `package-release.sh` is not a release.
- Honest NOT_RUN. A skip, an emulator, a Linux refusal, or a check that did not run stays NOT_RUN or a reported skip. It is not hardware acceptance.
- The Mac never launches headless Chrome, Playwright, or Lighthouse. Browser tests run in hosted Linux CI or on the PC, and Chrome there uses `--use-mock-keychain --password-store=basic`.
- Reuse the existing runner mappings. Do not register, start, stop or restart runner services as a source-check step.

## Jaryo

Ink on paper. The accent is red `#C21A27`. Text is Plex Sans. Code and figures are Plex Mono.

`node jaryo/cli.mjs tokens` prints `jaryo/tokens.json`. Its retained foundation records provisional Inter and surface `black-white`; `appliedChoiceFile` records the applied choice file and SHA-256, and `chosenFamilyIds` records all 17 choices. The Android color check allows `#000000` and `#FFFFFF` only. When web typography or accents diverge from the foundation, record and verify an explicit platform variant; do not silently change Android's fonts or palette. Keep token contracts, CSS, distribution assets and affected checks in agreement. D4 Split is rejected. Files in `jaryo/marks/` are studies, not the wordmark.
