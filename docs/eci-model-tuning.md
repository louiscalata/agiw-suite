# AGIW x ECI Model Tuning Tool

Use recorded ECI evidence to inspect model results and explicit comparison barriers.
Recommendations remain NOT_COMPUTED without a target profile and validated selection rule. The tool runs offline from a source checkout with Python 3.9+ and
the standard library. The Overview's **Model tuning** link opens a read-only page
at `/eci-tuning`. This source feature is not in the signed 1.0.0 download.

## Recorded evidence · 10 November 2026

The bundled catalog records one computed baseline local ECI of **105.38**, with
**90% CI 94.31–115.14**. This dated observation does not establish an overall
winner or a route for a target machine. The bundled policy has null models and
status NOT_COMPUTED for general, reasoning and coding. Task-specific routing
outcomes are NOT_MEASURED; benchmark scoring is not a deployed routing test.

The baseline is a local partial four-component scoring protocol. ARC and
Winogrande use generated choices; Winogrande uses labeled XL validation because
test labels are unavailable. GSM8K uses a capped 1024-token rerun; its twelve
truncated completions were scored wrong. GPQA diamond is NOT_RUN because of the
dataset gate. “Epoch-comparable” identifies this baseline class and does not
claim exact official harness parity or completely untuned generation.

The same completions with pipeline-configured lenient extraction have ECI
**108.59 [98.29, 118.43]**. This is a separate, non-comparable scoring view and
cannot establish a default route. Its 90% CI overlaps the baseline; no clean
improvement is established. Published Epoch **111.24 [105.25, 116.30]** belongs to
a different artifact and component set. That figure is reference context only;
it is not imported as this local artifact's score.

**Bonsai Q2_0** and **Mac Muse Glimmer Q4_K_M** remain NOT_COMPUTED.
**LFM3 1.7B** remains NOT_RUN in the snapshot after the earlier backend reported
an unsupported architecture; a newer-backend run was pending. These labels are
dated score evidence, not current process or installed-model observations.
Glimmer's informal/withheld inspect accuracy is not admitted as a component score.

## Source checkout commands

Run these from the repository root. None calls a model, starts a server, contacts
a worker, downloads weights, or loads/unloads a model.

```sh
python3 -B -m eci_tuning validate
python3 -B -m eci_tuning rank
python3 -B -m eci_tuning recommend
python3 -B -m eci_tuning status

python3 -B -m eci_tuning ingest results.json --manifest run_manifest.json \
  --comparability baseline_protocol --output normalized.json
python3 -B -m eci_tuning validate items.jsonl runs.csv
python3 -B -m eci_tuning rank normalized.json

# With no validated recommendation, this reports policy unchanged and exits 2.
python3 -B -m eci_tuning apply --config /tmp/eci-policy.json
```

The named inputs default to the small reviewed `eci_tuning_catalog.json` snapshot.
Ingest reads only named files, with no recursive discovery of inspect logs or
draws. Extra-score lists alone provide component evidence, never computed ECI.
Unknown protocols stay `unclassified` unless metadata or an explicit
`--comparability` assertion supplies a class. Lenient scoring cannot be asserted
as baseline. Manifest/result statuses and diagnostics must agree, and an explicit
manifest CI level must be 0.9. Validation failure exits 2 without echoing payloads.

## Schema and analysis compatibility

[`eci_tuning.schema.json`](../eci_tuning.schema.json) defines normalized dataset
version **1**: `schema_version`, `runs`, and `items`. The Python validator also
checks finite numbers, score ranges, ordered posterior bounds, scorer names,
duplicate/conflicting identities, and item/run setting and protocol conflicts.
Unknown input columns are omitted by an allowlist; they are not echoed into
normalized output. Keep inputs and exports free of secrets and private runtime
fingerprints. Ingest is data normalization, not permission to publish its output.

| Record | Required identity and retained evidence |
| --- | --- |
| Run | `run_id`, `model`; normalized `status` and `comparability_class`; `quant`, `backend`, `backend_version`, `machine`; `protocol_id`, `settings`, `components`, `diagnostics`; aggregate score, n, stderr, ECI posterior mean/p05/p95 when computed. `source_status`, `partial`, `admitted_score` and `eci_associations` preserve admission boundaries. |
| Item | `run_id`, `model`, `benchmark`, `item_id`, `scorer_id`; optional `inference_id`, `split`, `epoch`; settings, gold, parsed_answer, correct, finish_reason, truncated, tokens_in/out, latency_s, tokens_per_s and memory fields. |
| Settings | `temperature`, `max_tokens`, `ctx`, `n_gpu_layers`, `reasoning_budget`, `stop`, `system_message`, `n_epochs`, `prompt_template_id`, `scorer_id`. Unknown fields remain null. |
| Measurements | `cost_usd`, `latency_s`, `tokens_per_s`, `memory_bytes`, `peak_memory_bytes`, `gpu_memory_bytes`; null when unknown. No file-size-to-memory or missing-to-zero conversion. |

Item keys are `(run_id, benchmark, scorer_id, item_id, epoch)`. Separate attempts,
settings or scoring views require distinct run IDs. Scorers are `strict_choice`,
`lenient_v1`, and `gsm_numeric_em`. Correctness accepts booleans and analysis
binary integers 0/1, retaining null for unscored items. Items alone do not admit
aggregate scores or compute ECI; use an independently admitted runs/refit record.
An empty JSONL system message stays explicitly empty. Empty CSV cells mean null,
so use JSONL to preserve that distinction.

Adapters accept:

- Current refit `results.json` wrappers (`results` list), including raw/normalized
  components, posterior mean/p05/p95 and diagnostics. An associated
  sibling `run_manifest.json` is accepted with `--manifest` for exactly one
  refit input/model; mismatching run/model identities are rejected when supplied.
  A named standalone manifest
  validates its metadata but contributes no model rows. Refit run IDs default to
  the containing directory plus model ID, preserving distinct refit attempts.
- `extra_scores` JSON lists using `model_id`, `benchmark`, `raw_score`, status,
  n/stderr and host. Only components marked OK are admitted for ranking.
- The parallel analysis job's `data/items.jsonl` and `data/runs.csv`, aligned with
  its `schema.md` read on 10 November 2026. Its `configuration_label` maps baseline
  and pipeline-configured classes; `mem_bytes`/`gpu_mem_bytes` map to memory
  fields. Execution OK/PARTIAL becomes ECI NOT_COMPUTED while source status is
  retained. Failed, partial, NOT_RUN and withheld aggregates never enter
  competitive component rankings. `eci_associations` retain full-fit links;
  scalar analysis `eci` values do not become component-run ECI. Supply the actual
  refit files as well to rank those fits. Completion text, source paths and
  provenance payloads are deliberately outside this small app schema.

The analysis source directory remains read-only. The tool accepts its current
tables without depending on that job's Python environment. Small synthetic item
fixtures and redacted numerical refit fixtures live in `test_support/eci_tuning/`.

## Run a fast check against an existing server

The opt-in adapter queries an already running OpenAI-compatible HTTP server on
**127.0.0.1**, excluding `remote_machines.PROTECTED_PORTS`. It never starts, stops
or reconfigures a server. Supply local JSON files mapping benchmark names to
fast-check descriptors (`items`, optional `revision`/`chance`/strata). Items need
string `id`, `question`, canonical `answer`, and `choices` for choice questions:

```json
{"arc":{"items":[{"id":"example-1","question":"Which number is even?",
 "choices":["3","4","5","7"],"answer":"B"}]}}
```

```sh
python3 -B -m eci_fast_check_adapter \
  --base-url http://127.0.0.1:9000/v1 --model example-model \
  --samples arc-sample.json gsm8k-sample.json --output-dir ../fast-check-output
python3 -B -m eci_fast_check validate ../fast-check-output/result.json
```

Defaults are 200 items per benchmark, **16,384 completion tokens per item** and
180 seconds per request. Empty final content with `reasoning_content` is recorded
as `truncated_empty`, even with finish reason `stop`; reasoning is never scored.
Use `--max-tokens` (at least 8192, internally capped below context minus the prompt estimate), `--timeout` and `--count`
as needed. Set `--disable-thinking` per lane when its default includes thinking;
the adapter then sends `chat_template_kwargs` with `enable_thinking: false`.

Server facts come from `/props` or `/v1/models`; unknown hardware stays null.
Caller-supplied public hardware facts use `--machine-label`, `--cpu`, `--gpu`,
`--memory-bytes` and `--runtime-build-id`; server machine labels are ignored.
A path-shaped served model ID requires a safe `--model-label` for records.
Always declare the actual GGUF file size; reported tensor sizes remain separate
and a mismatch is unverified. If other artifact or fit inputs are missing, supply verified values
with `--model-sha256`, `--file-size-bytes`, `--quantization` and
`--available-memory-bytes`. Missing evidence refuses inference. One transient request retry is allowed; three consecutive exhausted item errors
stop the check. Every final item outcome is checkpointed with flush/fsync. Repeat the same command to resume; use a fresh
output directory to change settings. Resume re-asks trailing error rows as
append-only superseding entries and resets the consecutive-error count. Attempts
and retried-item counts stay visible; successful-attempt timing excludes failed retries. Exit 0 means a complete result was written; its post decision remains separate.
Incomplete results print their stop reasons and exit 3. Incomplete coverage stays
UNKNOWN. An excessive transport-error rate keeps overall quality UNKNOWN,
even when another completed benchmark has a quality rejection. Real inference and full
ECI are NOT_RUN for this implementation. See the
[adapter contract](eci-fast-check.md#loopback-harness-adapter) for formats,
metadata, deadlines, resume restrictions and exit codes.

## Rankings and uncertainty

The [fast check](eci-fast-check.md) adds a separate `fast_checks` collection to
dataset version 1 and a `fast_check.hardware_views` entry to ranking output.
Records use protocol `agiw-fast-check-v1` and the separate
[`eci_fast_check.schema.json`](../eci_fast_check.schema.json) contract. They never
enter `runs`, ECI/component rankings, policy recommendation or policy apply.
Ingest validates each fast-check record on its own. A record that fails
validation is left out and listed in `rejected_fast_checks` with its
`record_id` (null when it has no SHA-256 id) and the validator's reason; the
other records and the rest of the dataset stay usable. `validate` prints the
same list. A duplicate valid record still fails the whole ingest.
Set `AGIW_ECI_TUNING_DATASET_DIR` to an absolute local directory before starting
either observer. It reads only that directory's `dataset.json`, a normalized v1
dataset produced by `python3 -B -m eci_tuning ingest <input files> --output
<directory>/dataset.json`. Include full ECI inputs and fast-check collections in
the same ingest when both should appear. The observer caches validated data and computed views by file identity, size and
modification time. Transient `OSError` read failures are returned but not cached;
the next request retries even when the file fingerprint is unchanged. On a slow first read it returns unknown with a validation-in-progress
reason before the request deadline; the page retries a bounded number of reads and
then asks for a reload. Loading data never starts a model check. Oversized, linked
or non-regular dataset files are refused with a specific reason. The loader opens
and validates the same descriptor rather than reopening a previously checked path.
Directory aliases are allowed and resolved once; parent segments are refused.
Serving limits are 16 MiB input, 128 combined runs/checks, 2,048 unordered
comparison pairs, 64 KiB per full-run record, and 2 MiB for the derived view.
An exceeded limit reports unknown with a reason. The operator must choose local
storage; the observer does not verify whether a volume is network-backed.
Without configuration the bundled catalog is used. If configured input is
missing, unreadable or invalid, the page reports unknown with a reason and does
not substitute the catalog. Screening is shown separately from full ECI.

Recorded ECI and component rows retain input order; their placement is not a
ranking or winner claim. Every computed run pair has explicit comparability
reasons. The same pair appears in each class containing either run. Overlapping
or touching 90% posterior intervals are described as overlapping even when the
runs are not comparable; disjoint intervals alone do not prove a causal improvement.

To support a comparison, record `hardware_profile_id`, `dataset_artifact_id`,
`model_artifact_id` and `settings_id` alongside machine, backend/build, protocol,
quantization and complete settings. These are caller-supplied evidence identifiers,
not proof of runtime attestation. Hardware, dataset, protocol, backend and settings
must be known and match; model artifacts may differ, but the same model artifact
cannot claim different quantization. Shared components need admitted scores,
matching denominators, scorer and prompt template. Observed item evidence must
exist without truncation, unknown truncation or missing parsed answers, and both
runs must have equal sets of `(benchmark, item_id)`. Mismatches say Not comparable
with an explicit reason. `compare_runs(a, b)` retains its two-argument public
signature; ranking attaches item evidence derived from the normalized dataset.
Direct callers can attach `item_evidence` using the same `item_evidence(items)`
helper; missing item identity keeps the comparison unknown. Unknown
metadata never matches merely because both sides are null.

Cost and latency rows retain their run IDs. When no run metric is supplied, the
tool uses the mean of measured item values and exposes measured/observed counts.
Unknown measurements remain unknown. These are recorded observations, not matched
workload comparisons. No rates or costs are summed across strict/lenient views of
the same inference.

The source label keeps the evidence `as_of` date separate from `ingested_at`.
An ingestion timestamp cannot establish when a result was measured. Missing
snapshot dates are shown as unknown. Set the dataset environment variable in
the environment of the observer process. For a login-agent-started app, place
`AGIW_ECI_TUNING_DATASET_DIR` in that LaunchAgent's `EnvironmentVariables` and
restart it through the normal owner-managed procedure; setting it in an unrelated
terminal does not change an already running app. This feature does not edit or
reload the LaunchAgent.

## Policy application and integration boundary

`eci_tuning_routes.json` is the checked-in default. The user override is
`~/.config/agiw/eci-tuning.json`; `AGIW_ECI_TUNING_CONFIG` changes that path.
`apply --config PATH` selects an explicit target, but recommendation and apply
are currently withheld: no target profile and validated selection rule are
available. The CLI reports “No recommendation is available; policy unchanged”
and exits 2. It never derives a route solely by sorting recorded ECI means.

The page labels the bundled policy **Policy reference** and an override **Saved
policy**. A corrupt policy is shown as unreadable while valid dataset evidence
remains visible. A route whose source run is absent from the loaded dataset is
marked unknown with a reason. A saved policy is recorded configuration, not
proof that a runtime selected or served that model.

An AGIW integration can call `eci_tuning.select_route(task_type, machine, policy)`
for `general`, `reasoning`, or `coding`. It returns a valid saved PC route or null; the bundled policy never supplies a
route. An ineligible machine/task or unknown route status also returns null. No model ID is hardcoded in server/UI code.
The current execution router is separately installed and outside this repo;
**policy file applied** does not mean **external runtime activated**.
`runtime_activation` remains NOT_APPLIED until that owner integrates and tests the
hook. Existing machine admission and model availability checks still apply.

Nisi's Mac author/reviewer hook remains **NOT_APPLIED**, with null defaults.
It requires **two distinct LLMs already resident in Mac LM Studio**, as documented
in [advanced configuration](advanced-configuration.md#resident-model-budget).
The PC Llama artifact is never assigned to those roles.

Recorded evaluation at `/eci-tuning` is separate from Diagnostics and repair.
Opening diagnostics reads cached observations and starts no repair. Choose one
target, review its effects and duration, then confirm. Local runtime recovery may
start the Mac loopback server; Nisi recovery retains its idle-state checks;
route recovery may send one short Windows probe after its guards pass. Share
recovery remains manual.

`POST /api/eci-tuning/repair` and the deprecated `/api/inference/fix` alias accept
exactly `{"scope":"local","reviewed":true}`, with `local` replaceable by `route`
or `nisi`. They reject bundled scopes. Both retain native authorization and the
L2 gate. Windows denies repair through either route. Closing the panel does not
cancel an operation.

## Verification

```sh
python3 -B -m unittest test_eci_tuning test_package_resources test_server.TransportTest
node --test test_eci_tuning_ui.mjs test_inference_fix_ui.mjs test_tab_consistency.mjs
```

These are offline fixture, policy, loopback transport and Node module/source
checks. Browser rendering, native app acceptance, live routing, new model scores,
and deployed task quality remain NOT_RUN or NOT_MEASURED. No browser automation
is required on the development Mac.
