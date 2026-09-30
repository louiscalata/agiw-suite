# AGIW Suite · Inference Monitor

The Inference Monitor is a local Mac view of AGIW Suite's inference activity. It shows model activity when fresh local telemetry can verify it, displays recorded client identities and route state, and provides guarded recovery controls for configured routes and workers. Missing or stale evidence is shown as unknown.

The monitor observes local inference and recorded router state. Explicit controls can inspect or repair narrow owner states; launching the app does not start a coding task or load a model. The Windows and SharedChami route repair flows are optional and require separately managed components.

Core 6 filters the Mac's LM Studio inventory to six pinned model IDs: five LLMs and Nomic Embed Text v1.5. Reported in-use models outside that set remain visible. All discovered shows the full Mac inventory. The filter does not imply that the six models are installed, loaded, or active; switching views does not change model state. Model weights are managed separately.

## Release status

**[Download AGIW Suite 1.0.0 for Apple silicon](https://github.com/louiscalata/agiw-suite/releases/tag/v1.0.0)** — the latest regular public release, version **1.0.0 (7)**, requiring **macOS 13 or later**. The ZIP contains the Developer ID signed, Apple-notarized app, installation and release notes, LICENSE and NOTICE. Put the ZIP and `SHA256SUMS.txt` together and run `shasum -a 256 -c SHA256SUMS.txt`; expect `OK`.

Install **Python 3.9 or later** at `/opt/homebrew/bin/python3`, `/usr/local/bin/python3`, or `/usr/bin/python3`. Extract the ZIP, quit any running AGIW Monitor edition, copy `Inference Monitor.app` into Applications, then open it. It starts in the menu bar: click the chip icon for the compact monitor, or right-click it and choose **Open Dashboard Window**. The fixed Nisi self-check additionally needs an external **Apple-silicon Node.js 22 or newer** at `/opt/homebrew/bin/node` or `/usr/local/bin/node`.

Build 7 passed [exact-source CI](https://github.com/louiscalata/agiw-suite/actions/runs/36758069996), notarization, signature/Gatekeeper checks, ZIP verification and installation parity on the maintainer's Mac. The v1.0.0 ZIP preserves all 53 audited app files and modes; only the surrounding installation/release notes changed from rc.2. Its native Nisi offline self-check passed without model calls. The Monitor and observer remained alive during Jev helper launch, correcting Build 6's observed process-exit regression. **Complete native rendering/animation, a quarantined clean-Mac first install, and Jev Cancel/reopen and credential entry/save/remove remain unverified.** These checks remain open in the roadmap. [Build 5 rc.1](https://github.com/louiscalata/agiw-suite/releases/tag/v1.0.0-macos-rc.1) remains available for rollback.

Model weights, local services and optional router/Windows integrations are configured separately. The Mac App Store edition has its own application, privacy declaration and release process. The [source-preview release](https://github.com/louiscalata/agiw-suite/releases/tag/v1.0.0-source-preview.1) remains available for source-only use.

## Try the local view

On a Mac with Python 3, clone this repository and run from its root:

```sh
python3 -B server.py --port 8765
```

The server prints a JSON line containing its port and process ID. Open `http://127.0.0.1:8765/` in a browser; press Ctrl-C in the terminal to stop it. This starts the observer on loopback. Runtime cards can show unavailable or unknown when optional local model, router, or Windows worker services are absent.

## Included Nisi and optional Jev connection

The **1.0.0 (7) application** includes the public **Nisi 0.2.0** package by default. Build 5 rc.1 predates this integration. The bundled release contains Nisi's workflow engine, journal, CLI, original Apache-2.0 license, and a pinned file manifest. It loads no extra model and installs no weights. The bundle is copied and verified offline; it never copies the maintainer's private Nisi checkout.

Open **Browse → Nisi & optional components** to inspect the package or run its fixed, model-free self-check. To run the same CLI from this checkout, install **Node.js 22 or newer**, then use:

```sh
python3 -B bundle_nisi.py --verify
node vendor/nisi/package/bin/nisi.mjs --version
node vendor/nisi/package/bin/nisi.mjs demo
```

Expected results are package verification `PASS`, version `0.2.0`, and a demo report with `outcome: COMPLETED`, `reportStored: true`, and `modelCalls: 0`. The native app checks for an Apple-silicon Node installation at `/opt/homebrew/bin/node` or `/usr/local/bin/node`. Node is a separate prerequisite. Nisi's public CLI provides fixed demonstrations; applications can use its library APIs to define broader workflows. It does not include the private shared router or run arbitrary repository tasks from the command line. See the [public Nisi release](https://github.com/louiscalata/nisi/releases/tag/v0.2.0).

**Jev is optional and hosted by TypeSafe.** Build 7 includes a connector, disabled until configured. In the native app's Components page, **Set up Jev** opens a secure native dialog for your own TypeSafe API key. The signed helper stores it in the login Keychain; credentials are never bundled, passed through the browser, or returned to the Python observer. Saving the key makes no network request. **Check Jev connection** explicitly sends the saved API key in the authorization header and one fixed synthetic classification test to TypeSafe over HTTPS. TypeSafe receives your IP address and standard request metadata; provider usage charges may apply. No files, task prompts, or model inventory are sent by this check. It verifies connectivity only and does not enable coding-task routing. Existing external Nisi/Jev router settings are separate. See [TypeSafe's API documentation](https://docs.typesafe.ai/api).

One authorized fixed hosted probe using an existing Keychain credential returned `connected`. This result establishes connectivity only; provider model identity and coding-task routing are unverified. Native setup and credential-lifecycle validation remain open as recorded in Release status.

## Resident model budget

The Mac Nisi route selects two distinct LLMs already resident in LM Studio. It prefers Gemma 4 as author when present and chooses another resident LLM as reviewer; it does not load an extra model for those roles. Jev uses a separate remote adapter and does not need a Mac model resident. Core 6 is a catalog view, not a request to hold six models in RAM.

Automatic unloading is off by default. When explicitly enabled, it protects the current resident Nisi pair, considers only other observed idle Mac models, and refuses to unload anything if the pair cannot be established from a fresh snapshot. These policies do not establish that a live routed task succeeded.

## Optional hardware access for nearby workers

For setup or recovery of nearby headless machines, an [Openterface Mini-KVM](https://openterface.com/minikvm/) or compatible KVM is recommended for direct keyboard, video and mouse access through USB and HDMI. See the [Mini-KVM FAQ](https://docs.openterface.com/products/minikvm/faq/) for connection requirements. Network inference still requires configured workers/model endpoints and their network transport; a KVM is optional and does not pool RAM or GPUs or increase token throughput. Native AGIW KVM integration has not been verified.

## Share recovery settings

The source contains no address or login name for the owner's actual PC. To enable the explicit Fix Route remount on a Mac, set both environment variables in the Inference Monitor process environment before launch:

```sh
AGIW_SHARE_HOSTS='10.222.33.10,10.222.33.20,pc.example.invalid'
AGIW_SHARE_USERNAME='registered-user'
```

Those addresses and the name are examples; replace them with the owner's actual LAN values. `AGIW_SHARE_HOSTS` accepts one or two numeric IPv4 addresses in the `10/8`, `172.16/12`, or `192.168/16` private ranges and optionally a third trusted DNS name or address for matching the mounted share. `AGIW_SHARE_USERNAME` is the registered SMB account; this repository stores no password. Guest and Anonymous are refused. If `login-agent.sh install` runs with both variables exported, it stores only these non-password settings in the private per-user LaunchAgent. A partial pair is refused. A share already mounted as Guest or from an unconfigured host is not treated as healthy; Fix Route stops without remounting it.

## Memory readings for sandboxed callers

When the Mac Monitor can read the kernel memory counters, its sampler writes a short-lived `level.json` under `~/.local/state/agiw/mem-guard/`. The directory is owner-only and the file is mode `0600`. A sandboxed memory-guard caller can use this file for up to three seconds when it cannot read the counters directly. The sampler never renews the file from an older file reading or fallback.

`mem-guard status` reports one sample. When high swap use is its only TIGHT signal, `mem-guard admit` can take 5.1 seconds for a second direct kernel sample, even with `--wait 0`. Heavy work is admitted in that special case only if pressure and headroom remain healthy and swap use and swapouts do not grow. Missing counters, fallback data, or another TIGHT reason still refuse admission. Use the `admit` exit code as the gate; `status` is informational.

## Source checks

For an isolated source checkout, run the same commands as the clean GitHub runner:

```sh
MONITOR_ALLOW_ROUTER_FIXTURE_SKIP=1 PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -p 'test_*.py' -q
node --test test_*.mjs
```

The CI app typecheck targets arm64. Source tests and the isolated Swift policy harness run on the CI host architecture; passing those on Intel does not establish native Apple silicon runtime acceptance.

Forty-five router integration cases need writers from a separate project. The explicit allowance permits a skip only when those writers are absent; without it, missing writers fail the test. Report the actual skips alongside passes. The tests use fixtures and mocked model calls, so a passing source suite does not establish a live route.

The native app supports Apple silicon only. `build.sh` requires a native arm64 macOS terminal; Intel Macs and Rosetta shells are refused before creating installation paths. It compiles and signs the app, then replaces `$HOME/Applications/Inference Monitor.app` when its owner lock permits. Do not run it as a read-only build check. The installer intentionally writes into the local Applications directory and does not publish content elsewhere.

### Build a distribution candidate

These instructions package the full Developer ID edition. The Mac App Store edition is a separate application with its own feature scope and validation; it has a separate release process.

`package-release.sh` builds a separate Developer ID signed DMG in `dist/`; it does not install or launch the app. It requires a clean source tree, an exact Developer ID Application identity in `MONITOR_RELEASE_SIGN_IDENTITY`, and secure Apple timestamps. It verifies the mounted DMG against frozen inputs, including LICENSE and NOTICE, and writes a manifest with source, toolchain, input and installation-note hashes. Missing, empty or symlinked legal inputs stop the build before output creation. `MONITOR_RELEASE_ALLOW_DIRTY=1` is only for local candidate checks; it marks the output dirty and must not be used for a release.

The packager always produces an arm64 app for Apple silicon Macs, independent of the macOS build host architecture. The app targets macOS 13 or later and needs Python 3.9 or later at `/opt/homebrew/bin/python3`, `/usr/local/bin/python3`, or `/usr/bin/python3`. The DMG contains the app, an `INSTALL.txt` note, LICENSE and NOTICE. The legal files also travel with the app in `Contents/Resources/Legal`. Distribution verification includes Apple notarization, ticket validation, a recorded final hash and a quarantined clean-Mac assessment. The Build 7 ZIP was exported through Xcode with its app ticket attached; its clean-Mac assessment remains open as documented above. The earlier candidate DMG is unsubmitted and is not a download asset.

After choosing the Developer ID Application identity from `security find-identity -v -p codesigning`, run `MONITOR_RELEASE_SIGN_IDENTITY=<certificate-SHA-1> ./package-release.sh` from a clean checkout to create a signed candidate. Notarization, stapling, final hash verification, and a quarantined clean-Mac test remain separate gates.

The [roadmap](roadmap.md) records this source package's verification state and remaining integration work.

## Contributing

Contributions are welcome for bug fixes, tests, and documentation improvements. Please open a pull request with a focused change, keep the scope narrow, and share the relevant validation output when practical.

Before submitting a change:
- run the Python and JavaScript test suites locally on macOS
- avoid adding credentials, hostnames, or sensitive local state to the repo
- keep examples generic and safe for public review

See [CONTRIBUTING.md](CONTRIBUTING.md) for source-check commands, external router fixture boundaries and change scope.

## Security

See [SECURITY.md](SECURITY.md) for private vulnerability reporting. Do not put credentials, private hostnames or sensitive machine state in issues.

## License

Licensed under the [Apache License, Version 2.0](LICENSE), matching the public Nisi release. Copyright 2026 Louis Calata. See [NOTICE](NOTICE) for the project notice. The license covers this repository's source; external model weights and separately managed services are not included.
