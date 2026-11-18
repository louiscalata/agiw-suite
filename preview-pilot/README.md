# Isolated AGIW → Nisi 0.3.1 pilot

This is an executable private integration slice, not a deployment of AGIW Suite or a fleet dispatcher. The installed app and original vendor/nisi 0.2.0 are unchanged.

Pinned official source: the upstream Nisi repository, tag v0.3.1 object `54b1c64b03fd8f7b2bd892945d17f6fae92057e1`, commit `be7a5c99338947baf130bc0d0364eef12cbef5cb`, tree `b9eee926a54eff6d566385022513ce90f2da5cb2`. Retrieved by public HTTPS with credential helpers/global config disabled. Git fsck passed. This verifies object consistency and public-tag correspondence, not an independently verified publisher signature. No npm install or global changes.

From the preview repository root:

```
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -B -m agiw.fleet_local_pilot --mode fixture
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -B -m unittest discover -s tests_cli -p 'test_fleet*.py' -v
```

The fixture runs the actual Python routing policy, actual pinned Nisi workflow and local-chat adapter with an injected response. It makes ZERO inference requests. The candidate is synthetic JSON, parsed and checked as data, never executed. The checker is deterministic, NOT an independent model reviewer.

Live mode is opt-in and requires the exact already loaded model ID on an unauthenticated literal-loopback llama.cpp-compatible endpoint, health ok, nonempty matching model inventory, and a nonempty /slots response with every `is_processing` exactly false. A missing/error result refuses inference. No token access, loads, downloads, redirects, proxy, peer dispatch or hosted/Jev adapter. This does not adapt LM Studio's alternative inventory schema. The default fixture model name cannot establish live readiness.

Only after readiness is actually available:

```
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -B -m agiw.fleet_local_pilot --mode live --endpoint http://127.0.0.1:PORT/v1/chat/completions --model EXACT_LOADED_ID
```

Limits: one author call, zero repair/retry, 256 output tokens, 15s adapter timeout, 20s Nisi deadline, 25s child wall timeout; 16KiB request/32KiB response. Node has a clean allowlisted environment and empty preview HOME, permission-confined filesystem reads, no subprocess/addon/worker grant. Audited import graph is only local-chat, workflow engine/contracts and Node builtins, with no credential or provider discovery. Default transport is direct node:http and never follows redirects or proxy env. A local model server may continue processing after client cancellation, so a timeout must not trigger retry and requires owner inspection before another run.

The preview lock prevents two copies of THIS pilot, not jobs from other apps. Readiness is a point-in-time observation, not an atomic backend reservation. Local loopback trust is not authenticated fleet identity. No cross-device readiness is inferred. Before multi-client production dispatch, an actual backend lease/queue integration remains necessary.

Evidence remains under preview-pilot/evidence. Workflow reportStored is false: the wrapper saves the returned report plus receipts, not Nisi's report-store attestation. No publication or installed-app integration.

## Observed result, 2026-11-17 19:45 UTC

22 policy/integration tests passed, 0 skips; 25 upstream local-chat/transport tests passed, 0 skips. Fixture COMPLETED, modelCalls 0. Filesystem boundary check denied reading /etc/hosts outside the preview.

Live inference NOT_RUN: 8081 healthy but model list empty and slots 400; 56562/56835 healthy but model metadata 401; Glimmer1235 connection refused; LM Studio1234 inventory 502. No idle loaded model verified. No key read, model load or app restart attempted.

## Real CPU-only smoke, 2026-11-17 19:58 UTC

A separately owned ephemeral server used pinned LM Studio-community SmolLM2-135M-Instruct Q4_K_M (105,454,144 bytes, provenance in tiny-model/provenance.json), CPU only, one thread/slot, nice15, ctx1024. Health/model inventory and idle slot verified. One actual inference ran through the planner and Nisi. Backend timing reports 262 prompt tokens and 256 generated tokens; Nisi refused the completion with LOCAL_CHAT_FINISH_REFUSED, so outcome BLOCKED. No candidate/checker/review acceptance. Exact finish_reason was not preserved by this upstream failure receipt; output cap was reached, consistent with length finish but not independently captured. No retry. Server PID13906 exited and was reaped; port53011 subsequently had no listener. Sampled startup RSS peaked at 282,992KiB (not a full-run peak).

Next design: a tiny-model-specific author adapter asks for a minimal constrained JSON value, preserves the real model output as candidate data, and binds it into Nisi via createCandidate; deterministic checks verify that value. Capture metadata-only HTTP finish_reason and usage before strict validation, including rejected responses. Preserve refusal on length, mismatch, timeout or malformed output; do not relabel this failed run as success or simply loosen validation.

## Distinct arithmetic smoke, 2026-11-17 20:00:22 UTC

A second explicitly authorized, settled isolated run asked the same tiny model what 6 multiplied by 7 is. JSON-object format constrained only syntax, not the correct answer. Its genuine output was `{"result":34}`. HTTP200, finish_reason stop, 42 prompt +16 completion tokens,105ms adapter time. Nisi accepted the complete transport response but FAILED at static shape validation (wrong key); the value was also wrong. No coercion, replacement, retry or independent model review. PID16132 reaped, port55509 no listener. This demonstrates the real route and refusal boundary, not useful arithmetic competence or production readiness. Five synthetic HTTP tests verify correct/wrong candidates, length refusal and model mismatch without invoking any model. Original first-run failure evidence is preserved.

## Final distinct route acceptance smoke, 2026-11-17 20:01:48–49 UTC

The same verified tiny model extracted a color from the synthetic sentence “The car is red.” The server JSON schema restricted only the object key and string type, with no enum, constant or desired answer. Model generated `{"color":"red"}`. HTTP200; finish_reason stop;51 prompt+16 completion tokens;105ms adapter time. Actual AGIW policy→Nisi0.3.1→local CPU model→static shape→exact-value test→deterministic check COMPLETED, all six stages PASS. Exactly one request in this distinct third job, no retry, no replacement/correction of output. PID17688 reaped and listener closed. Two earlier failed jobs remain preserved. This establishes a small real route smoke only, not general coding quality, independent LLM review, production AGIW integration or fleet readiness. Five extraction fixture tests additionally verify wrong semantic value is rejected despite valid schema. No more live requests authorized in this sequence.

## Worker ids

The pilot and its programs accept any worker id listed in `preview-pilot/fleet-workers.json` (router config: m5-macbook-pro, dual-gpu-pc, monterey-mac-pro). Tests keep it equal to `agiw.fleet_routing_plan.WORKERS`. Unknown ids are refused by the pilot, the worker, the router client and the Node route check.

## Lane settings and the worker LaunchAgent

`preview-pilot/fleet-workers.json` `lanes` holds per-lane defaults. Monterey has `max_tokens` 512 and `model_timeout_s` 840: at about 0.68 tok/s, 512 tokens take about 750 s, plus about 60 s for the prompt. Its `job_timeout_s` (the router's wait) is 900. Other lanes use the program defaults (64 tokens for the tiny programs, 256 for nisi-smoke) and a 30 s timeout. A worker can override these with `--max-tokens` and `--model-timeout-s`. A job overrides them with `max_tokens` and `model_timeout_s`, which can come from the router config's per-worker `max_tokens`, `model_timeout_s`, `disable_thinking` and `job_timeout_s`.

When the finish reason isn't `stop`, or the content won't parse, the receipt's `authorEvidence` includes `outputSample`: up to 300 sanitized characters of the raw model output. A known preamble is stripped only if it is followed by exactly one valid JSON object, which is used unchanged; `preambleStripped` records which kind. The known preambles are a harmony `analysis`→`final` channel pair, a `final` channel alone, or one leading `<think>` block. Anything else is refused.

launchd gives agents a minimal PATH, so node in `~/.local/bin` or Homebrew isn't found when a job runs. Generate the worker LaunchAgent from the resolved binaries:

```sh
python3 -B -m agiw.worker_service render -- --worker-id monterey-mac-pro --bind 127.0.0.1 --port 18791 --token-file /path/worker.token --model-endpoint http://127.0.0.1:PORT/v1/chat/completions --model EXACT_ID --disable-thinking
python3 -B -m agiw.worker_service install -- <same worker arguments>   # writes ~/Library/LaunchAgents/...plist and prints the launchctl command
```

The plist sets `AGIW_NODE`, `AGIW_GIT` and a minimal `PATH` in `EnvironmentVariables`. It runs from the repo checkout and never contains a token value. `--load` also runs `launchctl bootstrap`; leave it off to load the agent yourself.

## Network permission and the pin check

Node 25 and later deny network access under `--permission` unless `--allow-net` is given. That flag can't be scoped: Node accepts a host value but ignores it. In live mode, on Node 25 or later, the pilot passes a bare `--allow-net`. In every mode, each program installs `net-guard.mjs`, which refuses any socket other than the configured literal `127.0.0.1:<port>`. Node 22–24 doesn't gate the network and rejects `--allow-net`, so only the guard applies there. Fixture mode never gets network access. Node older than 22, or a Node version that can't be read, is refused.

The pinned Nisi checkout is checked under the pilot lock. `git status` runs read-only (`GIT_OPTIONAL_LOCKS=0`, so there are no `index.lock` races) and includes untracked files. On Windows it runs with `core.autocrlf=true`, matching Git for Windows' default checkout, because system config is disabled. Refusals are now specific: `NISI_PIN_MISMATCH`, `NISI_CHECKOUT_DIRTY: <first entries>` or `NISI_GIT_FAILED: <sanitized git error>`.

## Structured output (opt-in per lane)

`response_format: "json-schema"` in a lane, the worker flag `--response-format json-schema`, or a job's `response_format` field turns on structured output. The tiny programs then send an OpenAI-style `response_format: {type: "json_schema", json_schema: {name, strict: true, schema}}` built from the job's expected answer schema. llama-server compiles this into a grammar, so decoding is constrained and EOS follows the closing brace. That stops models that echo the prompt or reason in plain text until `max_tokens` runs out. If the server rejects the schema request with HTTP 400, 422, 500 or 501, the program retries once with `{type: "json_object"}` within the same time budget, and the receipt records `responseFormatFallback`, `fallbackHttpStatus`, `fallbackServerMessage` and `attempts`. Timeouts and model answers are never retried. A `finish_reason` other than `stop`, including `length`, still fails closed: a truncated answer is never accepted. `monterey-mac-pro` turns this on by default. `nisi-smoke.mjs` uses the vendor adapter and ignores the setting.

## Request mode `completion-grammar` (opt-in per lane)

Some chat templates always open a reasoning channel on `/v1/chat/completions`, and the request options can't close it. `request_mode: "completion-grammar"` (set on a lane, with the worker flag `--request-mode`, or in a job's `request_mode` field) skips the chat route. The tiny programs then:

1. POST the job's messages to `/apply-template` on the same `127.0.0.1:<port>` as the configured endpoint. The net guard and route check are unchanged.
2. Append the lane's `completion_prefill`. The default, ` to=self<|message|><|eom|><|start|>assistant to=user<|message|>`, closes an empty reasoning channel and opens the answer. When a prefill is set, the rendered prompt must end with the lane's `prompt_suffix` (default `<|start|>assistant`). Both values are printable ASCII or newline, at most 160 characters.
3. POST to `/completion` with `n_predict` = max_tokens, `temperature: 0`, and a GBNF grammar built from the program's answer schema. With `completion_constraint: "json-schema"`, it sends `json_schema` instead and falls back once to the grammar if the server rejects it. Both steps share the model timeout.

`stop_type` `eos` or `word` maps to finish `stop`, and `limit` maps to `length`, which still fails closed. A failed `/apply-template` call blocks the job before any completion request. Receipts record `requestMode`, `templateRender`, `stopType`, `tokensPredicted`, `constraint` and any fallback. No prompt text is recorded beyond the 300-character output sample. `monterey-mac-pro` turns this mode on. The default stays `chat`.
