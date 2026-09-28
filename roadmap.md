# AGIW Suite repository candidate roadmap

This is the canonical roadmap for this source-only repository. The Monitor's active engineering roadmap remains in its original checkout until an owner accepts a cutover; this file does not claim installation or release acceptance.

## Current source state — 2026-09-28

- [x] Copy the Mac Inference Monitor's explicit build resource list, installer and login-agent source into an isolated package, with meaningful source tests.
- [x] Remove the current machine's LAN addresses, host name, SMB username and personal absolute paths from the copied source and tests. Fix Route now reads bounded process-environment settings and fails closed when they are absent or invalid.
- [x] Complete a second source review of the publish copy. It found public IPv4 remount targets, unsafe existing login-plist file types and links, Guest or duplicate SMB mount ambiguity, and an unconfirmed-unmount remount path. All were repaired with focused regression coverage before publication.
- [x] Run the copied Python and JavaScript tests and non-installing build checks; exact results are below.
- [x] Compare the publish copy against its source baseline. Only `.gitignore`, `README.md`, `roadmap.md`, `online_code_repair.py`, `login-agent.sh`, `test_share_and_probe.py`, `test_mem_guard.py` and the new `test_login_agent.py` intentionally differ from the original checkout.
- [x] Create a new, empty private GitHub repository for this source package.
- [x] Commit and push the explicit source allowlist after outgoing-change review. Private `main` is verified at `fe8b8cf`; the first CI run failed and is tracked below.
- [x] Add a macOS GitHub Actions source-check workflow for Python, JavaScript, shell, plist and Swift build inputs.
- [ ] Repair and inspect clean-runner CI. The first run failed 45 router dual-read tests because their state-writer fixtures live in the separate router project, plus one test that assumed an initialized host router journal. The workflow now explicitly permits only the 45 integration tests to skip; the other test creates its own private journal fixture. A second run must confirm the self-contained checks pass and report the skip count.

## Excluded from this package

Router development and installed scripts, Nisi and Jev runtime source, the Windows observer and worker, the security scanner, the generated suite report and its evidence, model files, credentials, local configuration, receipts, logs, staged candidates, installed app bundles and archives remain outside this candidate. They require their own source and acceptance decisions before any later inclusion.

## Verification record

On this Mac, Python 3.14.7 ran `PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -p 'test_*.py' -q`: **661/661** copied tests passed. The installed macOS `/usr/bin/python3` 3.9.6 ran `PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -B -m unittest -q test_login_agent test_share_and_probe test_online_code_repair test_mem_guard`: **276/276** focused tests passed, and `/usr/bin/python3 -B -m py_compile online_code_repair.py` passed. `node --test test_*.mjs` passed **226/226** copied JavaScript tests. `bash -n build.sh login-agent.sh`, `plutil -lint Info.plist`, and `xcrun swiftc -typecheck -target "$(uname -m)-apple-macosx13.0" -framework Cocoa -framework WebKit Monitor.swift` passed. A loopback smoke test started `server.py` with an isolated home, received HTTP 200 for the view, and exited cleanly. The app installer was not run. A bounded source scan found no occurrence of the original machine's account name, LAN addresses, host name or personal home path in the candidate, and no high-confidence PEM, GitHub, OpenAI or AWS key string. This scan is not a full credential audit. The new GitHub workflow remains unproven until its first clean-runner result.

These source checks do not establish live model inference, native Windows acceptance, a signed/notarized distribution, or release readiness.
