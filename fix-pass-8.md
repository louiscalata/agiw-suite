# A1 bounded fix pass 8 — 18 October 2026

Both Medium findings in `../../astra/sol7-A1.md` are fixed. The final generated probe-to-dashboard matrix passes **2,977 distinct cases**. Replaying the same matrix against the saved pre-pass production files fails **24 invariant check categories**; the final revision fails none. This is source-level verification. The sandbox cannot complete the socket/process integration tests; Claude still owns the outside-sandbox rerun.

## Findings and dispositions

Line numbers below identify the final fix locations. The “before” outputs come from the pre-pass reproduction or deterministic baseline replay. Related failed assertions are grouped by cause rather than counted as separate bugs.

| ID | Location | Degraded input and incorrect output before this pass | Disposition |
|---|---|---|---|
| Sol7-1 · Medium | `telemetry.py:2933` | 256 inactive API models plus a distinct generating CLI model. Merging retained only 256 rows but said `modelsComplete=true`, `loadedCount=0`, `busyCount=0`; history recorded zero and the dashboard said “quiet now.” | **Fixed.** Mark the combined inventory incomplete before truncation. Machine and history counts become null; presence is withheld; the renderer cannot fall back to zero. |
| Sol7-2 · Medium | `web/map-layout.mjs:542` | A valid inactive PC row plus a loaded row rejected for an invalid ID. The real Python normalizer drops the invalid row; the dashboard said “Reports at least 0 loaded · activity unknown.” | **Fixed.** Zero retained loaded rows in a partial inventory says “loaded unknown.” Positive lower bounds remain explicitly labelled. |
| G1 | `web/map-layout.mjs:542` | A partial PC inventory retaining one busy model said “at least 1 loaded · 1 busy,” including an exact “Reports 1 busy” tiny caption. | **Fixed.** Busy totals also say “at least”; missing activity never becomes zero or idle. |
| G2 | `web/app.js:356` | Missing, partial, capped or age-invalid API inventory still displayed exact discovered totals and “Activity counts use the full inventory,” including “0 of 6 primary models discovered.” Hidden rows had no uncertainty explanation. | **Fixed.** The scope note says inventory is incomplete/unavailable and discovered totals are unknown. |
| G3 | `peer_feed.py:953` | A stale/future/unknown-age local observation, or an expired store read, still produced a presence document with `modelsTotal=1` and a loaded row even though its machine summary was unknown. | **Fixed.** Presence respects the machine’s current state and completeness and refuses publication with an explicit inventory explanation. |
| G4 | `web/app.js:55` | Missing, malformed or future model ages could retain the “Idle” node caption. | **Fixed.** Residency and activity require a finite, non-negative, unexpired age. Invalid observations lose their current state. |
| G5 | `web/app.js:546` | A model with no usable observation age still displayed the exact loaded-instance count `1` in the inspector. | **Fixed.** Current instance lists/counts and parallel slots become unknown when the observation is not current. |
| G6 | `telemetry.py:2774` | Partial inventory still made Nisi say “only fixture/model is loaded,” “no local model is loaded,” or that a second model was needed. | **Fixed.** Component readiness consumes inventory completeness and says its local inventory is incomplete/unavailable. |
| G7 | `local_callers.py:49` | Malformed `lsof` output, permission warnings, or an invisible listener produced an empty live list, “0 local process(es),” and “No local process is calling the model server.” | **Fixed.** Reject incomplete output, stderr warnings and missing listener visibility; preserve null caller evidence. |
| G8 | `web/map-layout.mjs:489`, `web/app.js:609` | Stale or future caller-source ages retained a checked caller list and a present-tense “Calling … now” claim. | **Fixed.** The view checks source state and source age plus snapshot age. The actual runtime inspector passes that evidence. |
| G9 | `local_callers.py:42`, `web/app.js:614` | Missing CLI activity caused the skipped caller probe to report `state=idle`. A failed caller read beside a working model said “Not checked while local models are idle.” | **Fixed.** Skipped probes say `not-checked`; the inspector says caller observation is unavailable or not checked. No machine idleness is inferred. |
| G10 | `telemetry.py:1455`, `telemetry.py:1851` | A capped router directory scan retaining no run was rendered as “Task idle” and “Idle; no active route.” | **Fixed.** An incomplete empty listing is unknown in both pipeline and Online Code Mode projections. |
| G11 | `telemetry.py:1344`, `web/online-code-mode.mjs:53` | Missing, unreadable, malformed or stale router evidence showed `Mac pair 0/1 … PC fast 0/2`. | **Fixed.** Carry route-count completeness through to lane occupancy, which becomes unknown. |
| G12 | `telemetry.py:1344`, `web/app.js:561`, `web/map-layout.mjs:937`, `web/online-code-mode.mjs:88` | Nine genuinely held fixture run locks generated an eight-row projection rendered as an exact “8 routes running” / “Routed tasks (8).” Queued-only totals could also imply no other work was running. | **Fixed.** Projection limits invalidate total completeness. Retained positive route counts say “at least”; incomplete queue evidence cannot claim nothing else is running. |
| G13 | `telemetry.py:2451` | A worker heartbeat 600 seconds in the future was clamped to `ageSeconds=0`, `state=advertised`, and “Recent registered Windows worker heartbeat.” | **Fixed.** Future heartbeat evidence cannot become available through age clamping. |
| G14 | `telemetry.py:2535`, `telemetry.py:2577` | Malformed, partially parsed, line-capped or byte-tailed journals were treated as ordinary recorded evidence; old journal evidence could support empty/current totals. | **Fixed.** Preserve byte-tail and parse completeness, projection bounds and freshness. Unknown job activity is explicitly labelled. |
| G15 | `web/map-layout.mjs:92`, `web/map-layout.mjs:774` | A partial journal retaining an enqueue still marked the job “running now”; a retained success could remain `current=true` despite an unreadable later record. The omitted record could be its completion or a newer failure. | **Fixed.** Incomplete journals cannot establish current jobs, current success or live lane activity. Retained open records are shown as unknown, not as a positive lower bound on running work. |
| G16 | `activity.py:210`, `activity.py:282`, `activity.py:568`, `activity.py:834`, `web/app.js:947`, `web/map-layout.mjs:973` | Missing/unreadable history, rejected records, future timestamps, stale jobs, and bounded activity lists still produced exact tiles (`In flight 0`, `Answered 0`, etc.) or “Nothing running now.” | **Fixed.** Preserve history read completeness and future-date uncertainty through both full and fast publications. Activity totals are unknown, with positive retained observations labelled “At least N”; zero lower bounds are never shown. |

The two Sol regressions were recorded failing before their fixes and passing after them:

- `test_pipeline_invariant.py:11`: combined inventory cap → machine counts, history counts and presence publication.
- `test_map_layout.mjs:7`: partial PC inventory with only inactive retained rows.
- The generator also exercises both exact inputs through the Python-to-JS path.

## Generator design and coverage

`invariant_pipeline.py` supplies controlled acquisition inputs to real production code. `test_pipeline_invariant.mjs` streams the resulting JSON into the real `web/map-layout.mjs`, `web/online-code-mode.mjs` and `web/app.js` functions using the existing fake DOM. It executes summary, vitals, scope notes, graph model captions, runtime/model/pipeline inspectors, and the activity list/tiles. The inspector is explicitly opened, and omitted model rows require an uncertainty explanation.

The ten independently degraded input sources are:

1. LM Studio API inventory.
2. LMS CLI activity/inventory.
3. Windows peer report.
4. Authenticated Linux peer report.
5. Local caller process/socket observation.
6. Router journal/listing/lock observation.
7. Windows worker heartbeat.
8. Windows job journal.
9. Local model observation-age envelope.
10. Route archive/receipt activity history.

Every source receives all eight degradation classes: missing, unreadable, malformed, partial/incomplete, capped/truncated, stale/expired, hidden, and future-dated. All source pairs receive the full Cartesian product of those classes. Invalid list rows, distinct over-limit inventories, actual private fixture directories, real fixture file locks, malformed journal bytes, missing listener visibility and stderr permission warnings exercise the relevant parsers. Ages are changed at the parsed observation/cache boundary where the source protocol itself has no timestamp. OS/network acquisition is substituted; no machine or model service is contacted.

The collector, peer normalizers, caller probe, router reader/projections, worker parser/cache projection, job parser, activity reader, `machine_state.local_summary`, `server.SnapshotStore.publish/read`, and `peer_feed.mac_presence` are real code. The fixture mirrors the production `_mark_live_route` composition step. The test does not pre-fill expected unknown results or replace display functions.

**Case count: 1 + 10×8 + C(10,2)×8×8 + 16 = 2,977.**

- 1 healthy generating fixture.
- 80 single-source degradations.
- 2,880 two-source combinations.
- 16 additional cases: eight age degradations of an idle fixture; healthy idle and empty fixtures; stale and future whole-feed reads; nine-live-route projection cap; Sol’s combined local cap; Sol’s partial PC zero; and a partial journal retaining a successful result.

The oracle checks dependent outputs, not unrelated healthy evidence. For example, damaging a worker heartbeat must not erase a separate fresh authenticated peer report. Healthy busy, idle and truly empty fixtures retain their valid values. This is a finite regression matrix over these source envelopes, not a proof over all possible programs, payloads or native transport states.

Baseline evidence: `.fix8-evidence/generator-baseline.json` and `.log` contain the failing check categories and representative input/output pairs. Final evidence: `.fix8-evidence/generator-final.json` has `cases: 2977` and `counts: {}`. Baseline production files are byte-preserved in `.fix8-evidence/baseline/`; an isolated replay uses those files. Its test driver omits the new `models_complete` keyword only because the old component function did not accept it; no baseline production logic is changed.

## Final validation

Node **24.18.0**, Python **3.14.7**. Each broad suite uses its own fresh physical TMPDIR:

```sh
t=$(cd "$(mktemp -d "$PWD/.tmp.XXXXXX")" && pwd -P)
```

| Suite | Exact result |
|---|---|
| `TMPDIR="$t" node --test ./test_*.mjs` | **414 passed, 0 failed, 0 skipped; exit 0.** The matrix runs inside this suite. |
| Root: `TMPDIR="$t" CLANG_MODULE_CACHE_PATH="$t/clang" SWIFT_MODULECACHE_PATH="$t/swift" python3.14 -B -m unittest discover -s . -p 'test_*.py' -v` | **1,152 methods run: 1,049 passed, 10 skipped, 87 methods with errors, 6 methods with assertion failures; exit 1.** Unittest reports 96 error events and 8 failure events. |
| Windows: `cd windows`; fresh TMPDIR there; `TMPDIR="$t" python3.14 -B -m unittest discover -s tests -p 'test_*.py' -v` | **288 run: 192 passed, 43 skipped, 51 errors, 2 failures; exit 1.** |
| `linux/tests` | **Absent from this worktree. No Linux suite ran here.** The supplied external Linux result is not counted as a pass for this revision. |
| `git diff --check` | **Passed.** |

Sandbox failures, excluded from passes:

- Root: **93 denied socket-bind error events** and **3 direct `/bin/ps` permission errors**. The **8 router assertion failures across 6 methods** reproduce against the saved pre-pass production source; that replay captures **8 `/bin/ps` denials**, with no error events. The affected root method sets match the pass-7 local evidence exactly.
- Windows: **51 denied socket-bind errors**, plus the known macOS-only failures `test_probes.Round2.test_nan_and_deep_nesting_are_malformed` and `test_probes.Hardening.test_publisher_holds_parent_against_directory_swap`.
- Detailed method names and event counts: `.fix8-evidence/suite-counts.json`; baseline router reproduction: `.fix8-evidence/router-baseline.{json,log}`.

The final broad logs are `.fix8-evidence/{js,root,windows}.log`; the adjacent `.tmpdir` and `.exit` files record their environment and exit status. No denied or skipped test is counted as passing.

Existing assertions and gates were not weakened. The partial-PC busy expectation was made explicitly lower-bound, the skipped-caller expectation now requires `not-checked`, and the existing Core 6 healthy fixtures now declare a live complete inventory source while retaining their assertions. Repair, Fix and Headless remain the only registered A1 L2-gated actions; model controls remain ungated, and `handsOff` remains the machine field. No commit, index mutation, push, installation, deployment, remote shell, privileged command or model-service contact occurred.
