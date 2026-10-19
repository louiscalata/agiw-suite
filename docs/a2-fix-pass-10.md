# A2 bounded fix pass 10 — 18 October 2026

All three findings in `../../astra/sol9-A2.md` are fixed in the working tree. The canonical plan and source-clock contract are in `roadmap.md`. This pass uses local fixtures only. No commit, index write, push, install, deployment, remote shell, privileged command, machine contact or model-service call was made. The request's explicit no-service boundary supersedes the standing external-reviewer preference.

## Sol dispositions and measured failures before the fixes

The final added generator is run against the saved pre-pass Python and JavaScript under `.scratch/fix10/baseline/`. Counts below are distinct generated cases, not the number of failed assertions. The same assertions run against the final source.

| Finding | Disposition and source | Property / dimension | Failing cases before → after |
|---|---|---|---:|
| F1: a lone surrogate crashes URI encoding | **Fixed**, `web/map-layout.mjs:1561` and `:1611`. Validate identity before encoding; invalid identities get a separate nonthrowing key and explicit unknown label/state. A valid neighboring model retains its own details. | No false certainty + total renderer; `invalid-unicode-identity`: high/low/embedded lone surrogate × first/second source model × alone/beside a valid model. Runs through the actual peer renderer, shared inspector entry point and literal-text DOM renderer. | **12 → 0** of 12 |
| F2: no lane still permits loaded/idle/zero claims | **Fixed**, `linux/agiw_linux/collect.py:202`. Every model requires a present string lane matching a healthy observed row. Failure clears residency, activity and all slot/rate counters. | No false certainty; `required-model-lane`: absent key/null/empty/nonmatching/non-inventory lane × idle/generating × live/direct hostile-cache reprojection. Cached cases inject unsanitized model assignments directly into an otherwise valid private cache. | **8 → 0** stated missing/null cases; **12 → 0** of 20 including four non-inventory-source failures (N5); empty/nonmatching were already safe controls |
| F3: two identities on one wrapper remain certified | **Fixed**, `linux/agiw_linux/collect.py:206` and `:246`. Wrapper conflicts clear each model's residency/activity/counters and invalidate exact inventory totals. PAIR's multiple identities remain legal. Counts are computed before the output cap. | No false certainty; `exclusive-wrapper-identity`: 2/3/32 models × idle/generating × all-loaded/one-inactive × live/direct hostile cache × both orders. | **48 → 0** of 48 |

The three Sol groups account for **1,460 failed assertions/exceptions in 68 cases** against the saved source. An invalid model identity cannot erase all peer rows via an exception. A missing lane cannot retain a numeric zero, and neither lane ambiguity nor an inventory witness certifies conflicting wrapper identities.

## Additional violations found by the two-sided generator

| Violation | Evidence before the fix | Fix | Property / cases failing before → after |
|---|---|---|---|
| N1: valid 120-character Unicode alias becomes unknown | Python's public ID limit counts Unicode characters; JavaScript counted UTF-16 units. A valid ID of 120 emoji characters lost its exact total/residency/activity/counters. | `web/map-layout.mjs:1561` uses code-point length and separately rejects lone surrogates. | No false doubt, `healthy-complete` / `unicode-boundary`: idle/generating × observation ages 0/9.999 s × live/cache. **8 → 0**; 52 failed assertions against original source. The expanded oracle found 100 assertions against the intermediate invalid-ID-key implementation, because that version also renamed the valid identity. |
| N2: older retained job suppresses a newer success | A recent, valid `lastSuccess` was demoted when the retained `recent` list contained an older failure or a different older success. | `web/map-layout.mjs:110` compares settlement ages; only equally recent or newer conflicting results can suppress the success. | No false doubt, `healthy-jobs` / one-more-job and older-success × snapshot ages 0/2.999 s × both orders × fast/deep lanes. **16 → 0**. |
| N3: future success promoted to current | A success age of -0.001 s passed the existing upper-bound-only check, both before and after adding 0.001 s of snapshot age. | `web/map-layout.mjs:97`, `:101`, `:105` require nonnegative original event ages for open/recent/last-success rows. Adding snapshot age cannot legitimize a future event. | No false certainty, `success-event-clock` / future age × snapshot ages 0/0.001 s. **2 → 0**. |
| N4: frozen summary claims “now” and omits capture time | Settled-success frozen views said “Nothing running now.” All generated frozen states omitted an absolute capture timestamp in the summary and page header. | `web/map-layout.mjs:962` uses sample-qualified wording; `:963` adds capture time. `web/app.js:924` supplies the snapshot capture and `:963` displays it in the header. | Capture-time property, `frozen-summary-and-app`: empty/success/open/failure/route × capture at epoch/fixture time × rerender after 0/20/3,600 s. **30 → 0**, including **6** forbidden “now” cases; 66 failed assertions. |
| N5: a healthy non-inventory lane certifies model residency | Assigning a model to the healthy `coordinator` row retained `loaded` even though the total was unknown. | `linux/agiw_linux/collect.py:202` requires a PAIR or GPU-wrapper inventory source as well as matching healthy lane evidence. | No false certainty, `required-model-lane` / coordinator × idle/generating × live/direct hostile cache. **4 → 0**, 8 failed assertions. These are additional to the eight original F2 missing/null cases. |

Across all new dimensions, saved pre-pass source has **1,604 violations in 128 distinct cases**. The Linux addition contributes 1,520 violations in 80 cases; jobs/frozen views contribute 84 violations in 48 cases. The failure receipts include input coordinates, property, field, actual value and expected value. No additional violation from the final added matrix remains.

## Properties, dimensions and coverage

The original **63,152** generated cases, degradation modes, source-dependency checks and existing positive controls remain. No existing assertion, timeout, gate or generator dimension was weakened or removed. The generated filesystem failures now construct errno values symbolically with `errno.EACCES`, `errno.ENOENT` and `errno.EIO`.

`linux/tests/truth_pipeline_cases.py` adds **136 cases** to the existing publisher → JSON → real peer renderer → literal DOM path:

| Added Linux dimension | Cases |
|---|---:|
| Invalid Unicode identities (`3 × 2 × 2`) | 12 |
| Required lane evidence (`5 × 2 × 2`) | 20 |
| Exclusive wrapper assignment (`3 × 2 × 2 × 2 × 2`) | 48 |
| Complete healthy observations (`7 × 2 × 2 × 2`) | 56 |
| **Added Linux total** | **136** |

Healthy Linux variations are baseline, one more model on its own wrapper, tagged/path alias, Unicode alias, Unicode alias at the 120-character boundary, old log-file mtime, and recent measured success. Each crosses idle/generating, age 0/9.999 seconds and live/cache publication. The oracle independently specifies exact field values, the complete field set, caller rows and lane observation rows. Thus a deleted field, lower-bound text, stale label or blanket unknown result fails. The historical log fixture uses the actual file reader and `os.utime`, independently of the fresh enclosing observation.

The oracle does not invent data for sources the Linux observer cannot observe. Whole-host routes/activity, staged/passive lanes, one-shot heartbeat state and idle decode throughput have explicit, enumerated unknown expectations. These are outside the healthy/complete-source premise; treating them as exact zero/idle/down would violate the first property. Every supplied healthy complete field is required to remain exact. Historical measured rates are exactly rendered with “last measured,” which identifies their source semantics rather than declaring the peer stale.

`test_two_sided_views.mjs` adds **107 generated cases**:

| View dimension | Cases |
|---|---:|
| Healthy jobs: baseline/one-more-job/older-success/alias/recent-success/idle-lane × snapshot ages × order × fast/deep | 48 |
| Success event clock: -0.001/0/1799.999/1800/1800.001 s × elapsed 0/0.001 s × fresh/stale feed | 20 |
| Publication event age independent of observation freshness: event ages 0/5/86,400 s × snapshot ages 0/2.999/3.001 s | 9 |
| Frozen activity and actual app header: five activity shapes × two capture times × three elapsed times | 30 |
| **View total** | **107** |

The healthy job oracle compares the entire returned job structure, exact lane rows, headless state, feed cardinality and all activity-summary tiles. Frozen cases execute the actual app's `updateFreshness` against a small DOM fixture, without polling or network calls. The final suite therefore checks **63,288 Linux pipeline cases + 107 view cases = 63,395 generated cases**.

The complete source-clock table is in `roadmap.md` under “A2 source clock contract — fix pass 10”: observation clocks, journal settlement/enqueue events, record mtimes and capture time are kept separate. An old success/publish/log event never ages out a fresh observation merely by being old. Conversely, a new observation cannot turn a future event into valid evidence.

## Verification receipts

Final suite outcomes and sandbox classifications are recorded below. The runner is the unchanged normal-unittest receipt wrapper from the previous pass; it records actual callbacks without filtering, retries, skip injection or assertion changes. Every suite gets its own physical worktree-local temporary directory:

```sh
t=$(cd "$(mktemp -d "$PWD/.tmp.XXXXXX")" && pwd -P)
```

Receipts: `.scratch/fix10/truth-final-baseline.json`, `views-before.json`, `truth-expanded-before.json`, `truth-lane-source-before.json`, `truth-final.json`, `invariant-final.json`, `views-final.json`, and the full suite logs/outcome JSON. Baseline files are saved under `.scratch/fix10/baseline/`.

The final Python runs used Python **3.15.0**, serially, with a separate physical TMPDIR per suite:

| Suite | Methods | Passed | Failure/error entries | Skipped | Exit |
|---|---:|---:|---|---:|---:|
| Root: `python3 .scratch/fix10/run_unittest.py discover -v` | 975 | 871 | 10 failures + 95 errors, across 94 sandbox-affected methods | 10 | 1 |
| Windows, from `windows/`: `python3 ../.scratch/fix10/run_unittest.py discover -s tests -v` | 288 | 192 | 2 known macOS failures + 51 denied-bind errors | 43 | 1 |
| Linux: `python3 .scratch/fix10/run_unittest.py discover -s linux/tests -v` | 305 | 300 | None | 5 | 0 |

Root denial categories are **91 socket-bind errors**, **3 `/bin/ps` errors plus 8 process-visibility assertion failures**, and **1 Swift/xcrun cache error plus 2 cache-related assertion failures**. `root-classification.json` lists the individual test IDs; no other error/failure category remains. Root's 105 failure/error entries do not represent 105 distinct methods, because unittest records subtests separately.

Windows has **51 socket-bind errors** and the same two failures as the supplied `../../astra/verify9-A2/win.log`: `test_probes.Hardening.test_publisher_holds_parent_against_directory_swap` and `test_probes.Round2.test_nan_and_deep_nesting_are_malformed`. The first is the macOS fallback publisher's directory-swap behavior; the second accepts deeply nested JSON. These were not changed by A2. The supplied previous-pass outside-sandbox root run had exit 0; this pass's denied checks still need Claude's outside-sandbox rerun.

Linux's skips are one denied-bind round trip and four native-Linux `/usr/bin/timeout` watchdog tests. Denied or skipped checks are **not passes**, and local fixtures do not establish native Linux deployment or systemd behavior.

Two interim JavaScript runs are retained as `js-intermediate.log` and `js-expanded-intermediate.log`. Both ran 405 tests with 404 passes; their invariant test stopped at its unchanged in-memory case-count guard when late coverage additions increased the generated total (63,284 versus 63,276, then 63,288 versus 63,284). These runs are not the final verification. The command is rerun after freezing all implementation and generator files; no case-count assertion is disabled or relaxed.

The final frozen-revision command `TMPDIR="$t" node --test ./test_*.mjs` passed **405/405 tests**, with **0 failures, 0 skips, exit 0**, on Node **v24.18.0**. Its full Linux invariant ran **63,288 cases with zero violations**; the view generator ran **107 cases with zero violations**. Total generated coverage is **63,395 cases**, including all original 63,152 cases plus **243 additions**. The final run completed in 241.140 seconds. `js.log`, `js-exit.txt`, `js-tmpdir.txt`, `invariant-final.json` and `views-final.json` are the exact receipts.

Four changed/added Python files passed AST parsing; both production JavaScript files and both generator test files passed syntax checks. Diff whitespace checks passed. All eight code/test hashes in `final-source-sha256.json` match the files used for final verification. The staged binary diff SHA-256 remains **`27031e749f2fc65825ece8bba5b03dd585a061aed4a62f7b595fc210a08f9538`**, identical to the start of the pass. Working-tree changes preserve the prior staged checkpoint.

The bounded fix pass and local fixture verification are complete. Repair/Fix/Headless remain behind A1's existing default-off L2 gate, model-control endpoints remain ungated, and `handsOff` is preserved. Claude still needs to rerun denied checks outside this sandbox; the two known Windows-on-macOS failures remain separate baseline issues. No native deployment or model inference is claimed.
