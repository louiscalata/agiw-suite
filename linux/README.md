# Linux inference observer (retired)

**Retired.** The Linux Mac Pro 5,1 is not an AGIW machine. Do not run `linux/install.sh`, start the coordinator, or install PAIR for this node. AGIW runs on the Apple-silicon MacBook Pro and the Windows PC only. On that PC the router is port 1234, qwen3.8-27b is port 1236, and gpt-oss-20b is port 1245. Port 1235 is not in service. The rest of this file is historical and is not a setup guide.

A standard-library, read-only Linux edition. It observes AMD sysfs/hwmon, `/proc`,
PAIR's local llama.cpp router, the mapped on-demand GPU wrapper, and bounded
historical state records. It does not load, unload, repair, start, stop, or pause
models or change scheduler authority. The canonical project roadmap is
[../roadmap.md](../roadmap.md).

This workstream has fixture verification on macOS. Native Linux installation,
systemd activation, hardware accuracy, reboot survival, six-hour soak, remote
transport, and the combined three-machine UI remain separate acceptance work.

## Commands and transport

On the Linux node, after acceptance:

```sh
# The forced command must use an absolute path and accepts no arguments:
/ABSOLUTE/RELEASE/agiw-linux-once
/usr/bin/python3 -I -B linux/agiw-linux-status --serve
# Optional read-only JSON API, bound to 127.0.0.1 only:
/usr/bin/python3 -I -B linux/agiw-linux-status --serve --port 8766
```

`--once` returns one schema-1 `agiw-peer` document with `platform` and `edition`
set to `linux`. It reuses a private, schema-valid heartbeat at most ten seconds
old without refreshing its observation time. Otherwise it takes two samples one
second apart. Output is capped at 65,536 bytes including the newline. A 3.6-second
sampling deadline returns an unknown document on expiry; nonblocking stdout has
an overall 3.85-second deadline from command dispatch. A blocked or broken output
pipe exits 1; an ordinary unknown observation still exits 0. The dedicated
`agiw-linux-once` forced-command launcher starts `/usr/bin/timeout --signal=KILL
3.8s` before `/usr/bin/python3 -I -B`, covering interpreter startup and imports
as well as sampling and output. It takes no arguments and ignores the SSH
client's command. Use the installed absolute path
`/ABSOLUTE/OBSERVER/current/agiw-linux-once` as the forced command. No Python
interpreter or directory-resolution command runs before the native watchdog.
Missing `/usr/bin/timeout` exits 69; extra arguments or a relative invocation
exit 64. A deadline kill exits nonzero and may leave no complete document; the
remote reader must treat that as unknown, never down or zero. The underlying
`agiw-linux-status --once` remains available for direct diagnostic use, but its
in-process timer excludes interpreter startup. Both paths use absolute Python
in isolated mode, independent of PATH, PYTHONPATH and user site. Cold installed
Linux timing still needs measurement before accepting the four-second target.

`--serve` samples and publishes every five seconds without opening a listener.
It replaces `~/.local/state/agiw/linux-status.json` atomically with mode 0600.
`XDG_STATE_HOME`, when absolute, replaces `~/.local/state`; a relative setting is
ignored. The final `agiw` state directory must be owned by the user and mode 0700.
Neither `--once` nor any probe writes state. The service publisher writes only
its state directory, using a pinned directory descriptor and nofollow opens.
Python bytecode writes are disabled by the entry point and service command.

The optional API exposes only `GET /api/snapshot` (full local detail) and
`GET /api/status` (projection). It checks Host and Origin and rejects mutations.
It does not serve the Mac/PC dashboard assets. No LAN listener is opened by this
edition. A forced-command SSH key and peer-link pairing are deployed separately;
this installer does not edit SSH files, firewall rules, or peer trust.

The existing `peer_link.py` now defaults on Linux to
`${XDG_STATE_HOME:-$HOME/.local/state}/agiw/peer-link`. Its existing Linux POSIX
owner/mode checks do not inspect POSIX extended ACLs. Native peer-link ACL and
pairing acceptance remain outside this observer workstream.

## Observation contract

- Machine and lane health use `up`, `down`, or `unknown`, with a reason. Failed
  reads, refusals, missing files, malformed data and timeouts remain unknown.
  A locally read boot ID establishes that the machine was observed; incomplete
  GPU identification keeps machine health unknown. Machine health does not
  claim that every lane is healthy.
  Known platform/virtual non-PCI DRM devices are skipped while scanning AMD
  cards; an unreadable PCI candidate or an unrecognized device layout still
  makes GPU completeness unknown.
- PAIR requires its stable mapped process identity and corroborating sole
  loopback-listener ownership before allowing exact
  `GET http://127.0.0.1:8081/health` and `/models` URLs.
  Queries, fragments, redirects, alternative ports/hosts, proxy settings, and
  model-bearing requests cannot expand that allowlist. There are no Ollama or
  PAIR-proxy requests. PAIR model lifecycle comes from its local router listing;
  activity and prompt/generation rates remain unknown.
- A GPU-wrapper probe requires the exact mapped executable
  `/srv/storage/work/claude-llama-vulkan/bin/llama-server`, an explicit loopback
  host, a validated non-8081 port, stable process start identity, matching sole
  listener ownership, and a sanitized model identity. Only its
  `/health`, `/v1/models` and `/slots` endpoints are allowed. Generation rate
  needs two polls of the same boot/PID/process-start/port/slot/task. It is not
  carried across a process or task change, and a missing process start identity
  prevents endpoint probing. Every busy slot needs valid two-poll evidence;
  a partial sample keeps the whole lane's rate unknown. Prompt live rate is unavailable.
- PAIR's Ollama row is passive: a stable mapped process owning its loopback
  listener sets `processPresent` and `listenerPresent`. Service health and
  status remain unknown with reason `application-health-unverified`, even if
  the process owns the port. Missing or ambiguous ownership stays unknown.
  No Ollama endpoint is queried.
- `last-code.log` and `last-extract.log` are read from
  `/srv/storage/work/claude-llama-vulkan/logs/`. Parsed prompt and generation
  rates are historical, with file age. The actual model identity is unknown;
  the dated role map supplies a separately labeled, unverified expected model.
- AMD cards are discovered by PCI vendor and amdgpu driver, not by card number.
  The GPU label uses PCI IDs instead of an assumed RX 570/RX 580 marketing name.
  VRAM bytes become MiB, temperature millidegrees become Celsius, and
  `power1_input` microwatts become watts. Every measurement retains its source
  in both full detail and the peer projection. Projected sources accept only
  the matching sysfs measurement path; missing hwmon readings are null. GPU
  utilization is instantaneous. Virtual file metadata size never substitutes
  for a bounded read. Failed, capped or partial card identification publishes
  `gpus: null`, `gpusComplete: false`, and `gpusState: "unknown"`; a successful
  empty scan publishes `gpus: []` and explicit completeness. Projection and
  cache reuse also reject incomplete GPU row shapes as complete inventories.
- CPU utilization needs two samples from the same boot. Memory uses the PC's
  portable available/commit thresholds, escalated by memory PSI. `some avg10`
  at least 1 gives watch; `some` at least 10 or `full` at least 1 gives tight;
  `full` at least 10 gives critical. Missing memory stays unknown.
- The full snapshot carries boot, coordinator, authority records, disks,
  temperatures, GPU lock holder, exposed listeners, local caller names, NIC
  counters, historical rates, and PAIR's attributed cluster view. `/proc/locks`
  and the GPU lock's metadata are read; the lock is never opened or acquired.
  Incomplete process/socket ownership is explicit. Raw arguments, environment,
  prompts, and arbitrary journal fields are not returned. Environment files are
  not read. Unconfirmed authority-journal string schemas are omitted entirely,
  including reasons and run IDs; only bounded numeric counters and age leave
  those journal readers, including through the full local API.
  Caller lane labels require the matching lane process identity and a sole,
  stable loopback listener owner, plus the reverse server connection. A foreign
  service on a familiar port never establishes that lane's callers.
- A loaded PAIR model together with an observed GPU-wrapper process produces a
  CPU contention warning and an owner next step. The observer takes no action.
- Coordinator state is `active`, `disabled`, `absent`, or `unknown`, alongside
  three-state health. Both user and system unit observations and the process
  inventory must be complete. On native Linux, two fixed `/usr/bin/systemctl`
  read-only `show` commands query only `agiw-coordinator.service`; each has a
  0.35-second deadline and 4,096-byte cap. Failures and partial properties stay
  unknown. Fixture roots and other platforms do not execute these commands.
  The main state follows the intended system v8 unit and its configured full
  release hash, using the reviewed `coordinator_v8.py` invocation. The retired
  v7 user unit remains a separate observation; it cannot turn an absent intended
  system unit into disabled or active. A live retired process is accounted for
  only when its runtime, release and PID match the active user unit's MainPID.
  Another process with identical runtime arguments remains conflicting evidence.
  An active intended v8 unit likewise needs its observed MainPID to match the
  configured runtime and full release hash. A missing or mismatched process,
  or an additional unaccounted coordinator, keeps state and health unknown.
  Wrong or ambiguous release identity stays unknown. Unit command text and
  upgrade arguments are not published. These
  identity checks describe configuration, not installed-file hash verification.
  The projection preserves coordinator state separately from health, including
  through heartbeat reuse. An unobserved GPU wrapper remains unknown. The qwen35
  profile is labeled as staged historical context; promotion and serving are
  not inferred. A fresh service heartbeat and live fallback are distinguished:
  fallback shows `heartbeatUnit: unknown` with a reason, even if the node itself
  answers. This is not proof that the systemd unit is down.
- The complete model count remains unknown because CPU-role/qwen35/Ollama
  serving inventory is not fully observed. A partial list never becomes zero
  loaded models. Only allowlisted fields leave through the peer projection.
  An exact total additionally requires `modelInventoryLanes`: a bounded, unique
  list of all inventory-producing PAIR/wrapper lane IDs, each matched by healthy
  lane evidence. Missing source rows or witnesses keep the total unknown, including
  after cache reuse. Public model ID collisions remain unknown, and lane-wide
  activity is not attributed to several model identities sharing one lane.
  `historicalRates` has at most one row per `code`/`extract` role, with separate
  prompt/generation rates, the original observation time and age at sampling.
  Actual model identity remains unknown; the expected role model is explicitly
  unverified. Consumers add the document's age to the historical sample age.
  `network` carries at most 32 listener rows (port, basename, exposure and
  plaintext flags) and 32 caller rows (basename, lane and connection count).
  `callersReason` explains caller visibility independently of listener health.
  Missing, malformed or truncated inventories remain unknown. Addresses, PIDs,
  private paths, arguments, boot IDs and authority records stay local. The
  heartbeat reader revalidates these shapes and reprojects nested fields before
  reuse; it never refreshes observation time. The planned Mac SSH consumer is
  outside this worktree and must preserve and validate this same contract.

The `lanes.py` portable functions are vendored from `windows/agiw_win/probes.py`.
Parity tests compare both function ASTs and identical behavior fixtures. The
Linux live-rate function intentionally adds complete-busy-slot validation;
its single-slot parity fixtures and separate multi-slot regressions are tested.
Windows
status helpers are retained for parity; Linux production publication uses its
own projection and does not inherit Windows' down-on-no-lanes behavior.

## Staging and rollback

Run only on Linux as the intended user, after Claude's review and accepted
release selection:

```sh
sh linux/install.sh --commit FULL_COMMIT_HASH
# Activation needs the separately reviewed, matching idle-analyzer successor:
sh linux/install.sh --commit FULL_COMMIT_HASH --enable
# Return to a retained, verified release:
sh linux/install.sh --rollback PREVIOUS_FULL_COMMIT_HASH --enable
```

The installer refuses root, non-Linux hosts, dirty source checkouts, mismatched
commit labels, linked paths, unsafe ownership/modes and unexpected release files.
It stages under `~/.local/share/agiw-linux-observer/<commit>/` and verifies content
hashes. In a Git checkout, every runtime file must match its blob at the selected
commit, including files Git status would ignore. Exported trees require a
caller-supplied full hash, are explicitly labeled unverified, and cannot activate.
Older releases are retained.

Default installation stages only the release, including its unit template; it
never repoints `current`, replaces the active unit, or invokes systemctl. A
`--rollback` without `--enable` only verifies/selects the retained release.
Activation and rollback with `--enable` require local Git objects, a successful
`/usr/bin/git verify-commit` using the host's configured signer trust, and byte-for-byte
runtime matching against that commit. All Git and systemctl commands use fixed
`/usr/bin` paths. Activation additionally requires a verified observer allowance
in the pinned idle analyzer. The retained analyzer rejects unlisted same-user
processes, including this Python observer. Its passive-attestation replacement
allows only the exact coordinator PID; it does not grant an observer exception.
The source-only successor in `agiw_linux/idle_allowance.py` preserves that gate
and adds an exception for one exact observer service PID. It requires the
reviewed observer bytes, release, argv, interpreter, user, cgroup and invocation
identity, and rechecks identity after process enumeration. Other Python
processes and transient `--once` helpers retain the original blocking behavior.
If systemd explicitly confirms no resident observer, the successor runs the
historical gate without any process exemption and rechecks absence afterward.
An unknown manager state or an unverified resident observer still refuses idle
analysis; absence alone never declares the machine idle.
The historical passive coordinator checks also remain in force; this successor
does not declare their old coordinator pins current or establish full-idle
acceptance.

`bundle(home, base, commit, files, baseline, *, config_home=None, state_home=None)`
returns the successor's reviewable
file bytes without writing or installing anything. `files` is the selected
observer release's relative-path-to-bytes mapping; `baseline` contains the four
exact pinned historical files named in `PINS`. The generated launcher embeds
the observer hashes and pins the additional attestor. The successor belongs at
`~/.local/share/agiw-runtimes/idle-analysis-observer-COMMIT/`, with its generated
`agiw-idle-analysis.service` installed separately after review. This monitor
installer does not deploy analyzer runtime files. Automatic failure rollback
can restore the unit for an already retained, verified analyzer pairing.
HOME and XDG paths share the installer's restricted alphabet, including spaces;
the generated analyzer unit quotes its working directory and launcher path.

Before activation and again after daemon-reload, the installer verifies the
successor's actual bytes and exact unit file against the signed observer
candidate. It requires an inactive analyzer, the expected effective fragment
and command, no drop-ins, and `NeedDaemonReload=no`. Missing, modified, stale or
different-release pairings refuse activation without a flag, environment or
caller-receipt bypass. Fixture tests exercise the production verifier through
both install and retained-release rollback, with Git and systemd mocked at the
OS boundary. They do not establish native installation or effective manager
state. Before an upgrade, the installer also authenticates the prior observer's
signed blobs and retained analyzer runtime, deriving its exact unit with that
release's builder. A missing or modified prior pairing withholds the upgrade
before promotion. After failed activation it restores both observer and analyzer
units, reloads systemd, checks the prior observer's fresh heartbeat when it was
active, and verifies the effective restored analyzer pairing. A pairing failure
is reported as incomplete rollback. First-install rollback removes the observer;
the remaining successor can then use the unmodified historical gate.

With the pairing verified, the transaction promotes the unit and `current`,
runs `systemctl --user enable --now`, and restarts an
already active observer on upgrade. It rejects user and effective systemd
drop-ins, and requires the expected loaded unit after daemon-reload.

Activation succeeds only when the unit is active and a private heartbeat is
published after activation, identifies the selected release, and reports its
publisher running, within a 15-second polling deadline. Process identity is
captured at import so an old process cannot adopt a repointed `current` label.
The unit uses `ProtectSystem=strict`,
`ProtectHome=read-only`, state-only `ReadWritePaths`, `MemoryMax=128M`, and
`CPUQuota=10%`. XDG data/config/state locations are supported only below the
user's real HOME, with safe path characters and ownership. Existing state-folder
permissions are not silently changed. Lingering is not enabled by the installer.

Exception rollback restores the prior link/unit and compensates for service
changes. A previously active release must return to active state and publish a
fresh heartbeat naming that prior release after restoration. A successful start
command alone is insufficient; failed proof reports `rollback incomplete`.
Stale install locks and incomplete transactions fail closed for manual
inspection. Power-loss recovery and a hostile process already running as the
same UID are not isolation guarantees.

## Fixture tests

From the repository root, with a fresh private TMPDIR for each suite. Keep the
temporary directory inside the worktree for bounded runs that permit no other
writes, and resolve its path for the no-symlink guards:

```sh
mkdir -p .scratch/fix6
task_tmp=$(mktemp -d "$PWD/.scratch/fix6/linux.XXXXXX")
task_tmp=$(cd "$task_tmp" && pwd -P)
TMPDIR="$task_tmp" python3 -B -m unittest discover -s linux/tests
# Repeat with /usr/bin/python3 and another fresh directory.
/bin/sh -n linux/install.sh
/bin/sh -n linux/agiw-linux-once
```

Fixtures cover sysfs/hwmon, proc/PSI/memory, processes and socket ownership,
portable PC parity, strict PAIR transport, historical rates, symlink/FIFO/parent
replacement refusal, private atomic publishing, cache corruption/freshness,
one-shot time/output caps, installer root refusal/rollback, and end-to-end
collection through the isolated entry point. Installer tests mock platform,
identity, Git and systemctl; they never install or run a real service. The real
loopback HTTP test reports a skip when the sandbox refuses socket binds.

## Read-only confirmations for Claude

1. Confirm the inferred PAIR membership file location
   `~/.config/Nvidia Corporation/Personal AI Router/cluster/members.json` and its
   actual vendor schema. The observer accepts a bounded list or `members` list,
   labels it “PAIR says”, and exposes `pathConfirmed: false` until this is checked.
2. Confirm the authority journal key names beneath
   `~/.local/state/codemode-router/scheduler-authority/{epochs,admission,pauses}`
   and `install-fence.json`. Records carry file age and never establish present
   coordinator activity. Terminal record schema and a passive idle-analysis
   record path were not supplied; both remain explicitly unknown.
3. Confirm coordinator argv layout under the mapped
   `agiw-runtimes/linux-scheduler-authority-*` releases, qwen35/CPU-role runtime
   process identities and serving ports. No endpoints are assumed for those
   staged lanes. Verify the actual systemd property responses for both managers;
   only read-only unit inspection is performed, never the coordinator CLI.
4. Confirm the PSI escalation thresholds, the wrapper's explicit host and model
   flags, and hardware/source accuracy. Interface IP addresses are not available
   from `/proc/net/dev`; only NIC counters are implemented.
5. Review and deploy the exact idle-analysis successor separately, then verify
   its effective native unit and release pairing before observer activation.
   Source fixtures now exercise a passing production guard, including explicit
   retained-release rollback; the previously unconditional refusal is removed.
   Native activation, systemd hardening, lingering, private state permissions,
   rollback, reboot survival and soak remain unverified. The source-only builder
   requires the retained `idle-observer-native-readback-20261012` baseline:
   `runtime/analyzer.py` (SHA-256
   `ef068501d46d4d74b854da173ebcdbb3cf4a9fb01ec9e3ecc209743bd70ea5a8`)
   rejects Python through `process_gate`; `observer/passive_attestation.py`
   (SHA-256 `9a0a56ced049137904f637edca6e39c5489bd99d09dcb0b450d964a046d32571`)
   exempts only the attested coordinator. Fix pass 6 verified the generated
   successor's hash checks, imports and gate replacement against the retained
   baseline bytes without running the analyzer's main function. Its historical
   coordinator pins remain unchanged and may independently refuse idle work.
   Fix pass 7 checks the actual allowance gate after simulated first-install and
   upgrade failures at enablement and heartbeat validation. The fixtures use
   synthetic baseline/kernel data and mocked Git/systemd; they do not execute
   the historical analyzer's full main function or establish installed evidence.
   Review the safe default of withholding an upgrade whose prior analyzer
   runtime is unavailable, and verify both native analyzer outcomes and restored
   observer heartbeats, including the effective command for paths with spaces.
   For the Low deadline finding, use `agiw-linux-once` as the forced command and
   measure a cold, uncached installed invocation on Linux before accepting L1.
   The four native-watchdog tests are skipped on macOS, where `/usr/bin/timeout`
   is unavailable; real startup timing and descendant termination remain open.
6. Review integration of the Linux projection in A1/A4. This workstream does not
   make the Mac/PC combined UI or signed peer transport accept a new schema.
