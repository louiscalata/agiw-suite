# A2 bounded fix pass 11 — hermetic two-sided generator

The supplied outside-sandbox failure was a fixture-path bug, not early expiration of historical throughput. This pass changes tests and test support only. Product readers and history retention are unchanged. No commit, index write, push, install, deployment, remote shell, privileged command, machine contact or model-service call was made.

## Root cause and reproduction

`truth_pipeline_cases.py` constructed its H1 log with `Path(temporary)` while the production `probes._record` deliberately walks every parent with `O_NOFOLLOW`. A TMPDIR beneath a symlink (including macOS `/var` and `/tmp` aliases) therefore returns `reason=unavailable` before the timing parser runs. The clock was already supplied as `NOW`; the reader did not discard the history for age. On a physical path, the exact same file produces 80 prompt / 30 generation tokens/s. Through a symlink alias it produces unknown/null/unavailable. The executable reproduction is recorded in `.scratch/fix11/root-cause.json`.

The H1 path now resolves physically at `linux/tests/truth_pipeline_cases.py:155`. Old-file-mtime (one day old), recent measurement and baseline controls retain their exact historical values and last-measured labels. No production security or freshness rule was relaxed.

## Working-tree diff for this pass

- `linux/tests/pipeline_fixture.py:29`: one fresh physical fixture root, fixed observation epoch, injectable wall time, guarded source paths, and hard failures for ambient HOME/default XDG lookup, processes, HTTP, sockets and real disk statistics. `AmbientAccess` derives from `BaseException` so product unavailable-data catches cannot hide a test escape. Empty fixture-cache misses are observed once and reused; real cache replay cases still use the actual reader.
- `linux/tests/generate_pipeline_cases.py:193`: batch-scope plain-callable stubs replace four mocks per case. `all_cases` imports readers before patching and runs the complete matrix in one environment. Source, mode and product ordering remain explicit; JSON keys are sorted. Identical snapshots are deduplicated on the JSON wire by their complete serialized bytes, with validated IDs/references; every case still invokes the publisher.
- `linux/tests/raw_pipeline_cases.py:60`: cache immutable file-reader inputs, fixed sysfs path resolution, retained records and journal name inventories within a single fixture. Missing-file and permission variants remain in the relevant cache keys. Real parsers, collection, two-poll rate computation, projection and all pairwise assertions remain active. Profile evidence identified repeated `_journal_names` parent-directory walks as the remaining major cost.
- `linux/tests/shape_pipeline_cases.py:24` and `truth_pipeline_cases.py:155`: replace fixed assumed-nonexistent workspace state with the private fixture root; use physical log paths and scoped plain callables.
- `linux/tests/test_fixture_observer.py:28`: permit the generated raw fixture to select its private parent directory.
- `linux/tests/test_pipeline_fixture.py:18`: four new regression methods cover symlinked TMPDIR/history at three ages, blocked ambient reads/services, independent wall/observation clocks, and cached versus uncached file/visibility pairs in both orders.
- `test_linux_invariant.mjs:160`: isolated Python (`-I -S -B`), injected browser wall clock, validated batched snapshot references, monotonic runtime and SHA-256 receipts. All 63,288 cases and existing assertions remain; the separate view generator still checks 107 cases.

The generator already used one Python subprocess per matrix, not one per case. The measured costs were repeated mocks, fixture IO and redundant JSON transport. No child process is permitted inside the Python fixture environment.

## Final hermeticity proof

Both runs use the final eight code/test hashes in `.scratch/fix11/source-sha256.json`. They vary HOME, TMPDIR, all three XDG paths, TZ, locale and wall clock. The producer observation epoch stays fixed at 1,800,000,000 seconds; only the injected ambient wall clock moves by one day. The second TMPDIR is also a symlink alias to exercise the original failure directly.

| Run | Environment | Cases | Violations | Invariant runtime | Exit |
|---|---|---:|---:|---:|---:|
| A | Separate HOME/XDG paths; physical TMPDIR; UTC; C locale; wall offset 0 | 63,288 | 0 | 40.848 s | 0 |
| B | Different HOME/XDG/TMPDIR; symlinked TMPDIR alias; Pacific/Honolulu; C.UTF-8 locale; wall offset +86,400 s | 63,288 | 0 | 40.841 s | 0 |

Both complete serialized publisher streams have the identical SHA-256:

`e315b18b9256e393908e1620688c32b57db470735c439d75a4558ed252d56e44`

The supplied outside run took **347.559 s** for the failing invariant. The final runs take approximately **41 s**, an **8.5x improvement**, but the **under-30-second performance target remains unmet**. No cases were dropped to meet a timing goal. Timing is measured using monotonic clocks, not the injected wall clock. The first two environment checks also passed with identical streams before the final IO/transport optimization; their logs are retained. An intermediate full JS run passed 405/405 in 214.922 s before the journal-directory cache correction and is explicitly not the final timing result.

Reproduce both final environments and inspect all metadata/results in `.scratch/fix11/final-a/` and `final-b/`. Run A executes the full requested JS suite; run B executes the same full 63,288-case invariant in isolation. `environment.json`, `result.json` and `node.log` retain exact commands, paths, durations and exit codes.

## Exact final suites

Each suite used a newly created, physical, worktree-local TMPDIR. Python is 3.15.0; Node is v24.18.0. Python discovery uses the normal unittest runner with an additional receipt result class, copied from the prior pass without changing test selection or expectations.

| Suite / command | Methods | Passed | Failures | Error entries | Skips | Exit |
|---|---:|---:|---:|---:|---:|---:|
| JS: `node --test ./test_*.mjs` | 405 | 405 | 0 | 0 | 0 | 0 |
| Root: `python3 -B .scratch/fix11/run_unittest.py discover -v` | 975 | 871 | 10 | 95 | 10 | 1 |
| Windows, from `windows/`: `python3 -B ../.scratch/fix11/run_unittest.py discover -s tests -v` | 288 | 192 | 2 | 51 | 43 | 1 |
| Linux: `python3 -B .scratch/fix11/run_unittest.py discover -s linux/tests -v` | 309 | 304 | 0 | 0 | 5 | 0 |

Root has 94 affected methods: 91 socket-bind error entries; 3 `/bin/ps` error entries and 8 process-visibility assertion failures; 1 Swift/xcrun cache-write error and 2 related assertion failures. Subtest entries make the failure/error-entry total differ from the method count. Every affected method ID matches the pass-10 receipt; the current traces were inspected and classified in `root-classification.json`. None is counted as a pass.

Windows has 51 denied socket-bind errors and the two unchanged macOS baseline failures: `test_probes.Hardening.test_publisher_holds_parent_against_directory_swap` and `test_probes.Round2.test_nan_and_deep_nesting_are_malformed`. Its affected IDs exactly match pass 10. Linux skips one denied socket-bind round trip and four native-Linux watchdog tests. Denied and native checks still need the outside-sandbox/native rerun.

The final JS total duration is 40.979 s; its publisher invariant and the 107-case view matrix both report zero violations. The final Linux rerun passes 304 methods, including all four new regressions, with five skips. Seven Python AST checks, the JS syntax check, final source-hash matching and `git diff --check` pass.

The staged binary-diff SHA-256 remains `27031e749f2fc65825ece8bba5b03dd585a061aed4a62f7b595fc210a08f9538`, identical before and after this pass. Existing staged and unstaged product changes are preserved. `roadmap.md` is the canonical plan; sub-30-second runtime and denied/native checks remain explicitly open.
