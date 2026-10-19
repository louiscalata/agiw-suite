# A3 bounded fix pass 10

Input: `../../astra/sol9-A3.md` and `../../astra/verify9-A3/win.log`.
All changes are working-tree edits in `wt/ws-A3`. No model/machine services,
remote shells, privileged commands, commits, pushes, installs or deployments were used.

## Generator proof

The existing Python publisher → JSON writer → actual JavaScript renderer matrix is
extended in `test_support/fix10_dimensions.py` and `test_fix9_invariant.mjs`. All
**16,881 original cases and their assertions remain**. The additional **1,904 cases**
make **18,785** total. The final generator passes with **zero violations**.

The identical extended test against saved entry production sources fails **1,062
checks across 740 cases**. Counts below count distinct cases, not the number of
assertions in a case. Some violation families overlap.

| Finding / dimension | Generated cases | Failed cases before | Failed cases after |
| --- | ---: | ---: | ---: |
| Sol 1: elapsed source age, source subsets, exact 30-second limit, full-feed age/skew, live/unresolved routes and job availability | 1,568 | 540 | 0 |
| Sol 2: collector row counts around 12, mixed active layouts, active name-scan truncation, bounded history and job availability | 216 | 144 | 0 |
| Windows boundary: idle/busy, 5-second activity vs inventory deadline, polling intervals, timestamp magnitudes, future times and adjacent representable timestamps | 120 | 56 | 0 |
| Retained pass 9 matrix | 16,881 | 0 | 0 |

Reproduction receipts:

- `.fix10-evidence/generator-baseline.json`: every pre-fix failing input and wrong output.
- `.fix10-evidence/generator-baseline.log`: deliberate baseline test failure.
- `.fix10-evidence/js-final-generator.json`: final case counts and zero failures.
- `.fix10-evidence/generator-summary.json`: baseline/final comparison and production source hashes.
- `.fix10-evidence/failure-families.json`: independently grouped distinct-case counts.
- `.fix10-evidence/before/` and `.fix10-evidence/reverse-tree/`: saved entry source and final-generator reverse check.

The early `generator-final-before` experiment used the caller's working directory
for its Python subprocess, so it tested only the old renderer with the new publisher.
It is **not** the baseline receipt. The generator now sets its child working directory
from `import.meta.url`; `generator-baseline` uses entry code for both languages.

## Sol finding dispositions

1. **Medium, Sol 1 — fixed**, `web/app.js:47`. Source freshness now adds elapsed
   snapshot time and requires a fresh full feed. The 30-second source deadline is
   inclusive. Small tolerated snapshot skew cannot make evidence younger. Missing
   source ages retain the legacy envelope contract; invalid/negative explicit ages
   remain unknown. Within the 540 failing clock cases, **140** reproduce source-age
   expiry while the full feed is still fresh; **35** produce the specific false
   inactivity/zero claim. Example input: `ageSeconds=29`, snapshot elapsed `2.5`,
   completed PC job plus unresolved route. Before: `Nothing running now`,
   `In flight 0`, `Routes 1`. After: route coverage/current totals unknown.

2. **Medium, Sol 2 — fixed**, `activity.py:830`, `activity.py:903` and
   `activity.py:910`. `_active_rows` retains its observed rows until the collector
   applies the public 12-row limit. Dropped active rows now reach `historyBound`;
   omitted current work sets `missingFromAgeSeconds=0`. The public limit remains
   twelve. Of the 144 failing cap cases, **36** use more than 12 active routes with
   an untruncated name scan. Example input: 13 verified live note rows, no history.
   Before: 12 output rows, `truncated:false`, exact `12 routes running now` and
   `Routes 12`. After: `truncated:true`, `At least 12 routes running now` and
   `Routes at least 12` (combined in-flight counts also become lower bounds where
   the job feed is available).

## Additional violations found and fixed

| Final file:line | Generated input | Wrong pre-fix output | Fix / failing cases |
| --- | --- | --- | --- |
| `web/app.js:47` | Source age 0, whole snapshot age 3.004 s or future skew beyond 1 s | Exact `Routes 1` despite expired/untrusted full feed | Require full-feed freshness; **280 cases** |
| `web/app.js:892`, `web/map-layout.mjs:837` | Running route, router-active age 29 s plus 2.5 s elapsed; also invalid source ages | Route row `in-flight`, summary `running now` | Independent route-reader clock yields `in-flight-stale`, retaining healthy PC job evidence; **160 cases** |
| `activity.py:727`, `activity.py:911`, `activity.py:929` | `listing='truncated'`, including zero, one or twelve observed active routes and mixed history tails | `historyBound.truncated:false`, absent/old missing-evidence floor, exact running totals or `Routes 0` | Active scan cuts mark incomplete coverage and a zero missing-evidence floor, including the no-active-row path; **108 cases** |
| `windows/agiw_win/collect.py:244`, `windows/agiw_win/collect.py:251` | Idle/busy lanes after 5 s but inside the inventory window, e.g. age 5.001 s displayed as 5.00 | Raw `slots_unknown` lane retained `slotsTotal:2`; publisher/worker already cleared it | Clear slots and capacity in the raw lane, preserve historical values only under `lastObservation`; **56 cases**, **112 assertions** across both lanes |
| `windows/agiw_win/server.py:101` | Poll interval 2 s, inventory valid through 12.8 s, including the immediately preceding timestamp | `history.loaded:null` while residency and UI still know two loaded models | History uses the snapshot's configured inventory window and rejects negative ages; **8 cases** |

## Exact Windows boundary and the outside-sandbox failures

`windows/tests/test_server.py:274` had the wrong expected capacity. Pass 9 deliberately
cleared published `slotsTotal` after slot activity expired; the three subtests still
expected `2`. They now expect `None` and additionally require the historical total
to remain `2` in the model's `lastObservation`, and the raw lane total to be unknown.
The status/up/residency assertions are preserved.

The documented contract in `windows/agiw_win/collect.py:18` is:

- Slot activity **and capacity** are current through `observedAt + 5.0` inclusively.
- Inventory/health are current through `observedAt + 11.8 + max(0, lane_interval - 1)`
  inclusively. At the default interval: age 11.799 and 11.8 have `slots_unknown`,
  `up:true`, unknown slots and loaded residency; 11.804 has `stale`, `up:null`, unknown
  slots and unknown residency. Stale never means down.
- Comparisons use observation timestamps/deadlines before rounding. `ageSeconds`
  is rounded only for display. The generator includes `math.nextafter` timestamps
  immediately below and above each deadline, at small and Unix-sized timestamps.

These paths pass through the real lane cache and snapshot publisher without sockets.
The existing HTTP end-to-end test still needs the outside-sandbox rerun because
its setup binds a socket.

## Assertion and scope integrity

- No original generated case or behavioral assertion was removed. The original
  case count still has its own exact assertion.
- The source-wiring assertion in `test_map_layout.mjs` now additionally requires
  the route-source freshness argument. The live-route fixture in
  `test_monitor_ui.mjs` supplies a live source and adds an assertion that the same
  row without that source is stale. Its existing healthy-route expectations remain.
- The staged diff hash remains unchanged; `.fix10-evidence/scope-check.json`
  records the comparison. Windows HTTP/control classes are AST-identical to entry.
  Root HTTP/control code is untouched. Repair/Fix/Headless and model-control gate
  placement is unchanged. No `gaming` field was introduced.
- See `roadmap.md` and `.fix10-evidence/suite-results.json` for final suite counts,
  environment errors and the remaining outside-sandbox verification boundary.
