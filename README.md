# AGIW Suite · Inference Monitor

AGIW Suite's Inference Monitor is a Mac menu-bar app for people running local models. It shows model and client observations alongside Mac resource state, and offers explicit controls for configured models and workers. When evidence is missing or stale, activity stays unknown.

**Current GitHub release: [v1.0.0 (Build 7) for Apple silicon](https://github.com/louiscalata/agiw-suite/releases/download/v1.0.0/AGIW-Inference-Monitor-1.0.0-7-arm64.zip)** · [Release notes](https://github.com/louiscalata/agiw-suite/releases/tag/v1.0.0) · [SHA256SUMS.txt](https://github.com/louiscalata/agiw-suite/releases/download/v1.0.0/SHA256SUMS.txt)

Build 7 supports macOS 13 or later. The GitHub download is Developer ID signed and Apple-notarized. The Mac App Store edition has a separate release path.

## What it does

| Area | In Build 7 |
| --- | --- |
| Model activity | See discovered models, supported activity telemetry, and recorded client choices with their evidence and freshness. A recorded choice alone does not prove live generation. |
| Mac resources | See memory pressure, best-effort GPU readings, and a headroom estimate before an explicit model load. |
| Controls | Request guarded model load/unload and supported recovery for configured routes or workers. Optional automatic unloading is off by default. |
| Components | Inspect the bundled Nisi workflow engine and run its fixed offline self-check; optionally set up a Jev hosted connection check. |

Opening AGIW does not start a model or coding task. Model weights, LM Studio, the coding router, and Windows workers are configured separately.

## First run

**Required:** an Apple-silicon Mac running macOS 13+ and Python 3.9+ at `/opt/homebrew/bin/python3`, `/usr/local/bin/python3`, or `/usr/bin/python3`. The optional Nisi self-check also needs Apple-silicon Node.js 22+ at `/opt/homebrew/bin/node` or `/usr/local/bin/node`.

1. Download the Build 7 ZIP and `SHA256SUMS.txt` from the links above into the same folder.
2. In that folder, run `shasum -a 256 -c SHA256SUMS.txt` and expect `AGIW-Inference-Monitor-1.0.0-7-arm64.zip: OK`.
3. Extract the ZIP, quit any other AGIW Monitor edition, and copy `Inference Monitor.app` to Applications.
4. Open the app from Applications. Use its menu-bar icon for the compact monitor, or right-click it and choose **Open Dashboard Window**.

[Activate local model monitoring and optional components](docs/activation.md) for expected states and troubleshooting.

## Included and optional components

Build 7 bundles public **Nisi 0.2.0**: its workflow engine, journal, CLI, and fixed offline self-check. The self-check uses the external Node runtime and makes no model calls. [Nisi CLI instructions](docs/development.md#public-nisi-cli)

**Jev is optional.** Its hosted connection check requires your own TypeSafe API key, internet access, and an explicit request; provider charges may apply. The [activation guide](docs/activation.md#optional-set-up-hosted-jev) explains setup and data sent. Neither Jev nor Nisi installs model weights.

Build 7 can display recorded OpenCode model or session choices. It does not configure MCP servers, grant tool permissions, or include a Tool Connections page. Configure external tools in their own client.

## Try the source view

To inspect the local browser view on a Mac with Python 3.9+, run:

```sh
git clone https://github.com/louiscalata/agiw-suite.git
cd agiw-suite
python3 -B server.py --port 8765
```

Open `http://127.0.0.1:8765/` and press Ctrl-C to stop it. This runs the source observer on loopback; it does not install the signed Build 7 app. [Source setup and checks](docs/development.md#try-the-local-view)

## Documentation

- [Activation and troubleshooting](docs/activation.md)
- [Architecture and local API](docs/architecture.md) · [Advanced configuration](docs/advanced-configuration.md)
- [Development and source checks](docs/development.md) · [Performance methods and historical data](docs/performance.md)

## Known validation limits

The [Build 7 source CI run](https://github.com/louiscalata/agiw-suite/actions/runs/36758069996) checks Python, JavaScript, and build inputs at the [compiled source commit](https://github.com/louiscalata/agiw-suite/tree/caaa7d482a1f168d86c496af4f573dfcbf0490b8). It does not establish installed-app acceptance or a performance speedup. The [release record](roadmap.md) tracks local checks and remaining native setup work.

For bugs, [open an issue](https://github.com/louiscalata/agiw-suite/issues) with sanitized steps. Report vulnerabilities privately through [SECURITY.md](SECURITY.md); never post credentials or private host details. Contributions: [CONTRIBUTING.md](CONTRIBUTING.md).

[Apache-2.0](LICENSE) · [NOTICE](NOTICE) · Copyright 2026 Louis Calata
