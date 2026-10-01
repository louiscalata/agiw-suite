# Activate AGIW Suite

Start with the **GitHub 1.0.0 (7) Apple-silicon application**. This guide explains how to launch the monitor, connect local models and run the included Nisi tools. The Mac App Store edition has separate menus, runtime packaging and acceptance requirements.

[Download and verify](../README.md#first-run) · [Architecture](architecture.md) · [Configuration](advanced-configuration.md) · [Developer commands](development.md)

## Quick start: get the monitor running

1. Download the application ZIP and `SHA256SUMS.txt` from the [v1.0.0 release](https://github.com/louiscalata/agiw-suite/releases/tag/v1.0.0). Verify the ZIP with `shasum -a 256 -c SHA256SUMS.txt` in the download folder; expect `OK`.
2. Make sure Python 3.9+ is executable at `/opt/homebrew/bin/python3`, `/usr/local/bin/python3` or `/usr/bin/python3`. The GitHub app uses an external interpreter.
3. Extract the ZIP, quit an existing Monitor edition, and copy **Inference Monitor.app** to Applications. Open it in Finder.
4. Click its **chip icon in the Mac menu bar** to open the compact monitor. Right-click that icon and choose **Open Dashboard Window** for the full view. This is a menu-bar app; no Dock icon is expected.
5. Wait for the first reading. **LIVE · STREAM** or **LIVE · 1s** confirms a fresh observer feed. Select **This Mac** to inspect resources. Model cards can remain unavailable until you connect a model service.

You have now launched the Monitor. Local model generation, the Nisi self-check, hosted Jev and external worker routing each require their own setup or explicit action below. There is no AGIW account sign-in for the monitor or the local Nisi examples.

## Connect and monitor local models

1. Install/configure LM Studio and download a model suited to the Mac's available memory. Model weights are separate from AGIW.
2. In LM Studio's **Developer** tab, start its API server on **127.0.0.1:1234**. The Monitor uses that fixed local endpoint for inventory. See the [official server instructions](https://lmstudio.ai/docs/developer/core/server).
3. With LM Studio's CLI installed, its equivalent explicit command is:

   ```sh
   "$HOME/.lmstudio/bin/lms" server start --port 1234 --bind 127.0.0.1
   ```

   Activity uses the CLI; model controls specifically require the supported user-owned `~/.lmstudio/bin/lms` installation with safe executable/parent permissions. An arbitrary `lms` on PATH is insufficient for controls. See the [official CLI setup](https://lmstudio.ai/docs/cli) and [server flags](https://lmstudio.ai/docs/cli/serve/server-start). The observer does not configure API authentication or accept a model-server bearer token; an authenticated-only server may be unavailable to this edition.
4. Return to AGIW and open **Browse → Models**. Choose **All discovered** if your model is outside **Core 6**. Select a model to inspect its source, age and state.
5. To load an eligible downloaded model, select it and press **Load**. AGIW rechecks inventory and memory admission before dispatch. Wait for the reported operation and fresh loaded-state evidence; an accepted request alone is not completion. Alternatively, load the model in LM Studio and observe it in AGIW.
6. Run a small prompt in LM Studio or your configured local client. Fresh activity should report a busy/generating phase and then idle. A loaded model alone is not evidence of generation; a recorded cloud-client model identity is not live activity.

Use **Unload** only for an eligible idle instance with zero queued work. Keep **Automatic model unload** off unless you explicitly want that policy and its external route/admission prerequisites are available. Core 6 filters the inventory; it does not load six models. [Memory and resident-pair details](advanced-configuration.md#resident-model-budget).

**Pause / Resume** freezes and resumes the dashboard's displayed readings. It does not stop inference, observer sampling or the memory watchdog. Use the model service or task owner's controls when you need to stop work.

## Verify included Nisi without a model

1. Install **Node.js 22+ for Apple silicon** at `/opt/homebrew/bin/node` or `/usr/local/bin/node`. The native self-check verifies the runtime version and arm64 architecture when it runs.
2. In AGIW, open **Browse → Nisi & optional components**.
3. Confirm **Package integrity: Verified · public 0.2.0 payload**, then press **Run Nisi self-check**.
4. Expect **Nisi 0.2.0 passed its fixed workflow self-check. No model was called.** The checked report requires `COMPLETED`, `reportStored: true` and `modelCalls: 0`.

This checks the bundled engine and fixed offline example. It does not activate the external coding router or establish model inference. If Node is missing/incompatible, install the required runtime and run the check again; package integrity failure requires repairing the application payload.

## Run the local-only Nisi model pipeline

For an actual model-backed example, use the public CLI from a source checkout. Configure two **different local language models** and a compatible local chat server with JSON-schema output support. Use models that fit your resource budget; Jev is not required.

```sh
git clone https://github.com/louiscalata/agiw-suite.git
cd agiw-suite
python3 -B bundle_nisi.py --verify
node vendor/nisi/package/bin/nisi.mjs --version
node vendor/nisi/package/bin/nisi.mjs local-model \
  http://127.0.0.1:1234/v1/chat/completions AUTHOR_MODEL REVIEWER_MODEL
```

Replace `AUTHOR_MODEL` and `REVIEWER_MODEL` with two different configured model IDs before running the last command. Package verification should report `PASS`; version should be `0.2.0`. The model example drafts fixed JSON data, checks it, runs four assertions, obtains separate review and prints a run report. It allows one repair attempt and returns a nonzero exit code if it does not complete.

Configure the server to execute both models locally: a loopback address alone does not prove where a proxy runs inference. This CLI does not execute model-generated programs or run arbitrary repository tasks. The [development guide](development.md#local-only-model-example) explains its scope.

## Optional: set up hosted Jev

1. Open **Nisi & optional components inside the installed native app**. A normal browser/source checkout cannot open its secure native setup bridge.
2. Press **Set up Jev** and enter your own TypeSafe key in the native secure dialog. Save it; expect **API key saved in Keychain** when Components refreshes. Saving sends no network request.
3. If you want to make the disclosed hosted request, press **Check Jev connection**. It sends the saved key and a fixed synthetic test to TypeSafe over HTTPS; IP/request metadata and possible provider charges apply.
4. Expect a successful connection message, or an explicit authentication, rate-limit, network or response error. Do not treat a connection check as coding-task routing or provider-model identity verification.

No files, project prompts or model inventory are sent by this check. Nisi's offline and local-model paths can be used without Jev. [Jev data flow](architecture.md#6-jev-credential-and-network-boundary).

## Optional: use an existing coding router or PC worker

The GitHub package includes observation/recovery adapters. It does **not install the external coding router, client integrations, Windows worker, models or their credentials**. On a fresh Mac, unknown/unbound routing is expected until those separately managed tools are installed and configured. There is no included command that installs the entire multi-machine development stack.

For a machine that already has the supported owner launcher/router and worker contracts:

1. Confirm the external owner programs and state are configured for this Mac account. If SMB recovery is needed, supply both settings in [Share recovery settings](advanced-configuration.md#share-recovery-settings); do not use Guest/Anonymous.
2. In AGIW, press **Check readiness**. In this GitHub edition that button can inspect and safely reconcile exact eligible pending records, then verify readiness. It does not submit a coding task or call a model. Read the returned steps if it reports **needs action**.
3. Review the recovery scope before requesting repair: it can start a stopped local server, recover eligible Nisi records, reconcile worker state, remount a configured share and send one short Windows probe after its guards pass. Pressing the **top-bar Fix inference button starts that guarded run immediately**; watch its result without a second initiating click. The inspector's **Fix Nisi Inference** shortcut only opens the same panel; from that entry, press **Run check** to start. The panel's **What this does** explains the scope. Closing it or pressing Escape does not cancel the running repair.
4. Submit a development task through the separately configured client/router. Then inspect **Activity**, **Nisi Inference** and **Windows PC** for fresh run/job evidence. Idle readiness is not proof that a task has run.

Do not repeat a request merely because it timed out: an external job can still be pending. Follow the retained job/owner-state instructions. Parallel task scheduling belongs to the external router; the Monitor's HTTP concurrency is not a scheduler. [Recovery architecture](architecture.md#7-recovery-workers-and-parallel-work).

When fresh supported worker/headless evidence is available, **PC LLM** exposes a separate switch. Turning it **on** requests a four-hour headless LLM hold and sends one short guarded probe; turning it **off** releases that mode. A hidden/unknown switch means the capability is not currently established. This switch does not install the worker or launch a development task.

## Optional: start the GitHub app at login

Manual launch needs no login agent. The included source script supports a per-user LaunchAgent for exactly **`~/Applications/Inference Monitor.app`**. If you installed in `/Applications`, that script will not target that location.

To use this optional source-checkout tool, quit AGIW, place the verified signed app at the required per-user path, and run from the checkout:

```sh
bash login-agent.sh install
bash login-agent.sh status
```

Expected install result: **Installed and loaded the per-user Inference Monitor login agent**. It starts the app immediately and at login; an unexpected failure can restart it. A normal **Quit Inference Monitor** stays stopped. To start it again manually, open the installed app; to disable this launcher, run:

```sh
bash login-agent.sh uninstall
```

The script validates the signed app and refuses a conflicting launcher or already-running target. It does not install models, route tasks or configure the MAS edition's login setting. [Launcher implementation](../login-agent.sh).

## When activation does not work

| What you see | Next step |
| --- | --- |
| No visible app window | Look for the chip icon; right-click → **Open Dashboard Window**. Confirm you opened the intended installed edition. |
| Python unavailable / observer reconnecting | Check the supported Python paths and the app's bundled resources. Quit/reopen after correcting the dependency. |
| LIVE feed, but models unavailable | Start the LM Studio server at `127.0.0.1:1234`; verify the CLI and compatible inventory/activity endpoints. |
| Loaded model, activity unknown | Inspect the source/age and CLI activity feed; inventory alone cannot prove generation. |
| Model action disabled or refused | Read the selected model's reason: stale evidence, missing CLI, busy/queued work, memory admission or unsettled prior operation. |
| Nisi needs Node / integrity unavailable | Install native arm64 Node 22+ at a supported path, or repair/reinstall a damaged pinned payload. |
| Jev setup unavailable | Open Components in the installed app. For authentication/network errors, inspect the explicit status without putting a key into an issue or chat. |
| Route unknown / launcher unavailable | Install/configure the separately managed router tools. The bundled Nisi self-check cannot provision them. |
| PC absent or stale | Check the independently managed worker, dispatcher and transport. A KVM is optional console access, not a replacement for the network route. |

For a bug report, use [GitHub Issues](https://github.com/louiscalata/agiw-suite/issues) with sanitized steps and observed status. Keep keys, private hostnames and machine state out of public reports. [Known validation limits](../README.md#known-validation-limits) remain separate from these source-backed instructions.
