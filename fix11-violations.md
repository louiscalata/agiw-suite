# A3 bounded fix pass 11 — two-sided evidence

All changes are working-tree edits. Entry production is preserved under
`.fix11-evidence/entry/`; the identical final generator is run against the entry
code in `.fix11-evidence/before-tree/`. No machine/model service was contacted.
The saved staged diff hash is unchanged. No commit, push, install, or deployment.

## Sol10 findings

| Finding | Disposition and final implementation | Property / dimension | Cases failing on entry |
| --- | --- | --- | --- |
| Fresh queued and long-running routes discarded | FIXED. Separate lock/read observation age in `telemetry.py:1952`, `telemetry.py:2020`, `activity.py:929`; source policy `server.py:75`; browser clock `web/app.js:47`; route rows `web/map-layout.mjs:835`. Checkpoint/start ages remain history. | Exact healthy values, `route-semantics` (180 cases), plus degraded expiry | 40 lost pipeline status/identity; 42 lost either that state or an exact route row. Whole dimension: 124 failing cases. |
| Recent validated Windows success became unavailable history | FIXED. Successful complete journal reads carry availability and an independent observation age (`telemetry.py:2674`, `web/map-layout.mjs:112`). Result age still governs the 30-minute recent-success display. | Exact healthy values, `journal-semantics` (90 cases) | 8 completed-success cases showed unknown Answered counts / lost the recent success; 16 count failures including running-job controls. Whole dimension: 45 failing cases. |
| Pause claimed current inactivity | FIXED. Frozen counts explicitly say “at last sample”, the inactivity line loses “now”, and both summary and activity panel expose the ISO capture time (`web/map-layout.mjs:988`, `web/app.js:621`, `web/app.js:939`). | Frozen capture, `frozen-capture` (48 cases) | 8 had an unqualified current zero; 4 of those said “Nothing running now”. All 48 lacked the required capture timestamp. |

## Generator properties and retained coverage

- **Degraded safety:** retain all existing single/pair mutations and boundary checks;
  missing, denied, malformed, partial, capped, stale, hidden and future evidence cannot
  establish exact affected values or inactivity. Existing healthy controls stay present.
- **Healthy exactness:** exact loaded/working/queue-or-slot totals, supplied model
  identities and activities, GPU zero, memory level, callers, lane health/capacity,
  peer residency, route state/identity/counts, answered/failed counts and recent success.
  Test each edition idle and busy, plus each single benign variation: another model,
  another job, alias, 60-second-old success, one idle lane and 2.999-second full-feed age.
  Source-specific 30/60-second boundaries and event ages are independent axes.
  Unsupported capabilities with no supplied evidence remain unknown; Windows manifest
  history still cannot establish live PC-job activity.
- **Frozen capture:** paused and stale views, empty/history/running rows, both editions,
  idle/busy runtimes and 0/2.5/60-second elapsed time. The rendered summary/activity
  must contain the capture timestamp, contain no current “now” claim, and cannot show
  an unqualified `In flight 0`.

The final **19,245 cases** contain all **18,785** original cases plus **460** new cases.
The entry tree fails **588 checks in 288 cases**. Final: **zero violations**.
Property counters: **19,049 safety-matrix cases including legacy controls**, **148
healthy-exact cases**, and **48 frozen-capture cases**.

| Dimension | Cases | Failing cases on entry | Failing cases after |
| --- | ---: | ---: | ---: |
| `fix9` | 16,881 | 0 | 0 |
| `route-clock` | 1,568 | 0 | 0 |
| `route-cap` | 216 | 0 | 0 |
| `lane-window` | 120 | 0 | 0 |
| `healthy-exact` | 28 | 14 | 0 |
| `route-semantics` | 180 | 124 | 0 |
| `journal-semantics` | 90 | 45 | 0 |
| `source-boundary` | 42 | 9 | 0 |
| `history-read` | 30 | 10 | 0 |
| `frozen-capture` | 48 | 48 | 0 |
| `fast-overlay` | 36 | 36 | 0 |
| `journal-read` | 6 | 2 | 0 |

## Additional violations found and fixed

Counts overlap when one input breaks several displayed fields; do not sum these
rows as independent cases. The complete per-check entry counts follow below.

| Violation | Correction | Evidence count |
| --- | --- | --- |
| Valid route without a checkpoint timestamp disappeared | Keep lock-verified running/queued rows even when duration is absent; retain truthful unknown duration | `route-semantics`: 22 exact-route-row failures overall; 10 of these have no event age |
| Observation expiry did not expire the route chip or queued rows | Use the observation clock in the chip and feed; queued rows become `queued-stale` with a last-sample label | 26 route-chip failures; 68 route-feed failures (52 queued, 16 running); 16 false inactivity failures |
| Journal observation expiry could preserve current coverage/jobs | Expire coverage and live-source claims using read age plus elapsed snapshot time, separately from terminal-event age | 22 coverage failures; 11 current-job failures |
| Recorded successful journal read could not establish current empty coverage | An explicit successful read provides `liveAvailable`; completed manifests remain history-only | 20 journal-liveness / summary failures; `journal-read` also catches the valid empty journal |
| Healthy Windows failed-job count became unknown because no Mac route reader exists | Count failures over the edition's included sources; Mac mixed-source totals still require route coverage | 14 `healthy-exact` cases |
| Valid 30–60-second worker heartbeat was discarded | Apply its own inclusive 60-second source limit; peer reports retain their 30-second limit | 9 `source-boundary` cases |
| Successfully read empty/old receipt and archive history appeared unavailable | Successful listing/read receipt carries observation age; event age remains historical. Denied, malformed, unsafe or capped reads retain incomplete coverage | 10 `history-read` cases; 20 degraded controls also pass |
| New unknown-duration route rendered `0 s` during implementation | Render `duration unknown`; never coerce null to zero | 5 failures in `.fix11-evidence/duration-before.json`, then zero |
| New observation clock was not advanced by fast overlays | Age `observationAgeSeconds` alongside `ageSeconds` while preserving `fullSampledAt` | 27 failures / 48 checks in `.fix11-evidence/overlay-before.json`, then zero; full entry-code overlay dimension fails 36 cases |
| Successful byte read of a malformed journal became exact empty coverage during implementation | Track rejected records and bounded line coverage independently from read success; preserve known rows but withhold exact totals/live coverage | 4 failures in `.fix11-evidence/journal-before.json`, then zero |

The overlay fixture was corrected to match the actual sampler envelope: source rows
live once at `snapshot.sources`, not also by reference inside `activity.sources`.
The heartbeat oracle checks displayed lane nodes, not a retained internal view object.
These fixture corrections did not remove cases or relax the final assertions.

## Every entry-code failed check

| Dimension | Check | Failing cases |
| --- | --- | ---: |
| `fast-overlay` | overlay job liveness | 5 |
| `fast-overlay` | overlay observation clock | 27 |
| `fast-overlay` | overlay route chip | 5 |
| `fast-overlay` | overlay route liveness | 5 |
| `frozen-capture` | frozen capture time | 48 |
| `frozen-capture` | frozen current zero | 8 |
| `frozen-capture` | frozen never now | 4 |
| `healthy-exact` | exact failed | 14 |
| `history-read` | exact history count after read | 10 |
| `history-read` | exact history failures | 10 |
| `history-read` | exact history inactivity | 10 |
| `journal-read` | exact readable journal | 2 |
| `journal-semantics` | exact journal count | 16 |
| `journal-semantics` | exact journal liveness | 20 |
| `journal-semantics` | exact journal summary | 20 |
| `journal-semantics` | expired journal coverage | 22 |
| `journal-semantics` | expired journal liveness | 11 |
| `journal-semantics` | journal observation clock | 18 |
| `journal-semantics` | recent success visible | 8 |
| `route-semantics` | exact current route summary | 24 |
| `route-semantics` | exact pipeline state/identity | 40 |
| `route-semantics` | exact route chip | 40 |
| `route-semantics` | exact route feed state | 22 |
| `route-semantics` | expired route chip | 26 |
| `route-semantics` | expired route feed | 68 |
| `route-semantics` | expired route inactivity | 16 |
| `route-semantics` | retained checkpoint age | 40 |
| `route-semantics` | route observation clock | 30 |
| `route-semantics` | unknown route duration never zero | 5 |
| `source-boundary` | exact heartbeat lane visibility | 5 |
| `source-boundary` | worker heartbeat retained | 9 |

## Verification receipts

- `.fix11-evidence/before-final.json` and `before-final.log`: full final generator on entry production; expected failure.
- `.fix11-evidence/after-final.json` and `js-final.log`: final JS suite and all generated cases.
- `.fix11-evidence/root-final.json` / `.log`: exact root outcomes and actual `/bin/ps` denial receipts by test method.
- `.fix11-evidence/windows.json` / `.log`: Windows discovery from `windows`, start directory `tests`.
- `roadmap.md`: final suite outcomes, source-clock contract and remaining outside-sandbox rerun.

Every suite invocation uses a fresh `TMPDIR` from
`t=$(cd "$(mktemp -d "$PWD/.tmp.XXXXXX")" && pwd -P)`.
The root compiler caches are also placed inside that fresh directory and warmed
with `xcrun swiftc -typecheck JevKeychain.swift`; no timeout/assertion was changed.
The initial run without those cache settings exposed three extra compiler-cache
sandbox failures. The final run avoids that environmental issue.
New denied-read fixtures use `errno.EACCES`; errno reporting uses `errno.errorcode`.
