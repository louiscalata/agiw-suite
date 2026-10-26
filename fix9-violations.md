# A3 bounded fix pass 9 — violation and verification record

Working tree only. No Git index changes, commits, pushes, installation, deployment, privileged commands, remote shells, or machine/model-service calls. This record describes local source fixes and deterministic evidence; it does not claim native Windows acceptance.

## Sol 8 findings

| Finding | Disposition and location | Regression evidence |
|---|---|---|
| Medium: completed PC jobs plus unavailable routes said “Nothing running now” and “In flight 0” | Fixed in `web/map-layout.mjs:979` and `web/app.js:45`. Combined current activity and failure totals stay unknown when route coverage is unavailable. | `test_fix9_ui.mjs:5` fails against entry code, passes after. The existing UI test that asserted the incorrect absence now requires unknown. |
| Low: POSIX publication could follow a parent directory swap | Fixed in `windows/agiw_win/probes.py:1515` and `:1710`. Walk directories with no-follow descriptors, then create, replace, and clean up relative to the retained parent descriptor. Refuse platforms without the required flags. | Existing `windows/tests/test_probes.py:279` fails before by finding `outside/x.json`; passes after. The transient-replace test's fake now forwards descriptor keywords to the real `os.replace`, retaining its retry and payload assertions. |

## Generator violations found and fixed

Locations below identify the final fix. “Degraded” means a missing, unreadable, malformed, incomplete, capped, expired, hidden, or future-dated source with an otherwise healthy retained payload. Claims from independent healthy inputs remain available; affected claims must be unknown, an explicit lower bound, or omitted with a reason.

| ID | Final location | Input | Wrong output before the fix | Result now |
|---|---|---|---|---|
| G01 | `windows/agiw_win/probes.py:1315`; `windows/agiw_win/server.py:65` | Degraded lane configuration/inventory or slot source; retained idle/busy rows | Exact loaded/working totals, “quiet now,” current published idle/busy, and exact local-machine loaded count | Source evidence invalidates the affected rows, aggregate history, publication, and machine view. Unknown residency does not become zero. |
| G02 | `windows/agiw_win/probes.py:1341` | Same source loss, with model metadata retained | Model inspector still said `0/2 busy` or `2/2 busy` after its headline became unknown | Slot metadata, activity, and derived totals are cleared together. |
| G03 | `server.py:83`, `:151`; `windows/agiw_win/server.py:43` | Partial residency or missing, stale, or future model-row age | Numeric history totals; current idle/working or loaded counts despite an unverified row | Unknown history points and aggregate counts; invalid row timestamps cannot authorize current evidence. |
| G04 | `server.py:83` | Degraded LM Studio source envelope | Retained loaded/activity values still produced exact totals or quiet/working copy | Residency requires a healthy inventory source; activity requires the activity source. An independent healthy API/CLI inventory is retained. |
| G05 | `windows/agiw_win/probes.py:1366`; `server.py:114` | Caller/process source degraded, cached empty list retained | “None connected,” “No local process is calling,” or a checked empty list | Callers become unchecked/unknown, including per-model caller metadata. |
| G06 | `windows/agiw_win/probes.py:1375`; `server.py:116` | Job source degraded, cached completed rows retained | Exact “Answered 1” and “Failed 0” | Feed availability is unknown. Recorded rows remain history without establishing complete totals or live activity. |
| G07 | `windows/agiw_win/probes.py:1372`; `server.py:102` | Windows queue source or Mac pipeline source degraded | “Idle” and exact queue counts | Route state/counts are unknown with an unavailable/incomplete reason. |
| G08 | `windows/agiw_win/probes.py:1380`; `server.py:105` | GPU source degraded, fresh-looking zero reading retained | `0%` GPU use | GPU reading is omitted/unknown; the stale cached zero is not displayed as a measurement. |
| G09 | `windows/agiw_win/probes.py:1387`; `server.py:107` | Memory source degraded, cached OK/50% payload retained | Known memory level and exact availability | Unknown memory block; consumer/usage claims no longer survive source loss. |
| G10 | `windows/agiw_win/probes.py:1377` | GPU, process attribution, or hands-off source degraded | `handsOff.active: false`, state `clear` | `handsOff.active: null`, state `unknown`, with the evidence-loss reason. |
| G11 | `windows/agiw_win/probes.py:1384` | PAIR source degraded with a cached empty census | Exact process/engine zero counts | Unknown PAIR census and counts. |
| G12 | `windows/agiw_win/probes.py:1390`, `:1995` | Incomplete peer roster, capped/refused peer scan, or unavailable peer source | `1 loaded`, including the cached Mac-presence chip; scan detail also looked exact | Scan completeness travels with the source. Peer/Mac residency is unknown when its supporting evidence is incomplete; retained scan counts are labelled “at least.” |
| G13 | `web/map-layout.mjs:1613` | No current peer report | “None has published on SharedChami yet” | “Unknown (no current peer report available).” Absence of a report does not establish absence of peers. |
| G14 | `server.py:109` | PC telemetry or worker source degraded on the Mac | Cached PC loaded/idle report and up/slot lane readings | PC report omitted and worker evidence marked unknown. |
| G15 | `web/app.js:45`; `web/map-layout.mjs:979` | One or two route readers degraded while another remained readable | Exact Routes 0 and complete-activity claims | Complete route counts require all route readers; combined in-flight/failure totals stay unknown when route coverage is unavailable. |
| G16 | `web/app.js:932`; `web/map-layout.mjs:1000` | Thirteen activity rows reduced to the twelve-row display; includes live routes with unavailable live PC jobs | Exact twelve answered/running routes and possible “Nothing running now” | Explicit lower bounds, including “At least 12 routes running now”; incomplete coverage cannot prove inactivity. |
| G17 | `web/map-layout.mjs:589` | One reported busy model plus another with unknown residency or activity | `1 busy` as an exact aggregate | `at least 1 busy`. Complete healthy reports retain their exact busy counts. |
| G18 | `windows/agiw_win/probes.py:303` | Raw `/slots` envelope says incomplete or truncated | `idle`, slots busy 0 | `slots_unknown`, no exact occupancy. |
| G19 | `windows/agiw_win/collect.py:233` | Cached lane observation timestamp ahead of the collector clock | Current idle, age clamped to zero | Unknown lane and slot counts. A poll completing during the current collection is compared with the current clock, so ordinary sampling latency is not a clock fault. |
| G20 | `windows/agiw_win/probes.py:1194` | Run without a manifest, more manifests than the reader cap, or future manifest mtime | History marked available; future rows given age zero | Incomplete availability; future rows refused. These three raw cases failed before. |
| G21 | `windows/agiw_win/probes.py:729` | Hidden/nonregular queue file, malformed JSON, or future file mtime | Idle/zero for the hidden file; queued/exact count for malformed or future entries | Unknown queue with an explicit reason. These three raw cases failed before. |

The same final generator against the preserved entry code produced **68,019 failed invariant checks across 16,881 cases**. These are repeated manifestations of the families above, not 68,019 independent defects. The final tree produces **zero violations**.

## Test design and coverage

`test_fix9_invariant.mjs` invokes `test_support/fix9_cases.py` with Python, consumes its serialized snapshots, and runs the actual `web/app.js` summary, vitals, activity, runtime inspector and model inspector functions plus the actual view-model functions. `test_support/monitor-harness.mjs` provides a fake DOM and fixed clock; it does not implement replacement renderer logic.

Python runs the real Mac and Windows `SnapshotStore.publish`, `pc_status`, `peer_presence`, `mac_presence`, and `write_json_atomic`. The raw-input cohort also runs `probe_lane`, `Collector._lane`, `codemode_jobs`, `route_queue`, and `read_peers`. Transport responses and permission failures are injected at boundaries; filesystem inputs are real temporary files. No observer server or model service is started by the new tests.

The deterministic source matrix uses **idle and busy healthy fixtures**, eight degradation classes, every source alone, and every distinct source pair with all 64 combinations of degradation classes.

- Windows: lane-config, hands-off, nvidia-pair, lane-inventory, llama-slots, lane-processes, codemode-runs, route-queue, pc-gpu, mem-guard, agiw-peers — **11 channels**, 3,609 cases per fixture, 7,218 total.
- Mac: pipeline-router, mac-gpu, mem-guard, windows-telemetry, windows-worker, lmstudio-api, lms-ps, local-callers, windows-jobs, router-active, monitor-receipts, router-archive — **12 channels**, 4,321 cases per fixture, 8,642 total.
- Raw cohort: health response, model response, slot response, job files, queue files and peer files — **1,009** single/pair/control cases. Stale/future transport evidence is injected at the retained observation clock, not as invented HTTP fields.
- **12 boundary cases** cover display caps, incomplete busy reports, partial residency and model-row missing/stale/future ages.
- **Total: 16,881** — 8,231 Windows and 8,650 Mac cases. Five healthy controls preserve exact known values, including 2 loaded and 0 or 2 working. Four standalone Python regression methods cover nine raw failing subcases; two standalone JS methods cover the report finding and capped activity.

This is exhaustive within the declared fixture/mutation matrix, not a proof over every possible JSON value or native OS behavior. Legacy direct publisher inputs without a source envelope retain their pre-existing contract; the real collectors provide the envelope. Missing individual sources within that envelope are covered.

## Verification

Every suite used a separate real-path TMPDIR created by `t=$(cd "$(mktemp -d "$PWD/.tmp.XXXXXX")" && pwd -P)`. Python used `-B`; root Swift checks used the installed Command Line Tools and compiler caches inside that TMPDIR.

| Suite | Exact result |
|---|---|
| `node --test ./test_*.mjs` | **430 tests, 430 passed, 0 failed, 0 skipped**; includes the 16,881-case invariant test. |
| `python3 -B -m unittest discover` | **976 methods: 875 passed, 10 skipped, 85 erroring methods / 94 error records, 6 failing methods / 8 failed assertions**. Exit 1. |
| `cd windows; python3 -B -m unittest discover -s tests` | **511 tests: 388 passed, 72 skipped, 51 errors, 0 failures**. Exit 1. The POSIX directory-swap regression now passes. |
| `linux/tests` | **Not run: absent in this worktree.** Not counted as a pass. |

Sandbox errors are separate from passes: root has **91 socket-bind denials and 3 direct `/bin/ps` denials**; Windows has **51 socket-bind denials**. An instrumented rerun records real `/bin/ps` PermissionErrors in each of the six root failing methods and reproduces all eight assertions, without substituting outcomes. There are no new root error or assertion-failure identities versus pass 8; three former compiler-cache-affected methods now pass. Windows bind-error identities match pass 8. The 94 root error records represent 85 distinct methods; repeated subtests are not counted as additional methods. Claude's outside-sandbox rerun remains necessary for those methods; native Windows installer/ACL/publisher acceptance is not established here.

No test assertion was removed and no control gate was relaxed. Expectations that asserted the incorrect absence/count were strengthened to require unknown or a lower bound. Valid fixture timestamps and complete-route controls were supplied where the new validation exposed incomplete fixtures; existing positive assertions remain. Both servers' code from `class MonitorServer` onward is unchanged from entry, including HTTP/model-control and owner checks. `handsOff` remains the published field.

Evidence: `.fix9-evidence/js-final.log`, `root-final.log`, `windows-final.log`, `linux-final.log`, `suite-results.json`, `generator-final-against-before.log`, `raw-before.log`, `finding1-before.log`, `finding1-after.log`, `finding2-before.log`, `finding2-after.log`, `root-ps-diagnosis.log`, `root-ps-denials.json`, and `control-boundaries.json`. `git diff --check` passes. The staged checkpoint was compared byte-for-byte using `git diff --cached --binary` before/after and is unchanged.
