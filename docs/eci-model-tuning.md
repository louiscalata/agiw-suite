# AGIW x ECI Model Tuning Tool

Use recorded ECI evidence to compare models and write a provisional, machine-aware
routing policy. The tool runs offline from a source checkout with Python 3.9+ and
the standard library. The Overview's **Model tuning** link opens a read-only page
at `/eci-tuning`. This source feature is not in the signed 1.0.0 download.

## Current decision · 10 November 2026

The repo policy selects **PC Llama-3.1-8B-Instruct Q4_K_M**
(`Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf`) for PC-eligible general, reasoning and
coding work: independent local ECI **105.38**, **90% CI 94.31–115.14**.
Confidence is **provisional / low-to-moderate**: it is the only computed
Epoch-comparable local ECI in this snapshot. There is no evidence of an overall
winner or task-specific coding superiority. Task-specific routing outcomes are
NOT_MEASURED; benchmark scoring is not a deployed routing test.

The baseline is a local partial four-component scoring protocol. ARC and
Winogrande use generated choices; Winogrande uses labeled XL validation because
test labels are unavailable. GSM8K uses a capped 1024-token rerun; its twelve
truncated completions were scored wrong. GPQA diamond is NOT_RUN because of the
dataset gate. “Epoch-comparable” identifies this baseline class and does not
claim exact official harness parity or completely untuned generation.

The same completions with pipeline-configured lenient extraction have ECI
**108.59 [98.29, 118.43]**. This is a separate, non-comparable scoring view and
cannot replace the baseline default. Its 90% CI overlaps the baseline; no clean
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

# Preview, then explicitly write. Omit --config for the user override path.
python3 -B -m eci_tuning apply --config /tmp/eci-policy.json
python3 -B -m eci_tuning apply --config /tmp/eci-policy.json --write
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
Use `--max-tokens` (minimum 8,192), `--timeout` and `--count` as needed.

Server facts come from `/props` or `/v1/models`; unknown hardware stays null.
If metadata omits the actual artifact hash or fit inputs, supply verified values
with `--model-sha256`, `--file-size-bytes`, `--quantization` and
`--available-memory-bytes`. Missing evidence refuses inference. Every attempt is
checkpointed with flush/fsync. Repeat the same command to resume; use a fresh
output directory to retry recorded errors or change settings. Exit 0 means a
result was written; its post decision remains separate. Real inference and full
ECI are NOT_RUN for this implementation. See the
[adapter contract](eci-fast-check.md#loopback-harness-adapter) for formats,
metadata, deadlines, resume restrictions and exit codes.

## Rankings and uncertainty

The [fast check](eci-fast-check.md) adds a separate `fast_checks` collection to
dataset version 1 and a `fast_check.hardware_views` entry to ranking output.
Records use protocol `agiw-fast-check-v1` and the separate
[`eci_fast_check.schema.json`](../eci_fast_check.schema.json) contract. They never
enter `runs`, ECI/component rankings, policy recommendation or policy apply.
Page integration is the next slice. Mac and Windows endpoints use the bundled catalog,
which contains no real fast-check measurements; an ingested `tuning.fast.json`
does not reach the page. This slice retains the pure view/ranking function and
renderer tests with supplied fixture data. Ingest reads explicitly named records;
it does not replace the catalog or start a check. A future page source needs an
explicitly configured, validated, read-only local dataset and an end-to-end data
path test before the page can claim to show ingested fast checks.

ECI is sorted descending **within** `baseline_protocol`, `pipeline_configured`,
`published_reference`, and `unclassified`. Overlapping or touching 90% posterior
intervals produce “no clean winner”; disjoint intervals alone do not prove a
causal improvement. A single computed ECI establishes no overall winner.
Diagnostics other than PASS exclude a fit from recommendation.

Components are ranked separately by benchmark, scorer, prompt template and
protocol ID. Unknown grouping metadata is shown as unknown and does not prove
matched protocols. Raw and normalized scores remain distinct. Cost and latency
lists appear separately when measured, with explicit unknown lists otherwise.
When no run metric is supplied, the tool uses the mean of measured item values
and exposes measured/observed counts. No first-item value becomes a run metric.
These are recorded observations, not matched workload comparisons. No rates or
costs are summed across strict/lenient views of the same inference.

## Policy application and integration boundary

`eci_tuning_routes.json` is the checked-in default. The user override is
`~/.config/agiw/eci-tuning.json`; `AGIW_ECI_TUNING_CONFIG` changes that path.
`apply --config PATH` selects an explicit write target. Apply previews a diff by
default, requires `--write`, writes atomically, refuses symlink targets, and
preserves unrelated top-level keys, custom task routes, and any existing Nisi Mac
pair. With no eligible baseline PC score it refuses to write. Keep this dedicated
policy file free of secrets; its diff is displayed locally. To undo, retain and
restore the previous file, or remove the override to use the repo default.

An AGIW integration can call `eci_tuning.select_route(task_type, machine, policy)`
for `general`, `reasoning`, or `coding`. It returns a config-selected PC route or
null for an ineligible machine/task. No model ID is hardcoded in server/UI code.
The current execution router is separately installed and outside this repo;
**policy file applied** does not mean **external runtime activated**.
`runtime_activation` remains NOT_APPLIED until that owner integrates and tests the
hook. Existing machine admission and model availability checks still apply.

Nisi's Mac author/reviewer hook remains **NOT_APPLIED**, with null defaults.
It requires **two distinct LLMs already resident in Mac LM Studio**, as documented
in [advanced configuration](advanced-configuration.md#resident-model-budget).
The PC Llama artifact is never assigned to those roles.

The Model Tuning Tool replaces the repair-only Fix inference panel. The toolbar
and inspector **Model tuning** controls open the tool without repair; `/eci-tuning`
links back to its **Check and repair inference** panel. Press **Run check** to
confirm the guarded repair. Closing the panel does not cancel it. Local runtime
may start the Mac loopback server; Nisi recovery keeps its idle preconditions;
route recovery may remount SharedChami and send one short Windows probe only
after the existing guards pass. No repair runs on open or GET.

`POST /api/eci-tuning/repair` and the deprecated `/api/inference/fix` alias share
the same handler, native capability, L2 gate and scopes (`all`, `local`, `route`,
`both`, `nisi`). Legacy control IDs `fixInference` and `drawerFixButton` open the
new tool. Windows stays read-only and denies repair through both routes. There
is no `agiw` CLI repair subcommand to alias. This is unreleased source work, not
a tagged release; PR 117 must merge before the replacement PR.

## Verification

```sh
python3 -B -m unittest test_eci_tuning test_package_resources test_server.TransportTest
node --test test_eci_tuning_ui.mjs test_inference_fix_ui.mjs test_tab_consistency.mjs
```

These are offline fixture, policy, loopback transport and Node module/source
checks. Browser rendering, native app acceptance, live routing, new model scores,
and deployed task quality remain NOT_RUN or NOT_MEASURED. No browser automation
is required on the development Mac.
