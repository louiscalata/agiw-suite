# Check jobs

`eci_jobs.py` queues, tracks, cancels and ingests screening checks, one at a
time. It is an engine only. It has no page, no server route and no thread, and
nothing in the monitor imports it yet. All real checks are **NOT_RUN**.

A screening check is a [model fast check](eci-fast-check.md) run by the
[loopback adapter](eci-model-tuning.md#run-a-fast-check-against-an-existing-server)
against a lane that is already serving. Its result is a fast-check record. It
is not an ECI score and it selects no model for routing.

## Rules

- **Fail closed.** Every unreviewed, unknown or undecided state refuses. A
  refusal has a stable code and fixed text.
- **The gate ships closed.** `Engine(state_dir)` with no other argument refuses
  every submission, resume and start. There is no environment variable and no
  file that opens it.
- **Reviewed submission.** A job is queued only with a review that names the
  SHA-256 of the exact normalized request and attempt.
- **One job at a time.** One engine owns a state directory. One job runs.
- **It starts nothing.** The engine never starts a model, a server or a lane.
  It passes a base address to a runner. If the lane is not serving the model,
  the job fails.
- **Reading starts nothing.** Status, job detail and preview never write to the
  journal and never call the runner.
- **Nothing resumes by itself.** After a restart, a job that was running or
  queued becomes `interrupted`. Only a new reviewed resume queues it again.
- **Ingest only valid, complete records for the reviewed request.** A record
  is validated and compared with the reviewed request before it reaches the
  dataset. An invalid, incomplete, cancelled or mismatched result leaves the
  dataset untouched.
- **Append-only state.** Each change is a new journal entry that supersedes the
  previous one for that job. No entry is rewritten or removed.

## Decisions the engine does not make

Three owner decisions are open. `Policy` holds them, and its default refuses.

| Decision | `Policy` field | Default | Effect of the default |
| --- | --- | --- | --- |
| Is a check owner-gated, and what opens it? | `gate` | a function that returns `False` | `gate_closed` on submit, resume and start |
| Which lanes may be targets? | `allowed_targets` | empty | `target_not_allowed` for every lane |
| Wall budget cap and default | `max_wall_budget_s`, `default_wall_budget_s` | none | `budget_refused`, or `budget_required` when the request names no budget |

The gate opens only when `gate()` returns exactly `True`. Any other value, or
an exception, is closed. The dataset folder is also undecided: `DatasetSink`
takes a folder and has no default.

The policy is read again on every check. A budget cap that is no longer a
valid budget string counts as undecided and refuses with `budget_refused`.

## States

```mermaid
stateDiagram-v2
    [*] --> queued: reviewed submit
    queued --> running: run_next, gate open
    queued --> cancelled: cancel
    queued --> failed: refused at start
    queued --> interrupted: engine restart
    running --> running: cancel requested
    running --> finished: valid complete record ingested
    running --> failed: error, invalid, incomplete, ingest failed
    running --> cancelled: cancel honoured
    running --> interrupted: engine restart or interrupt
    failed --> queued: reviewed resume
    cancelled --> queued: reviewed resume
    interrupted --> queued: reviewed resume
    finished --> [*]
```

Each arrow depends on the state at its tail. `finished` is final. A job has at
most 16 attempts.

## Interface

```python
import eci_jobs

engine = eci_jobs.Engine(
    state_dir,                      # absolute; holds jobs.jsonl, engine.lock and runs/
    policy=eci_jobs.Policy(...),    # default: eci_jobs.CLOSED
    targets=lambda: [...],          # remote_machines.lane_targets(config)
    sample_sets={"screening": ["/absolute/path/samples.json"]},
    runner=eci_jobs.adapter_runner, # default: None, which refuses
    sink=eci_jobs.DatasetSink(dataset_dir),  # default: None, which refuses
    now=..., clock=...,             # injected in tests
)
```

| Call | Writes | Result |
| --- | --- | --- |
| `status()` | nothing | `status` (`ok` or `unknown`), `reason`, `owner`, `gate`, `jobs`, `active` |
| `job(job_id)` | nothing | one job view |
| `preview(request)` | nothing | normalized request, `job_id`, `review_sha256`, `effects`, `refusals` |
| `preview_resume(job_id)` | nothing | the same, for the next attempt |
| `submit(request, review)` | one `queued` entry | job view; the same submission again returns the same job and writes nothing |
| `resume(job_id, review)` | one `queued` entry with the next attempt | job view; the same resume again writes nothing |
| `cancel(job_id)` | `cancelled`, or a `running` entry with `cancel_requested` | job view; repeating writes nothing |
| `run_next()` | `running`, then one final entry | the job view, or `None` when nothing is queued |
| `eci_jobs.read_status(state_dir)` | nothing, takes no lock | job list as the journal records it, for a read-only mirror |

`read_status()` reports what the journal says, not what is happening. It takes
no lock and cannot tell whether an engine is alive. If the owning engine died
during a run, the journal still ends with a `running` entry, so `read_status()`
shows that job as `running` and names it in `active` until an engine opens the
directory and marks it `interrupted`. A `running` state from `read_status()` is
not evidence that anything is running. `Engine.status()` does not have this
limit: it holds the lock, and it reports `owner`.

A request has `submission_id`, `target`, `model_id` and `sample_set`, and may
have `count`, `max_tokens`, `wall_budget_s`, `request_timeout_s`,
`disable_thinking` and `facts`. Any other field refuses. Normalizing makes every
default explicit, so the review covers the values that will run.

- `target` is a registry lane id such as `registry:machine:lane`. The lane must
  be in the registry when the job is submitted and again when it starts. Its
  address must be a loopback lane address. The eight protected ports refuse.
- `model_id` is a public model identifier, never a path or an address.
- `sample_set` names a configured set. A request cannot name a file path.
- `wall_budget_s` is an exact positive decimal string of at most 32
  characters, at most the policy cap. A longer string refuses with
  `invalid_request` before it is converted, on every supported Python version.
  The same length limit applies to the two `Policy` budget fields.
- `facts` are the adapter's optional model and hardware facts: a lowercase
  SHA-256, sizes and short public descriptors. `model_label` must be a public
  model identifier like `model_id`, because the adapter writes it as the
  record's model name.

`job_id` is derived from the normalized request, which includes
`submission_id`. `review` is exactly
`{"reviewed": true, "review_sha256": "<from preview>"}`. The digest covers the
job id, the attempt and the request, so a review cannot be reused for another
request or for a later attempt.

`preview` lists every reason a submission would be refused now, and the
effects a reviewer must see: the target lane, the sample set, the planned
number of requests, the upper bound with one retry per item, the wall budget,
`starts_model: false` and what is written.

Refusal codes: `invalid_request`, `budget_required`, `budget_refused`,
`not_reviewed`, `review_mismatch`, `gate_closed`, `unknown_target`,
`protected_port`, `invalid_target`, `target_not_allowed`, `unknown_sample_set`,
`not_configured`, `samples_unreadable`, `queue_full`, `journal_full`,
`journal_unavailable`, `not_owner`, `unknown_job`, `not_resumable`,
`not_cancellable`, `busy`.

`samples_unreadable` appears only in the `refusals` list of a preview, when the
configured sample files cannot be read or planned. Submit does not read the
sample files; a job whose files cannot be read fails when the runner loads
them.

## Running a job

`run_next()` runs the oldest queued job to its end in the calling thread. The
caller decides when to call it. A second call while a job runs refuses with
`busy`. The gate, the policy, the registry and the sample set are checked again
at start. A closed gate leaves the job queued. A lane that left the registry or
the policy fails the job without calling the runner.

The gate is checked on submit, on resume and at start only. Closing it while a
job runs does not stop the runner and does not block ingestion of that job's
record. Cancel stops a running job.

The runner contract is `runner(spec, should_stop, progress)`:

- `spec` is the normalized request plus `base_url`, `sample_files`,
  `output_dir`, `job_id` and `attempt`. Every attempt of a job uses the same
  output directory, `runs/<job_id>`, so the adapter continues from its durable
  checkpoint.
- `should_stop()` returns `None`, `"cancel"` or `"budget"`. A runner asks
  between items and raises `eci_jobs.Stopped` when told to stop.
- `progress(completed, planned)` is kept in memory for the running job only.
- The return value is a fast-check record.

| Runner outcome | Final state | Dataset |
| --- | --- | --- |
| Valid, complete record that is bound to the request, and the sink accepts it | `finished` | record added |
| Cancel was requested, whatever the runner returned | `cancelled` | untouched |
| Record fails `validate_record` | `failed` | untouched |
| Record has another model name, `max_tokens`, count, wall budget or declared model hash | `failed` | untouched |
| A benchmark attempted fewer items than planned | `failed`, checkpoint kept | untouched |
| The sink raises | `failed` | untouched |
| The runner raises | `failed`; only the exception type, or a validator's fixed text, is kept | untouched |
| `Stopped` at the wall budget | `failed` | untouched |
| `KeyboardInterrupt` or another interrupt | `interrupted`, then the interrupt propagates | untouched |

An incomplete record is not ingested because a resumed attempt would produce a
second record for the same model with a different id.

### Record binding

A record is ingested only when all of these equal the reviewed request. Each
is compared whether or not a model hash was declared.

| Record field | Must equal |
| --- | --- |
| `model.name` | `facts.model_label` when the request has one, otherwise `model_id` |
| `runtime_settings.max_tokens` | `max_tokens`, as an integer |
| `plan.items_per_benchmark` | `count` |
| `plan.wall_budget_s` | `wall_budget_s` |
| `model.file_sha256` | `facts.model_sha256`, only when the request declares one |

Any difference fails the job with "The record does not match the reviewed
request." and the dataset is untouched. The rule is exact on purpose:

- The adapter lowers `max_tokens` to the lane's context size when the context
  is smaller. That record ran with a setting the reviewer did not approve, so
  it is refused. Submit a new request whose `max_tokens` is at most the lane's
  context. The smallest allowed value is 8192, so a lane with a smaller
  context cannot produce an ingestible record through this engine.
- A record with no runtime settings or no `max_tokens` is refused.
- Without a declared hash the model file is identified only by the name the
  lane advertises. Declare `facts.model_sha256` to bind the file itself.

`adapter_runner` binds a job to the adapter's public `run()`. The adapter has
no stop or progress hook. With this runner a cancel takes effect before the
first request and when `run()` returns, and the adapter's own wall budget
bounds the time in between. Progress stays unknown. A per-item hook in the
adapter is a follow-up.

## Journal

`jobs.jsonl` holds one JSON entry per line. Each entry has a sequence number,
the SHA-256 of the previous entry and its own SHA-256. The hashes detect
accidental edits. They do not authenticate deliberate rewriting.

- Each entry is written and synced before memory changes. An entry that replay
  would reject is never written.
- Replay checks every hash, the sequence and every state change. An unknown
  state, an illegal move, a missing entry or an entry out of sequence makes the
  journal damaged. Then status is `unknown`, the job list is empty, and submit,
  resume, cancel and start refuse. The file is left as it is.
- A tail is every byte after the last valid entry: an unterminated line, or
  any number of complete lines that do not validate, including a last entry
  whose own SHA-256 no longer matches. A crash that cuts the last line is one
  cause, not the only one. A tail is never applied. On the next open the owner
  terminates an unterminated line and appends a `repair` entry that names the
  SHA-256 of exactly the skipped bytes. The bytes stay in the file. A complete
  entry that lost only its newline is kept.
- Two consequences of that rule, both on the safe side. A journal that holds
  only invalid lines opens as `ok` with no jobs, after a repair entry. A
  damaged last entry of a `finished` job is skipped, so the job reverts to
  `running` in the journal and becomes `interrupted` at open; the dataset
  keeps the record, and nothing runs again without a new reviewed resume.
- An invalid line before a valid entry is accepted only when that entry is a
  `repair` entry naming the SHA-256 of exactly those bytes. Anything else is
  damage. An edit to a middle entry is damage even when the edited values
  would be a legal state change, because the entry's own SHA-256 no longer
  matches.
- `engine.lock` is held for the life of the engine. A second engine on the same
  directory owns nothing: its status is `unknown` and its actions refuse with
  `not_owner`. Holding the lock at open proves that no engine is running the
  jobs, which is why running and queued jobs can be marked `interrupted`.
- The journal is a regular file of at most 8 MiB with at most 256 jobs. A link
  refuses. New work refuses 1 MiB before the limit, which leaves room for
  running jobs to record their end. Every request field is bounded in length,
  so a `queued` entry is a few kilobytes at most.

The journal holds the normalized request, states, fixed reasons, record ids,
the post decision and per-benchmark counts. It holds no prompt, no answer and
no server text.

## Dataset sink

`DatasetSink(folder)` adds one record to `fast_checks` in `folder/dataset.json`,
the normalized dataset the Model evaluation page reads.

- The record is validated first. The whole candidate dataset is then validated
  by `eci_tuning.ingest` from a temporary file in the same folder.
- The run, comparison-pair and size limits of the dataset reader are checked,
  so the sink cannot write a dataset the reader would refuse.
- The file is replaced atomically, and only if it did not change during
  validation. Any failure leaves the existing file untouched and removes the
  temporary file.
- A record that is already present is not written again.

## Boundaries

- Pre and post decisions stay deterministic local functions in the fast-check
  core. The engine sends nothing to Jev or to another model.
- The record's `journal` field is a Nisi journal reference or null. The engine
  does not create a Nisi journal and does not read one.
- The engine imports no model control, no process launcher and no socket code.

## Not in this module

- Server routes for status, reviewed submit, resume and cancel, with the native
  capability header, and a read-only or 501 Windows mirror.
- Bundling `eci_jobs.py` and the adapter in `build.sh`, `package-release.sh`
  and the Windows installer.
- The check queue, check detail and reviewed submission on the Model
  evaluation page.
- A stop and progress hook in the adapter, so that a cancel is honoured between
  items with the real adapter.
- A worker thread that calls `run_next()` after a reviewed submit.
- The three owner decisions above, the dataset folder and the sample files.
- Journal rotation.
- A way for `read_status()` to report whether an engine holds the lock.
- A link from `docs/README.md` and a roadmap entry.

## Tests

`test_eci_jobs.py` uses a fake runner, fake clocks and temporary folders. It
opens no socket and calls no model.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest test_eci_jobs -q
```
