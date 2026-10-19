# A1–A4 monitor integration — 18 October 2026

## Integrated fix pass 2c — read-only Git fixture teardown

`windows/tests/install_helpers.py` now retries read-only unlink/rmdir failures after
`os.chmod(path, stat.S_IWRITE)`, using Python 3.12+ `onexc` or older `onerror`. Failed retries and unrelated
errors propagate. Every install-root caller registers teardown with its owning `TestCase.addCleanup`.
The three portable signed-repository fixtures and native enrollment cleanup share the same remover.
The inherited-ACL profile placement and default mkdir mode are preserved; production modules are unchanged.

The actual pre-fix helper, replayed under Windows-style deletion denial, raises and leaves its object/tree;
an assertion inside it becomes an error. The fixed helper removes the tree and records the assertion as
a failure. A permanent cleanup refusal records both the original failure and a separate cleanup error.
Eight new tests pass, including the legacy callback and directory paths; the focused group passes 14/14.
The sibling `windows/tests/mutate_install_cleanup.py` removes both handlers by substituting bare rmtree:
control 2/2, mutant killed by two assertion failures and zero runner errors.

Mac JS passes **489/489**. Full Windows tests run **529**, with **405 passed, 52 errors, 72 skipped**;
all errors are sandbox-denied socket binds. Replaying the exact starting test sources in memory produces
**521 run, 397 passed, 52 errors, 72 skipped**, with the exact same 52 error IDs. The requested all-green
Windows suite cannot be claimed in this managed sandbox. Original signature mutations remain skipped on Mac
(exit 2), not kills. [Pass-2c evidence](integration-evidence/fix2c/verification.md) records scope, simulation,
mutation results and preservation checks. No network, remote shell, privileged command, other models,
production edits, commits or PC cleanup were performed. Existing fix-pass-2/2b contents remain intact.

## Integrated fix pass 2b — six POSIX-edition JS tests

The four root-publisher tests now join the two previously guarded generators in skipping only on win32.
Reasons name the POSIX-only `fcntl` imports in `telemetry.py`/`server.py`, or the Linux fixture's `os.statvfs`.
Existing interpreter resolution and all six test bodies are preserved. This pass changes no production module.

The complete Mac JS suite passes both before and after: **489 tests, 489 passed, zero failures or skips**;
all six generators execute. The expanded existing platform regression fails before the four new guards and
passes afterward, requiring the exact name/reason for every skip in an injected-win32 child (**6/6 skipped**).
This is local skip-behavior evidence, not native Windows execution. Initial file hashes and a code-only delta
verify preservation of pass 2. See [pass-2b verification](integration-evidence/fix2b/verification.md).
This pass's edits remain uncommitted; no network, remote shell, privileged command or other-model call was used.
The final audit observed another actor's `923a822` fix-pass-2 checkpoint in the local reflog at 11:43:13 PDT.
This pass ran no commit command; the recorded initial file hashes still verify preservation of the starting tree.

## Integrated fix pass 2 — 18 October 2026

Current working baseline is `a042a61` on `integration-20261018`. This pass changes production only in
`windows/agiw_win/probes.py:739`: route queue roots and pending/claimed/completed directories use the existing
guard to refuse symlinks/junctions/reparse points and return unknown/incomplete with no queue counts. Eight linked-input regressions
fail before the fix; the three final methods pass. Root-guard and state-guard mutations are killed by two and six
assertion failures respectively. The absent-directory and healthy complete-empty controls remain intact.

Test-only fixes put the injected installer in an existing fixture ancestor, give provenance checks their own real
signed Git repository/key/trust configuration, use a DACL-only `icacls` fixture, resolve the four Python subprocess
tests through the documented override/fallback sequence, and skip the two POSIX publisher generators on win32 with
explicit reasons. The original three native signature tests now each include a real unsigned control.

Full Mac commands completed: JS **489/489** (484 + 5 new); root **1,335**, **10 failure records / 100 errors /
10 skipped**; Windows **521** (514 + 7 new), **52 errors / 72 skipped**; Linux **309**, **304 successful / 5 skipped**;
all seven generator groups **95,483 cases**, zero violations. Root and Windows affected-method sets and non-pass
totals exactly match the prior managed-sandbox record. Socket/process/Swift-cache restrictions remain non-passes.
Native PowerShell signature/ACL green runs and signature mutation kills are unexecuted on this Mac. There was no
network, remote shell, privileged command, other-model call, project commit or push.

[The complete pass-2 record](integration-evidence/fix2/verification.md) gives per-item file/line references,
red/green scope, exact commands/counts, logs and mutations. The full production diff plus tracked diff stat are in
[the prepared artifact](integration-evidence/fix2/integrated-fix2-code.diff); the requested `../../astra` copy is
outside this session's writable/work-only boundary and remains for the owning reviewer. All edits remain uncommitted.

## Integrated fix pass 1 — 18 October 2026

Current status for the working changes over `a77d2b2` on `integration-20261018`. This section supersedes the historical excluded-finding table and Finish pass below. The supplied merge is the baseline; this pass edits only `wt/integration` and leaves the changes uncommitted for Claude. No machine or model service, remote shell, privileged command, installation, deployment, index mutation, commit or push was used.

The disposition table follows the supplied Jev order. An already fixed finding receives a new executable replay rather than a production change.

| Jev item / review finding | Disposition and current evidence |
| --- | --- |
| 1 — sol10-A4 #1, default-off L2 | Already fixed. `server.py:599` and `online_code_repair.py:1470` retain strict `is True` owner-action checks. `test_l2_merge_contract.py` passes all 19 ordinary tests, including all 16 missing/None gate cases and ungated model control/auto-unload. The combined gate/replay run passes 49/49. |
| 2 — sol10-A1 #2, missing queue directories | Already fixed. `test_integration_fix1.py:85` checks both absent directories (unknown/incomplete) and an observed complete empty queue (idle). |
| 3 — sol11-A3 #1, dropped journal prefix | Already fixed. `telemetry.py:2799` / `telemetry.py:2861` retain the reader's prefix-loss flag. `test_integration_fix1.py:134` writes an enqueue followed by either one result or 1,600 valid results through the real file reader; only the complete read can publish live availability or an unsettled count. |
| 4 — sol10-A1 #4, unsupported metric finish | Already fixed. `windows/agiw_win/probes.py:1284` validates finish reasons. `test_integration_fix1.py:97` compares supported `stop` with `banana`, retaining the valid neighboring success and refusing an invented error count. |
| 5 — sol10-A1 #3, run-shaped non-directory | Fixed at `windows/agiw_win/probes.py:1223`. Matching files, symlinks and Windows reparse entries make the scan incomplete; linked/reparse runs roots are refused. Valid neighbors survive. A1 adds 14 `run-entry-kind` cases, including Windows reparse metadata. |
| 6 — sol10-A4 #3, capped primary route | Fixed at `server.py:250`. Display/inventory bounds qualify `pipelinesComplete` and `runCountsComplete`; a current verified running/queued primary keeps its state and positive Online Code Mode activity. A4 adds 36 `primary-route-cap` cases. |
| 7 — sol10-A4 #4, absent waiter count | Fixed at `web/map-layout.mjs:109` and `web/app.js:969`. Exact in-flight coverage requires a nonnegative safe integer `unsettled` equal to the full in-flight list length. The Activity summary now consumes that evidence too. A4 adds 63 `waiter-count-shape` cases. |
| 8 — sol10-A1 #1, capture RangeError | Fixed at `web/app.js:42`. Both capture renderers validate Date representability before `toISOString()`, with an explicit unknown fallback. A1 adds 14 `capture-range` cases, including `1e20`, negative overflow, exact Date limits, missing time, pause and disconnection. |
| 9 — sol11-A3 #2, positive handsOff lost | Fixed at `windows/agiw_win/probes.py:1411`. A fresh confirmed busy GPU or full-screen notification retains `active=true`; a complete clear signal does not require process attribution. Unavailable attribution clears only attribution fields/text. An unavailable/stale hands-off observation still yields unknown. A3 adds 72 `hands-off-signals` cases. |
| 10 — sol10-A4 #2, complete empty router | Fixed at `server.py:245`. A current complete empty-journal scan can establish idle with no checkpoint age. Explicit observation age controls current scans; expired legacy idle ages still become unknown. A4 adds 36 `idle-scan-clock` cases. |
| 11 — sol10-A1 #5, pathological JSON depth | Already fixed. `windows/agiw_win/jsonutil.py:31` enforces 64 containers. `test_integration_fix1.py:113` checks a normal object and 100,000 nested arrays. |
| 12 — sol11-A3 #5, helper half | Fixed at `activity.py:778`: `read_failed` now sets `truncated=true` and a zero missing-age floor in `_history_bound()` itself. The already safe collector publication is retained. A3 adds 16 `history-helper-read` combinations; the existing 30 `history-read` publication cases remain. |
| 13 — sol11-A3 #3, stale route inspector | Fixed at `web/app.js:531` and `web/app.js:575`. Inspector rows, primary summary and recovery view use the same qualified pipeline as the graph. A3 adds 40 `route-inspector-clock` cases, including 29 + 2.5 seconds and the inclusive 30-second boundary. |
| 14 — sol11-A3 #4, null-age route at UI cap | Fixed at `web/map-layout.mjs:890`. Running/queued rows precede history, including unknown-duration routes, and truncation metadata remains intact. A3 adds 48 `activity-ui-cap` cases, including two live PC jobs + ten historical results + one null-age route. |
| 15 — sol10-A4 #5, duplicate capture finding | Covered by item 8 and the same 14 Date-range cases. |
| 16 — sol10-A1 #6, empty archive source | Already fixed. `activity.py:916` / `activity.py:978` use source-specific archive rows. `test_integration_fix1.py:117` keeps an empty archive live beside both a saved receipt and an active route. |

The Windows LAN-clock contract is reconciled with the passive collector at `windows/agiw_win/collect.py:146`. `Collector.mac_lan_observation` is an optional already acquired result; the default remains absent and no LAN acquisition service is started. The merge checks configured identity and the original observation timestamp independently of share presence. Only failures observed within **[-5, 45] seconds inclusive** survive; neither a share refresh nor a changed configured host renews them. Share observation ages remain exact floating-point subtraction, without rounding. `pc_status()` publishes a current failure or an explicit reachability-unknown limitation only when this optional evidence was supplied. Linux's vendored pure `pc_status()` stays identical for the existing AST parity contract.

`windows/tests/test_server.py:134` retains all five A4 boundary cases (0, 45, 45.04, -5, -5.04), positive share linkage, failure ages, independent LAN source, unverified/address/latency fields and limitations. Its fixture now supplies the actual cached-result API instead of the removed `probes.MacPeer` class. Expected share/source ages use the exact subtraction that produced the timestamps; the boundary assertions are unchanged. `windows/tests/test_server.py:43` additionally executes that same assertion body through the real collector with only lane acquisition/platform adapters substituted, so the five clock cases run even when the sandbox denies socket setup. A3 adds **48** independent LAN/share-clock/name combinations, and the focused Python replay also covers invalid timestamps and names.

The seven groups still run together through `test_support/run-integration-generators.sh` or the complete JS suite. Every original dimension/case remains. The new healthy controls assert exact values; degraded inputs assert unknown or bounded evidence.

| Generator group | Prior cases | Current cases | New dimensions | Violations |
| --- | ---: | ---: | --- | ---: |
| A1 base | 2,977 | 2,977 | Original single/pair matrix retained | 0 |
| A1 boundaries | 2,546 | 2,574 | Capture range 14; run entry kind 14 | 0 |
| A1 two-sided | 347 | 347 | Original exact/capture controls retained | 0 |
| A2 Linux | 63,288 | 63,288 | Original Linux dimensions retained | 0 |
| A2 two-sided views | 107 | 107 | Original exact/capture controls retained | 0 |
| A3 | 19,245 | 19,469 | Hands-off 72; inspector 40; UI cap 48; history helper 16; LAN clock 48 | 0 |
| A4 | 6,586 | 6,721 | Idle scan 36; primary cap 36; waiter shape 63 | 0 |
| **Total** | **95,096** | **95,483** | **387 added** | **0** |

Fixture reconciliations: healthy A1 jobs, A4 job-evidence cases and the ordinary `pcSnapshot` helper now explicitly supply their known waiter counts; a missing count is tested separately rather than silently repaired by production. A4's old-event controls keep every input, but the four paired partial/capped/stale/future-router controls now assert incomplete coverage instead of exact inactivity. A3's old degradation cases now assert that complete clear GPU/full-screen evidence survives missing lane attribution; failed GPU or hands-off observations remain unknown. No expected failure or skip was added, and no boundary was relaxed. JSON cannot distinguish `1.0` from `1`, so the malformed numeric waiter dimension uses a fractional value.

Independent before/after replays use product functions and web modules read from baseline `HEAD`: all five selected Python regression methods fail before (13 assertion records, zero errors), and pass afterward within the 49-test focused group. Nine web controls/reproductions produce five failed cases before (two capture overflows, absent waiter coverage, stale inspector and hidden null-age route), and zero afterward. The complete results and final suite commands are recorded in [integration-evidence/fix1/verification.md](integration-evidence/fix1/verification.md); the seven JSON generator reports are in `integration-evidence/generators/`.

Full outside-sandbox verification remains unavailable in this managed session: its policy permits neither sandbox elevation nor privileged execution. All requested suite commands use separate fresh **real-path TMPDIRs**, including Node, and exercise the unmodified socket/process tests. Their denials are reported separately; the supplied earlier outside-sandbox run is historical evidence, not validation of these edits. The final suite table is in the linked verification record. Source changes and the five-case in-process LAN replay are complete; socket transport, process-inspection and compiler-cache dependent checks still require Claude's outside-sandbox rerun before committing.

## Scope and handoff

The working tree integrates A1, A2, A3 and A4, in that order, over `monitor-3m` / `80ec65b4b1695288062d696e148427b1fc658d1b`. The starting branch remains `integration-20261018`. No commit, tag, push, installation, deployment, privileged command, remote shell, machine contact or model-service request was made. The supplied workstream tips were read locally:

| Workstream | Tip |
| --- | --- |
| A1 | `914ac693190ac15c38760f8782861cdcc1a27520` |
| A2 | `6d6cdc7c2e71ad2477d8245add07608fba434729` |
| A3 | `23647c305680d3d1761ef3d030cb0c25f0a32a69` |
| A4 | `1130a8ab653a08961122e6c81f4f34096c58d116` |

Each diff from common base `af39471` was applied with `git apply -3`. Because this worktree's Git administration directory is outside the writable boundary, the application used a private index and object directory under ignored `.scratch/integration/`, with the existing object database as a read-only alternate. The real index, HEAD, branch refs and other worktrees were not modified. There is no pending `git merge` state. Claude must construct the final signed commit with the four supplied branches as parents; this run does not claim to have created that ancestry. The three `80ec65b` Mac/peer-view changes were retained and verified by the integrated JS suite.

Original conflict texts, ordered patches and resolution scripts are retained under `.scratch/integration/{conflicts,patches}`. This document records semantic decisions rather than only the chosen side of each textual hunk.

## Per-file conflict decisions

“A1/A2/A3” below means the already integrated side at the indicated merge; it includes the starting monitor-3m changes.

| File | Competing versions | Integrated decision and reason |
| --- | --- | --- |
| `activity.py` | A1 scan/active caps, completeness and missing-history bounds; A3 source-specific history and observation clocks. | Keep both. Failed reads and cut scans invalidate publication completeness; archive/receipt sources describe their own rows. Verified running/queued routes survive missing checkpoint age. Historical event age does not age a successful scan. |
| `build.sh` | A1 adds machine summaries and `agiw-status` bundle resources; A4 freezes build inputs and guards source replacement. | Keep A4's frozen-input packaging and include every A1 resource. No real release build or installation was performed. |
| `live_feed.py` | Earlier fast projection fields; A4 additional caller, source and runtime evidence. | Use the expanded A4 projection while retaining the merged full snapshot's machine/certainty fields. Both field-projection regression sets remain. |
| `local_callers.py` | A1 rejects malformed PID/process output; A4 verifies listener ownership, pairs socket endpoints and bounds sampling. | Combine strict record grammar with listener identity, matching two-sided sockets and rate limits. Production and injected acquisition paths use the same PID parser. A skipped idle probe says `not-checked`, never a fabricated caller count. |
| `peer_feed.py` | A1 canonical model grouping, explicit completeness and refusal of unsafe presence; A4 loaded-only versus catalog inventories. | Keep A1's refusal boundary and canonical grouping; expose `loadedListComplete` independently. Only a verified direct API catalog can establish catalog totals. A malformed row never silently shrinks an exact inventory. |
| `roadmap.md` | All four branches prepend their own status/history. | Add one canonical integrated section, retaining all four histories below it. Historical pass counts remain historical, not claims about this tree. |
| `server.py` | A1 default-off L2 gate and per-host liveness; A3 evidence invalidation; A4 strict inventory types, alias handling and dispatch expectations. | Keep the union. Only Repair/Fix/Headless use L2. Closed dispatch returns 423 with `l2_closed`; strict `True` opens it. Missing/None/false gates remain closed. Cached readiness GET is read-only; the old receipt-writing POST is unavailable. Model-control and auto-unload remain ungated. Retain the expressly excluded A4 route-publication findings. |
| `telemetry.py` | A1 canonical instances, combined inventory caps and journal-tail provenance; A3 observation clocks and independent history availability; A4 endpoint verification, atomic alias validation and read-change detection. | Keep all boundaries. Catalog/instance collisions cannot retarget controls: the strict merge API rejects atomically, while the collector isolates conflicting CLI observations outside control rows. Incomplete input invalidates aggregate counts while retaining valid observed rows. Over 256 combined models retains at most 256 rows but marks the sources/error, counts, history and presence incomplete. Journal read completeness and bounded display completeness are distinct, with both observation-age spellings supported. |
| `web/app.js` | monitor-3m/A1 machine identity and correlated counts; A2 Linux peer detail; A3 elapsed source clocks and paused capture labels; A4 caller/runtime controls, partial activity and inventory scope. | Retain host-specific graph nodes, qualified counts, Linux detail, caller sampling, runtime/sidebar controls and capture times. Positive incomplete activity is a lower bound; unavailable sources do not become idle/zero. Preserve explicitly excluded inspector and invalid-date findings. |
| `web/map-layout.mjs` | All four modify jobs, machine/peer projections, callers, activity summaries and clocks. | Combine strict schema/completeness with independent observation/event ages. A complete history-only manifest feed cannot prove zero live jobs. Preserve exact healthy values, paused captured values, two-sided attribution and unknown lane states. Detailed Linux payloads use A2's renderer; minimal relayed PC peers retain A3's summary. Caller counts include unnamed/overflow entries without inventing names. Both capture-time argument forms work. |
| `web/online-code-mode.mjs` | A1 completeness-aware run counts; A4 queued-only/readiness/runtime wording. | Keep complete-inventory requirements before an inactivity claim and retain `runCountsComplete`. A bounded list alone cannot certify nothing running. |
| `windows/agiw_win/collect.py` | A1 explicit jobs completeness; A3 passive peer publication and collector safety; A4 older independent LAN-probe result merging. | Keep A3's passive observer and all A1/A4 supported peer evidence. Failure fallback explicitly marks jobs incomplete. Do not reintroduce network probing during this pass. The incompatible, expressly excluded A4 LAN-clock contract remains open and its original test is retained. |
| `windows/agiw_win/probes.py` | A1 all-entry scan caps, nonregular queue guards, required metric fields and unique metric IDs; A3 missing-folder/JSON-depth/finish-reason hardening and history availability; A4 exact peer ages and scan completeness. | Keep the strict union. Missing, malformed, future or hidden queue evidence cannot yield idle or exact zero counts. A skipped matching queue entry returns unknown counts. Unsupported metric finishes invalidate completeness; manifest directory + ordinal prevents duplicate IDs. Peer scan refusal/cap metadata and exact ages survive. A historical manifest scan cannot certify current inactivity. |
| `test_support/monitor-harness.mjs` | A3 production-module/fake-DOM helpers; A4 runtime/control and inspector APIs. | One shared production-module loader and fake DOM, with both helper APIs. All A3/A4 generators use this shared harness; no duplicated competing fixture implementation. |
| `test_client_models.mjs` | Earlier model-scope fixtures; A4 stricter freshness/source envelopes. | Retain the scenarios, make healthy fixtures explicitly complete and use A4's runtime projections. |
| `test_live_feed.py` | A1 full-feed completeness/machines; A4 fast caller/runtime fields. | Union the expected fields and both regression blocks. |
| `test_mac_peer_view.mjs` | monitor-3m/A1 machine attribution; A2 detailed Linux view; A4 loaded-list completeness. | Keep every added case, including per-host/Linux separation and loaded-list/catalog distinction. |
| `test_map_layout.mjs` | Older jobs/source fixtures and A4's independent job evidence. | Keep the stricter source/journal envelopes, including unknown arrays, clocks and truncated projections; retain all branch-added named tests. |
| `test_monitor_ui.mjs` | A1/A3 capture wording and complete activity; A4 runtime/caller/source rendering. | Union scenarios and assertions. Healthy fixtures now explicitly supply the evidence needed by all branches. Use `Captured at` consistently and preserve lower-bound count checks. |
| `test_pc_link_label.mjs` | Earlier PC label fixture; A4 qualified loaded inventory. | Keep A4's qualified fixture and existing positive/unknown label assertions. |
| `test_peer_presence.py` | A1 machine fields; A4 `loadedListComplete`. | Expected field set includes both. Safe publication remains mandatory. |
| `test_probes.py` | Earlier caller and jobs fixtures; A4 listener/socket parser requirements. | Use the strict caller fixtures with matching listener/client/server evidence and retain every branch-added test. |
| `test_server.py` | A1 L2/readiness transport cases; A4 evidence/publication cases. | Union both test blocks. No action-gate or model-control guard was removed. |

## Additional semantic reconciliations and coverage

- `test_l2_merge_contract.py`: removed exactly 16 `expectedFailure` decorators. The 16 assertions now pass; all 19 tests in the file pass. The ancillary readiness test was renamed to assert A1's read-only contract: receipt-writing POST entry is absent (404), while cached GET remains available. `test_no_gate_writes.py` expects 423 for a closed gate. The combined files pass **38/38**, including ungated model-control/auto-unload checks.
- `test_lmstudio_endpoint.py`: the competing tests demanded either zero retained rows or 256 retained valid rows after a combined inventory overflow. Retain 256 bounded observations, require incomplete/error sources and unknown aggregates, and prevent a misleading exact empty inventory. For source failure, positive valid observations remain while aggregate counts are unknown. For alias collision, retain the catalog and isolate the ambiguous CLI observation. These assertions preserve the safety requirement rather than deleting either scenario.
- `invariant_pipeline.py`, `invariant_dimensions.py`, `invariant_two_sided.py`, `test_support/fix9_raw.py`, `test_support/fix10_dimensions.py` and affected ordinary fixtures now provide actual required queue directories, metric identity/finish fields, listed receipt paths, explicit completeness, direct API provenance and paired socket records. The missing-directory dimension still creates a genuinely missing directory. No generated dimension or case was removed.
- A1's healthy Windows manifest expectations now require **Unknown** current in-flight activity for a history-only scan, while still asserting exact recorded Answered/Failed/Routes results. This preserves A3's stricter distinction between historical data and current execution.
- `windows/tests/test_fix9_probes.py` accepts either null status or explicit `unknown` for unreadable queue evidence, but requires every exposed numeric total to be null. A hidden entry cannot publish zero. The malformed/future cases remain.
- `linux/agiw_linux/lanes.py` retains A2's two-poll decode-rate proof and incorporates final shared pure probe guards. The vendored strict JSON reader and pure Windows-format identity helper are in `linux/agiw_linux/jsonutil.py` and `pc_buildinfo.py`; Linux still publishes its own service identity. `linux/tests/test_lanes_parity.py` compares the actual shared function bodies, including the extracted JSON module. Linux tests and all 63,288 Linux generator cases pass.
- The explicitly named A4 Windows LAN-clock test and its four helpers were restored byte-for-byte from A4 in `windows/tests/test_server.py`; A3 had deleted that inherited test when removing active LAN probing. It is not hidden behind `expectedFailure` or a new skip.
- A branch-added test-name audit finds no missing JS tests. Python has only three deliberate renames (readiness POST, alias collision, source-failure aggregation), with replacement assertions described above. Shared `test_support/` contains the union of helpers and generators.

## Generators before and after

Baselines were executed from local `git archive` copies of the supplied tips inside this worktree's ignored scratch directory. Integrated reports are in `integration-evidence/generators/`. Run them together with `bash test_support/run-integration-generators.sh`; the script makes a fresh physical-path TMPDIR and writes all seven reports.

| Workstream / generator | Before | Integrated | Violations |
| --- | ---: | ---: | ---: |
| A1 main pipeline | 2,977 | 2,977 | 0 |
| A1 boundary dimensions | 2,546 | 2,546 | 0 |
| A1 two-sided dimensions | 347 | 347 | 0 |
| A2 Linux publisher → renderer | 63,288 | 63,288 | 0 |
| A2 two-sided views | 107 | 107 | 0 |
| A3 fix9/fix10/fix11 matrix | 19,245 | 19,245 | 0 |
| A4 pipeline invariants | 6,586 | 6,586 | 0 |
| **Total** | **95,096** | **95,096** | **0** |

A3 remains 8,389 Windows + 10,856 Mac cases. A4 remains 5,938 base + 339 pass-9 + 309 pass-10 cases. Reports preserve all dimension/property counts. The combined Node runner executes seven passing test entries. The A2 Linux publisher stream SHA-256 is unchanged from its baseline: `e315b18b9256e393908e1620688c32b57db470735c439d75a4558ed252d56e44`.

## Suite verification

Run outside the sandbox on 18 October 2026 (socket binds and `/bin/ps` available), each suite with its own fresh
physical-path `TMPDIR` under `/private/var/folders/…` (`t=$(cd "$(mktemp -d)" && pwd -P)`), Python 3.14 with `-B`,
after the finish-pass edits listed in the last section. Two full passes were made: the first found one non-baseline
Windows failure (fixed below); the second is the recorded result.

| Suite | Command | Ran | Passed | Failed | Errors | Skipped | Exit |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| JS | `node --test ./test_*.mjs` | 484 | 484 | 0 | 0 | 0 | 0 |
| Root Python | `python3.14 -B -m unittest discover -p 'test_*.py' -s . -t .` | 1,324 | 1,314 | 0 | 0 | 10 | 0 |
| Windows Python | `cd windows && python3.14 -B -m unittest discover -s tests` | 513 | 436 | 0 | 5 | 72 | 1 |
| Linux Python | `cd linux && python3.14 -B -m unittest discover -s tests` | 309 | 305 | 0 | 0 | 4 | 0 |

- The five Windows errors are the five `subTest` cases of the expressly excluded A4 fix-10 regression
  `test_server.EndToEnd.test_linked_share_retains_only_a_current_lan_failure_on_its_own_clock`, each
  `AttributeError: module 'agiw_win.probes' has no attribute 'MacPeer'` (A3 removed the active LAN probe). They are the
  only non-passing Windows results. The two known macOS-only failures named in the brief
  (`test_publisher_holds_parent_against_directory_swap`, `test_nan_and_deep_nesting_are_malformed`) did not fail in this
  tree; their status is recorded in the finish-pass section.
- The first pass also failed `test_server.EndToEnd.test_linked_share_has_no_lan_claim_and_no_limitation` (A3) with
  `2.4000000953674316 != 2.4`; that conflict between A3's rounded and A4's exact peer age is resolved and recorded in the
  finish-pass section. No other suite result changed between the two passes.
- The seven generators ran inside the JS suite: A1 2,977 + 2,546 + 347; A2 63,288 and 107; A3 19,245 (8,389 Windows +
  10,856 Mac); A4 6,586 (5,938 base), all with zero violations, matching the counts asserted by each branch's own test file.
- `test_l2_merge_contract.py` alone: 19 tests, OK; with `test_no_gate_writes.py`: 38 tests, OK; no `expectedFailure`
  marker remains in any test file.

## Excluded findings: local reproduction results

These are the explicitly excluded fixes. Local fake-DOM and filesystem replays are retained in `test_support/reproduce-integration-findings.{py,mjs}`; their results are in `integration-evidence/open-findings-{python,js}.json`. No remote service was involved. “No” means the reported input no longer reproduces, not that the entire component is proven correct.

| Finding | Reproduces? | Integrated evidence |
| --- | --- | --- |
| `sol10-A1 #1` / `sol10-A4 #5` malformed capture timestamp | **Yes** | Pausing `sampledAt=1e20` still throws `RangeError`. |
| `sol10-A1 #2` missing queue directories | **No** | A3's guard returns unknown/unavailable, null queue and incomplete pipelines. |
| `sol10-A1 #3` run-shaped non-directory/symlink skipped | **Yes** | The matching symlink still yields complete=true, empty history and live source. |
| `sol10-A1 #4` unsupported metric finish counted as error | **No** | `finish=banana` produces unknown state and incomplete data. |
| `sol10-A1 #5` pathological JSON nesting | **No** | Explicit depth guard rejects 100,000 nested arrays under Python 3.14. |
| `sol10-A1 #6` receipt makes empty archive source recorded | **No** | Empty archive remains a successful live scan; it is not labeled recorded because of another source's rows. |
| `sol10-A4 #1` absent L2 gate | **No** | All 16 formerly expected failures pass as real assertions; model control and auto-unload remain outside L2. |
| `sol10-A4 #2` fresh empty idle router becomes unknown | **Yes** | Live source plus idle pipeline with null checkpoint age still becomes unknown. |
| `sol10-A4 #3` truncated routes erase verified primary state | **Yes** | Verified queued primary becomes unknown and Online Code Mode active becomes null. |
| `sol10-A4 #4` missing unsettled count permits zero | **Yes** | Partial jobs payload without `unsettled` still produces `inFlightComplete=true` and “In flight 0.” |
| `sol11-A3 #1` truncated journal tail certifies no jobs | **No** | A valid 587,341-byte journal with an early live enqueue is marked incomplete/liveAvailable=false when its prefix is dropped. |
| `sol11-A3 #2` positive handsOff discarded | **Yes** | Confirmed 90% GPU signal starts active=true, then publication changes it to null when lane attribution is unavailable. |
| `sol11-A3 #3` stale route inspector | **Yes** | At 31.5 seconds effective observation age, graph says route unknown; inspector still says running. |
| `sol11-A3 #4` active null-age route lost at UI cap | **Yes** | Two current jobs + ten results hide the verified null-age route at the 12-row cap. |
| `sol11-A3 #5` failed history read ignored | **Helper only** | `_history_bound()` alone still ignores `read_failed`; integrated `collect_activity()` compensates with truncated=true and zero missing-age floor. The reported publication-level exact-count failure no longer reproduces. |
| A4 fix-10 `test_server.EndToEnd.test_linked_share_retains_only_a_current_lan_failure_on_its_own_clock` | **Yes (5 errors)** | A3 removed `probes.MacPeer` and the active LAN merge path. Outside the sandbox the retained test runs and every one of its five `subTest` cases errors with `AttributeError: module 'agiw_win.probes' has no attribute 'MacPeer'` before reaching the clock assertions; passive linked presence has no `lanFailure`. The test is kept as written, not skipped. |

The source integration and generated cases do not supersede these open findings. In particular, the requested general unknown/healthy/capture rules still have the expressly excluded exceptions above. The reproduction scripts were rerun outside the sandbox on 18 October 2026 with the same results as the table. The excluded fixes, if separately authorized, and the signed multi-parent commit remain pending.

## Finish pass — 18 October 2026 (Claude, after the Codex run stopped)

The Codex run above stopped before suite verification. This pass started by distrusting the tree: a three-way
`git merge-file` of every branch-changed file against `af39471` and each tip, plus a per-branch audit of every added
line missing from the tree and every removed line still present. Result: all four branches were already in. Every file
a branch added exists (A2's `linux/` tree byte-identical except `lanes.py`, `test_lanes_parity.py`); every file a single
branch modified is byte-equivalent to that branch; every multi-branch file contains each branch's hunks or a recorded
reconciliation of them. The roadmap keeps every `##` section of all four branches under the integrated section. No
branch content had to be brought in. The additions of this pass are the items below.

| File | Competing versions | Decision and reason |
| --- | --- | --- |
| `windows/tests/test_server.py` (`test_linked_share_has_no_lan_claim_and_no_limitation`) | A3 (and base) published the share-report age as `round(age, 1)` and its test asserts `ageSeconds == 2.4` for a report written at `now - 2.4`. A4 removed the rounding so a 30.04 s report cannot become a “fresh” 30.0 s downstream, and its tests assert the exact float (`now - (now - age)`). The integrated `probes.py` already carried A4's exact age, which left A3's test failing on float64 noise (`2.4000000953674316`). | Keep A4's exact age (the stricter freshness guard; it is what the Mac reader re-checks). The A3 assertion now checks `state == "linked"`, `ageSeconds ≈ 2.4` to six places and `linkAgeSeconds == ageSeconds`; every other assertion in the test is unchanged. The restored A4 LAN-clock test's own `(2.4, 2.4)` line is left byte-for-byte since that test is excluded from this pass. |
| `web/app.js` (runtime inspector caller row; sources list in `renderSummary`) | A4's collector reported a skipped caller probe as source state `idle` and the UI mapped `idle` with unverified UI-side activity to “Unknown · local model activity could not be verified” (inspector) and “unavailable / Local model activity could not be verified” (sources list). The integrated collector keeps A1's `not-checked` state (recorded above), which the UI did not special-case, so a `not-checked` source with unverified activity would have shown the server's “Not sampled; no local model requests observed” text instead. | The UI now treats `idle` and `not-checked` identically in both places. `test_monitor_ui.mjs` runs the two affected tests over both states and additionally asserts the sources list shows “unavailable” with the verification wording and never the server detail. |
| `telemetry.py` (`_windows_jobs` tail) | The reconciled function ended with three stacked `summary.update` calls and two `source.update` calls (A1's `incomplete` state then A4's `error` state), where only the last assignment of each key was effective. | Collapsed to one `summary.update` and one `source.update` with exactly the previously effective values and key order: `journal="invalid"`, `unsettled=None`, `liveAvailable=False`, source `state="error"` with A4's detail whenever the read is incomplete. No behavioural change; verified by the root suite. |
| `.gitignore` | Codex's and this pass's suite runs left 38 untracked `.tmp.*` directories (two over 100 MB) at the worktree root, created by the brief's `mktemp -d "$PWD/.tmp.XXXXXX"` convention and `test_support/run-integration-generators.sh`. | Added `.tmp.*/`. The directories were not deleted (no recursive removal in this pass); they are now ignored so a `git add -A` cannot sweep them in. |

Decisions by the Codex run that this pass reviewed and kept, flagged here because each changed a test's expected value
rather than only production code (all are already described in the tables above):

- `test_no_gate_writes.py`: a closed gate returns **423** with the read-only diagnosis body (A1 asserted 202; A4's
  contract requires 403/423/503 and no controller call). The web client already treats any non-OK owner-action reply
  as a rejection and surfaces `result.message`, so the diagnosis text still reaches the UI.
- `test_l2_merge_contract.py`: A4's `test_readiness_entry_remains_ungated` (POST `/api/online-code-mode/entry` → 202 and
  `request_entry` called) became `test_receipt_writing_entry_is_not_exposed` (404, not called) because A1 made readiness
  a cached read-only GET and removed the receipt-writing POST.
- `test_probes.py` / `test_server.py` (root): a skipped caller probe with known-idle models is `not-checked` (A1), not
  `idle` (A4); the UI shows neither as a caller count.
- `test_lmstudio_endpoint.py`: an alias collision isolates the CLI observation in `localInstanceObservations` and keeps
  the `lms-ps` source live (A4 rejected the whole source); an incomplete inventory source keeps its valid rows but the
  published `loaded`/`active` aggregates are `None` (A4 asserted the complete source alone could give exact counts); a
  combined inventory over 256 keeps 256 rows marked incomplete (A4 asserted an empty list).
- `test_pipeline_invariant.mjs` (A1 generator): a history-only Windows manifest scan renders **In flight Unknown** with
  exact Answered/Failed/Routes (A1 asserted an exact 0); A3's generator requires it and the manifests carry no in-flight
  evidence.
- `windows/tests/test_fix9_probes.py` (A3): unreadable queue evidence may be `status: None` or `"unknown"`, with every
  numeric total `None`; `windows/tests/test_fix7_probes.py` (A3): a metric with no `finish` is dropped and marks the
  scan incomplete rather than producing an `unknown` row.
- `test_inventory_certainty.py` (A4): A1's canonical grouping publishes the model key rather than the instance alias,
  and A1 refuses (`InventoryUnknown`) the unsafe capped publication that A4 expected to publish with
  `loadedListComplete: false`.

The two Windows failures the brief lists as known macOS-only, `tests.test_probes.Hardening.test_publisher_holds_parent_against_directory_swap`
and `tests.test_probes.Round2.test_nan_and_deep_nesting_are_malformed`, both **pass** in the integrated tree when run
explicitly outside the sandbox (A3's parent-descriptor publisher and explicit JSON depth limit cover them). They are not
among the 72 skips.
