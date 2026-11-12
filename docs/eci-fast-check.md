# Model fast check

The fast check screens daily model candidates on a fixed sample before spending
6–12+ hours on a full benchmark. It is a local, offline source feature with no
model integration in this slice. All real fast checks and full runs are
**NOT_RUN**. No model is selected for live routing from a fast-check result.

The owner's division of responsibility is preserved: pre decides whether a
model fits the hardware and is worth checking; Nisi supplies the run-journal
reference; post decides whether the measurements are admissible. Here pre and
post are deterministic local functions. Optional advisory objects are data,
never requests to Jev or another model. Lane execution belongs to the future
harness adapter; no lane is inferred or started here.

## Freeze one plan for every candidate

`eci_fast_check.make_plan` accepts benchmark descriptors containing `items`, an
optional `revision`, optional `chance`, optional `answer_options` and optional
`stratum_key`. `answer_options` is an integer of at least two for a choice
benchmark, or null for an unspecified answer space. It is inferred from supplied
choice lists, known ARC/Winogrande metadata, or at least two options for a nonzero
chance baseline. It is frozen in the plan; choice benchmarks cannot hide it with
null. A chance-zero benchmark with at least two possible answers must declare it.
Every item
has a unique string `id`. The adapter owns scoring; keep the reference `answer`,
question, choices and all scorer inputs in the item so the
content hash pins them too. Example input:

```json
{"arc":{"revision":null,"items":[
  {"id":"example-1","question":"Synthetic example","answer":"A","subject":"example"},
  {"id":"example-2","question":"Synthetic example","answer":"B","subject":"example"}
]}}
```

The default is 200 items per benchmark, capped at the available population.
The seed is SHA-256 of the benchmark ID and plan version. Item priorities are
SHA-256 of that seed and item ID, so selection is stable across input ordering,
Python versions and model identity. Use the same saved plan for all candidates.

Selection is simple random sampling without replacement from the whole
benchmark, represented by a deterministic SHA-256 priority order. Subject strata
are detected first, then task strata; an explicit `stratum_key` overrides detection.
Missing labels form a recorded null stratum. Strata population and selected counts
are information only: they never change selection, and a stratum may have zero
selected items. There is no forced minimum per stratum. If the requested count is
at least the dataset size, the plan uses the whole dataset and explicitly records
that fact. The plan records population, selected IDs, seed, benchmark, revision
(null if unknown), version and a hash of the full canonical dataset. The plan ID
hashes the entire plan, including count and wall budget.

```sh
python3 -B -m eci_fast_check plan datasets.json --count 200 \
  --version fast-check-plan-v1 --wall-budget-s 1800 --output plan.json
```

This reads only the named JSON and creates a new file exclusively. It refuses
to replace an existing plan. The runner reconstructs the plan from the supplied
dataset before any answer call, rejecting changed content, strata or selection.
No datasets are downloaded or discovered.

## Statistics and interpretation

Primary accuracy is correct / **all planned items**, including items not yet
attempted. Truncated, unparsed and error outcomes are not correct; none is dropped
from the primary denominator or normalized score. Every benchmark reports counts
and rates for `correct`, `wrong`, `truncated_empty`, `unparsed` and `error`, with
rates over the planned item count. Attempted and parsed coverage remain explicit.
Accuracy over parsed items is a separate, clearly labelled diagnostic; it does not
replace the primary score. With no parsed items that diagnostic is null.

Each benchmark has a two-sided Wilson 95% interval with
`z = 1.959963984540054`. At zero correct its lower endpoint is exactly zero; at
all correct its upper endpoint is exactly one. The calculation works for one
item and does not use a zero-width normal approximation at the extremes.

Normalization matches the existing admission rule at
the benchmark runner's `admit.py` (lines 47–49 at the time of writing):

```text
normalized = min(1, max(0, (raw - chance) / (1 - chance)))
```

Decimal conversion from the decimal string matches that rule, including its
inclusive boundaries. The same monotone transform is applied to both Wilson
endpoints. The admission file declares ARC chance 0.25, GSM8K 0 and Winogrande
0.5 at line 28; those values are fixed here. Other benchmarks must explicitly
supply chance or retain null normalized scores and remain unranked. Fast checks
do not apply the full-run informative-band filter or fit an ECI posterior.

Against a verified baseline, complete expected scored item IDs form four counts: both correct, both
wrong, candidate only correct (`b`), and baseline only correct (`c`). The
two-sided exact McNemar probability is:

```text
p = min(1, 2 * sum(comb(b+c, k), k=0..min(b,c)) / 2**(b+c))
```

A conclusive verdict requires both sides to contain the complete expected scored
item set from the same frozen plan. Otherwise the verdict is INCONCLUSIVE with
reason `incomplete pairs` and the expected, candidate, baseline and shared counts.
A complete comparison is BETTER when `b > c` and `p < alpha`, WORSE for the
reverse, and INCONCLUSIVE otherwise. Equality to alpha is not significant. Default
alpha is the exact decimal string `"0.05"`. With no discordant pairs p is 1.
The exact reduced numerator and denominator are retained as decimal strings;
verdicts compare rational numbers, so floating underflow in the display p value
does not change the decision.

The candidate binds its comparison to the full validated baseline record, its
record ID and a SHA-256 of the baseline per-item outcomes used. The full baseline
must have the same plan ID and item set, hardware and complete runtime settings.
Validation and ingest revalidate that evidence and recompute the binding and
comparison. An unresolved or unverifiable baseline comparison is UNVERIFIED and
INCONCLUSIVE; its claimed ID alone is insufficient. This is supplied evidence,
not runtime attestation.

## Local pre, journal and post

Pre records file size, quantization, supplied available memory and safety margin.
Default safety margin is the exact decimal string `"0.25"`: CHECK requires
`file_size_bytes <= available_memory_bytes * (1 - safety_margin)`.
The file size already reflects quantization; no invented quantization multiplier
is used. Missing size, quantization or available memory gives UNKNOWN. An
oversized file gives SKIP. Available memory is an input, not a system probe, and
cannot exceed a supplied hardware total. Fit is an estimate: KV cache, context,
backend allocations, concurrent load and actual residency still need verification.

`journal` is a Nisi journal ID or null. The seam does not create a journal or
claim that a null journal was reconciled. Model name, optional file SHA-256,
quantization, hardware identity and runtime settings remain in the result.

Post records configurable thresholds and alpha as exact decimal strings and
compares counts with rational arithmetic. Defaults require complete planned
coverage, a raw Wilson interval width strictly below `"0.20"`, error rate at most
`"0.01"`, cutoff-without-answer rate at most `"0.05"`, and unparsed rate at most
`"0.10"`. Unparsed rate is (attempted - parsed) / planned, including errors and
cutoffs without an extracted answer. The separate complete-coverage gate accounts
for unattempted items. Rate equality passes; Wilson width equality rejects.

Above the unparsed bound, post REJECTs with
`runtime/model incompatibility suspected: answers could not be parsed`.
Zero parsed answers also independently REJECTs with
`runtime/model incompatibility suspected`, including on chance-zero benchmarks.
Excessive cutoff-without-answer rate REJECTs with
`answers cut off at the token limit: raise the limit and re-run`.

For nonzero chance, post tests H0: p <= chance with the one-sided exact binomial
upper-tail probability over all planned items. It rejects H0 when p-value <= the
record's alpha (default `"0.05"`), using exact fractions throughout. There is no
fixed chance margin. A result that is not significantly above chance is:

- REJECT with `runtime/model incompatibility suspected` if unparsed rate exceeds
  its bound or the answer distribution is degenerate: one normalized answer value
  accounts for at least 90% of parsed answers on a benchmark with at least two
  answer options.
- Otherwise UNKNOWN with `not distinguishable from chance on this sample: weak
  model or incompatible runtime; re-run with a larger sample or check the runtime`.
  It is never admitted or ranked.

Chance-zero benchmarks skip the chance test and use parseability, degeneracy and
the other gates. Degeneracy rejects independently there when at least two answer
options are declared. On a nonzero-chance benchmark, degeneracy only corroborates
a not-above-chance result: 200/200 correct at chance 0.9 is admitted when the other
gates pass. Unknown finish evidence stays UNKNOWN until resolved.

At n=200, chance 0.5 and alpha 0.05, the smallest significant correct count is
113; 112 or fewer is not distinguishable from chance. Assuming independent items,
true accuracy 0.55, complete parsed observations and a nondegenerate answer
distribution, the probability of UNKNOWN is
`sum(comb(200,k) * .55**k * .45**(200-k), k=0..112)` =
**0.6380034604943883 (63.80034604943883%)**. Accuracy alone produces zero false
incompatibility labels by construction: all those nonsignificant samples are
UNKNOWN. This is a binomial planning assumption, not a measured failure rate on
real models or a finite-population power claim. Use larger independent samples
when screening models close to chance.

Thresholds remain configurable for local inspection. A policy looser than any of
the standard defaults above yields at most UNKNOWN, even if its own gates pass.
It cannot rank the reviewer's 190-error/10-correct record. Stricter policies can
admit, but rankings group by the exact admission policy as well as hardware,
plan, settings and alpha, preventing comparisons across different policies.
Wilson width decisions use rational centre and squared half-width calculations;
displayed intervals remain floating-point values. Throughput never affects
admission or rank. These are local statistical gates, not runtime attestation.

Advisories have exactly `{"decision": ..., "reasons": [...]}`. Pre decisions
can tighten CHECK → UNKNOWN → SKIP; post can tighten ADMIT → UNKNOWN → REJECT.
Both local and final decisions, all inputs and the advisory are retained.
An optimistic advisory never relaxes a local refusal or unknown decision.

## Callable runner seam and record contract

`run_check` invokes only the supplied adapter. New callbacks may accept
`answer(item, *, benchmark, spec)` and `score(prediction, item, *, benchmark,
spec)`; benchmark is the frozen benchmark key and spec is a defensive copy of its
plan entry. Either keyword may be accepted independently. Legacy one-argument
answer and two-argument score callbacks remain supported. Signature binding occurs
before invocation; an internal TypeError is never retried as another signature. It supplies each
item's explicit `outcome` (`correct`, `wrong`, `truncated_empty`, `unparsed` or
`error`), whether an answer was `parsed`, its short normalized `answer_value` (1–128
characters, without outer whitespace or control characters, or null when
unparsed), explicit boolean `cut_off`, raw `finish_reason` (or null), and available
`seconds`, `prompt_tokens` and `completion_tokens`. The adapter owns extraction
and scoring and identifies its scorer in runtime settings. Missing measurements
are null; outcome classification and the cutoff boolean must be explicit. A null
finish reason is retained and keeps admissibility UNKNOWN. Invalid or contradictory
adapter evidence raises ValidationError; it is not rewritten as a runtime failure.
Actual runtime exceptions remain error observations. No harness, server client,
process manager or model is imported. For example, a synthetic adapter result is:

```python
{"outcome": "correct", "parsed": True, "answer_value": "A",
 "cut_off": False, "finish_reason": "stop",
 "prompt_tokens": 8, "completion_tokens": 4, "seconds": 2, "error": None}
```

The existing harness's cutoff signals are `length`, `max_tokens`, `model_length`
and any non-null `sample.limit`, regardless of finish reason. Adapters must OR
these signals into `cut_off`; `token_limit`, `sample.limit` and `sample_limit`
finish labels are also recognized. Preserve the raw finish reason: a sample-limit
cutoff can legitimately have `finish_reason="stop"`. A recognized length reason
with `cut_off=False` is inconsistent and rejected. Every `cut_off=True` item with
no parsed answer is canonicalized to `truncated_empty`, whatever class the adapter
claimed. A cutoff item with a parsed answer retains its scored class and is not
counted as an empty truncation. All no-answer cutoff items count toward the rate.
Per benchmark, `answer_counts`, `top_answer_count` and `top_answer_share` are
recomputed from item values, so a supplied aggregate cannot hide degeneracy.

The seam derives `correct` from the outcome and adds the frozen `item_id`. Optional
`prediction` supports an explicit `score` checker; disagreement is a validation error. Exceptions may expose measured `seconds`,
`prompt_tokens` and `completion_tokens` attributes. If seconds are absent on an
exception, the seam records elapsed call time from its supplied monotonic clock.

The optional plan budget is an exact positive decimal string of seconds, global
across benchmarks in the current invocation and checked between calls. Each resume
gets a fresh invocation budget; the frozen plan records that budget unchanged.
It never starts another item after expiry. An in-flight call may finish after
the deadline; its outcome and actual wall time are retained. A strict wall bound
therefore requires the adapter's own per-call deadline, ideally capped by the
remaining budget. The seam handles a raised TimeoutError without leaving a
thread or subprocess. It cannot interrupt an arbitrary hung Python callable.
The optional checkpoint is an append-only JSONL file with fsynced per-item
observations. Its identity binds the plan, model, hardware and runtime settings;
resume validates every observation against the frozen execution prefix before
calling the adapter. A restarted run reuses persisted items instead of repeating
them. A different plan or identity refuses to reuse the checkpoint. An incomplete
or corrupt checkpoint cannot silently mix observations. Cancellation of an
in-flight arbitrary callable remains adapter-owned. Supply `checkpoint=Path(...)`
to enable persistence; absent that argument the seam writes no checkpoint. An exclusive nonblocking OS lock owns the read/append lifetime; a second writer
is refused before adapter execution. Resume of any nonempty checkpoint requires
a stable model file SHA-256. A fresh unhashed run may write evidence but cannot
resume it. Each row has a chained SHA-256 over its binding, predecessor and
observation, so valid-looking accidental edits are refused. This detects damage,
not intentional rewriting with recomputed hashes. The Windows lock branch still
needs native Windows execution. Only successfully persisted observations are
reused; recovery of an interrupted final JSONL line is an explicit custody step,
and the seam refuses that torn file instead of silently dropping evidence.

One JSON record contains a single model/hardware/plan check and all its benchmark
summaries and item outcomes. It records attempted, parsed and planned counts,
outcome counts/rates, errors/timeouts, raw and normalized intervals, per-item
prompt/completion tokens and seconds, aggregate completion tokens, answer seconds,
token throughput, current-invocation wall time and stop reason.
`wall_time_s` measures only the adapter-execution phase of the current invocation;
it does not invent prior adapter overhead or elapsed downtime on resume. Per-item
answer seconds and their aggregate retain measured values from checkpointed calls. Missing measurements remain
null; partial totals never masquerade as complete throughput. If an exception
occurs after measurements become available, known tokens/seconds are retained.
Only exception class names are retained, avoiding prompts in exception text.

Hardware identity is a validated free-form JSON object requiring
`machine_label`, `cpu`, `gpu`, `memory` and `runtime_build_id`. The machine label
and runtime build ID are nonempty strings or null; CPU, GPU and memory are nonempty text, nonempty JSON objects, or null. Optional `memory_bytes` supplies a positive
integer total for the fit sanity check. Additional JSON fields are preserved.
All five required fields must be known for ranking; a machine without a GPU can
record `{"present": false}` instead of claiming an unknown GPU. Exact complete
hardware objects participate in grouping, so
three-machine results remain distinct. Committed examples and fixtures use
placeholders; they contain no real machine names or addresses. A synthetic
hardware descriptor is:

```json
{"machine_label":"fixture-machine-a","cpu":"fixture-cpu",
 "gpu":{"label":"fixture-gpu"},"memory":{"bytes":1000},
 "memory_bytes":1000,"runtime_build_id":"fixture-build"}
```

`eci_fast_check.schema.json` publishes the contract. Python validation adds
finite-number, boolean/integer, hash, identity, coverage, timing and recomputed
score/test/decision checks. Extra or missing record fields are rejected; unknown
optional evidence uses explicit null. Content hashes detect changes, not fraud.
No schema or local gate proves the file was served or a model actually ran.
The schema describes representation; Python validation additionally recomputes
hashes, statistics, exact threshold decisions, completeness and baseline binding.

## Offline ingest and best-model view

```sh
python3 -B -m eci_fast_check validate candidate.fast.json
python3 -B -m eci_tuning ingest fast-check-collection.json --output tuning.fast.json
python3 -B -m eci_tuning validate tuning.fast.json
python3 -B -m eci_tuning rank tuning.fast.json
python3 -B -m eci_tuning status tuning.fast.json
```

Normalized dataset v1 gains optional `fast_checks`. This dedicated root
collection is the only accepted ingest location for fast-check records:
`{"schema_version":1,"runs":[],"items":[],"fast_checks":[...records...]}`.
Standalone fast records and lists of them are rejected. Before dropping or
normalizing fields, ingest recursively validates provenance on envelopes, runs,
items, components, metadata and every alias. Fast-check markers anywhere outside
the dedicated collection (including arbitrary strings and mapping keys) and
conflicting protocol aliases or parent/child fields reject the entire input.
Direct ranking, recommendation and policy apply enforce the same barrier. Fast checks use protocol
`agiw-fast-check-v1` and remain outside ECI runs, components and recommendations.
The Mac and Windows endpoints can read an explicitly configured, validated local
`dataset.json` and show `rankings.fast_check.hardware_views` separately from full
ECI. See [dataset configuration](eci-model-tuning.md#rankings-and-uncertainty).
Without configuration the endpoints use the shipped catalog, which contains no
actual fast-check results. Named-file ingest does not change policy or run a check.

Views group by exact hardware identity, plan ID, runtime settings, alpha and admission policy.
Required known comparison settings are `scorer_id`, `prompt_template_id`,
`backend`, `backend_version`, `temperature`, `max_tokens`, `ctx`, `seed`, `stop`,
`system_message`, `n_epochs`. Record explicit empty arrays/messages when known;
null or missing settings keep a record pending. Additional runtime settings
are retained and also participate in grouping. Unknown hardware identity, chance, or a fit/admission refusal cannot produce a best
model. Missing throughput measurements remain null and do not affect admission or
rank.
Relaxed policies and incomplete samples stay pending; competitive ranking requires
the entire plan and a policy within the standard safety bounds.

The view says “best model for this hardware as of” the latest supplied record
timestamp in its group. It describes only those supplied records. Rows are
labelled **fast check**, ordered by equal-weight mean normalized component
score, and carry per-component intervals. Displayed aggregate bounds are means
of the component Wilson endpoints, **not a joint 95% confidence interval**.

Every pair is recomputed from complete expected scored item sets; incomplete pairs remain INCONCLUSIVE. Across benchmarks, a directional
screening verdict requires at least one significant benchmark in that direction
and none significantly in the other direction; conflicting directions are
INCONCLUSIVE. This is a screening rule, not a pooled hypothesis test. Exact
per-benchmark counts/tests remain in output. INCONCLUSIVE pairs are explicitly
ties; ties need not be transitive and score order is not evidence of superiority.
An undominated candidate is flagged as a finalist for a full run, with full run
NOT_RUN. A sole candidate is provisional, INCONCLUSIVE, and a finalist.

Wilson intervals describe binomial screening uncertainty on the whole-benchmark
simple random sample. Strata counts are descriptive and do not reweight the
unweighted estimator. Repeated adaptive reuse and model comparisons remain
unadjusted for multiplicity. Do not tune against these IDs or treat a tie as
equivalence. Finalists need independent full runs and full admission before
changing routing policy or claiming an ECI improvement.

## Existing harness handoff: NOT_RUN

Both inspected `run_mac_suite.py` versions use `--limit` for first-N smoke runs;
the sharded draft adds BBH campaign scheduling, not fast-check sample selection.
Neither accepts this plan. Running `--limit 200` would not execute a valid fast
check. A real fast-check command does not exist in the current harness.

The next PR must add `mac_fast_check_adapter.py` in this source tree, exposing
`bind_answer(model, settings, timeout_s)` and `score` using the existing harness's request, extraction,
truncation and timeout rules, without starting a server implicitly. It must
consume the frozen IDs and write to new task-owned outputs, never reuse the
currently running benchmark's files. Before that, the execution owner must
provide reviewed `fast-check-input/datasets.json`, `hardware.json`, shared
`settings.json`, and `baseline.model.json`, `baseline.pre.json`,
`candidate.model.json`, `candidate.pre.json`, then run the same plan for both
models. Those files must carry observed identity/settings rather than copied
defaults. Nisi journal custody and any model/server admission are separate gates.

The adapter can reuse the formats and logic in `harness_core.load_dataset`,
`run_benchmark.generation_settings`, `execute` and `collect_log`, but must not
blindly call their job-wide setup: `configure_runtime` and the existing suite
write to their own runtime/results directories. Adapting these routines to an
item-level callable, isolated output custody and the new protocol is remaining
work, not an existing supported harness mode.

The exact later sequence below is **unrun**, conditional on that adapter and
the owner's approved input files. Its explicit file check currently fails;
the adapter is deliberately NOT_IMPLEMENTED in this slice. `bind_answer` returns
the callable bound to the already admitted endpoint and exact supplied model,
and raises TimeoutError at its request deadline. It must refuse identity or
settings mismatches. Use fresh output filenames; no model is started implicitly.

```sh
cd <agiw-suite checkout>
set -eu
test -f mac_fast_check_adapter.py || exit 2
python3 -B -m eci_fast_check plan fast-check-input/datasets.json --count 200 \
  --version fast-check-plan-v1 --wall-budget-s 1800 --output fast-check-input/plan.json
for fast_check_name in baseline candidate; do
FAST_CHECK_NAME="$fast_check_name" python3 -B - <<'PY'
import json
import os
from pathlib import Path
from eci_fast_check import load, run_check
from mac_fast_check_adapter import bind_answer, score  # next PR; absent today
p = Path('fast-check-input')
name = os.environ['FAST_CHECK_NAME']
model = load(p / (name + '.model.json'))
settings = load(p / 'settings.json')
answer = bind_answer(model, settings, timeout_s=60)
baseline = load(p / 'baseline.fast.json') if name == 'candidate' else None
record = run_check(load(p / 'plan.json'), load(p / 'datasets.json'), answer,
    score=score, model=model, hardware=load(p / 'hardware.json'),
    runtime_settings=settings, pre=load(p / (name + '.pre.json')),
    journal=None, baseline=baseline,
    checkpoint=p / (name + '.checkpoint.jsonl'))
with (p / (name + '.fast.json')).open('x', encoding='utf-8') as stream:
    stream.write(json.dumps(record, indent=2, allow_nan=False) + '\n')
PY
done
python3 -B -m eci_fast_check validate fast-check-input/baseline.fast.json
python3 -B -m eci_fast_check validate fast-check-input/candidate.fast.json
python3 -B - <<'PYCOLLECTION'
import json
from pathlib import Path
p = Path('fast-check-input')
data = dict(schema_version=1, runs=[], items=[], fast_checks=[
    json.loads((p / (name + '.fast.json')).read_text())
    for name in ('baseline', 'candidate')])
with (p / 'fast-check-collection.json').open('x') as stream:
    json.dump(data, stream, indent=2, allow_nan=False)
PYCOLLECTION
python3 -B -m eci_tuning ingest fast-check-input/fast-check-collection.json \
  --output fast-check-input/tuning.fast.json
python3 -B -m eci_tuning rank fast-check-input/tuning.fast.json
```

This uses the same seam and frozen plan for baseline and candidate. Reuse the
plan without invoking plan creation again (creation is exclusive). Executing
both iterations requires each model to have a separately approved endpoint;
that admission is NOT_RUN. A missing baseline may produce a first record with
`baseline=None`, but cannot produce a favorable paired verdict. No commands
above were used to run real inference during this implementation.

## Verification

Tests are root `test_*.py` unittest files and `test_*.mjs` Node tests, discovered
by `.github/workflows/source-checks.yml` (Python 3.9/3.14, Node 22). No CI change
or new dependency is needed. The focused source checks are:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -p 'test_eci*.py' -v
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest test_package_resources -v
node --test test_eci_tuning_ui.mjs
node --check web/eci-tuning.mjs
bash -n build.sh package-release.sh
# Minimum supported interpreter available on this Mac:
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -B -m unittest test_eci_fast_check test_eci_tuning test_package_resources -v
```

The fix-round report records the current focused Python, touched Windows-edition
and Node results, interpreter versions, skip counts and red/green regressions.
Historical counts do not verify this revision. These are source checks; the
hosted CI matrix, native Windows installation, Mac packaging and wrapper build
remain NOT_RUN.

Tests use only synthetic data, fake callables and fake clocks, including error,
timeout and budget paths. Browser rendering, hardware/native acceptance, real
models, served identity, actual Nisi journals and hosted advisories are NOT_RUN.
