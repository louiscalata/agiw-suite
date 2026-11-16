# Mac presence golden fixtures

`cases.json` records 308 synthetic snapshots and their complete results or
exceptions from the revision in `generated_from`. Each observation includes
ordered helper calls and whether the snapshot remained unchanged. The replay
adapter rejects file access, socket creation, subprocesses and clock reads.

The two grouping counterexamples distinguish loaded API activity from an
unloaded CLI row and require API instance coverage to come from loaded CLI
instances. The first yields `idle`; the second yields `unknown` when the API
row lists only its own identifier.

Eight cases trigger interpreter-owned `TypeError` messages: `snapshot_string`,
`sampled_none`, `sampled_object`, `sampled_list`, `sources_unhashable_id`,
`api_coverage_unhashable`, `cli_coverage_unhashable`, and
`cli_instance_unhashable`. Their recorded messages describe the capture
interpreter. `expected_outcome` runs the same built-in operations on the current
interpreter to obtain its exact wording. It preserves the exception type,
ordered events and snapshot-preservation expectation. All other messages,
including application refusals and injected helper failures, remain literal.
There is no fallback to another Python minor version's wording.

Run commands from the repository root with bytecode writes disabled and a
private `TMPDIR` for each check:

```sh
python3 -B -m unittest -q test_mac_presence_golden
python3 -B -m test_support.mac_presence_golden.checks mutations
```

The mutation check changes each of the two grouping rules in memory and requires
the corresponding golden test to fail with an assertion. Its negative controls
also require every interpreter-message case to reject a changed message, error
type, event list or snapshot-preservation flag, and require application messages
to remain strict. Production files are never rewritten by these checks.

To recapture from the recorded source revision, export it into a temporary
directory and use the repository's capture command:

```sh
revision="$(python3 -c 'import json; print(json.load(open("test_support/mac_presence_golden/cases.json"))["generated_from"]["revision"])')"
baseline="$(mktemp -d)"
git archive "$revision" | tar -x -C "$baseline"
python3 -B -m test_support.mac_presence_golden.checks capture \
  --source-root "$baseline" --revision "$revision" \
  --output "$TMPDIR/cases.json"
```

Capture refuses a `peer_feed.py` hash that differs from the corpus provenance.
Use the recorded Python version for a byte-for-byte `cmp` against `cases.json`;
other versions can change the capture metadata and built-in error messages.
Capture refreshes expectations for the existing input definitions, so input
changes must be reviewed separately from the resulting observations.

With that same pristine source export, compare complete raw observations in
separate processes:

```sh
python3 -B -m test_support.mac_presence_golden.checks differential \
  --baseline-root "$baseline"
```

The differential includes every corpus input and 1000 deterministic inputs from
seed 71116. It compares results, exception types and messages, ordered helper
calls, and snapshot preservation without message adaptation. This complements
source-byte comparison; it does not establish device or service behavior.

Only the golden module, focused harnesses and selected fixture-based existing
classes are appropriate for these local checks. Full root, Windows, CLI and Node
suites, server-backed observer classes, native device checks and hosted CI are
separate checks and must be reported as `NOT_RUN` when they are not executed.
