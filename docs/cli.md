# agiw command-line tool (v0.1)

`agiw` connects your Mac and Windows PC on the same LAN. Each machine runs a small peer link that
announces itself, pairs with your other machines through a 6-digit PIN, and answers paired machines with a
signed, read-only status: health and model names. It is standard-library Python 3.9+ with no dependencies, and
it reuses the suite's `peer_link.py` (protocol `agiw-peer/2`). The host-kind default follows the current OS.
`agiw status` reads this machine's own status through its own control route (`/control/status`), bounded to
one worker and 3 s; a stuck read cannot accumulate workers, block polling or slow `/control/peers`, which
the Mac monitor polls. Component `detail` leaves the machine only when it is exactly the fixed sentence for
the component's state; any other text stays local. Status `schemaVersion` must be exactly integer 1 (bool, float and
string values are rejected), and strict JSON parsing rejects duplicate object keys and non-finite numbers.
Pairing, keys, signatures and the trust store are unchanged.

**What v0.1 does not do.** Status is **signed but not encrypted**: anyone capturing LAN traffic can read the
model names and states your machines report. v0.1 **dispatches no jobs**: it discovers, pairs and reads
status, and no route starts, loads or changes anything on another machine. IPv4 LAN only. **Linux is not
supported:** on Linux every `agiw` command refuses with exit 2 and changes nothing.

## Install

Available once published (replace `<tag>` with a release tag):

```sh
pipx install "git+https://github.com/louiscalata/agiw-suite@<tag>"
# or
uv tool install "git+https://github.com/louiscalata/agiw-suite@<tag>"
```

From a checkout: `pipx install .` (or `python3 -m pip install --user .`). Check it with `agiw --version`.

## First run and pairing

Do steps 1–2 on every machine, then pair each pair of machines once.

1. **Allow the two ports** (see [Firewall](#firewall)): tcp/8768 for signed status and pairing, udp/8769
   for discovery.
2. **Start the peer link**, in a terminal you keep open: `agiw serve`. Or start it at every login:
   `agiw service install` (user level, no admin rights). What it reports depends on the machine:
   - **Mac:** the local LM Studio model list (`--lmstudio URL` to change it; default `http://127.0.0.1:1234`).
   - **Windows:** the Windows observer's status file, which you name:
     `agiw serve --status-file "<observer folder>\state\peer-link-status.json"` (turn on the observer's
     `peerLinkStatus` option). Alternatively, use `agiw serve --lmstudio http://127.0.0.1:1234`.
     Both `serve` and `service install` refuse on Windows without `--status-file` or `--lmstudio`.
3. **Pair.** On machine A, list what it sees, then invite B by the name shown:

   ```sh
   agiw peers
   agiw pair studio-pc
   ```

   A shows a PIN, valid for 5 minutes. On B, run `agiw accept` and type it (add A's name if several
   invites wait). Three wrong PINs end the invite. Nothing pairs without a typed PIN.
4. **Check:** `agiw status` on either machine.

The PIN feeds SPAKE2, so a LAN attacker gets one online guess per attempt and nothing to brute-force later.

### Mac

```sh
pipx install "git+https://github.com/louiscalata/agiw-suite@<tag>"
agiw doctor
agiw service install        # or: agiw serve
agiw peers && agiw pair <windows machine>
```

### Windows

```powershell
pipx install "git+https://github.com/louiscalata/agiw-suite@<tag>"
agiw doctor
agiw service install --status-file "<observer folder>\state\peer-link-status.json"
# Or: agiw service install --lmstudio http://127.0.0.1:1234
agiw accept
```

## Commands

| Command | What it does |
| --- | --- |
| `agiw serve` | Run the peer link in the foreground (what the service runs). `--host`, `--name`, `--status-file`, `--lmstudio`. |
| `agiw pair <name>` | Invite a discovered machine; shows the PIN here. |
| `agiw accept [<name>]` | Type the PIN another machine shows. |
| `agiw unpair <name>` | Forget a machine. Run it on the other machine too. |
| `agiw peers [--json]` | Discovered and paired machines. |
| `agiw status [--json [--full]]` | Contract state for the local machine, paired peers and every platform. `--full` includes projected source documents in JSON. |
| `agiw doctor` | Local read-only checks: Python, port occupancy, firewall hints, state folder and token privacy, service definition. |
| `agiw service install\|uninstall\|status` | User-level login service. |
| `agiw uninstall [--purge-keys [--yes]]` | Remove the service; optionally list, then delete, pairing keys. |

Global options: `--state-dir` (default below) and `--status-port` (default 8768). Exit codes: 0 done,
1 failed (including a peer link that is running but did not answer), 2 usage (Linux is refused with 2), and
3 the peer link is not running here. Every command that talks to the link uses them; only `status --json`
turns the last two into unknown rows and exits 0. Exit 75 applies to a busy state-folder lock only for foreground
`agiw serve`. The LaunchAgent and Windows task use `serve --service` and exit 0
on a busy folder. Key deletion with `uninstall --purge-keys --yes` exits 1 when the lock is held.

**Status meanings.** Each row is a `machine_state.machine_summary` row, the same shape the monitor's
`snapshot.machines[]` uses; the envelope's `contract` value `machine-state/1` names that row format.
`up` needs a fresh local or authenticated report with accepted `ok` or `degraded` health. `down` needs a
fresh report that explicitly says `down`. Silence and disconnected peers are `unknown`. Freshness uses the
document's own `observedAt`: 3 seconds for the local Mac and 30 for local Windows or any remote
authenticated report, inclusive, with at most 2 seconds of future clock tolerance. Connection time and
the CLI's sampling time never refresh an observation.

The human table has MACHINE, PLATFORM, STATE, REASON, AGE and MODELS columns. MODELS is the row's
`loadedCount`, or `?` when unknown; AGE is elapsed seconds or `?`. Local health comes from the observer
document in the authenticated control snapshot, rather than from the peer link answering.
Only `/control/status` (used by `agiw status`) reads the local provider, in a daemon worker with a
three-second deadline. `/control/peers` (used by `agiw peers`, the pairing commands and the Mac monitor) never
does and never includes `selfStatus`; polling and the `peers.json` snapshots it writes do not either. A timeout
returns `selfStatus: {"error": "local status timed out", "code": "timeout"}`. Other provider errors include
`code: "unavailable"`. While a read started by another request is still inside its deadline, a later request
gets `{"error": "local status read in progress", "code": "busy"}` (shown as `unknown/unavailable`); after the
deadline it gets the timeout answer, and no second read starts while one is stuck.

`agiw status --json` emits this schema-2 envelope (each `machines` element is one full contract row):

```json
{
  "schemaVersion": 2,
  "contract": "machine-state/1",
  "sampledAt": 1700000000.0,
  "observedAt": "2023-11-14T22:13:20+00:00",
  "protocol": "agiw-peer/2",
  "linkState": "running",
  "self": {"node": "local-node-uuid", "name": "my-mac", "host": "mac"},
  "machines": [],
  "signed": true,
  "encrypted": false
}
```

The example omits the row bodies for readability; actual output always includes at least one row for
each of `mac` and `windows` (a paired Linux peer also gets a row). Machines on the same platform keep
separate rows. Local and paired
rows use their node UUID as `id`, a reported machine identity as `host` when the document supplies one,
and the display name as optional `label`. Legacy documents put a platform kind in `host`; those rows use
their node UUID for `host` too. Missing-platform rows use the platform as both `id` and `host`.

`--json --full` adds `documents`, keyed by row `id`, containing the projected local or verified signed
documents used for those rows. These documents stay outside the rows; each row has only contract fields.
`signed` describes the peer transport, not a signature over the JSON envelope; local evidence has trust
`local`, connected signed reports have trust `authenticated`, and placeholders have trust `unverified`.

Connected peers still pass through health and freshness checks. This adapter retains no previous verified
document. A paired peer without an accepted status is `unknown`, and the reason follows the monitor's rule:
a peer whose link state is `connected` but whose signed status was missing or rejected is `unavailable`; a
peer never heard (no hello yet) or heard but never sent a status (`connecting`) is `not_observed`; a peer
that answered before and went silent (`stale`, or `offline` after a hello) is `not_connected`. The monitor's own PC row reads `not_configured` only when no PC is paired, no PC report
is on the share and no reader failed; a paired PC that has not been heard stays `not_observed`. A peer's last
fetch error and the link's `pollError` are diagnostics; they never choose the word. Unpaired available
machines have no row, and transport diagnostics do not replace an accepted document. A local `selfStatus` error with machine-readable `code: "timeout"` becomes `unknown/timeout`;
all other local provider errors become `unknown/unavailable`. Error text is not used to select the reason.
Both keep `linkState: "running"`: the peer link answered the control request.

The producer selects the health vocabulary: exact `agiw-linux-observer` uses the Linux table (`up` becomes
`ok`); the Windows producer is `agiw-win-observer <version>[+<build provenance>]` and uses the Windows
table. All other producers use the CLI table. CLI/Windows `unknown` and Linux `unknown` become
`unavailable`; an unlisted health word becomes `schema_invalid`.

Compared with schema 1, silence never implies `down`, the Mac and Windows rows are always listed (a paired
Linux peer also gets its own row), and unknown counts are JSON `null`. Counts require
`loadedListComplete: true`, a list with every residency `loaded` or `inactive`, and a matching `modelsTotal`
when present. A list alone establishes no count. The LM Studio provider does not supply completeness
evidence, so its counts stay null; the Windows observer's presence sets it when every lane is listed with a
known residency. A complete empty list gives zero. The CLI reads no token rates, so both rate fields stay null.

`linkState` describes the local control exchange, separately from machine health:

| Value | Meaning | `status --json` | Human output |
| --- | --- | --- | --- |
| `running` | The link returned a usable control snapshot. | Contract rows from that snapshot; exit 0. | Machine table; exit 0. |
| `not_running` | Connection refused or no listener on the status port. | Two unknown rows: local `unavailable`, the other expected platform `not_observed`; exit 0. | Start-the-link message; exit 3. |
| `unreachable` | The control exchange timed out, reset, or could not be read or decoded. | Two unknown rows: local `timeout` for a timeout, otherwise `read_error`; the other expected platform `not_observed`; exit 0. | `the peer link is running but did not answer: <short reason>`; exit 1. For `pair`, `accept` and `unpair` the line adds `; the request may or may not have taken effect; run `agiw peers` to see`, because a write with no readable answer may still have been applied. |

Without a usable snapshot, `self.node` uses the detected platform as a fallback identity and `--full`
contains an empty `documents` object. Control authentication and permission failures retain their normal
error behavior, including exit 1 for both JSON and human output.

`status --json` and `peers --json` print unredacted values for scripts, including LAN addresses and raw
error strings wherever those fields are included. Text output is redacted. Treat JSON captures as local
diagnostics and remove private values before sharing them.

All commands except `serve`, `doctor`, `service` and `uninstall` talk to your own peer link on
127.0.0.1. It must first prove that it holds the control token in your state folder, so another program on
the port learns nothing.

`agiw doctor` never contacts the peer link. It observes local file metadata and whether the status TCP and
discovery UDP ports are in use. A busy status port is reported as probably the peer link, without proving
which program owns it. Use `agiw status` for the live check.

## Firewall

agiw never changes firewall settings; `agiw doctor` prints these hints.

- **macOS:** with the firewall on, allow incoming connections for the Python that runs agiw
  (System Settings > Network > Firewall > Options).
- **Windows:** add inbound rules yourself, Private profile, in an administrator PowerShell:

  ```powershell
  New-NetFirewallRule -DisplayName "AGIW peer link TCP" -Direction Inbound -Protocol TCP -LocalPort 8768 -Profile Private -Action Allow
  New-NetFirewallRule -DisplayName "AGIW peer link UDP" -Direction Inbound -Protocol UDP -LocalPort 8769 -Profile Private -Action Allow
  ```

## Service management

`agiw service install` writes one definition under your own folders and starts it; nothing asks for admin
rights, sudo or UAC. The resolved absolute state folder is always stored in the definition, including when
you use the default. Options you pass (`--status-file`, `--name` and so on) go into it too.

| OS | Definition | Started with |
| --- | --- | --- |
| macOS | `~/Library/LaunchAgents/io.github.louiscalata.agiw.peer-link.plist`; stdout and stderr in `~/Library/Logs/agiw/peer-link.log` | `launchctl bootstrap gui/<uid>` |
| Windows | Task `\AGIW\PeerLink-<SID>` (your account's SID, logon trigger, least privilege); XML copy under `%LOCALAPPDATA%\AGIW\service\` | `schtasks /Create /XML`, then `/Run` |

On Windows, inspect
the task in Task Scheduler (state, Last Run Result and task history when enabled); agiw defines no stdout
or stderr log file for the task. The macOS LaunchAgent writes both streams to the log in the table.

On Windows, agiw reads your SID with `whoami /user /fo csv /nh` and uses that same account-specific task
name for install, status and uninstall. Installation refuses if the SID cannot be read. After `/Run`,
installation checks the locale-independent ScheduledTasks State enum through PowerShell for up to about
five seconds. It reports running only after observing `Running`; otherwise it reports the installed task
as not confirmed running, gives the last state or query failure, and exits 1. Status and uninstall use the
same State query; a failed query leaves status unknown and keeps the definition during uninstall.

`agiw service status` reports whether the definition exists and whether the service manager has it loaded
and running. `agiw service uninstall` stops it and checks that launchd no longer holds the job on macOS,
or that the task is stopped on Windows, before removing the definition. A waiting or exited
LaunchAgent is still loaded. If removal cannot be confirmed, agiw keeps the definition and reports how to stop it.

## State folder

Identity, control token and one trust record per paired machine, private to your account:

- macOS: `~/Library/Application Support/AGIW/peer-link`
- Windows: `%LOCALAPPDATA%\AGIW\peer-link` when `LOCALAPPDATA` is absolute, otherwise
  `~/AppData/Local/AGIW/peer-link`, with link keys sealed by DPAPI and pairing generations in
  `HKCU\Software\AGIW\PeerLink`. The service definition uses the same `LOCALAPPDATA` fallback.

`agiw doctor` checks the folder's owner and permissions and the privacy of existing key files without
reading or unsealing their contents. It never repairs state or contacts the peer link; `agiw status` is the
live check. `agiw serve` repairs the state when started.

`agiw serve` holds an exclusive process lock for its whole run and refuses to start if that state folder is
already locked. The lock file sits next to the state folder, with `.agiw-lock` appended to its name (for
example, `peer-link.agiw-lock` beside `peer-link`), so it does not enter the peer link's state-file checks.
Lock refusal exits 75. The LaunchAgent and Windows task pass `serve --service`,
which prints the same lock-refusal message and exits 0. The LaunchAgent throttles crashes to 60 seconds;
the Windows task retries failures up to three times, one minute apart.

## Uninstall

```sh
agiw uninstall                       # remove the login service; keys stay
agiw uninstall --purge-keys          # removes service; key deletion is a dry run
agiw uninstall --purge-keys --yes    # deletes them (stop any running `agiw serve` first)
pipx uninstall agiw                  # or: uv tool uninstall agiw
```

`--purge-keys --yes` deletes only files the peer link creates and leaves links, junctions, reparse points and
unrecognized files in place. It first removes the login service, then must acquire the same process lock as
`agiw serve` before deleting keys. If the lock is held, it reports the service manager's result (removed,
or none installed) and that no key file was deleted, even when that process uses
custom ports, and exits 1. The dry run lists files without taking this lock. Deletion holds the
lock through deletion and removes the lock file after the state folder is gone. The selected and default
status TCP ports and the discovery UDP port must also all be free. Existing control tokens must pass the
privacy and validity checks; unsafe tokens and busy or unknown ports prevent key deletion. These checks
make no peer-link exchange. `--yes` requires `--purge-keys`.

Purge protects against `agiw serve` and the login service; if you started peer_link.py directly, stop it before purging.

If a key file cannot be deleted, purge reports the file and reason, continues deleting the others and
exits 1. On Windows it still attempts to revoke this account's pairing generations in the registry. If
revocation fails, it prints the file-deletion summary first, then the registry error and reason, and exits 1.

If you started the peer link with a non-default `--status-port`, pass the same `--status-port` to uninstall,
for example `agiw --status-port 8878 uninstall --purge-keys --yes`.
Your other machines keep their side: run `agiw unpair <this machine>` there.

## What leaves the machine

Discovery hellos (unsigned): node id, machine name, host kind and port. Signed status, to paired machines
only, after cutting it down to an allowlist: host name, platform, health, timestamps, model ids with
loaded/activity state, a few GPU and memory numbers when the status source provides them, and component
id, label, state and age. Component `detail` is sent only when it is exactly the fixed sentence for the
component's state; any other text, which can contain a path or account name, is dropped.
The allowlist does not include files, prompts, keys or path fields; allowed strings such as model ids
and component labels are still supplied by the source and should be safe to share. `agiw doctor` prints
your home folder as `~` and never prints keys, tokens or share paths.

## Tests

Run from the repository root. [CLI CI](../.github/workflows/cli.yml) runs this suite on macOS and
Windows with Python 3.9 and 3.12:

```sh
python -B -m unittest discover -s tests_cli -v
```

Use `python3` in place of `python` where needed. Run the peer-link regression suite alongside it:

```sh
python -B -m unittest test_peer_link
```

Service tests use fake command runners. Provider-selection tests in `test_host_kind.py` replace
`PeerLink` and `directed_broadcasts`, not the status factories: the factories select providers without
calling the returned providers. `LinuxStatusSource` separately reads temporary status files without sockets.
Two CLI tests use real loopback sockets: `test_loopback_pairing.py` runs two in-process peers on ephemeral
`127.0.0.1` ports to exercise PIN pairing and signed status; the
`test_purge_real_probe_refuses_a_listening_loopback_status_port_without_token` test in `test_cli_review.py`
uses a real loopback listener and port probe. The peer-link suite also contains socket integration tests.
If a sandbox denies port creation, report those cases as `NOT_RUN` with the denial message, and retain the
command's actual exit code and test counts. A blocked socket check does not establish integration success.

The hosted Windows CI runner is elevated. In `LoopbackPairing.setUp`, only in that elevated session, the
test wraps `_win_owner_and_allow_sids` to treat an Administrators owner as the current account. This
bypasses the owner precondition in `_win_restrict_dir` and `_win_check_private` for the pairing fixture;
it preserves the native DACL and reparse checks and the real pairing, DPAPI, key and signature checks.
The patch is removed by test cleanup, an assertion limits it to elevated Windows, and the existing
`test_peer_link` ownership-refusal tests keep the real checks. The CLI suite also simulates elevation
and verifies that non-elevated Windows and other platforms receive no owner patch.

The broader source checks documented in [Development](development.md#source-checks) are:

```sh
MONITOR_ALLOW_ROUTER_FIXTURE_SKIP=1 PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -p 'test_*.py' -q
node --test test_*.mjs
```

The allowance permits the documented router fixture skips when the separate project's writers are absent;
report actual skips alongside passes. These source checks do not establish a live model route or native
platform acceptance. CLI CI separately installs the package and checks `agiw --help`, `agiw --version` and
`python -m agiw --version`; it does not run the separate peer-link command above.
