# AGIW Suite

A menu-bar monitor for engineers who run local models: activity, memory pressure, worker status, and explicit load and unload.

[![Source checks](https://github.com/louiscalata/agiw-suite/actions/workflows/source-checks.yml/badge.svg)](https://github.com/louiscalata/agiw-suite/actions/workflows/source-checks.yml)
[![CLI](https://github.com/louiscalata/agiw-suite/actions/workflows/cli.yml/badge.svg)](https://github.com/louiscalata/agiw-suite/actions/workflows/cli.yml)
[![Windows edition](https://github.com/louiscalata/agiw-suite/actions/workflows/windows-edition.yml/badge.svg)](https://github.com/louiscalata/agiw-suite/actions/workflows/windows-edition.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

The published download is **1.0.0 (7)** for Apple silicon, macOS 13 or later. It is Developer ID signed, Apple-notarized, and published on GitHub only. There is no Mac App Store release. This checkout is source **1.1.0 (14)**. It is not tagged. Building it does not reproduce the 1.0.0 ZIP. No screen capture of the Jaryo interface is stored in this repository.

**[Download 1.0.0 (7)](https://github.com/louiscalata/agiw-suite/releases/download/v1.0.0/AGIW-Inference-Monitor-1.0.0-7-arm64.zip)** · [v1.0.0 notes](https://github.com/louiscalata/agiw-suite/releases/tag/v1.0.0) · [SHA256SUMS.txt](https://github.com/louiscalata/agiw-suite/releases/download/v1.0.0/SHA256SUMS.txt) · [1.1.0 draft notes](docs/release-notes/v1.1.0.md)

> **Security warning.** The 1.0.0 (7) download has a known issue: its local control routes do not authenticate callers. Another process or account on your Mac can load or unload models, change auto-unload settings and trigger the Jev check. Jev is an optional hosted TypeSafe connection that uses your own API key; its check can incur charges. Quit the app when local callers on your Mac are not trusted. Details: [SECURITY.md](SECURITY.md#known-issue-in-v100-build-7-local-control-authentication).

## What is verified

| Surface | State |
| --- | --- |
| Mac download 1.0.0 (7) | Published for Apple silicon. The local-control authentication issue above is still in that ZIP. |
| Native macOS GUI of this source | **Not yet verified on hardware.** |
| Windows MSI and service | **Not yet verified.** The OSMF EULA decision is on HOLD. A POST ROLLBACK finding is open. |
| Galaxy Z Flip6 inner and cover screens | **Not yet verified on a device.** |
| This checkout's loopback observer | Starts with the command below. It does not replace the Mac app. |
| `agiw` command | Installs from this checkout. On Linux every command refuses and changes nothing. |

## Install the 1.0.0 download

Requirements (Apple silicon, macOS 13 or later):

- Python 3.9+ at `/opt/homebrew/bin/python3`, `/usr/local/bin/python3` or `/usr/bin/python3`. The last needs the Xcode Command Line Tools.
- Optional: Node.js 22+ at `/opt/homebrew/bin/node` or `/usr/local/bin/node`, for the Nisi self-check. Nisi is the public workflow engine bundled with the app.
- Model weights and services such as LM Studio are separate.

1. Put the ZIP and `SHA256SUMS.txt` in one folder and run `shasum -a 256 -c SHA256SUMS.txt`. Expect `OK`.
2. Extract the ZIP, quit any running copy of the app, and copy `Inference Monitor.app` into Applications.
3. Open it (read the security warning above first). Click the chip icon in the menu bar for the compact monitor, or right-click it and choose **Open Dashboard Window**.
4. Expect your LM Studio models and Mac memory readings. A reading that is missing or stale shows as unknown.
5. Optional: **Browse → Nisi & optional components** runs Nisi's fixed offline self-check.

Next: [docs/activation.md](docs/activation.md) connects LM Studio, Nisi and Jev and sets up start at login.

## Quickstart from this checkout

Python 3.9 or newer. From the repository root:

```sh
python3 -B server.py --port 8765
```

The process prints one JSON line, `{"port": 8765, "pid": ...}`, and listens on `127.0.0.1` only. Open [http://127.0.0.1:8765/](http://127.0.0.1:8765/) for Overview and [http://127.0.0.1:8765/monitor](http://127.0.0.1:8765/monitor) for the inference map. Press Ctrl-C to stop. Without LM Studio, readings stay unknown. A standalone process cannot dispatch control requests: those return HTTP 403 until the native Mac app supplies the per-launch capability.

The separate `agiw` command pairs a Mac and a Windows PC. It is not the monitor.

```sh
python3 -m pip install .
agiw --version
```

Expect `agiw 1.1.0 (agiw-peer/2)`. On Linux the version flag prints, and every command exits 2 with `Linux is not supported`.

## How it works

This describes the 1.0.0 (7) download.

1. The app starts a local Python process (the observer) that listens only on IPv4 loopback.
2. The observer samples the Mac: LM Studio inventory and activity, memory pressure and best-effort GPU readings. It can also read a separately configured Windows PC worker through SSH forwards.
3. The app window shows the readings.
4. You change state only through explicit controls, such as guarded model load or unload. Opening the app starts no coding task or model.
5. Optional: run Nisi's self-check, or one fixed Jev connectivity check.

A separate peer link between machines is off by default. When enabled, it listens on the LAN ([SECURITY.md](SECURITY.md#network-surfaces)). All features: [docs/features.md](docs/features.md). Source differences: [docs/status.md](docs/status.md). Draft notes for 1.1.0: [docs/release-notes/v1.1.0.md](docs/release-notes/v1.1.0.md). Changes: [CHANGELOG.md](CHANGELOG.md).

## Limits

- **Not yet verified.** Native macOS GUI on hardware, Windows MSI and service (OSMF EULA on HOLD, POST ROLLBACK open), and Galaxy Z Flip6 inner and cover screens on a device.
- **Not measured.** There is no measured AGIW speedup. Recorded tokens/s describe separately configured workers and cannot be compared across machines. A loaded model alone does not prove active generation. See [docs/performance.md](docs/performance.md).
- **Jev data.** Check Jev connection sends your key and one fixed test request to TypeSafe. It tests connectivity only, and charges may apply. See [docs/features.md](docs/features.md#jev-optional).
- **Separate pieces.** Model weights, LM Studio, the private router and external Windows workers are not bundled. The Windows edition source in `windows/` is an unreleased preview. The Android tree is a read-only monitor, not a store build.

## Learn more

- [docs/activation.md](docs/activation.md): after installing, to connect models, Nisi and Jev.
- [docs/features.md](docs/features.md): every feature, the local-only pipeline, Nisi, and what Jev sends.
- [docs/status.md](docs/status.md): what the download and the source each cover, and open checks.
- [docs/architecture.md](docs/architecture.md): process boundaries, local HTTP routes and guardrails.
- [docs/development.md](docs/development.md): to run the local view, source checks and builds.
- [docs/cli.md](docs/cli.md): the separate `agiw` command-line tool that pairs a Mac and a Windows PC.
- [docs/README.md](docs/README.md): every other document and the repository layout.

## Support and license

- Report bugs by [opening an issue](https://github.com/louiscalata/agiw-suite/issues) with sanitized steps. Contributions: [CONTRIBUTING.md](CONTRIBUTING.md).
- Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md). Keep credentials and private hostnames out of public issues.
- License: [Apache License, Version 2.0](LICENSE). Copyright 2026 Louis Calata. See [NOTICE](NOTICE).
