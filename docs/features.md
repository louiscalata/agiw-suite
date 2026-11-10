# Features and components

This page describes the 1.0.0 (7) download unless marked otherwise. See [status](status.md) for what the v1.1.0 source changes. The download does not include the per-launch control check that is in that source.

- **Nisi**: the public workflow engine, journal and CLI that the app bundles (version 0.2.0).
- **Jev**: the optional hosted connection to TypeSafe, used with your own TypeSafe API key.

**Known issue:** the 1.0.0 (7) download does not authenticate callers of its local control routes (model load or unload, auto-unload settings, the Jev check). See [SECURITY.md](../SECURITY.md#known-issue-in-v100-build-7-local-control-authentication).

## What you can see and control

| Capability | What you can do |
| --- | --- |
| **Observe models** | Inspect inventory, activity supported by fresh telemetry, and recorded client and route state. **Browse → Feeds & evidence** explains the observations. |
| **Understand resources** | See Mac memory pressure and best-effort GPU readings. Model-load admission estimates size and headroom before an explicit load. |
| **Choose an inventory view** | **Core 6** filters five pinned LLM IDs plus Nomic Embed Text v1.5. **All discovered** expands the Mac inventory; other reported in-use models stay visible. |
| **Control and recover** | Request guarded local model load or unload. Optional automatic unloading is off by default. |
| **Inspect public Nisi** | Run the bundled 0.2.0 package’s fixed offline self-check, or explore its workflow engine, journal and CLI. External Node 22+ is required. |
| **Check optional Jev** | Set up your TypeSafe key securely and request a fixed connectivity check. The check sends your key and one test request to TypeSafe; charges may apply. See [Jev](#jev-optional). |

Opening the app starts no coding task or model, and changing a catalog view loads or unloads nothing. Missing or stale observations remain unknown. A loaded model or recorded worker state alone does not prove active generation or a successful route.

### Recovery

Route and worker recovery depends on separately installed maintainer integrations (`chami-dispatch`, `pc-llm`, the private router and a configured share). These are not bundled and are not general-purpose setup tools. In the v1.1.0 source, the native launcher leaves owner recovery closed. Read the [known local-control issue](../SECURITY.md#known-issue-in-v100-build-7-local-control-authentication) before using the released app.

### Not bundled

Model weights, LM Studio, the private router and external Windows model workers are separate and not included. The Windows edition source in `windows/` is an unreleased preview.

### Related

- [architecture.md](architecture.md): read for process boundaries, data flow, local HTTP routes and guardrails.
- [advanced-configuration.md](advanced-configuration.md): read for resident-model policy, share recovery and memory admission. It also covers optional Mini-KVM hardware access for nearby headless workers; native AGIW Suite KVM integration is unverified.

## Recorded inference performance

Recorded tokens/s figures come from separately configured workers. They do not measure AGIW Suite speedup or the performance of the download. Windows and Mac figures use different denominators and must not be compared.

No controlled speedup percentage exists for the three before/after conditions: no AGIW Suite, AGIW Suite on Mac, and AGIW Suite on Mac + PC. None has been run. The Mac benchmark’s non-empty-response rate measures availability, not graded accuracy.

Figures, Windows lane layouts, methods, the comparison protocol and the Ollama and vLLM comparison: [performance.md](performance.md). Sanitized aggregate data: [inference-performance.json](data/inference-performance.json). These records do not change the signed release or close the open checks in [status](status.md#open-checks).

## Local-only pipeline

Nisi’s public CLI can use a model server on this machine for both models:

**Local author → JSON checks → four fixed assertions → local reviewer → run report**

The fixed JSON-configuration example uses a loopback chat endpoint and different author and reviewer models. It allows one repair attempt and stops at its deadline. Generated JSON is checked as data; generated programs are never executed. [Run the local-model example](development.md#local-only-model-example).

## Nisi (included)

The 0.2.0 package, its Apache-2.0 license and its pinned manifest are bundled and verified offline. The downloaded app’s **Nisi self-check** is a fixed, model-free example. It uses the external Node runtime, loads no model and installs no weights. Expected report fields:

```json
{"outcome":"COMPLETED","reportStored":true,"modelCalls":0}
```

The public CLI provides fixed demonstrations, not arbitrary repository tasks. Library APIs can define broader workflows. The private shared coding router is separately managed and not bundled. [Public Nisi release](https://github.com/louiscalata/nisi/releases/tag/v0.2.0) · [CLI instructions](development.md#public-nisi-cli)

## Jev (optional)

On the Components page, **Set up Jev** opens a secure native dialog for your own TypeSafe API key. The signed helper stores it in the login Keychain. The key stays out of the browser and the Python observer. Saving it sends no network request.

**Check Jev connection** sends the key in an HTTPS authorization header and one fixed synthetic classification test to TypeSafe. The provider receives your IP address and standard request metadata, and usage charges may apply. No files, task prompts or model inventory are sent.

The check tests connectivity only. It does not enable coding-task routing, verify provider model identity or establish native setup acceptance. Jev is separate from the local pipeline, and external Nisi and Jev router settings are separate. [TypeSafe API documentation](https://docs.typesafe.ai/api)

## Command-line connection tool

`agiw` is a separate command-line tool (1.1.0). It pairs your Mac and Windows PC with a 6-digit PIN and reads their signed, read-only status over the IPv4 LAN. Status is signed but not encrypted. It dispatches no jobs.

The install commands work only once a release tag is published. Each machine needs inbound tcp/8768 and udp/8769 from the LAN. `agiw` changes no firewall settings; you add the rules yourself. Pairing steps, firewall rules, the login service and uninstalling: [cli.md](cli.md).
