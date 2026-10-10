# AGIW Inference Monitor · Windows edition (0.1.0, preview)

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
| Peer | Windows worker heartbeat | The Mac's LM Studio over the LAN (mDNS name, then recorded addresses), or the Mac's unsigned SharedChami report (expires after 30 s; never proof of LAN reachability) |
| Controls | Guarded load/unload, repair | **Read-only** in 0.1: nothing loads, unloads or restarts a model |
| Peers | — | AGIW instances meet on SharedChami (`llm-lab/status/agiw-peers/<host>.json`); opt-in in `config.json` |

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
- Lanes declared in `C:\SharedChami\windows-llm-pipeline\config.json` → `runtime.lanes` (falls back to
  fast `:1235` gpt-oss-20b / deep `:1234` qwen3.8-27b). Slot activity needs llama-server's `/slots` endpoint.
- Optional: `C:\SharedChami\llm-lab\mac-reach\hosts.json` for the Mac peer; `nvidia-smi` for NVIDIA load.

## Options (`config.json` beside the observer)

```json
{ "publishStatus": true, "peerLink": true }
```

`publishStatus` writes `llm-lab/status/windows-status.json` for the Mac's status reader every 10 s; `peerLink`
writes this PC's presence to `llm-lab/status/agiw-peers/` and reads the others. Both are off by default.
Publishing writes a temporary file through checked directory handles and renames it into place, so a failed or
interrupted publish keeps the previous file. The dashboard's **Share publish** source shows the last success and
any failure.

## Install from a checkout

```powershell
powershell -NoProfile -File windows\install.ps1 -Destination $HOME\bin\agiw-win [-StartAtSignIn] [-Launch]
```

The destination must be a private local folder: network paths, mapped drives, SharedChami and any path under a
junction or symlink are refused. Updates are staged. Every file is copied into a hidden staging folder first, then
swapped in one at a time with the previous copy kept. If any step fails, every previous file is restored and the
installer reports that it rolled back. `state\` (logs and observer state) is never touched, so a running tray can
be updated in place.

## Recovery

- The tray restarts an observer that exits, after a short backoff (`monitor.log` records each restart).
- When the installed observer's Python source changes, the observer exits with code 3 and the tray starts the new
  code. A changed tray script loads on the next tray start (*Restart observer* does not reload it).
- An observer whose tray has gone exits by itself, so an orphan never holds port 8767.
- `observer.log` rotates at 1 MiB to `observer.log.1`. If a reader blocks rotation, it keeps writing up to 2 MiB.

## Tests

```powershell
cd windows; python -B -m unittest discover -s tests     # probes, store, HTTP + SSE, publisher, installer, tray
node --test (Get-ChildItem test_*.mjs).Name              # dashboard view logic (repo root)
```

## Known limits (0.1)

- Read-only: no lane start/stop, model load/unload, Fix Inference or auto-unload.
- GPU temperature and power are NVIDIA-only (from `nvidia-smi`).
- A reader that opens `windows-status.json` at the instant it is replaced can be refused once; it reads the next
  copy on its next poll.
- Job history comes from codemode run manifests in `~/code-runs`. Their rates are whole-request (prompt included), so
  they are kept apart from decode rates; the lane speed tiles show only the live decode rate measured from `/slots`
  (it needs llama-server's per-request task id, otherwise no rate is shown).
- Jev shows as unknown: its triage runs through the route queue and no passive opt-in record is read.
- The Mac peer probe is inventory only (LM Studio's model listing); it never sends a prompt to the Mac.
