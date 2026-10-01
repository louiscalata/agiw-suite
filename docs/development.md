# Development

Run these commands from the repository root. See the [README](../README.md) for the released application and prerequisites.

## Try the local view

On a Mac with Python 3, clone this repository and run from its root:

```sh
python3 -B server.py --port 8765
```

The server prints a JSON line containing its port and process ID. Open `http://127.0.0.1:8765/` in a browser; press Ctrl-C in the terminal to stop it. This starts the observer on loopback. Runtime cards can show unavailable or unknown when optional local model, router, or Windows worker services are absent.

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

The packager always produces an arm64 app for Apple silicon Macs, independent of the macOS build host architecture. The app targets macOS 13 or later and needs Python 3.9 or later at `/opt/homebrew/bin/python3`, `/usr/local/bin/python3`, or `/usr/bin/python3`. The DMG contains the app, an `INSTALL.txt` note, LICENSE and NOTICE. The legal files also travel with the app in `Contents/Resources/Legal`. Distribution verification includes Apple notarization, ticket validation, a recorded final hash and a quarantined clean-Mac assessment. The Build 7 ZIP was exported through Xcode with its app ticket attached; its clean-Mac assessment remains open as documented in the [README](../README.md#known-validation-limits). The earlier candidate DMG is unsubmitted and is not a download asset.

After choosing the Developer ID Application identity from `security find-identity -v -p codesigning`, run `MONITOR_RELEASE_SIGN_IDENTITY=<certificate-SHA-1> ./package-release.sh` from a clean checkout to create a signed candidate. Notarization, stapling, final hash verification, and a quarantined clean-Mac test remain separate gates.

The [roadmap](../roadmap.md) records this source package's verification state and remaining integration work.
## Public Nisi CLI

To run the fixed demonstrations from this checkout, install **Node.js 22 or newer**, then use:

```sh
python3 -B bundle_nisi.py --verify
node vendor/nisi/package/bin/nisi.mjs --version
node vendor/nisi/package/bin/nisi.mjs demo
```

Expected results are package verification `PASS`, version `0.2.0`, and a demo report with `outcome: COMPLETED`, `reportStored: true`, and `modelCalls: 0`. The native app checks for an Apple-silicon Node installation at `/opt/homebrew/bin/node` or `/usr/local/bin/node`. Node is a separate prerequisite. Nisi's public CLI provides fixed demonstrations; applications can use its library APIs to define broader workflows. It does not include the private shared router or run arbitrary repository tasks from the command line. See the [public Nisi release](https://github.com/louiscalata/nisi/releases/tag/v0.2.0).

### Local-only model example

Configure a compatible local chat server with JSON-schema output support and two different local models. With Node.js 22+, run from this repository’s root, replacing the two model-name placeholders:

```sh
node vendor/nisi/package/bin/nisi.mjs local-model \
  http://127.0.0.1:1234/v1/chat/completions AUTHOR_MODEL REVIEWER_MODEL
```

The fixed task generates `retry-config.json` as data. Nisi validates JSON, runs four assertions and sends the exact candidate and results to the reviewer. It allows one repair attempt within the total deadline; it prints a run report and returns a nonzero exit code when the workflow does not complete. Invalid endpoint/model arguments fail before inference.

Use a server configured to execute both models locally; a loopback address alone does not establish where a proxy runs inference. No Jev key or hosted provider is required by this example. It does not run arbitrary repository tasks or execute generated programs. The native GitHub app’s model-free self-check remains a separate entry point. Model weights and the server are installed and managed separately.
