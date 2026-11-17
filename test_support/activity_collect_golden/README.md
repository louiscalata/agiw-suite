# Activity collector fold goldens

These fixtures pin the projected-row behavior of `activity.collect_activity`.
They record the complete collector result or escaping exception type and
message, ordered calls through module globals, and post-call input state.
Expected values come from executing the unchanged function.

The corpus contains 261 cases: 138 history cases and 123 active cases. They
cover empty inputs, explicit clocks, explicit `None`, an omitted `now`
argument, the display cap, history age limits, evidence expiry, equal sort
times, incomplete listings, archive matching, mixed liveness, source priority,
merge mutations, trace keys, malformed values, and caught or escaping errors.

## Scope and isolation

The input rows are already projected records. The fixtures replace receipt and
archive enumeration, receipt projection, archive reads/projection, and active
observation using the same module-global patch seams as the existing activity
tests. History source metadata, bounded history selection, history bounds, and
current-run evidence validation remain real. The existing `test_activity`
module covers raw-record parsing and projection. These goldens do not establish
filesystem security or device acceptance.

The `fixture/` paths are inert relative values. File opens, directory/stat
calls, sockets, and subprocess creation are guarded during collection. The
clock is patched. No live service or device observation supplies an expected
value. Nonfinite inputs use a `{"$float": "nan"}` (or `inf`/`-inf`) marker so
the corpus stays strict JSON. `inputsAfter` preserves intentional input
mutations; it does not assume the collector is pure.

A case containing `now` passes its value explicitly, including `None`. A case
without that key omits the argument and exercises the signature default.

## Provenance and running the tests

- Baseline revision: `f29290f0f5bcb771739b6238828d628471c417e7`.
- `activity.py` SHA-256:
  `9b49403a676a325a93f242dbe34a1353b2a952308ea430088ccaab867062cbec`.
- Corpus SHA-256:
  `de2e00c2db714cbeab5705478f217df5e2c4d8c3090972d0b9ada406a62076ee`.

The main expectations were recorded from the pinned source under Python 3.15.
The two `expected_before_py314` variants were recorded from the same source
under Python 3.9. They apply to supported Python versions before 3.14, whose
unhashable-list exception messages differ from Python 3.14 and later. Complete
results, events, and input snapshots are compared in either branch.

From the repository root, run:

```sh
python3 -B -m unittest \
  test_activity_collect_golden test_activity_collect_active_golden -q
```

These focused tests do not run the full suites, native checks, or hosted CI.
