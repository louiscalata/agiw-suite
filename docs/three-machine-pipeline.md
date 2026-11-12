# Remote machine lane registry

The optional remote machine registry adds logical machines and model lanes to the inference map. Each lane observes a separately managed endpoint through an existing local binding. The feature reports the identity returned by that endpoint; it does not establish the identity of the remote hardware, run inference, load a model, create a tunnel, or change routing.

The canonical project plan is [roadmap.md](../roadmap.md). Operator inventories, concrete connection details, installed application state, and private audit receipts belong outside the repository. The accompanying [example registry](remote-machines.example.json) uses placeholder labels and illustrative ports. It is documentation, not an applied configuration.

## Configuration

The collector reads `~/.config/agiw/remote-machines.json`, or the file selected by `AGIW_REMOTE_MACHINES_CONFIG`. The root object has `schemaVersion: 1` and a `machines` list. It permits 1–32 machines, 1–8 lanes per machine, and at most 64 lanes overall.

| Field | Meaning and validation |
| --- | --- |
| Machine `id` | Unique logical token, at most 16 characters; starts with a lowercase letter and contains lowercase letters, digits, underscores, or hyphens. |
| Machine `label` | Display text, 1–80 ASCII letters, digits, spaces, dots, underscores, or hyphens. |
| Machine `lanes` | Nonempty list of lane objects. |
| Machine `replaces` | Optional; the only supported value is `"legacy-pc"`. At most one machine may declare it. |
| Lane `id` | Logical token with the same character rules as machine IDs; unique within its machine. |
| Lane `label` | Display text with the same rules as machine labels. |
| Lane `localPort` | Unique numeric integer from 1024 through 65535, excluding the protected ports below. Booleans and numeric strings are invalid. |
| Lane `timeoutSeconds` | Optional finite positive number, at most 6 seconds; defaults to 1.5 seconds. Set a longer timeout explicitly for a known slow endpoint. |
| Lane `contextTokens` | Optional nonnegative integer. Preserved unchanged in the lane record; no behavior consumes it yet. |
| Lane `thinking` | Optional `"on"`, `"off"`, or `"unknown"`. Preserved unchanged in the lane record; no behavior consumes it yet. |

Unknown fields are rejected. Credentials, addresses, commands, configured model aliases, and remote port mappings are not registry fields. Every request uses exactly `http://127.0.0.1:<localPort>/v1/models`. A logical ID is a stable display identity, never a claim about a particular computer or model.

The registry refuses these ports with an error naming the rejected entry: **8081, 1234, 1235, 8080, 11434, 18082, 8768, and 8769**. They are reserved for other local services and must never be probed by this feature. The check happens during configuration validation before any registry request is scheduled. Select an independently verified, nonprotected loopback binding for each lane; do not infer an available port from this example.

## Safe file handling

The configuration is opened with `O_NOFOLLOW` and `O_NONBLOCK`. The collector inspects that same descriptor with `fstat` and requires a regular file owned by the current user, without group or world write permission, and no larger than 64 KiB before reading. The read is also bounded. Symlinks, FIFOs, devices, unsafe ownership or permissions, oversized files, and malformed content disable the feature with one logged reason. A missing default file leaves the feature off. File failures cannot block the sampler waiting for a writer.

Create and maintain the external file under the operator's private configuration directory. The registry does not write that file or transport configuration itself.

## Namespaces and existing visibility

Registry roots use `registry:<machine-id>` and lanes use `registry:<machine-id>:<lane-id>`. The namespace is independent of legacy machine IDs. A machine whose configured ID is `pc` or `mac` therefore coexists with the legacy PC or local Mac rows.

The legacy PC collection and map rows remain present unless one valid registry machine explicitly declares `"replaces": "legacy-pc"`. A replacement suppresses only those legacy PC rows and their collection; the registry machine keeps its own namespaced IDs. Invalid registry configuration disables the registry and preserves legacy visibility. The local Mac rows retain their established behavior.

## Probe and identity contract

Registry probes run in the background using a bounded pool of eight workers. Each lane has its own timeout and is scheduled on a three-second probe interval. Snapshot collection reads the last completed result without waiting for outstanding or queued registry probes. The first pending observation is unknown. Slow endpoints cannot hold up unrelated snapshot collection, and responsive lanes are admitted as pool capacity becomes available.

A successful lane requires all of the following:

- HTTP status exactly 200, with no redirect followed.
- A valid `Content-Length` whose complete body is read, or a fully terminated chunked body. Unframed, conflicting, and incomplete responses are unknown.
- A body no larger than 1 MiB, decoded as strict UTF-8.
- Valid JSON without duplicate keys or nonstandard numeric constants.
- Exactly one `data` entry with one nonempty model ID of at most 1024 printable characters.

All other outcomes clear the model identity and carry a reason code. A failed probe never recovers a previous identity from a label or cached successful result. Model IDs and labels are rendered as text. An endpoint-reported model ID does not prove inference correctness, device placement, context capacity, reasoning behavior, or whole-machine health.

Each completed lane record carries its observation timestamp and age. A result older than two probe intervals—six seconds—is unknown with reason `stale` and no model identity. Registry lanes use this freshness rule regardless of the configured machine ID, including `pc`; the legacy peer expiry does not apply. Snapshot aging and the rendered map both enforce the rule, so a stalled sampler cannot leave an old registry identity marked OK. Machine roots describe only their configured lanes and become unknown when a child is unknown.

## Optional metadata and future integrations

`contextTokens` and `thinking` are optional operator-supplied metadata. Validation and pass-through do not verify that an endpoint actually supports those settings. Neither field changes probe behavior, context allocation, token budgets, or model requests. Omitted values remain absent. Future capability work needs separately sourced observations bound to the current model and runtime.

The registry's pure configuration and target helpers can support later lane selection, but adding an entry starts no task and grants no execution permission. Hardware inventory, authenticated transport ownership, routing migration, effective context measurement, and reasoning-budget support remain separate integrations. The general Machines and Overview projections do not become whole-machine inventory through this feature.

## Packaging and validation boundaries

Both Mac resource lists and the Windows installer ship `remote_machines.py`. A Windows package regression stages the actual installer inventory and imports the observer in an isolated Python subprocess, without falling back to the source checkout.

Source regression tests cover validation, safe file handling, namespace compatibility, background timing, freshness, strict HTTP transport against ephemeral loopback servers, and the actual map DOM. Transport tests issue GET `/v1/models` to their test servers; they do not generate model output. DOM tests run in the Node harness without launching a browser. Source checks do not establish native rendering, installed Windows startup, hardware acceptance, or deployment.
