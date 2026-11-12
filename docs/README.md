# Documentation index

Start with the [project README](../README.md). Each line says what the page covers.

## Use the app

- [Activate AGIW Suite](activation.md): after installing the 1.0.0 (7) download, to connect LM Studio, verify Nisi, set up Jev and start at login.
- [Features and components](features.md): to see every feature, the local-only pipeline, and what Nisi and Jev do and send.
- [AGIW x ECI Model Tuning Tool](eci-model-tuning.md): ingest score records, rank separate protocols and apply a provisional PC routing policy (unreleased source).
- [Model fast check](eci-fast-check.md): freeze paired samples, interpret uncertainty, ingest separate screening records and hand off the unrun harness adapter.
- [Advanced configuration](advanced-configuration.md): to set resident-model policy, LM Studio port, share recovery, memory readings for sandboxed callers and the Windows edition link.
- [Inference Monitor map](inference-monitor.md): to learn what the `/monitor` map nodes, tabs and health states mean.
- [agiw command-line tool](cli.md): to pair a Mac and a Windows PC with a PIN, add the firewall rules yourself, run the login service or uninstall.

## Status and evidence

- [Status](status.md): what the 1.0.0 (7) download and the v1.1.0 source each cover, the two-machine layout and the open checks.
- [Release checklist](release-checklist.md): checks before a signed Mac download, including the clean-Mac install. That install is NOT_RUN until a receipt is stored.
- [Mac and Windows PC connection record](three-machine-connection.md): the dated record of how the two machines connect, plus the retired Linux history.
- [Inference performance](performance.md): tables, methods, the Windows lane layouts, the comparison protocol and the comparison with Ollama and vLLM.
- [Source review results](source-verification.md): dated review from 3 November 2026, written while the source label was 1.2.0 (13). The checkout label is now 1.1.0 (14).

## Build and extend

- [Architecture](architecture.md): process boundaries, the evidence model, the local HTTP interface and state ownership.
- [Development](development.md): to run the local view, source checks, the Nisi CLI and a distribution candidate build.
- [Visual identity (Jaryo)](jaryo-identity.md): the logo, icon and theme decisions. Jaryo is the name of the visual-identity work.

## Data and assets

- [data/](data/): sanitized JSON records behind the performance, comparison and Jaryo receipts, and the 2026-10-16 three-machine checkpoint. [inference-performance.json](data/inference-performance.json) is the aggregate for the performance page.
- [assets/](assets/): the AGIW mark SVG.

## Outside this folder

- [Roadmap and release evidence](../roadmap.md): the canonical release gates and provenance.
- [Security reporting](../SECURITY.md): to report a vulnerability or check the known 1.0.0 issue.
- [Contributing](../CONTRIBUTING.md): before opening a pull request.

## Repository layout

The repository root is flat. This table groups its files.

| Group | Files |
| --- | --- |
| Mac app (Swift) | `Monitor.swift`, `JevKeychain.swift`, `Info.plist` |
| Observer and model control (Python) | `server.py`, `telemetry.py`, `inference_collect.py`, `inference_stats.py`, `machine_state.py`, `model_control.py`, `durable_model_journal.py`, `auto_unload.py`, `mem_guard.py`, `activity.py`, `client_models.py`, `live_feed.py`, `local_callers.py`, `gpu_probe.py`, `windows_probe.py`, `pc_tunnel.py`, `online_code_repair.py` |
| Nisi and Jev (Python) | `bundle_nisi.py`, `bundled_components.py`, `nisi_v02.py`, `jev_connection.py`, `vendor/nisi/` (pinned public Nisi) |
| Machine link and CLI | `agiw/` (package), `agiw-status`, `agiw_status.py`, `peer_link.py`, `peer_feed.py`, `pair_status.py`, `pyproject.toml` |
| Web views | `web/` (monitor, dashboard, components, setup pages), `usage-format.mjs` |
| Build and release | `build.sh`, `package-release.sh`, `login-agent.sh`, `freeze_build_inputs.py`, `scripts/`, `wrapper-patch/` |
| Tests | `test_*.py`, `test_*.mjs`, `invariant_*.py`, `tests_cli/`, `test_support/`, `.github/workflows/` |
| Other editions and assets | `windows/` (Windows edition, unreleased preview), `android/` (read-only Android monitor source, not part of the download), `jaryo/` (shared theme), `brand/` (marks and icons) |
| Docs and policy | `docs/`, `roadmap.md`, `SECURITY.md`, `CONTRIBUTING.md`, `LICENSE`, `NOTICE` |
