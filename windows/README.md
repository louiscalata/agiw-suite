# AGIW Inference Monitor · Windows edition (0.2.0, preview)

The Windows counterpart of the Mac menu-bar monitor. A notification-area icon supervises a loopback Python
observer that serves **the same dashboard** as the Mac edition, in its `host: "windows"` mode.

| | Mac edition | Windows edition |
| --- | --- | --- |
| Shell | Swift menu-bar app | PowerShell tray icon (`agiw-monitor.ps1`) |
| Local models | LM Studio inventory + `lms ps` | The PC's llama-server lanes (`/health`, `/v1/models`, `/slots`) |
| Activity | LM Studio phase | Slot `is_processing`, decoded tokens → busy / generating |
| Identity | Exact LM Studio keys | Served model must equal the declared lane alias, or the lane reads **identity mismatch** |
| GPU | Apple GPU via ioreg | Every adapter's load and memory from Windows GPU counters; NVIDIA temperature and power from `nvidia-smi` |
| Memory | macOS pressure + guard | Physical memory available and commit charge (no pause/resume guard) |
| Peer | Windows worker heartbeat | The Mac's own presence on SharedChami (unsigned, expires after 30 s), attributed only to the Mac named in `hosts.json`; nothing is sent to the Mac |
| Controls | Guarded load/unload, repair | **Read-only**: nothing loads, unloads or restarts a model |
| Peers | — | AGIW instances meet on SharedChami (`llm-lab/status/agiw-peers/<host>.json`); opt-in in `config.json`. Linux is retired, so a share file cannot become a machine row |
| Machines | — | `machines[]`: this PC and the Mac, each up / down / unknown with a named reason (unknown is never shown as down). The Linux node is retired |
| NVIDIA PAIR | — | Uninstalled. A leftover PAIR row is process and listener ownership only; lane polling requires a positively pinned AGIW process identity and parks for every other owner. PAIR's llama-server is never counted as a lane |
| Hands off | — | `handsOff`: a GPU above 40 %, a full-screen game/app, presentation mode or a Windows Store app, with the cause (AGIW lanes, other processes, mixed, unknown) |

## Run

Double-click **`AGIW Monitor.vbs`** (or `AGIW Monitor.cmd`). The tray icon appears and the dashboard opens in an
Edge app window at `http://127.0.0.1:8767/`. If 8767 is held by something else the observer stops and the tray
says so, rather than moving to another address.

Tray icon colours: **blue** a lane is working · **green** both lanes confirmed idle · **amber** a lane does not
answer, is loading, serves the wrong model, or memory is tight · **grey** starting, stale, or activity unconfirmed. Right-click for *Restart observer*,
*Start at sign-in* and *Open log folder*.

One-shot check without the tray:

```powershell
python agiw_observer.py --once           # one snapshot as JSON
python agiw_observer.py --port 8767      # observer only; open the URL it prints
```

## Requirements

- Windows 10/11, Windows PowerShell 5.1 (built in) or PowerShell 7.
- Python 3.9+ (found at `%LOCALAPPDATA%\Programs\Python\Python31x\python.exe`, on `PATH`, or `AGIW_PYTHON`).
  The tray checks this minimum; `install.ps1` copies source without choosing or installing an interpreter.
  The recorded PC environment uses Python 3.13. Python 3.14 test results on the Mac are not native PC validation.
- Lanes declared in `C:\SharedChami\windows-llm-pipeline\config.json` → `runtime.lanes`. There is no built-in
  layout: a lane that is not declared there is shown as "not configured" and never probed (fail closed). The live
  PC serves the router on `:1234`, qwen3.8-27b on `:1236` and gpt-oss-20b on `:1245`.
  NVIDIA PAIR is uninstalled, so an unreadable config must not guess a port. Slot activity needs llama-server's `/slots`.
  Native polling also requires explicit local enrollment in `lane-processes.json` beside the observer. Normal tray
  startup reads that fixed file and passes its PID, creation-time and exact executable-image pins to `LaneProcesses`.
  Missing, malformed or untrusted enrollment leaves lanes unknown and sends no lane HTTP. Neither the share nor
  `--config` can supply these pins. Each endpoint request rechecks the pinned owner.
- Optional: `C:\SharedChami\llm-lab\mac-reach\hosts.json` names the Mac whose presence is attributed;
  `nvidia-smi` for NVIDIA load.

## Options (`config.json` beside the observer)

The separate `lane-processes.json` records processes the local operator has already identified as AGIW lanes:

```json
{
  "schema": 1,
  "lanes": {
    "fast": { "pid": 12345, "created": 134000000000000000, "image": "C:\\Local\\AGIW\\llama-server.exe" }
  }
}
```

These are example values, not accepted PC identities. Inspect the actual lane owner locally before enrollment;
an executable basename, port or model alias alone does not establish ownership. `created` is the exact Windows
`GetProcessTimes` creation FILETIME integer (100 ns units since 1601), not Unix time. The existing
`agiw_win.winsys.process_info(pid)` read-only helper provides that value and the full image path for an explicitly
selected PID; it does not decide whether the process belongs to AGIW.

The file must be owned by the current account, with access limited to that account, SYSTEM and Administrators.
Its directories may also be owned by SYSTEM, Administrators or TrustedInstaller; other accounts may read them,
but may not write or replace the enrollment path. The reader requires a fixed local drive beneath the user profile,
rejects SharedChami, mapped/network paths, reparse points and hard-linked files, retains parent handles while reading,
and caps the file at 16 KiB. It only reads; it does not create the file or change ACLs. Paths remain local and are
excluded from snapshots. The shipped peer-link module supplies the SID/DACL check without starting its service.

Enrollment is read at observer startup. When a process restarts or is replaced, its old tuple stops authorizing HTTP.
After identifying and explicitly accepting the replacement, update its local tuple and restart the observer. There
is no automatic acceptance based on the old image path or reused PID. Enrollment never starts, stops or changes a
model process. Native Windows ACL, process and restart acceptance remains
required before deployment.

```json
{ "publishStatus": true, "peerLink": true, "peerLinkStatus": true }
```

`publishStatus` writes `llm-lab/status/windows-status.json` for the Mac's status reader every 10 s; `peerLink`
writes this PC's presence to `llm-lab/status/agiw-peers/` and reads the others. Both are off by default.
Publishing writes a temporary file through checked directory handles and renames it into place, so a failed or
interrupted publish keeps the previous file. The dashboard's **Share publish** source shows the last success and
any failure.

`peerLinkReader` is a separate observer option, also off by default. When enabled, it reads the pairing
service's existing local `%LOCALAPPDATA%\AGIW\peer-link\peers.json` and adds `pairedPeers` plus a `peer-link`
source to the snapshot. Reports expire after 30 s; model evidence also needs a fresh nested report. It reads
at most 64 KiB, reopens each sample, and attributes connection results to the pairing service. This preparatory
adapter has no dashboard controls.

`peerLinkStatus` (off by default) writes `state\peer-link-status.json` every 10 s: the private local status file
for the LAN pairing service, `peer_link.py` (agiw-peer/2), which the installer ships beside the observer. Installing
it starts nothing. To run it after pairing approval and the 8768/8769 firewall rules:

```powershell
python peer_link.py run --status-file state\peer-link-status.json   # announce and serve signed read-only status
python peer_link.py peers                                            # list machines; then invite / accept with the PIN
```

The pairing service reduces that file to its own allowlist before anything leaves the PC. The file is never written
to the share, which other machines can write.

## Install from a checkout

```powershell
powershell -NoProfile -File windows\install.ps1 -Destination $HOME\bin\agiw-win [-StartAtSignIn] [-Launch]
```

The destination must be a private local folder: network paths, mapped drives, SharedChami and any path under a
junction or symlink are refused. The destination must be beneath the current user profile, with trusted ownership
and no other-user write ACLs. Checked, no-follow handles retain destination parents and existing targets throughout
the swap. Updates are staged. Every file is copied into a hidden staging folder first, then
swapped in one at a time with the previous copy kept. If any step fails, every previous file is restored and the
installer reports that it rolled back. `state\` (logs and observer state) is never touched, so a running tray can
be updated in place.

The installer also writes `build.json` (version, commit, dirty state, signature and byte-verification results).
A clean `+<commit>` producer requires successful scoped status, signed-commit verification and a complete staged
file inventory matching the signed HEAD bytes. Failed or unavailable checks produce `.unverified`; dirty source
adds `.dirty.unverified`. Untracked and ignored copied modules also mark the build dirty. Without a usable record,
the commit remains unknown. Native verification of these checks remains required before installation acceptance.

## Recovery

- The tray restarts an observer that exits, after a short backoff (`monitor.log` records each restart).
- When the installed observer's Python source changes, the observer exits with code 3 and the tray starts the new
  code. A changed tray script loads on the next tray start (*Restart observer* does not reload it).
- An observer whose tray has gone exits by itself, so an orphan never holds port 8767.
- `observer.log` rotates at 1 MiB to `observer.log.1`. If a reader blocks rotation, it keeps writing up to 2 MiB.

## Tests

```powershell
cd windows; python -B -m unittest discover -s tests     # probes, store, HTTP + SSE, publisher, installer, tray
python -B -m unittest discover -s windows/tests -t windows   # the same suite from the repository root
node --test (Get-ChildItem test_*.mjs).Name              # dashboard view logic (repo root)
```

## Known limits (0.2)

- The shared dashboard now uses the Mac presence reasons (`unconfigured`, `no-report`, `stale`). Legacy positive
  LAN observations retain their evidence wording only while fresh. A5 still owns the combined-view integration.
- Linux relay origin and expected identity are not authenticated here; canonical Linux stays `unknown` with
  `relay-unverified` when a share file claims a Mac relay. The claim remains visible only in the peer diagnostics.
- Native Windows acceptance is still required before D3: run `windows/tests`, including `InstallerOnWindows`, and
  verify process/listener ownership, PDH GPU counters and `SHQueryUserNotificationState` on the PC. Portable mocked
  tests cover logic and cleanup; they do not verify these Windows APIs or a native installation.
- Read-only: no lane start/stop, model load/unload, Fix Inference or auto-unload.
- GPU temperature and power are NVIDIA-only (from `nvidia-smi`).
- A reader that opens `windows-status.json` while it is being replaced can be refused; the refusal can recur until
  the replace completes. A reader that keeps the file open keeps seeing the old contents (the replace swaps the
  name, not the open file), so readers must reopen the file on every poll.
- Job history comes from codemode run manifests in `~/code-runs`. Their rates are whole-request (prompt included), so
  they are kept apart from decode rates; the lane speed tiles show only the live decode rate measured from `/slots`
  (it needs llama-server's per-request task id, otherwise no rate is shown).
- Jev shows as unknown: its triage runs through the route queue and no passive opt-in record is read.
- The Mac is known only from its own presence report; this PC sends nothing to the Mac (the 0.1 LAN probe of
  the Mac's LM Studio was removed: behind NVIDIA PAIR it could only fail).
- Every GPU participates in hands-off before detail is capped at eight rows. Both NVIDIA and Windows utilization
  readings are retained; either known value over 40% triggers hands-off. Windows process shares cannot explain a
  higher NVIDIA reading, so its cause remains unknown.
- The hands-off cause splits the busiest GPU engine by process from Windows' GPU counters; without those counters,
  or without the lane-port owners, the cause is unknown. Full-screen detection uses the signed-in session's
  notification state, so a borderless-window game shows only as GPU load from other processes.
