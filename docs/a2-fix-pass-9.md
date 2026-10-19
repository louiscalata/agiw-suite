# A2 bounded fix pass 9 — 18 October 2026

All four findings in `../../astra/sol8-A2.md` are fixed in the working tree. The canonical plan is `roadmap.md`. No commit, index write, push, install, deployment, remote shell, privileged command, machine contact, or model-service call was made. The earlier instruction to use external reviewers was superseded by this pass's explicit no-service instruction.

## Sol disposition and failing-before evidence

The final added matrix contains **636 generated cases**. Running that same matrix against the saved pre-pass Python **and JavaScript** source produced **3,408 violations in 560 distinct cases**. After the fixes it produces **zero violations**. Counts below are distinct generated cases, not assertion/field counts. The four Sol groups and two additional defect groups have disjoint case labels.

| Finding | Fix and source location | Generator dimension | Added cases that failed before the fix |
|---|---|---|---:|
| F1, Medium: incomplete lane evidence becomes an exact model count | `linux/agiw_linux/collect.py:229` requires a bounded, unique `modelInventoryLanes` witness containing PAIR and the wrapper family, exact coverage by the observed inventory lanes, healthy source rows, and valid model-to-lane references. Missing witnesses and missing rows cannot certify a total. The real collector records the witness before projection at `collect.py:384`; cache revalidation preserves it. | Empty/nonempty inventories; absent key versus null versus empty list; PAIR-only, wrappers-only, unrelated-only and one-of-two-wrapper omissions; reversed order; malformed/duplicate witness IDs; live publication and cache replay. | **78**: 36 live, 36 cached, 6 malformed-witness cases |
| F2, Medium: two PAIR models on one lane collapse into one detail | `web/map-layout.mjs:1605` keys each detail by encoded model ID and labels it with model ID plus lane context. Tagged IDs retain their full spelling. | 1/2/3/31/32/33 PAIR rows; loaded/inactive/loading mixtures; both orders; plain/tagged/path IDs; duplicate IDs; public alias collisions. Real PAIR replies pass through the collector and parser. | **168** non-collision cases; collision cases are recorded separately as N1 |
| F3, Medium: unknown lane publishes idle and zero slots | `linux/agiw_linux/collect.py:119` removes activity, slot counts and rates when health is unknown, and changes unsupported idle/busy/serving/active/absent/disabled claims to unknown. | Wrapper/PAIR/coordinator lanes × absent/unknown/down/up health × six statuses × observed/timeout reasons. Positive healthy idle/busy and confirmed coordinator absence/disablement remain checked. | **116** |
| F4, Low: unknown callers get “observed” as their reason | `linux/agiw_linux/collect.py:154` publishes a caller-specific `callersReason`; `web/map-layout.mjs:1599` consumes it and uses expiry reasons for stale/future feeds. Listener evidence stays independent. | Caller list missing/empty/valid/malformed/capped at 32/33; known/unknown/absent visibility; valid and invalid listener feeds; freshness and timestamp-skew boundaries; invalid peer beside a valid peer. | **130**: 39 visibility cases and 91 clock/mixed-feed cases |

The lane diagnostic `status: unreachable, reason: timeout` is retained. It is failure evidence, not a claim that a machine is down. This preserves the existing pass-8 named-failure regression without weakening its exact assertion. Unknown health never retains idle/busy activity or numeric slot/rate claims.

## Additional generator violations found and fixed

| Defect | Source fix | Generated input | Wrong output before the fix | Failing cases |
|---|---|---|---|---:|
| N1: public identity collisions, including beyond the output cap | `linux/agiw_linux/collect.py:184` counts public identities before sorting/capping; `collect.py:220` makes every retained ambiguous identity unknown with null activity counters. `web/map-layout.mjs:1607` preserves separate detail rows and independently refuses exact state for duplicate IDs. | Distinct paths `/private/0/same.gguf` and `/private/1/same.gguf`, distinct IDs sharing their first 120 characters, and duplicate aliases in 31/32/33/64-row inventories with both orders. | The same public model was still marked `loaded`/`inactive`; the renderer collapsed rows. A duplicate in row 33 or 64 left retained `model-0` marked loaded even after the first collision fix. | **56** against pre-pass source: 48 identity-matrix cases and 8 tail cases. Expanding the first fix with tails exposed **4** still-failing cases, which were then fixed. |
| N3: lane-wide activity copied onto several model identities | `linux/agiw_linux/collect.py:208` requires unique model attribution before copying lane activity, slots or throughput. | 2/3/32 model rows sharing `gpu-wrapper-101`, idle/generating lane evidence, with either all loaded or one inactive row. | Several models each showed the same `idle`, `slotsBusy: 0`, or `generating`, `slotsBusy: 1`, `slotsTotal: 1`, `liveTokensPerSecond: 10`. | **12**, comprising **284** wrong output assertions |

The 100 clock/window cases are also a guard against regressions at ages `-0.001`, `0`, `9.999`, `10`, `10.001` seconds and timestamp skews `0`, `±0.009`, `±0.011` seconds, with both feed-freshness states and complete/incomplete callers. No further clock-validity defect was found; caller reason failures are included under F4.

## Coverage and assertion preservation

The default generator retains all **62,516** pass-8 cases and adds **636**, for **63,152** total:

| Added dimension | Cases |
|---|---:|
| Inventory witness / live and cache replay (`3 × 8 × 2 × 2`) | 96 |
| Invalid witness shapes | 6 |
| PAIR cardinality, identity, lifecycle and order (`6 × 6 × 3 × 2`) | 216 |
| Bounded model tails (`4 × 2 × 2`) | 16 |
| Per-model attribution on shared lanes (`4 × 2 × 2`) | 16 |
| Lane health/status/reason consistency (`3 × 4 × 6 × 2`) | 144 |
| Independent caller/listener visibility (`7 × 3 × 2`) | 42 |
| Clock windows, skew and mixed feeds (`5 × 5 × 2 × 2`) | 100 |

`linux/tests/shape_pipeline_cases.py` supplies the new dimensions. `test_linux_invariant.mjs` checks published JSON, the real `linuxPeerView`, integration through `agiwPeersRows`, and the actual app's literal-text DOM renderer. New model-detail assertions require row cardinality, identity, lane context and the correct per-model state. An invalid neighboring peer cannot erase or contaminate the valid peer's rows.

The original 62,516-case matrix, its degradation modes, dependencies and positive controls remain. The test harness translates the original lane-based assertion vocabulary into model-based field identities; no dependency or assertion was removed. The explicit-complete Python fixture in `linux/tests/test_collect.py:15` now supplies valid source rows and their witness while keeping its original count assertions. Returning unknown everywhere still fails the healthy controls. `AGIW_INVARIANT_SHAPES_ONLY=1` is a diagnostic option used for saved-source comparisons; the required full JavaScript suite uses the default full matrix.

This remains a finite generated invariant over supplied evidence, not proof of native Linux execution or a way to discover silently omitted sources with falsified upstream attestations. The real collector still reports whole-host `modelsComplete: false`; the generator explicitly attests its synthetic complete inventory. A1's default-off Repair/Fix/Headless gate, ungated model-control endpoints and `handsOff` behavior are unchanged.

## Verification receipts

Receipts are under `.scratch/fix9/`:

- `shapes-final-baseline.json`: all 3,408 pre-pass counterexamples with input coordinates, field, wrong output and expected result; saved source is under `baseline/`.
- `shapes-before.json`: first 556-case matrix, before the fixes (2,988 violations).
- `shapes-expanded-before.json`: 588-case matrix after the initial fixes, exposing 4 hidden-tail cases and 12 shared-lane cases (288 violations).
- `shapes-final.json`: final 636 added cases, zero violations.
- `invariant-final.json`, `js.log`: the complete final matrix and required JavaScript suite.
- `root-results.json`, `windows-results.json`, `linux-results.json`: actual unittest success/failure/error/skip callbacks, interpreter identity and physical TMPDIR; paired `.log` files contain full traces.

Every suite used a fresh worktree-local physical TMPDIR:

```sh
t=$(cd "$(mktemp -d "$PWD/.tmp.XXXXXX")" && pwd -P)
```

JavaScript ran `TMPDIR="$t" node --test ./test_*.mjs`. Python ran normal unittest discovery via the unchanged transparent receipt runner copied from pass 8: root `discover -v`, Windows from `windows/` with `discover -s tests -v`, and Linux `discover -s linux/tests -v`. That runner records outcomes without filtering, skipping, retrying or changing assertions. The current `python3` is **3.15.0**; Claude's supplied pass-8 logs used **3.14.7**.

The final JavaScript suite passed **404/404** tests, including the complete **63,152-case invariant with zero violations**.

| Final suite | Methods/tests run | Passed | Other outcomes | Exit |
|---|---:|---:|---|---:|
| JavaScript, `node --test ./test_*.mjs` | 404 | 404 | No failures or skips. | 0 |
| Root Python 3.15.0 | 975 | 871 | 94 sandbox-affected methods, 10 skips; unittest records 10 failure entries and 95 error entries, including subtests. | 1 |
| Windows Python on macOS | 288 | 192 | 51 socket-bind errors, 2 pre-existing failures, 43 skips. | 1 |
| Linux Python 3.15.0 | 305 | 300 | One denied-bind skip and four native-Linux watchdog skips; no failures or errors. | 0 |

Denied operations are **not passes**:

- Root: **91 socket-bind error entries**, **3 `/bin/ps` error entries plus 8 process-visibility assertion failures**, and **1 Swift/xcrun cache error plus 2 Swift/cache assertion failures**. `root-classification.json` lists every entry. The final run has no other failing outcome.
- Windows: **51 socket-bind errors**. The two failures also occur in the supplied outside-sandbox `../../astra/verify8-A2/win.log`: `test_publisher_holds_parent_against_directory_swap` and `test_nan_and_deep_nesting_are_malformed`. No Windows file was changed in this pass.
- Linux: `test_loopback_http_roundtrip` skips for denied binding. Four `test_outer_deadline` methods require native Linux `/usr/bin/timeout` and were not executed here.

Earlier concurrent runs are retained separately, not substituted for these final results. The initial root run had 866 passes, four additional launcher timeouts and one cancellation assertion failure (11 failure entries / 99 error entries overall); a focused five-method rerun while generators were still active reproduced them. The final serial root run passed those five methods with their original deadlines. Two earlier full Linux runs had a four-second cold-entry timeout (299 passes, one error, five skips); its isolated rerun passed, followed by the final serial full suite above. An intermediate JavaScript run overlapped the addition of 80 cases and failed its unchanged-in-memory case-count guard (`63,152` actual versus `63,072` expected); the final complete run used the frozen 63,152-case matrix and passed. No assertion, timeout, gate or coverage was relaxed to obtain the final results.

Four changed/added Python files passed AST parsing. Both changed JavaScript files passed `node --check`; `git diff --check` passed. `final-source-sha256.json` records the six code/test files at the final JavaScript run. The staged binary diff SHA-256 remains **`27031e749f2fc65825ece8bba5b03dd585a061aed4a62f7b595fc210a08f9538`**, identical to the start of the pass. Temporary suite fixtures were removed after completion; logs and receipts remain.

The bounded source fixes and local fixture checks are complete. Claude still needs to rerun denied checks outside the sandbox; native Linux deployment, systemd behavior and endpoint latency are not established by this pass.
