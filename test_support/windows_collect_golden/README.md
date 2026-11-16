# Windows snapshot golden corpus

`cases.json` records the observable behavior of `Collector.collect` from the
unmodified revision named in `generated_from`. Inputs are synthetic; no machine
state was copied into this corpus. The canonical project plan remains
`roadmap.md`.

The corpus contains 330 cases: 209 snapshots and 121 escaping exceptions.
Each expectation was recorded by executing the original function, including
ordered boundary calls, exception type and message, retained ownership and
poller keys, and mutable process-source state. No expected output is handwritten.

`windows/tests/test_collect_golden.py` replays snapshots.
`windows/tests/test_collect_golden_errors.py` replays escaping errors and checks
repeatability, reverse ordering, copied inputs, and restoration of patches.
JSON comparisons distinguish booleans, integers and floating-point values.
Nonfinite readings have explicit tagged representations.

`record.py` uses the saved inputs to regenerate every expectation from a fresh
archive of the full revision in `generated_from`. It checks the pinned source
hash, the working source bytes, and the imported module's identity. Recording
requires Python 3.12 or later for the standard-library archive data filter;
fixture replay also runs on Python 3.9. From the repository root:

```sh
python3.12 -B test_support/windows_collect_golden/record.py --output reproduced.json
cmp test_support/windows_collect_golden/cases.json reproduced.json
```

The generated file must compare byte for byte with `cases.json`. To record a new
input, add a uniquely named case with an empty `expected` object, then regenerate.
`--revision` accepts another revision only when its collector source bytes match
the recorded hash and the working source. No expectation is authored manually.

`process_foreign_owner_known` keeps ownership unknown and polling disabled when
the owner is foreign even though `ownerKnown` is true. `process_truthy_list`
records the escaping `AttributeError` together with ownership and polling events
that occur before it; comparing the exception alone would miss an earlier error.

The replay adapter bypasses collector construction, supplies lane/process and
probe observations, and forbids sockets, subprocesses, filesystem reads, thread
starts and native probes. Pure projections still execute with ordered spies.
Timestamp conversion uses a deterministic UTC epoch adapter and explicit fixture
errors; the function still performs datetime formatting. This avoids depending
on platform timestamp limits or version-specific conversion error wording.

Source tracing reached all 203 executable lines and 225 reachable source branch
edges. Raw opcode coverage was 231/238: six builtin-dispatch fallback edges
require replacing `all` or `any`, and a normal lock cannot suppress an exception
from its body. The selected mutation checks killed 74 behavior changes, including
both ownership counterexamples; three equivalent controls survived.

The adapter does not record lock entry/exit or owner-dictionary identity. Removing
the lock or retaining the process dictionary by reference remains unobserved.
Replacing `lane.get("ageSeconds")` with indexing is equivalent here because the
same key is already indexed earlier. These tests do not establish synchronization
or alias isolation.

Native Windows ctypes/NT ABI, registry and process APIs, TCP ownership readings,
PDH/DXGI/GPU readings, notification APIs, actual timestamp conversion,
subprocess behavior, sharing semantics and scheduling remain NOT_RUN here.
Fixture replay does not establish native or device acceptance.
