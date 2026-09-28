# AGIW Suite · Inference Monitor

The Inference Monitor is a local Mac view of AGIW Suite's inference activity. It helps the machine's owner see which model and client are active, inspect recorded route state, and request a guarded repair when the route is unhealthy. This repository contains the Monitor application source and its tests. It is a source snapshot, not an installed app or a signed release.

The monitor observes local inference and recorded router state. Explicit controls can inspect or repair narrow owner states; launching the app does not start a coding task or load a model. The Windows observer, Nisi runtime, router, security scanner and report generator have separate source ownership and are outside this first package.

The **Core 6** control is a display filter for five local LLM roles and the bundled Nomic Embed Text v1.5 embedding model. **All discovered** shows the rest of LM Studio's inventory. Switching views does not install, load, unload or remove models; the owner's current catalog can contain more than six.

## Try the local view

On a Mac with Python 3, clone this repository and run from its root:

```sh
python3 -B server.py --port 8765
```

The server prints a JSON line containing its port and process ID. Open `http://127.0.0.1:8765/` in a browser; press Ctrl-C in the terminal to stop it. This starts the observer on loopback. Runtime cards can show unavailable or unknown when the optional local model server, router, or Windows worker is absent. The Mac menu bar wrapper can be built with `build.sh`, which compiles, signs, and **installs** the app into `$HOME/Applications`; review that script before using it on your machine.

## Share recovery settings

The source contains no address or login name for the owner's actual PC. To enable the explicit Fix Route remount on a Mac, set both environment variables in the **Inference Monitor process environment** before starting the app:

```sh
AGIW_SHARE_HOSTS='10.222.33.10,10.222.33.20,pc.example.invalid'
AGIW_SHARE_USERNAME='registered-user'
```

Those addresses and the name are documentation examples; replace them with the owner's actual LAN values. `AGIW_SHARE_HOSTS` accepts one or two numeric IPv4 addresses in the `10/8`, `172.16/12`, or `192.168/16` private LAN ranges for bounded SMB reachability, and optionally a third trusted DNS name or address for matching the mounted share. `AGIW_SHARE_USERNAME` is the registered SMB account, with no password stored in this repository. Guest and Anonymous are refused. The values must be available to the app process. If `login-agent.sh install` is run with both variables exported, it stores only these non-password settings in the private per-user LaunchAgent plist so later login starts retain them. If neither is set, the agent installs without share recovery settings; a partial pair is refused. An existing login agent with different settings is not changed automatically: quit the Monitor, run `login-agent.sh uninstall`, then reinstall with both settings exported. When either value is missing or invalid at app runtime, Fix Route reports that the share cannot be safely assessed and does not probe, unmount or remount it. A share already mounted as Guest or from an unconfigured host is not treated as healthy; Fix Route stops without remounting it, so eject that mount in Finder and reconnect using the registered account.

## Memory readings for sandboxed callers

When the Mac Monitor can read the kernel memory counters, its sampler writes a short-lived `level.json` under `~/.local/state/agiw/mem-guard/`. The directory is owner-only and the file is mode `0600`. A sandboxed memory-guard caller that cannot read those counters can use this file for up to three seconds. It never renews the file from an older file reading or from a fallback. If the Monitor is not supplying a fresh reading, `memory_pressure -Q` can report available memory, but that fallback lacks swap data and may miss a swap-driven tight or critical level. The guard identifies which source it used. These readings support admission decisions; they do not load or unload a model.

`mem-guard status` reports one sample. When high swap use is its only TIGHT signal, `mem-guard admit` can take 5.1 seconds for a second direct kernel sample, even with `--wait 0`. It admits heavy work only if pressure and headroom remain healthy and swap use and swapouts do not grow. Missing counters, sandbox fallback, or another TIGHT reason still refuse admission. A build wrapper should use the `admit` exit code as its gate; the one-sample `status` result is informational.

## Source checks

From this directory, run `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -p 'test_*.py' -q` and `node --test test_*.mjs`. The full copied Python suite was checked with Python 3.14.7 on the owner's Mac; the share, login-agent, repair and memory tests were also checked with macOS `/usr/bin/python3` 3.9.6 using `PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -B -m unittest -q test_login_agent test_share_and_probe test_online_code_repair test_mem_guard`. The tests use fake owners, local fixtures and mocked network or model calls; they do not establish a live route. Forty-five router dual-read integration tests require state-writer files from the separate router project. The clean GitHub runner explicitly sets `MONITOR_ALLOW_ROUTER_FIXTURE_SKIP=1` and reports those tests as skipped; it must never imply they ran there. `bash -n build.sh login-agent.sh`, `plutil -lint Info.plist` and a Swift typecheck can validate the build inputs without installing an app. GitHub Actions runs the self-contained source checks on a clean macOS runner; inspect the run before treating it as passed.

`build.sh` is an **installer**: it compiles and signs the app, then replaces `$HOME/Applications/Inference Monitor.app` when its owner lock permits. Do not run it as a read-only build check. The app may require installed companion commands for some controls; missing companions are reported as unavailable.

### GitHub download candidate

`package-release.sh` builds a separate, Apple silicon or Intel Developer ID signed DMG in `dist/`; it does not install or launch the app. It requires a clean source tree, an exact Developer ID Application identity in `MONITOR_RELEASE_SIGN_IDENTITY`, a secure Apple timestamp for both the app and DMG, and the version in `Info.plist`. Set `MONITOR_RELEASE_OUTPUT_DIR` to write elsewhere. The script refuses to overwrite an existing output, verifies the mounted DMG contents against frozen inputs, and emits a sidecar manifest with source, script, toolchain, and input hashes. `MONITOR_RELEASE_ALLOW_DIRTY=1` is only for local candidate checks: it adds `-dirty` to the filename and records `sourceDirty: true` in the manifest. That candidate must not be treated as a releasable build.

The current packaged app targets macOS 13 or later and needs Python 3.9 or later at `/opt/homebrew/bin/python3`, `/usr/local/bin/python3`, or `/usr/bin/python3`. The DMG contains the app and an `INSTALL.txt`, but no interpreter or model files. Check that an installed path runs `--version` before copying the app to Applications. Python 3.9.6 started the observer and served the dashboard and snapshot on the owner's Mac; startup on a separate clean Mac remains a release check.

After choosing the Developer ID Application identity from `security find-identity -v -p codesigning`, run `MONITOR_RELEASE_SIGN_IDENTITY=<certificate-SHA-1> ./package-release.sh` from a clean checkout. The output remains a local candidate until the later distribution gates pass.

The DMG is **not a downloadable release** until Apple notarization accepts that exact asset, its ticket is stapled and validated, the final hash is recorded, and the quarantined download passes a clean-Mac test. No notarization credentials, model files, private network settings or installed app state are included in this source repository. Model serving and network/router integrations are separate components.

The [roadmap](roadmap.md) records this source package's verification state and remaining integration work.
