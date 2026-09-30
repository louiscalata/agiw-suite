# AGIW Suite · Inference Monitor

The Inference Monitor is a local Mac view of AGIW Suite's inference activity. It helps the machine's owner see which local models are working, inspect recorded client identities and route state, and request a guarded recovery action when a local route or worker needs intervention.

The monitor observes local inference and recorded router state. Explicit controls can inspect or repair narrow owner states; launching the app does not start a coding task or load a model. The Windows and SharedChami route repair flows are optional and require separately managed components.

The Core 6 control is a display filter for five local LLM roles and the Nomic Embed Text v1.5 embedding role. All discovered shows the rest of LM Studio's inventory. Switching views does not install, load, unload, or remove models. This source package does not include model weights.

## Release status

This repository provides a source preview of the macOS Inference Monitor, with Python, JavaScript and Swift components. The preview contains source only; no prebuilt application, DMG or model weights are included. Local model services and optional router/Windows integrations are managed separately.

Before distributing a binary:
- verify that no real machine names, credentials, tokens, or local-only state are embedded in the source tree
- confirm the local security model and release signing identity are appropriate for your environment
- review the package-release workflow before shipping a notarized DMG

## Try the local view

On a Mac with Python 3, clone this repository and run from its root:

```sh
python3 -B server.py --port 8765
```

The server prints a JSON line containing its port and process ID. Open `http://127.0.0.1:8765/` in a browser; press Ctrl-C in the terminal to stop it. This starts the observer on loopback. Runtime cards can show unavailable or unknown when optional local model, router, or Windows worker services are absent.

## Resident model budget

The Mac Nisi route selects two distinct LLMs already resident in LM Studio. It prefers Gemma 4 as author when present and chooses another resident LLM as reviewer; it does not load an extra model for those roles. Jev uses a separate remote adapter and does not need a Mac model resident. Core 6 is a catalog view, not a request to hold six models in RAM.

Automatic unloading is off by default. When explicitly enabled, it protects the current resident Nisi pair, considers only other observed idle Mac models, and refuses to unload anything if the pair cannot be established from a fresh snapshot. These policies do not establish that a live routed task succeeded.

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

Forty-five router integration cases need writers from a separate project. The explicit allowance permits a skip only when those writers are absent; without it, missing writers fail the test. Report the actual skips alongside passes. The tests use fixtures and mocked model calls, so a passing source suite does not establish a live route.

`build.sh` is an installer: it compiles and signs the app, then replaces `$HOME/Applications/Inference Monitor.app` when its owner lock permits. Do not run it as a read-only build check. The installer intentionally writes into the local Applications directory and does not publish content elsewhere.

### GitHub download candidate

`package-release.sh` builds a separate Developer ID signed DMG in `dist/`; it does not install or launch the app. It requires a clean source tree, an exact Developer ID Application identity in `MONITOR_RELEASE_SIGN_IDENTITY`, and secure Apple timestamps. It verifies the mounted DMG against frozen inputs, including LICENSE and NOTICE, and writes a manifest with source, toolchain, input and installation-note hashes. Missing, empty or symlinked legal inputs stop the build before output creation. `MONITOR_RELEASE_ALLOW_DIRTY=1` is only for local candidate checks; it marks the output dirty and must not be used for a release.

The current packaged app targets macOS 13 or later and needs Python 3.9 or later at `/opt/homebrew/bin/python3`, `/usr/local/bin/python3`, or `/usr/bin/python3`. The DMG contains the app, an `INSTALL.txt` note, LICENSE and NOTICE. The legal files also travel with the app in `Contents/Resources/Legal`. It is not a downloadable release until Apple notarization accepts that exact asset, the ticket is stapled and validated, the final hash is recorded, and the quarantined download passes a clean-Mac assessment.

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
