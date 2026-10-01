<img src="docs/assets/agiw-suite-overview.svg" width="1280" alt="AGIW Suite — inventory and activity, memory and GPU readings, guarded actions, public Nisi and optional Jev">

# AGIW Suite · Inference Monitor

Inspect local model activity, resource pressure and configured route evidence from one Mac menu bar, then use explicit recovery controls when work needs attention.

**[Download 1.0.0 for Apple silicon](https://github.com/louiscalata/agiw-suite/releases/download/v1.0.0/AGIW-Inference-Monitor-1.0.0-7-arm64.zip)** · [Release notes](https://github.com/louiscalata/agiw-suite/releases/tag/v1.0.0) · [SHA256SUMS.txt](https://github.com/louiscalata/agiw-suite/releases/download/v1.0.0/SHA256SUMS.txt)

[First run](#first-run) · [Features](#what-you-can-see-and-control) · [Architecture & API](docs/architecture.md) · [Configuration](docs/advanced-configuration.md) · [Development](docs/development.md) · [Validation limits](#known-validation-limits)

## Release status

**1.0.0 (7)** is available for Apple silicon. The ZIP contains the Developer ID signed, Apple-notarized app, installation notes, LICENSE and NOTICE. See [known validation limits](#known-validation-limits) below. The Mac App Store edition follows a separate release process.

## Requirements

| Component | Requirement |
| --- | --- |
| Mac | Apple silicon (arm64), macOS 13 or later |
| Observer | Python 3.9+ at `/opt/homebrew/bin/python3`, `/usr/local/bin/python3` or `/usr/bin/python3` |
| Nisi self-check | External Apple-silicon Node.js 22+ at `/opt/homebrew/bin/node` or `/usr/local/bin/node` |
| Model activity | Separately installed model weights and configured local services such as LM Studio |
| Optional Jev | Your own TypeSafe API key and internet access; provider charges may apply |
| Optional routes/workers | Separately managed router and Windows components |

## First run

1. Download the ZIP and `SHA256SUMS.txt` into the same folder. In that folder, run `shasum -a 256 -c SHA256SUMS.txt`; expect `OK`.
2. Confirm Python is available at one of the paths above. Install the external Node runtime if you want the Nisi self-check.
3. Extract the ZIP, quit any running AGIW Monitor edition, and copy `Inference Monitor.app` into Applications.
4. Open the app. Click its menu bar chip icon for the compact monitor, or right-click and choose **Open Dashboard Window**.
5. Review local readings. Cards show unavailable or unknown when services or fresh evidence are absent. Open **Browse → Nisi & optional components** to inspect Nisi or run its fixed offline self-check.

## What you can see and control

| Feature | Included behavior | Prerequisite or boundary |
| --- | --- | --- |
| Model and resource view | Inventory, activity supported by fresh telemetry, Mac readings and recorded client/route state | Local services/weights configured separately; stale or missing evidence stays unknown |
| Evidence inspection | **Browse → Feeds & evidence** explains available observations | Recorded state does not prove a live route succeeded |
| Catalog views | **Core 6** filters five pinned LLM IDs plus Nomic Embed Text v1.5; **All discovered** shows the full Mac inventory | Other reported in-use models remain visible; filtering loads/unloads nothing |
| Recovery controls | Explicit guarded model, route and worker actions | Supported external components must be configured; opening the app starts no task or model |
| Resident-model policy | Optional automatic unloading with fresh pair/route admission checks | Off by default; the coding router is separately managed |
| Public Nisi | Bundled 0.2.0 workflow engine, journal, CLI and fixed offline check | External Node 22+; fixed examples rather than arbitrary repository execution |
| Optional Jev | Native secure setup and explicitly requested fixed hosted check | Your TypeSafe key; connectivity only, with disclosed hosted data/charges |

[Explore the component reference](docs/architecture.md) for runtime responsibilities, data flow, local HTTP routes and enforced guardrails.

See [advanced configuration](docs/advanced-configuration.md) for resident-model policy, share recovery and memory admission. For direct keyboard, video and mouse access to nearby headless workers, an optional [Mini-KVM](docs/advanced-configuration.md#optional-hardware-access-for-nearby-workers) can help with setup and recovery; native AGIW KVM integration remains unverified.

## Included Nisi and optional Jev connection

**Public Nisi 0.2.0 is included.** Its workflow engine, journal, CLI, Apache-2.0 license and pinned manifest are bundled and verified offline. The native fixed self-check loads no model and installs no weights; its expected report includes `outcome: COMPLETED`, `reportStored: true` and `modelCalls: 0`. Node remains an external prerequisite. The public CLI provides fixed demonstrations; library APIs can define broader workflows. The private shared coding router is separately managed and is not bundled. See the [public Nisi release](https://github.com/louiscalata/nisi/releases/tag/v0.2.0) and [CLI instructions](docs/development.md#public-nisi-cli).

**Jev is an optional TypeSafe connection, disabled until configured.** On the Components page, **Set up Jev** opens a secure native dialog for your own API key. The signed helper stores it in the login Keychain; the key is never bundled, passed through the browser or returned to the Python observer. Saving the key sends no network request.

**Check Jev connection** sends the saved key in the authorization header and one fixed synthetic classification test to TypeSafe over HTTPS. TypeSafe receives your IP address and standard request metadata; usage charges may apply. The check sends no files, task prompts or model inventory. It checks connectivity only and does not enable coding-task routing or verify provider model identity. External Nisi/Jev router settings are separate. See [TypeSafe’s API documentation](https://docs.typesafe.ai/api).

## Known validation limits

Build 7 passed [exact-source CI](https://github.com/louiscalata/agiw-suite/actions/runs/36758069996), notarization, signature/Gatekeeper checks, ZIP verification and installation parity on the maintainer’s Mac. Its native Nisi offline self-check passed without model calls. One authorized fixed hosted Jev probe with an existing Keychain credential returned `connected`. See the [roadmap](roadmap.md) for compiled source, package provenance and regression evidence; [rc.2](https://github.com/louiscalata/agiw-suite/releases/tag/v1.0.0-macos-rc.2) and [rc.1](https://github.com/louiscalata/agiw-suite/releases/tag/v1.0.0-macos-rc.1) remain available for rollback.

**Complete native rendering/animation, a quarantined clean-Mac first install, and Jev Cancel/reopen and credential entry/save/remove remain unverified.** These are open [roadmap](roadmap.md) follow-ups. Publication and the successful connection probe do not establish native setup acceptance, live task routing or provider model identity.

## Development

Read the [architecture and capability reference](docs/architecture.md) for implementation boundaries. Use the [development guide](docs/development.md) for the local browser view, source checks, Nisi CLI and distribution packaging. The earlier [source-preview release](https://github.com/louiscalata/agiw-suite/releases/tag/v1.0.0-source-preview.1) remains available.

## Support and contributing

For bugs or documentation issues, [open an issue](https://github.com/louiscalata/agiw-suite/issues) with sanitized reproduction steps. For contributions, see [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

Report vulnerabilities privately using [SECURITY.md](SECURITY.md). Keep credentials, private hostnames and sensitive machine state out of public issues.

## License

[Apache License, Version 2.0](LICENSE), matching public Nisi. Copyright 2026 Louis Calata. See [NOTICE](NOTICE). External model weights and separately managed services are not included.
