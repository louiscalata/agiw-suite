# AGIW Suite

A menu-bar monitor for engineers who run local models: activity, memory pressure, worker status, and explicit load and unload.

[![Source checks](https://github.com/louiscalata/agiw-suite/actions/workflows/source-checks.yml/badge.svg)](https://github.com/louiscalata/agiw-suite/actions/workflows/source-checks.yml)
[![CLI](https://github.com/louiscalata/agiw-suite/actions/workflows/cli.yml/badge.svg)](https://github.com/louiscalata/agiw-suite/actions/workflows/cli.yml)
[![Windows edition](https://github.com/louiscalata/agiw-suite/actions/workflows/windows-edition.yml/badge.svg)](https://github.com/louiscalata/agiw-suite/actions/workflows/windows-edition.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

The latest Mac download is **1.1.1 (15)** for Apple silicon, macOS 13 or later. It is Developer ID signed and published on GitHub. It has no Apple notarization ticket, so Gatekeeper may block the first open. There is no Mac App Store release. The earlier [v1.1.0](https://github.com/louiscalata/agiw-suite/releases/tag/v1.1.0) tag contains source only. No screen capture of the Jaryo interface is stored in this repository.

**[Download 1.1.1 (15)](https://github.com/louiscalata/agiw-suite/releases/download/v1.1.1/AGIW-Inference-Monitor-1.1.1-arm64.dmg)** · [SHA-256](https://github.com/louiscalata/agiw-suite/releases/download/v1.1.1/AGIW-Inference-Monitor-1.1.1-arm64.dmg.sha256) · [v1.1.1 release notes](https://github.com/louiscalata/agiw-suite/releases/tag/v1.1.1)

The 1.1.1 app requires a per-launch capability on its local control routes. The older 1.0.0 (7) ZIP remains affected by the [local-control authentication issue](SECURITY.md#known-issue-in-v100-build-7-local-control-authentication).

## What is verified

| Surface | State |
| --- | --- |
| Mac download 1.1.1 (15) | Signed Apple-silicon DMG. Notarization and a quarantined clean-Mac first install are pending. |
| Native macOS GUI of this source | **Not yet verified on hardware.** |
| Windows installer | **Not yet released.** The Windows code in this repository is a preview. |
| Galaxy Z Flip6 inner and cover screens | **Not yet tested on a physical Galaxy Z Flip6.** |
| This checkout's loopback observer | Starts with the command below. It does not replace the Mac app. |
| `agiw` command | Installs from this checkout. On Linux every command refuses and changes nothing. |

<a id="install-the-100-download"></a>

## Install the Mac app

Requirements (Apple silicon, macOS 13 or later):

- Python 3.9+ at `/opt/homebrew/bin/python3`, `/usr/local/bin/python3` or `/usr/bin/python3`. The last needs the Xcode Command Line Tools.
- Optional: Apple-silicon (arm64) Node.js 22+ at `/opt/homebrew/bin/node` or `/usr/local/bin/node`, for the Nisi self-check. Nisi is the public workflow engine bundled with the app.
- Model weights and services such as LM Studio are separate.

1. Put the DMG and its checksum file in one folder. Run `shasum -a 256 -c AGIW-Inference-Monitor-1.1.1-arm64.dmg.sha256` and expect `OK`.
2. Open the DMG, quit any running copy of the app, and copy `Inference Monitor.app` into Applications.
3. Before opening the app, read the [Gatekeeper note in the release](https://github.com/louiscalata/agiw-suite/releases/tag/v1.1.1). Click the chip icon in the menu bar for the compact monitor, or right-click it and choose **Open Dashboard Window**.
4. Expect your LM Studio models and Mac memory readings. A reading that is missing or stale shows as unknown.
5. Optional: open **Browse → Nisi & optional components**, then click **Run Nisi self-check** to run the fixed offline check.

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
python3 -m agiw --version
```

Expect `agiw 1.1.0 (agiw-peer/2)`. A user install may also place an `agiw` script in `~/.local/bin`. If that directory is not on `PATH`, `python3 -m agiw` is the form that runs. On Linux the version flag prints, and every command exits 2 with `Linux is not supported`.

## How it works

This describes the 1.1.1 (15) download.

1. The app starts a local Python process (the observer) that listens only on IPv4 loopback.
2. The observer samples the Mac: LM Studio inventory and activity, memory pressure and best-effort GPU readings. It can also read a separately configured Windows PC worker through SSH forwards.
3. The app window shows the readings.
4. You change state only through explicit controls, such as guarded model load or unload. Opening the app starts no coding task or model.
5. Optional: run Nisi's self-check, or one fixed Jev connectivity check.

A separate peer link between machines is off by default. When enabled, it listens on the LAN ([SECURITY.md](SECURITY.md#network-surfaces)). All features: [docs/features.md](docs/features.md). Source differences: [docs/status.md](docs/status.md). Notes for 1.1.0: [docs/release-notes/v1.1.0.md](docs/release-notes/v1.1.0.md). Changes: [CHANGELOG.md](CHANGELOG.md).

## Limits

- **Not yet tested.** The Mac app built from this source has not been tested on a Mac. The Windows installer has not been released. The Android monitor has not been tested on a physical Galaxy Z Flip6.
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
