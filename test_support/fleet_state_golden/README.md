# Fleet-state golden corpus

This corpus captures `build_fleet_state` at revision
`5694d16e8d77e8a0dddd5cf597ac1a91f2db1784`. The source revision and SHA-256
pins are stored in `cases.json` and checked when the test modules load.
Those pins identify the capture; they allow a later implementation to change
its source bytes while preserving observable behavior.

Each expectation comes from running the pinned production files. The two
test modules compare complete returned projections or escaping exceptions,
ordered calls through real helpers, inputs after execution and shared mutable
input/output paths. Canonical JSON distinguishes Booleans, integers and
floats. Reserved `$float` and `$tuple` envelopes represent nonfinite numbers
and tuples without nonstandard JSON tokens. The clock is fixed; helpers remain
real pass-through calls. Cases perform no device, network or service I/O.

Exception types always remain part of the contract. Exact messages remain
part of the contract when the innermost traceback instruction is an explicit
Python `raise` (`RAISE_VARARGS`) in `fleet_state` itself (`test_support/exception_outcome.py`). Interpreter-generated diagnostics from failed
operations or builtin calls are compared by type because their wording varies
across Python versions and platforms. The normalization tests cover explicit
messages, builtin errors, overflow and bare/named reraises.

Cases cover finite-clock and freshness boundaries, expiry equality, count
types, attribution, legacy replacement ambiguity, duplicate ordering, lane
filtering, inventory contradictions, registry coverage, ordered summaries,
rounding, hostile strings, odd containers and escaping exceptions. Boundary
inputs distinguish report IDs from hosts, missing source fields from unequal
ones, local from peer inventories, and endpoint answers from host evidence.
Long values retain the function's existing behavior without adding a limit.

The malformed `unhashable_parent_lane` case shares its list-valued parent
between the input and returned `parentId`. The recorded shared path preserves
that existing behavior for a separate source change to consider.

`generate.py` exports the pinned production blobs with Git into a private
temporary directory and records expectations using the test adapter. From
the repository root, with an existing private `TMPDIR`, regenerate to a
separate output and compare it with the corpus:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B test_support/fleet_state_golden/generate.py \
  --repo . --output "$TMPDIR/fleet-cases.json"
cmp test_support/fleet_state_golden/cases.json "$TMPDIR/fleet-cases.json"
python3 -B -m unittest test_fleet_state_golden test_fleet_state_golden_protocol -q
```

`mutations.json` records concrete AST changes and the corresponding boundary
case names. `mutation.py` requires the recorded source hash, confirms that the
original witnesses pass, and executes the real golden/protocol unittest pair
against each mutation in memory. This checks the specified regressions; it is
not an exhaustive mutation score. `differential.py` compares isolated pinned
and current modules on the corpus and seeded inputs, including direct calls
against calls observed by the adapter:

```sh
python3 -B test_support/fleet_state_golden/mutation.py \
  --repo . --output "$TMPDIR/fleet-mutations.json"
python3 -B test_support/fleet_state_golden/differential.py \
  --repo . --generator test_support/fleet_state_golden/generate.py \
  --output "$TMPDIR/fleet-differential.json"
```
