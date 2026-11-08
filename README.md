# AGIW Suite

AGIW Suite is a Mac menu-bar app, installed as `Inference Monitor.app`, for engineers who run local models. It shows model activity, memory pressure and worker status, and has explicit controls to load or unload models.

Status: download **1.0.0 (7)** for Apple silicon, macOS 13 or later, Apache-2.0. It is Developer ID signed, Apple-notarized and published on GitHub only; there is no Mac App Store release. The source in this repository is unreleased 1.2.0 (13). Building it does not reproduce the download.

**[Download 1.0.0 (7)](https://github.com/louiscalata/agiw-suite/releases/download/v1.0.0/AGIW-Inference-Monitor-1.0.0-7-arm64.zip)** · [Release notes](https://github.com/louiscalata/agiw-suite/releases/tag/v1.0.0) · [SHA256SUMS.txt](https://github.com/louiscalata/agiw-suite/releases/download/v1.0.0/SHA256SUMS.txt)

> **Security warning.** The 1.0.0 (7) download has a known issue: its local control routes do not authenticate callers. Another process or account on your Mac can load or unload models, change auto-unload settings and trigger the Jev check. Jev is an optional hosted TypeSafe connection that uses your own API key; its check can incur charges. Quit the app when local callers on your Mac are not trusted. Details: [SECURITY.md](SECURITY.md#known-issue-in-v100-build-7-local-control-authentication).

## Install and first run

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

## How it works

This describes the 1.0.0 (7) download.

1. The app starts a local Python process (the observer) that listens only on IPv4 loopback.
2. The observer samples the Mac: LM Studio inventory and activity, memory pressure and best-effort GPU readings. It can also read a separately configured Windows PC worker through SSH forwards.
3. The app window shows the readings.
4. You change state only through explicit controls, such as guarded model load or unload. Opening the app starts no coding task or model.
5. Optional: run Nisi's self-check, or one fixed Jev connectivity check.

A separate peer link between machines is off by default. When enabled, it listens on the LAN ([SECURITY.md](SECURITY.md#network-surfaces)). All features: [docs/features.md](docs/features.md). Differences in the unreleased 1.2.0 source: [docs/status.md](docs/status.md).

## Limits

- **Open checks.** A first install on a clean Mac, native rendering and Jev dialogs are not fully verified. Windows and two-machine coordination are in development. See [docs/status.md](docs/status.md).
- **Not measured.** There is no measured AGIW speedup. Recorded tokens/s describe separately configured workers and cannot be compared across machines. A loaded model alone does not prove active generation. See [docs/performance.md](docs/performance.md).
- **Jev data.** Check Jev connection sends your key and one fixed test request to TypeSafe. It tests connectivity only, and charges may apply. See [docs/features.md](docs/features.md#jev-optional).
- **Separate pieces.** Model weights, LM Studio, the private router and external Windows workers are not bundled. The Windows edition source in `windows/` is an unreleased preview.

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
