# AGIW Suite · Inference Monitor

The Inference Monitor is a local Mac view of AGIW Suite's inference activity. It helps the machine's owner see which model and client are active, inspect recorded route state, and request a guarded recovery action when a local route or worker needs intervention.

The monitor observes local inference and recorded router state. Explicit controls can inspect or repair narrow owner states; launching the app does not start a coding task or load a model. The Windows and SharedChami route repair flows are optional and require separately managed components.

The Core 6 control is a display filter for five local LLM roles and the bundled Nomic Embed Text v1.5 embedding model. All discovered shows the rest of LM Studio's inventory. Switching views remains local to the machine and the app does not upload activity data elsewhere.

## Public release status

This repository is intended as a source-release project for public use. It is a macOS-focused application with Python, JavaScript, and Swift components, and it expects a local runtime environment rather than bundled model services.

Before publishing or distributing binaries:
- verify that no real machine names, credentials, tokens, or local-only state are embedded in the source tree
- confirm the local security model and release signing identity are appropriate for your environment
- review the package-release workflow before shipping a notarized DMG

The project is provided under the MIT license; see the LICENSE file for details.

## Try the local view

On a Mac with Python 3, clone this repository and run from its root:

```sh
python3 -B server.py --port 8765
```

The server prints a JSON line containing its port and process ID. Open `http://127.0.0.1:8765/` in a browser; press Ctrl-C in the terminal to stop it. This starts the observer on loopback. Runtime behavior is intentionally local-only.

## Share recovery settings

The source contains no address or login name for the owner's actual PC. To enable the explicit Fix Route remount on a Mac, set both environment variables in the Inference Monitor process environment before launch:

```sh
AGIW_SHARE_HOSTS='10.222.33.10,10.222.33.20,pc.example.invalid'
AGIW_SHARE_USERNAME='registered-user'
```

Those addresses and the name are documentation examples; replace them with the owner's actual LAN values. `AGIW_SHARE_HOSTS` accepts one or two numeric IPv4 addresses in the `10/8`, `172.16/12`, or `192.168/16` ranges or a hostname value that resolves on the LAN. The app validates them again before any share recovery action.

## Memory readings for sandboxed callers

When the Mac Monitor can read the kernel memory counters, its sampler writes a short-lived `level.json` under `~/.local/state/agiw/mem-guard/`. The directory is owner-only and the file is mode `0600` or stricter. The app does not persist unnecessary memory detail outside the local host.

`mem-guard status` reports one sample. When high swap use is its only TIGHT signal, `mem-guard admit` can take 5.1 seconds for a second direct kernel sample, even with `--wait 0`. It admits heavy memory pressure only after a second signal is observed.

## Source checks

From this directory, run `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -p 'test_*.py' -q` and `node --test test_*.mjs`. The full copied Python suite was checked with Python 3.14.7 on the macOS runner in CI.

`build.sh` is an installer: it compiles and signs the app, then replaces `$HOME/Applications/Inference Monitor.app` when its owner lock permits. Do not run it as a read-only build check. The installer intentionally writes into the local Applications directory and does not publish content elsewhere.

### GitHub download candidate

`package-release.sh` builds a separate, Apple silicon or Intel Developer ID signed DMG in `dist/`; it does not install or launch the app. It requires a clean source tree, an exact Developer ID App signing identity, and a macOS build host. The script is designed for a release candidate process and deliberately rejects a dirty source tree unless explicitly allowed.

The current packaged app targets macOS 13 or later and needs Python 3.9 or later at `/opt/homebrew/bin/python3`, `/usr/local/bin/python3`, or `/usr/bin/python3`. The DMG contains the app and an `INSTALL.txt` note; it is not a downloadable release until Apple notarization accepts that exact asset, the ticket is stapled and validated, the final hash is recorded, and the quarantined download passes a clean-Mac assessment.

After choosing the Developer ID Application identity from `security find-identity -v -p codesigning`, run `MONITOR_RELEASE_SIGN_IDENTITY=<certificate-SHA-1> ./package-release.sh` from a clean checkout to create a signed candidate. All release metadata is kept local to the build host until notarization and publication are complete.

The [roadmap](roadmap.md) records this source package's verification state and remaining integration work.

## Contributing

Contributions are welcome for bug fixes, tests, and documentation improvements. Please open a pull request with a focused change, keep the scope narrow, and share the relevant validation output when practical.

Before submitting a change:
- run the Python and JavaScript test suites locally on macOS
- avoid adding credentials, hostnames, or sensitive local state to the repo
- keep examples generic and safe for public review

See [CONTRIBUTING.md](CONTRIBUTING.md) for more details.

## Security

Please review [SECURITY.md](SECURITY.md) before reporting vulnerabilities or sensitive security issues.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
