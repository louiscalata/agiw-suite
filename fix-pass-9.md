# Bounded fix pass 9 — A1 — 18 October 2026

All seven Medium findings in `../../astra/sol8-A1.md` are fixed in the working tree. Final JavaScript verification passes; full Python verification remains non-green in this sandbox and needs Claude's outside-sandbox rerun. The prior rerun in `../../astra/verify8-A1/` is historical evidence, not a pass for this revision.

The original 2,977 generated cases and their assertions remain. `invariant_dimensions.py` adds 2,546 distinct cases, checked by `test_pipeline_invariant.mjs:164` against real production projections, the Windows collector, and the real dashboard renderer. Only acquisition boundaries are substituted. The new matrix fails on 2,200 distinct inputs against the saved pre-pass production files, and has zero violations on the final files. The combined final generator covers **5,523 cases**. Cases may fail more than one finding; per-finding counts below are deduplicated inputs, not assertion totals.

| Sol finding | Final disposition and code | Added generator dimension | New cases failing before → after |
| --- | --- | --- | --- |
| 1: combined pipeline cap | Fixed, `telemetry.py:1981`. The eight-row pipeline projection has its own bound; separately complete running/queued counts remain complete. | 108 running × queued count combinations around eight, at ages 0/300/301 seconds. | **20 → 0** |
| 2: active activity cap | Fixed, `activity.py:833`, `activity.py:895`, `activity.py:918`. Preserve omitted active rows until the collector builds the history bound; retained source counts say “at least.” | 24 cases: 0/1/11/12/13/17 active rows × running/waiting × absent/present archive history. | **8 → 0** (four completeness failures, eight lower-bound source failures, overlapping) |
| 3: incomplete-journal live edge | Fixed, `web/map-layout.mjs:137`, `web/map-layout.mjs:151`. Both lanes and edges use validated, current rows from positively complete journals. | 1,944 shared lane cases: true/false/absent completeness, 0/1/3 jobs, fast/deep/both lanes, six age boundaries, known/partial slots, unique/duplicate IDs, and three snapshot ages. | **1,128 → 0** |
| 4: lane inspector says no current job | Fixed, `web/app.js:594`. The lane inspector requires positive completeness, matching the worker inspector. | The same 1,944 cases exercise the actual inspector with retained and absent enqueues on each lane. | **1,116 → 0** |
| 5: invalid terminal result | Fixed, `telemetry.py:2640`. An invalid result marks the journal incomplete before it is skipped. | 60 cases: five invalid statuses × before/after enqueue × three timestamps × two lanes. | **60 → 0** |
| 6: Windows producer and implicit completeness | Fixed, `windows/agiw_win/probes.py:988`, `windows/agiw_win/collect.py:274`, `windows/agiw_win/collect.py:387`, `web/app.js:947`. Producer and exception fallbacks explicitly report completeness; exact activity totals require all three flags to be `true`. | 80 real Windows-collector cases (16 job inputs × five queue states), plus 250 true/false/absent/string/integer completeness-envelope combinations with empty/retained history. | **206 → 0** (80 producer, 126 envelope) |
| 7: malformed PID records | Fixed, `local_callers.py:59`. Validate ASCII PID range and process/command/socket ordering before counting; a malformed neighbor invalidates the probe. | 80 grammar cases: ten bad/partial record forms × listener/established output × valid neighboring row absent/present × before/after order. Valid continued socket rows remain controls. | **64 → 0** |

The final before/after counts are in `.fix9-evidence/finding-counts.json`; complete inputs and wrong outputs are in `.fix9-evidence/dimensions-before-final.json`. The final assertion report is `.fix9-evidence/dimensions-after-final.json`. The saved pre-pass production files are under `.fix9-evidence/pre/`, and the isolated replay under `.fix9-evidence/replay/` never replaced the working tree or touched the index.

Additional violations found and fixed while extending the dimensions:

| Final code | Generated input | Wrong output observed before the fix | Final behavior |
| --- | --- | --- | --- |
| `web/map-layout.mjs:96`, `web/map-layout.mjs:137` | Complete journal, enqueue age 301 s, timeout 60 s; also negative ages and age 90 s plus snapshot age 1/3 s. | Lane and client edge remained live after the job window; future jobs were accepted by the job view. | Require a finite, nonnegative age and timeout, and expire at timeout + 30 s after adding snapshot age once. |
| `web/map-layout.mjs:151` | Duplicate ID on a current fast job and an expired deep job. | Deep edge was live while the deep lane was not. The first fix's ID-based join also reproduced this in six intermediate cases. | Build live edges from the validated rows themselves; never transfer liveness through an ID join. |
| `web/app.js:595` | Up lane, unknown slot counts, no verified current job. | “Up · idle.” | “Up · activity unknown,” muted. |
| `web/map-layout.mjs:109`, `web/app.js:578`, `web/app.js:594` | Journal completeness absent, even with fresh retained rows. | Current-job/lane certainty was accepted; the inspector could say “None in flight.” | Only boolean `true` establishes completeness. |
| `web/app.js:947` | Activity/jobs/pipeline flags absent, string `"true"`, or integer `1`, beside valid empty/retained feeds. | “Nothing recorded yet” or “Nothing running now,” with exact zero tiles. | Unknown activity or qualified retained counts; no exact totals. |
| `windows/agiw_win/probes.py:994`, `windows/agiw_win/probes.py:1006`, `windows/agiw_win/probes.py:1045` | Directory scan cutoff, 41 manifests, six manifests with eight metrics each (48 rows), or nine metrics in one manifest. | Completeness absent despite omitted rows, allowing the dashboard's former optimistic default. | All scan, manifest, metric, and final-row caps invalidate completeness. |
| `windows/agiw_win/probes.py:1001`, `windows/agiw_win/probes.py:1019`, `windows/agiw_win/probes.py:1024` | Missing manifest, invalid metric beside a valid metric, or a manifest timestamp 60 s ahead. | Missing completeness could certify retained history as exact. | Incomplete journal; valid retained history cannot certify totals or current activity. |
| `windows/agiw_win/probes.py:653` | Nonempty queue with no per-route projection, or a queue scan cutoff. | Empty pipeline projection had no incompleteness evidence. | `pipelinesComplete=false`; a complete queue count is separate from complete per-route rows. |
| `web/map-layout.mjs:147` | Measured rate age 1 s, snapshot age 1/3 s. | Rate observation age remained 1 s. | Rate age becomes 2/4 s. This assertion failed in 1,296 generated inputs before the fix. |
| `web/app.js:126`, `web/app.js:893` | Healthy, complete in-flight journal passed through the job view and then the lane view. | During this pass, revalidating the already-projected object removed healthy lane animation because it lacked the raw schema envelope. | Each view consumes the raw journal; snapshot age is applied once. Existing positive animation assertions and generated inspector checks pass. |

The seven finding checks and the additional checks overlap; they are not separate test totals. No original generator case or assertion was removed. Existing JS assertions were retained; known-complete fixtures gained explicit producer fields, and lane fixtures gained their required age/timeout evidence. New fixture errors use `errno.EACCES` symbolically.

Final suite results, with a fresh real-path `TMPDIR` for each invocation:

```sh
t=$(cd "$(mktemp -d "$PWD/.tmp.XXXXXX")" && pwd -P)
TMPDIR="$t" node --test ./test_*.mjs

# New temporary directory from the root PWD:
t=$(cd "$(mktemp -d "$PWD/.tmp.XXXXXX")" && pwd -P)
TMPDIR="$t" python3.14 -B -m unittest discover -p 'test_*.py' -v

# From windows/; new temporary directory from that PWD:
t=$(cd "$(mktemp -d "$PWD/.tmp.XXXXXX")" && pwd -P)
TMPDIR="$t" python3.14 -B -m unittest discover -s tests -v
```

| Suite | Tests run | Passed methods | Skipped | Error methods (events) | Failed methods (assertions) | Exit |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| JavaScript | 415 | **415** | 0 | 0 | 0 | **0** |
| Root Python | 1,152 | **1,041** | 10 | 92 (101) | 9 (11) | **1** |
| Windows Python | 288 | **192** | 43 | 51 (51) | 2 (2) | **1** |
| `linux/tests` | Not present | Not run | — | — | — | — |

Denied operations and remaining failures are excluded from passes:

- Root: **93 socket-bind error events**, **3 direct `/bin/ps` error events**, **one Swift compilation error and two Swift assertion failures due to denied xcrun/module-cache writes**, and **four launcher timeout errors**. Six router methods have eight assertion failures; a separate unmodified-behavior replay captured eight `/bin/ps` denials for these methods. One further launcher cancellation assertion failed (`0` fixture children instead of `1`). The timeout and cancellation outcomes are not labeled passes or assumed to be product fixes.
- Windows: **51 denied socket-bind errors**, plus the same two failures named in Claude's supplied previous rerun: `Hardening.test_publisher_holds_parent_against_directory_swap` and `Round2.test_nan_and_deep_nesting_are_malformed`.
- The initial root run was noisier (17 failure events, 100 errors). The table records the subsequent final root run, with complete logs retained for both. The final source did not receive a later Python-production edit.

Evidence: `.fix9-evidence/js-final.log`, `root-final.log`, `windows-final.log`, `suite-counts.json`, and `router-blockers-final.json`. Method identities and denial categories are listed in `suite-counts.json`. `git diff --check` passes. `.fix9-evidence/final-manifest.json` records verified source hashes and the staged-diff hash, which equals `.fix9-evidence/index-before.sha256`.

The canonical `roadmap.md` is updated. No commit, staging operation, push, installation, deployment, remote shell, privileged command, other-machine contact, or model-service call was performed. Repair, Fix and Headless retain the default-off L2 gate; model-control endpoints remain outside it; `handsOff` is unchanged. Claude's outside-sandbox rerun and review remain pending.
