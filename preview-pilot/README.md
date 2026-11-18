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
