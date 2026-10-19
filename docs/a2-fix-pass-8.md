# A2 bounded fix pass 8 — 18 October 2026

This pass changes only the working tree in `wt/ws-A2`. Sol's four findings are fixed in source. The canonical plan remains `roadmap.md`. No commit, index write, push, install, deployment, privileged command, remote shell, remote machine contact, or model-service call was made. Outside-sandbox and native acceptance remain separate.

## Sol round-seven disposition

All four original regression methods failed against the starting tree and passed after the fixes. Receipts: `.scratch/fix8/findings-before.log`, `findings-after.log`, and `findings-final.log`. The last receipt also includes an additional lane privacy/consistency test.

| Finding | Disposition and source | Regression |
|---|---|---|
| Medium: extra Python flags hide a mapped coordinator | **FIXED.** `linux/agiw_linux/linsys.py:461` retains an exact mapped script argument as a candidate at any position. Only the existing strict identity parser verifies its runtime/release. `linux/agiw_linux/probes.py:438` treats an unaccounted candidate as a conflict, including beside an active verified unit. Unverified candidates retain `kind: unknown`. | `linux/tests/test_fix8_findings.py:15`; both absent units and an additional active unit are exercised. |
| Medium: SSH projection loses wrapper failure reasons | **FIXED.** `linux/agiw_linux/collect.py:98` projects bounded lane IDs, validated status/health, an explicit reason allowlist, activity and slot/rate evidence. URLs, argv, paths and free-form text are excluded. `linux/agiw_linux/probes.py:197` preserves typed transport failures such as timeout. | `linux/tests/test_fix8_findings.py:36`; `unreachable / timeout` survives projection. An additional test rejects private reasons and contradictory health. |
| Medium: `/slots` incorrectly uses the 350 ms allowance | **FIXED.** `linux/agiw_linux/probes.py:26` gives slots 750 ms while health/model requests retain 350 ms. The outer sampling/forced-command deadlines remain in force. | `linux/tests/test_fix8_findings.py:57`; a deterministic transport requiring 500 ms succeeds. This verifies the requested timeout, not native endpoint latency. |
| Low: spaces in a known caller name become unknown | **FIXED.** `linux/agiw_linux/collect.py:88` reuses the producer's bounded safe-basename alphabet. | `linux/tests/test_fix8_findings.py:75`; `Claude Code` and known caller completeness survive. |

The existing wrapper fixture now asserts the exact endpoint-specific timeout, replacing its former blanket upper-bound assertion. No unrelated assertion, model-control endpoint, action gate, or `handsOff` behavior was relaxed or changed. Only Repair/Fix/Headless remain subject to A1's existing default-off L2 policy.

## Generative invariant and proof boundary

`test_linux_invariant.mjs` starts `linux/tests/generate_pipeline_cases.py`, streams its JSON to Node, invokes the real `linuxPeerView` in `web/map-layout.mjs`, checks that `agiwPeersRows` returns those exact same rendered rows, and executes the actual `web/app.js` `detailPairs` DOM renderer for every generated snapshot. The rendered `<dd>` text must exactly match the checked values; HTML insertion is prohibited in the DOM harness. Python executes the real `Collector`, lane probes, one-shot `server.once`, bounded projection and JSON encoder. HTTP replies are fixture transports; real network access is forbidden in the fixture.

There are **62,516 cases**, each with a complete result checked, rather than a sampled random subset:

- **48,834 source-boundary cases:** 28 sources, eight degradation modes, every single source and every pair of distinct sources with all 64 mode combinations, plus a healthy control, repeated for idle and generating fixtures. Formula: `2 × (1 + 28×8 + C(28,2)×64)`.
- **13,682 filesystem-reader cases:** 15 proc/sysfs/visibility sources under the same single/pair construction and both activity fixtures. Formula: `2 × (1 + 15×8 + C(15,2)×64)`.
- Modes: missing, unreadable, malformed, partial/incomplete, capped/truncated, hidden, stale/expired, and future-dated.
- Source boundaries include both PAIR endpoints, all three wrapper endpoints, processes, sockets, GPUs and individual metric provenance, memory, callers, listeners, model inventory/counters, lanes, coordinator units, components, history, boot, routes, and explicitly omitted CPU/disk/temperature/lock/membership/authority/run/idle-analysis inputs.
- Filesystem inputs include IPv4/IPv6 tables, process status/stat/cmdline/executable, socket visibility, DRM discovery, all five published GPU measurements, memory and boot identity. Real bounded readers parse mutated files. Directory cases separately exercise interrupted scans, malformed entries and entry caps; executable-link cases separately exercise missing, denied, malformed, incomplete and oversized targets. OS visibility failures are injected without changing real permissions or contacting Linux.
- Identical filesystem evidence is reused across activity and snapshot-clock variants. Each distinct file mutation is still read by the real reader; each case separately traverses the publisher and renderer. This avoids repeating identical filesystem work and keeps the suite bounded.
- Kernel and live HTTP responses do not have independent observation clocks. Their stale/future variants invalidate the enclosing snapshot, whose original timestamp is preserved through publication. Historical figures are explicitly last-measured observations, never live rates.
- Assertions follow source dependencies. Degrading a caller source invalidates caller counts; it does not erase an independently observed GPU measurement. Unknown fields must include a reason. Intentionally omitted sources must include a `sourceReasons` entry. Host activity/routes remain unknown without complete evidence; the test forbids “nothing running”, “quiet”, “no callers” and “no routes”.
- Healthy controls require real known outputs: correct loaded/caller counts, observed idle, explicit complete empty inventories, generating state, and a 10 tok/s two-sample delta. Returning unknown everywhere cannot pass.

The same final matrix against the saved **pre-pass Python tree**, using the current renderer, produced **73,544 violating field outputs**. These are repeated field failures across combinations, not 73,544 distinct defects. The final fixed tree reports **zero violations** across the same 62,516 cases. Raw receipts: `.scratch/fix8/invariant-baseline-complete.json` (all counterexamples), `.scratch/fix8/invariant-final.json` (final result), and their logs.

This is a finite schema and dependency invariant, not a proof about every possible byte sequence or silent upstream omission with no completeness signal. The boundary fixture includes an explicitly attested complete model inventory to test totals; the real filesystem collector's whole-host model inventory remains incomplete. A4 transport deployment and native Linux/systemd/endpoint timing are not established here.

## Violations found and fixed

The table groups counterexamples by defect. Locations identify the final fix; full individual cases and wrong outputs are in the baseline JSON receipt.

| ID | Final source location | Degraded input | Wrong output before the fix | Result after the fix |
|---|---|---|---|---|
| G1 | `linux/agiw_linux/probes.py:100` | PAIR or wrapper health/models/slots explicitly marked incomplete or truncated | `up`, `loaded`, `idle`, `0` busy slots, exact slot totals; generating fixtures also retained live rates | Completeness/pagination/cap signals invalidate the affected endpoint evidence. Slots failure preserves independently verified residency but withholds activity/counts/rates. |
| G2 | `linux/agiw_linux/collect.py:170` | Missing, unreadable or malformed model list with a retained completeness bit | Exact `0` models and `0` loaded | Invalid list shape cannot certify completeness; total is null and the view says unknown. |
| G3 | `linux/agiw_linux/collect.py:203` | Failed PAIR/wrapper/process/network source with a retained complete-inventory claim | Exact totals of `0`, `1` or `2` from a partial surviving inventory | Failed inventory-producing lanes veto the aggregate completeness claim. |
| G4 | `linux/agiw_linux/collect.py:174` | Missing, malformed, partial, duplicated or capped lane evidence | `2` loaded; retained `loaded`, `idle`, slot counts or live generation from the old model row | Linked model claims require validated lane evidence. Lane and model activity/counts must agree. |
| G5 | `linux/agiw_linux/collect.py:228` | Memory is a string, lacks its percentage, or explicitly marks a capped result | Publisher `AttributeError`; retained `ok` or `30` percent | Malformed memory is normalized to unknown; level and percentage require valid, complete evidence. |
| G6 | `linux/agiw_linux/server.py:152`; `linux/agiw_linux/collect.py:238` | Components are null/string, partial, duplicated or capped | Publisher `.append` `AttributeError`, or exact coordinator/PAIR `up` | Publication remains available; invalid component inventories cannot certify health/presence. |
| G7 | `linux/agiw_linux/collect.py:65` | Historical-rate list is partial, duplicated or capped | `80` prompt and `30` generation tok/s retained as exact last-measured figures | Bounded role rows remain, with unknown kind and null rates until complete evidence exists. |
| G8 | `linux/agiw_linux/linsys.py:595` | Malformed/truncated IPv4 or IPv6 table header | `0` callers; `0` or `2` listeners; invalid IPv6 visibility also authorized `loaded`, `idle`/`generating`, slot totals and `10` tok/s | Invalid headers make the socket inventory incomplete. Observed rows may remain, but cannot authorize totals or lane probes. |
| G9 | `linux/agiw_linux/linsys.py:424` | Malformed/partial process status, command line or executable link | Exact `0` callers despite a hidden lane identity | Required identity fields, command framing and executable basenames are validated; inventory uncertainty propagates to callers and probes. PID identity loss also cannot establish coordinator absence. |
| G10 | `linux/agiw_linux/collect.py:212` | GPU metric provenance is missing while scalar values remain | Exact `25%`, `100/8192 MiB`, `42 °C`, `30 W` | Each scalar needs its validated source. Range and used/total consistency checks also apply. |
| G11 | `linux/agiw_linux/collect.py:271` | Eight intentionally unprojected source families, degraded singly or together | Omission without an explanation | Each omitted source carries a fixed bounded reason. No route/idle/empty-state claim is inferred. |
| G12 | `web/map-layout.mjs:1516`, `web/map-layout.mjs:1565` | Expired or future-dated Linux peer summary | Old generic renderer retained `1 loaded`, including a future report marked current | Linux-specific rendering invalidates all current claims at expiry/clock mismatch. Before/after rows: `.scratch/fix8/peer-ui-before-after.json`. |

Additional acceptance checks harden the new renderer itself against invalid timestamp fallback, malformed model identities, contradictory slot evidence, invalid caller connection rows, and memory levels without a valid percentage. Five focused tests failed during implementation before these guards were added; `test_linux_peer_view.mjs` now includes seven tests with positive controls. Python also validates slot counters and rejects contradictory lane health/private reason text.

## Final validation

Final JavaScript result: **404/404 passed**, zero failed/skipped, exit 0. This includes the **62,516-case invariant with zero violations** and seven focused Linux peer-view tests.

| Suite | Run | Passed | Other outcomes | Exit |
|---|---:|---:|---|---:|
| JavaScript (`node --test ./test_*.mjs`) | 404 tests | 404 | 62,516 generated cases inside the invariant test; zero violations. | 0 |
| Root Python 3.14.7 | 975 methods | 871 | 94 sandbox-affected methods; 10 skips. Unittest reports 10 failure entries and 95 error entries because subtests can contribute multiple entries. | 1 |
| Windows Python on macOS | 288 methods | 192 | 51 sandbox socket-bind errors; 2 known macOS baseline failures; 43 skips. | 1 |
| Linux Python 3.14.7 | 305 methods | 300 | 1 sandbox socket-bind skip; 4 native-Linux watchdog skips. No failures/errors. | 0 |

Sandbox outcomes are not passes:

- Root: **91 socket-bind error entries**, **3 `/bin/ps` error entries plus 8 process-visibility assertion failures**, and **1 Swift/xcrun cache error plus 2 Swift/cache assertion failures**.
- Windows: **51 socket-bind errors**. The two known macOS failures are `test_publisher_holds_parent_against_directory_swap` and `test_nan_and_deep_nesting_are_malformed`.
- Linux: `test_loopback_http_roundtrip` is skipped for denied socket binding; four watchdog tests require native Linux.

Every suite used a fresh worktree-local physical TMPDIR created with:

```sh
t=$(cd "$(mktemp -d "$PWD/.tmp.XXXXXX")" && pwd -P)
```

JavaScript ran with `TMPDIR="$t" node --test ./test_*.mjs`. Python used normal unittest discovery via the transparent `.scratch/fix8/run_unittest.py` receipt runner: root `discover -v`, Windows from `windows/` with `discover -s tests -v`, and Linux `discover -s linux/tests -v`. The runner only records actual success/error/failure/skip callbacks; it does not filter, skip or retry tests. Logs and exact outcome/TMPDIR receipts are in `.scratch/fix8/{root,windows,linux}.log` and `*-results.json`.

Eight changed/added Python files passed AST parsing. `git diff --check` passed. The staged binary diff SHA-256 remains `27031e749f2fc65825ece8bba5b03dd585a061aed4a62f7b595fc210a08f9538`, matching the initial checkpoint. Claude's outside-sandbox rerun is still needed for the denied checks; this pass did not contact Claude or any other model service.
