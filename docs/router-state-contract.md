# Router state contract

Contract version: **1.0.0** · schema `agiw.router-state-contract.v1` · reader identifier `codemode.router.readers.v2`

This page states exactly what the Monitor reads from the state of the separately managed coding router: which files, which fields, how old they may be, and what the Monitor shows when a file is missing, unsafe or malformed. It is derived from the readers in [telemetry.py](../telemetry.py) and [activity.py](../activity.py). The machine-readable half is [test_support/router_contract/contract.json](../test_support/router_contract/contract.json); [test_router_contract.py](../test_router_contract.py) keeps this page, that file, the readers and the fixture writers in agreement.

The router itself is not part of this repository. This page does not describe how the router decides anything. It describes the journal a router must leave on disk for the Monitor to report it correctly, and what the Monitor reports for every other journal.

**In this page:** [Scope](#1-scope) · [Paths](#2-paths) · [Reading rules](#3-reading-rules) · [Layouts](#4-journal-layouts) · [Files](#5-files) · [Liveness](#6-liveness) · [Row states](#7-row-states-and-projection) · [Failure behaviour](#8-failure-behaviour) · [Freshness](#9-freshness) · [Limits](#10-limits) · [Fixtures and tests](#11-fixtures-and-tests) · [Versioning](#12-versioning)

## 1. Scope

The Monitor is a reader. It never creates, renames, truncates or deletes a router file, and it never runs a router command to observe state.

The only locks the observer takes are momentary non-blocking probes, released immediately:

| Probe | Lock call | When |
| --- | --- | --- |
| Liveness | `LOCK_SH \| LOCK_NB` on `owner.lock` or `locks/<runId>.lock` | Each sample, per listed run |
| Drain | `LOCK_SH \| LOCK_NB`, then `LOCK_EX \| LOCK_NB`, on `owner.lock` | Only under an installed fence with policy `single` |

A probe opens the lock file read-only without `O_CREAT`. A missing lock file is never created. A router that polls its locks is not refused by a probe; it retries on its next poll.

The recovery adapters in [online_code_repair.py](../online_code_repair.py) are outside this contract. They run only after a person confirms a repair, and they hold `owner.lock` with `LOCK_EX | LOCK_NB` for the repair. While they may hold it, the readers stop treating an exclusive holder of `owner.lock` as a live legacy route (section 6).

## 2. Paths

All paths are under the home directory of the user running the Monitor.

| Path | Read as |
| --- | --- |
| `.local/state/codemode-router` | The journal root (sections 4 and 5) |
| `.codex/skills/local-llm-orchestrator/scripts/pipeline_router.py` | Never opened. Its absolute path is compared with the argument list of candidate router processes. The presence of `router_fence.py` beside it marks an installed per-run generation |
| `.local/state/codemode-nisi/pending.json` | The Nisi pending marker |
| `.local/state/codemode-launcher/readiness.json` | The launcher's readiness receipt |

## 3. Reading rules

Every file is read the same way. A file that fails any rule is not used.

**Private file.** The path is not a symbolic link. It is a regular file owned by the Monitor's user, with no group or other permission bits and exactly one link. It is opened with `O_RDONLY | O_NOFOLLOW | O_NONBLOCK`.

**One observation.** The reader takes `lstat`, opens, checks `fstat` is the same device and inode, reads, then takes `fstat` and `lstat` again. If the device, inode, size, modification time or change time differ at any point, or the file has zero or two links, the file is *changing*. A changing file is read again, up to `readAttempts` times, `readRetrySeconds` apart. A file still changing after that is unreadable. A router may therefore publish by rename or by link and unlink; a publish that died between link and unlink leaves an unreadable file until it is healed.

**Private directory.** A directory is used only when it is a real directory (not a link) owned by the Monitor's user with no group or other permission bits. Lock files are opened with `O_NOFOLLOW`, which guards only the last path component, so `locks/` must itself pass this rule.

**Closed JSON.** The content is UTF-8 JSON whose top level is an object, within the size limit for that file. A duplicate key, `NaN`, `Infinity` or `-Infinity` makes the file invalid. Where this page lists a key set, the set is closed: one unknown key or one missing key makes the file invalid.

**Numbers.** A time or count is a JSON number that is a finite float, or an integer between minus and plus `largestExactInteger`. `true` and `false` are not numbers. A longer integer is invalid and never an error.

**No content is copied.** The Monitor copies run identifiers, stage labels, digests, timestamps and the enumerated fields listed here. It never copies a checkpoint's `evidence` body, prompt text or model output into a snapshot.

## 4. Journal layouts

The Monitor reads two layouts in every sample and reports both when both are present.

| Layout | Root contents the Monitor reads |
| --- | --- |
| Legacy (single run) | `owner.lock`, `active.json`, `archive/`, `checkpoints/` |
| Per-run | `owner.lock`, `active/<runId>.json`, `locks/<runId>.lock`, `notes/<runId>.json`, `policy.json`, `install-fence.json`, `archive/`, `checkpoints/` |

The journal counts as initialized when the root, `archive/` and `checkpoints/` are private directories and `owner.lock` is a private file. The content of `checkpoints/` is not read. Without an initialized journal the Monitor reports activity as unknown, never idle.

A run identifier matches `[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}`. Only directory entries named `<runId>.json` are listed in `active/`, `notes/` and `archive/`; other names are ignored, so a writer may stage files there under other names.

## 5. Files

### 5.1 Run record: `active.json` and `active/<runId>.json`

Size limit: `recordBytes`.

A record that has begun and has no checkpoint has exactly these keys:

| Key | Value |
| --- | --- |
| `schemaVersion` | Integer `1` |
| `runId` | Run identifier. In `active/<runId>.json` it must equal the file name |
| `inputSha256` | 64 lowercase hexadecimal characters |
| `stage` | `started` |
| `checkpoint` | `null` |
| `startedUnix` | Number, seconds since the epoch |

A record with a checkpoint has those keys plus `sequence`, and:

| Key | Value |
| --- | --- |
| `stage` | Matches `[A-Za-z][A-Za-z0-9_.:-]{0,63}` |
| `sequence` | Integer, 1 or more |
| `checkpoint` | Object with exactly `schemaVersion` (integer `1`), `runId`, `inputSha256`, `sequence`, `stage`, `evidence` (object) and `recordedUnix` (number). `runId`, `inputSha256`, `stage` and `sequence` must equal the record's |

A per-run record has one more key, `runLock`: an object with exactly `dev` and `ino`, both non-negative integers, naming the device and inode of `locks/<runId>.lock` that the run held when it began. A legacy record never has `runLock`.

From a valid record's `evidence` the Monitor reads only:

| Field | Use |
| --- | --- |
| `recoveryRequired` | `true` marks the run as needing recovery |
| `code` | Shown only when `stage` is `incomplete` and the value matches `[A-Z][A-Z0-9_]{0,63}` |
| `selectedHost`, `host`, `requestedHost` | First of these that is `mac`, `windows` or `auto` is the run's host |

For the stages `intake_intent`, `intake_response`, `mac_return_intent`, `mac_return_response` and `final_validation_intent` the host is `mac` whatever the evidence says. A valid note's `host` replaces the record's. The stage `incomplete` is the terminal quarantine checkpoint: a record at that stage is never live in the legacy layout.

### 5.2 Run lock: `locks/<runId>.lock`

A private file. Its content is not read. A run holds it with an exclusive `flock` from before it begins until it exits. The file may remain after the run ends; a lock file with no record and no note produces no row.

### 5.3 Owner lock: `owner.lock`

A private file. Its content is not read. In the legacy layout the Monitor expects the one run to hold it exclusive for its whole life. In the per-run layout the Monitor reads a shared hold as runs admitted side by side, and an exclusive hold as one run admitted alone, an installer or a repair. It cannot tell those exclusive holders apart from the lock alone, which is why legacy liveness needs all four rules in section 6.

### 5.4 Note: `notes/<runId>.json`

Size limit: `noteBytes`. Advisory only: a note never makes a run live, and a note whose run lock is free is ignored. Exactly these keys:

| Key | Value |
| --- | --- |
| `schemaVersion` | Integer `1` |
| `kind` | `codemode.router.note.v1` |
| `runId` | Must equal the file name |
| `inputSha256` | 64 lowercase hexadecimal characters. For a run with a record it must equal the record's, or the note is ignored |
| `operation` | `null`, `work` or `feedback` |
| `client` | `null`, `codex`, `claude` or `opencode` |
| `host` | `null`, `mac`, `windows` or `auto` |
| `admission` | `shared` or `exclusive` |
| `phase` | `admitting`, `waiting` or `running` |
| `resource` | `null`, `mac-pair`, `pc-route`, `pc-lane-deep` or `pc-lane-fast` |
| `step` | `null`, `pre-begin`, `backend`, `mac-return`, `draft` or `review` |
| `sinceUnix` | Number |
| `untilUnix` | Number or `null` |
| `pid` | Integer, 1 or more |

Lanes shown from `resource`: `mac-pair` (capacity 1), `pc-route` (1), `pc-lane-deep` as `pc-deep` (1), `pc-lane-fast` as `pc-fast` (2). A run holding or waiting for a PC lane is also listed as a holder of `pc-route`.

### 5.5 Policy: `policy.json`

Size limit: `policyBytes`. Exactly `schemaVersion` (integer `1`), `kind` (`codemode.router.policy.v1`) and `concurrency` (`single` or `multi`). It is read only under an install fence in state `installing`, `installed` or `rolling-back`. Absent means `single`. Invalid means the Monitor reports no policy (`source: invalid`). Without a fence, or under `rolled-back`, the Monitor reports `single` from the single-run router whatever `policy.json` says.

### 5.6 Install fence: `install-fence.json` and the barrier

Size limit: `fenceBytes`. Exactly these keys:

| Key | Value |
| --- | --- |
| `schemaVersion` | Integer `1` |
| `kind` | `codemode.router.install-fence.v1` |
| `state` | `installing`, `installed`, `rolling-back` or `rolled-back` |
| `generation` | 64 lowercase hexadecimal characters |
| `stamp` | `YYYYMMDDTHHMMSSZ`, digits only |
| `sinceUnix` | Number |
| `ownerLock` | `null`, or an object with exactly `dev` and `ino` (non-negative integers) |

The barrier is a second link to `owner.lock` named `owner.lock.install-barrier`. It counts only when that name exists and `owner.lock` has exactly two links.

| Observation | Reported install state |
| --- | --- |
| No fence file, no `router_fence.py` beside the router script | `absent` (the single-run router) |
| No fence file, `router_fence.py` present | `missing` |
| Fence unreadable or outside the schema | `invalid` |
| Valid fence | Its `state` |
| State `installing` or `rolling-back`, or the barrier | Install in progress |

While an install is in progress the Monitor reports `install-in-progress` and verifies nothing else. Under `missing` or `invalid` it reports no admission policy and no legacy route as live. When `ownerLock` names another inode than the current `owner.lock`, the drain state is `unknown`.

### 5.7 Nisi pending marker

Size limit: `markerBytes`. A changing marker is read once more after `readRetrySeconds`. Two forms are recognised, both with `kind` `codemode.nisi.pending.v1`:

| Form | Keys | Owner shown |
| --- | --- | --- |
| Anonymous | `kind`, `started_unix`, `input_sha256` | `anonymous (legacy)` |
| Router | Those three, plus `runId` and `operation` | The marker's `runId` |

`started_unix` is a number no more than `markerFutureSeconds` in the future. The router form is attributed to a live run only when `operation` is `draft`, `review` or `answer`, `input_sha256` is 64 lowercase hexadecimal characters, `runId` matches `[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}`, and removing exactly one of the suffixes `.draft`, `.review`, `.mac-return` or `.answer` from `runId` gives the identifier of a run this sample verified live. The comparison is exact, never by prefix.

An attributed marker is that run's call in flight. An anonymous marker beside a live legacy route is that route's call. Every other marker means recovery is required. An unreadable marker is an error on the pipeline source.

### 5.8 Launcher readiness receipt

Size limit: `readinessBytes`. Its directory must be private. Exactly `schemaVersion` (integer `1`), `status` (`PREFLIGHT_COMPLETED`), `observedAtUnix` (number), `source` (`online-code-mode`), `client` (`null`) and `chatId` (`null`). Its state (`fresh`, `expired`, `absent` or `invalid`) is always reported as the setup state. It decides between `ready` and `inactive` only when no run is listed. It is never evidence of a model call or of a route.

### 5.9 Archive: `archive/<runId>.json`

Size limit: `archiveBytes`. The existence of `archive/<runId>.json` (not its content) decides whether a run that left its record behind is `archived-uncleared`, and whether a record that vanished during a sample finished cleanly.

For recent history the Monitor lists up to `archiveNamesScanned` names and shows the `archiveRunsShown` newest by modification time. An archive is shown only when `runId` is a run identifier and `result` is an object with `kind` `codemode.router.v1` and the same `runId`. Completion evidence is considered only from an archive with exactly the keys `schemaVersion`, `runId`, `inputSha256`, `result`, `exitCode`, `stage`, `checkpoint` and `finishedUnix`, and only when further checks on `result` pass. The fields read inside `result` are projected by `activity._project_archive`; they are not enumerated in this version of the contract.

For the last Jev judgment the Monitor reads the `judgmentArchivesRead` newest archives and uses the first whose `result.stages.intake` (or `stages.intake`) has `status` `JUDGED` and `kind` `chami.intake.typesafe.v1`. Its time is `finishedUnix`, else `recordedUnix`, else the file's modification time.

## 6. Liveness

Liveness comes from locks, never from a record's age and never from which processes have a file open.

**Per-run.** A run is live when all of these hold: its record is valid, `locks/` is a private directory, `locks/<runId>.lock` is a private file whose inode equals the record's `runLock.ino`, and a shared non-blocking probe of that file fails. A run with a note, no record and a held run lock is live and has not begun.

**Legacy.** A legacy record is live only when all four rules hold:

1. A shared non-blocking probe of `owner.lock` fails.
2. No file in `locks/` is held. If `locks/` cannot be listed, has more than `directoryEntriesScanned` entries, or holds an unsafe lock file, this rule fails.
3. The install state is `absent`, `installed` or `rolled-back`, no install is in progress, and the Monitor's own repair is not holding `owner.lock`.
4. `lsof` and `ps` name exactly one router work process that has `owner.lock` open read-write, has no file under `locks/` open, and started no more than `processStartWindowSeconds` before `startedUnix` and not after it.

The record must also satisfy `0 <= startedUnix <= recordedUnix <= now + 1` and must not be at stage `incomplete`. The record, the process and the lock are checked a second time before the run is reported live.

**Router work process.** The argument list is exactly five items: an interpreter, `-I`, `-B`, the absolute router script path, and `work` or `feedback`. The interpreter's file name is `python3`, `python3.10`, `python3.11`, `python3.12`, `python3.13` or `python3.14`, or it is a framework interpreter ending in `/Python.app/Contents/MacOS/Python` under `Python.framework` or `Python3.framework`. For a per-run run the process is attributed only for display: among the processes with the run's lock file open, the one work process that matches the note's `pid` (when there is a note) and the start window. An ambiguous or missing match leaves the run live with its process unknown.

## 7. Row states and projection

Each listed run is one row with one state:

| State | Meaning |
| --- | --- |
| `running` | Live. For a per-run run with a valid note, the note's phase is used instead |
| `waiting` | Live, and its note says it waits for a resource |
| `admitting` | Live, and its note says it is being admitted, or it has no record and no valid note |
| `unresolved` | Not live, record present, no archive |
| `archived-uncleared` | Not live, record present, archive present |
| `unverified` | The run lock is missing, replaced, unsafe or could not be probed, or it is held but the record that binds it was not read |
| `unreadable` | The record could not be read whole or fails the closed schema, even if its run lock is held |
| `changing` | Legacy only: the record, process or lock changed during the sample. It is checked again in the next sample and is not counted |

When several conditions apply, the first of these wins: live; run lock missing, replaced, unsafe or not probed (`unverified`); record unreadable (`unreadable`); run lock held beside a record that was not read (`unverified`); then `archived-uncleared` or `unresolved`. An unreadable record whose lock file is also missing is therefore `unverified`.

`waiting` and `admitting` rows are queued. Counts are `running` (live and not queued), `queued`, and `unresolved` (every row that is not live and not `changing`).

The single route older consumers read is the newest live row that is not queued, else the newest queued row, else the newest remaining row. A queued-only route is reported as `queued` and is never shown as processing.

One run identifier in both layouts is one row (the per-run one) only when the per-run record is valid, has the same `inputSha256`, and the legacy route is not live. Otherwise both rows are shown and the identifier is reported as a collision.

The counts are marked complete only when the journal is initialized, nothing was truncated, the observation has no error, no row is `unreadable`, `unverified` or `changing`, and every row is either verified live in this sample or has a checkpoint within `checkpointFreshSeconds`.

## 8. Failure behaviour

A missing or unusable reading is reported as unknown. It is never reported as idle, zero or none.

| Condition | Result |
| --- | --- |
| Journal root, `archive/`, `checkpoints/` or `owner.lock` missing or not private | Activity `unknown`. Pipeline `unknown` only when no run is listed; a listed run is still read and reported as its row says (for example status `unresolved`, source state `live`) |
| `active/`, `notes/` or `locks/` is a link, not private, or unreadable | Observation error "Router run directories are unsafe or unreadable"; counts incomplete; activity `unknown`. Only when `locks/` is the unsafe one is every per-run lock `unsafe` (rows `unverified`); an unsafe `active/` or `notes/` leaves run locks probed as usual |
| Record changing after `readAttempts` reads, unsafe, not JSON, over `recordBytes`, deeply nested, or outside the schema | That one row is `unreadable` (`unverified` when its run lock file is also missing or unsafe); other rows are unaffected |
| Record names another run than its file name | That row is `unreadable` and none of its fields are used |
| Listed record gone, archive present | No row: the run finished |
| Listed record gone, no archive, after `readAttempts` looks | One `unreadable` row |
| Run lock missing, replaced, unsafe or probe error | Row `unverified`, never live |
| Run lock held beside an unreadable record | Row `unreadable` with its lock reported held; never live |
| Note invalid, for another run, or for another input | Note ignored; a begun live run is `running`, a live run without a record is `admitting` |
| Note present, run lock free | No row |
| Policy invalid | Policy `null`, drain state `unknown` |
| Fence invalid or missing | Section 5.6; no legacy route is live |
| Marker unreadable | Pipeline source `error`; a live route stays `running` |
| More than `namesListed` names in a directory, or more than `directoryEntriesScanned` entries | `truncated`; counts incomplete |
| Readiness receipt invalid | Setup state `invalid`; activity `inactive` when no run is listed |
| One archive file unusable (unsafe, not JSON, deeply nested, or removed after the directory listing: before its stat, or before the mtime fallback) | That archive is skipped; the sample continues |
| Any other error inside the run observation | An empty observation with "Router run observation failed"; activity `unknown` |

`lsof` or `ps` unavailable, or timing out, leaves legacy liveness unverified (`unresolved`) and per-run process attribution unknown. It never makes a run live.

## 9. Freshness

| Reading | Rule |
| --- | --- |
| Live run | Proven by its lock in the current sample. A live legacy route whose last checkpoint is older than `checkpointFreshSeconds` is live and marked stalled |
| Run that is not live | Its checkpoint is fresh for `checkpointFreshSeconds`; after that the pipeline is `stale` |
| Readiness receipt | Fresh for `readinessFreshSeconds`; older is `expired`; more than one second in the future is `invalid` |
| Marker | Its age is shown; a start more than `markerFutureSeconds` in the future is not described |
| Observation | Computed once per sample. The activity reader reuses it only while it is a few seconds old |

## 10. Limits

| Name | Value | Meaning |
| --- | --- | --- |
| `recordBytes` | 3,145,728 | Largest run record |
| `noteBytes` | 4,096 | Largest note |
| `policyBytes` | 1,024 | Largest policy file |
| `fenceBytes` | 1,024 | Largest install fence |
| `markerBytes` | 4,096 | Largest pending marker |
| `readinessBytes` | 4,096 | Largest readiness receipt |
| `archiveBytes` | 3,145,728 | Largest archive |
| `directoryEntriesScanned` | 4,096 | Entries examined per directory |
| `namesListed` | 64 | Run names kept per directory, newest first |
| `fullRecordReads` | 4 | Per-run records read whole per sample, newest first |
| `extraLiveRecordReads` | 8 | Further records read because their run lock is held |
| `rowsProjected` | 8 | Rows in each projected list |
| `readAttempts` | 3 | Reads of a changing file |
| `readRetrySeconds` | 0.02 | Wait between those reads |
| `checkpointFreshSeconds` | 300 | Checkpoint freshness |
| `readinessFreshSeconds` | 600 | Readiness receipt freshness |
| `processStartWindowSeconds` | 660 | Largest gap between a work process start and its record |
| `markerFutureSeconds` | 5 | Clock tolerance for a marker's start |
| `largestExactInteger` | 9,007,199,254,740,991 | Largest integer accepted as a number |
| `archiveNamesScanned` | 4,096 | Archive names examined |
| `archiveRunsShown` | 12 | Archived runs shown |
| `judgmentArchivesRead` | 12 | Archives read for the last Jev judgment |

A run older than the newest `fullRecordReads` records is still listed with its lock state. If its run lock is held, its record is read as well, so the inode rule applies to every live row.

## 11. Fixtures and tests

[test_support/router_contract/](../test_support/router_contract/) holds two small journal writers and a fence writer. They are test doubles written from this page and the readers. They contain no router code and make no router decisions. The per-run fixture records a note when a run is claimed (`admitting`) and when it begins (`running`), because the dual-read cases expect a begun run to show the client and host its claim named; the Monitor itself never requires a note.

| Fixture | Writes |
| --- | --- |
| `per_run/pipeline_router_state.py` | The per-run layout: claim, begin, checkpoint, note, finish |
| `per_run/router_fence.py` | `install-fence.json` |
| `legacy/pipeline_router_state.py` | The legacy layout: begin, checkpoint, finish |

`test_router_contract.py` runs every router dual-read case in `test_telemetry.py` and `test_activity.py` against these fixtures, with no router installed. That covers the 45 cases that need a journal writer (39 in `RouterDualReadTests`, 6 in `DualReadActivityTests`) and the 6 that do not. It also checks that `contract.json` matches the readers' constants, that the fixtures write exactly the files and modes listed here, and that this page states the same values.

What a pass establishes: the Monitor reads a journal shaped as this page describes, and reports the listed failures as listed.

What it does not establish: that any router writes such a journal. The same cases in `test_telemetry.py` and `test_activity.py` still require the router project's own writers, and still fail without them unless `MONITOR_ALLOW_ROUTER_FIXTURE_SKIP=1` is set. Those skips are reported as skips. Router acceptance, a live route and a model call are NOT_RUN in this repository.

The `test_telemetry.py` cases attribute processes with `/usr/sbin/lsof` and `/bin/ps`, so their fixture run is macOS only and is skipped on other systems. The `test_activity.py` cases attribute no process. A case that starts a work process is skipped when the test interpreter's name is not one section 6 lists.

## 12. Versioning

The version is `MAJOR.MINOR.PATCH`, recorded at the top of this page, in `contract.json` and as `CONTRACT_VERSION` in the two journal writers (`per_run/pipeline_router_state.py` and `legacy/pipeline_router_state.py`); the tests check those two. The fence writer `per_run/router_fence.py` carries no version.

| Change | Version |
| --- | --- |
| A file, key, enumerated value or rule is removed or narrowed, so that a journal valid before is no longer read the same way | Major |
| A file, optional reading or enumerated value is added, or a limit is raised | Minor |
| Wording, examples or tests change with no change to what is read | Patch |

A change to the readers that alters anything on this page changes `contract.json`, this page and the fixtures in the same pull request.
