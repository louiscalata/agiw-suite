# AGIW Inference Monitor · Windows edition (0.1.0, preview)

The Windows counterpart of the Mac menu-bar monitor. A notification-area icon supervises a loopback Python
observer that serves **the same dashboard** as the Mac edition, in its `host: "windows"` mode.

| | Mac edition | Windows edition |
| --- | --- | --- |
| Shell | Swift menu-bar app | PowerShell tray icon (`agiw-monitor.ps1`) |
| Local models | LM Studio inventory + `lms ps` | The PC's llama-server lanes (`/health`, `/v1/models`, `/slots`) |
| Activity | LM Studio phase | Slot `is_processing`, decoded tokens → busy / generating |
| Identity | Exact LM Studio keys | Served model must equal the declared lane alias, or the lane reads **identity mismatch** |
| GPU | Apple GPU via ioreg | NVIDIA via `nvidia-smi`; every adapter (incl. AMD) listed from the driver registry |
| Memory | macOS pressure + guard | Physical memory available and commit charge (no pause/resume guard) |
| Peer | Windows worker heartbeat | The Mac's LM Studio over the LAN (mDNS name, then recorded addresses) |
| Controls | Guarded load/unload, repair | **Read-only** in 0.1: nothing loads, unloads or restarts a model |

## Run

Double-click **`AGIW Monitor.vbs`** (or `AGIW Monitor.cmd`). The tray icon appears and the dashboard opens in an
Edge app window at `http://127.0.0.1:8767/` (an ephemeral port is used if 8767 is taken).

Tray icon colours: **blue** a lane is working · **green** lanes up and idle · **amber** a lane is down, loading,
serving the wrong model, or memory is tight · **grey** starting or stale. Right-click for *Restart observer*,
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

## Install from a checkout

```powershell
powershell -NoProfile -File windows\install.ps1 -Destination $HOME\bin\agiw-win [-StartAtSignIn] [-Launch]
```

## Tests

```sh
cd windows && python -B -m unittest discover -s tests -q     # probes, store, HTTP + SSE (any OS)
node --test test_mac_peer_view.mjs test_client_models.mjs      # dashboard view logic (repo root)
```

## Known limits (0.1)

- Read-only: no lane start/stop, model load/unload, Fix Inference or auto-unload.
- AMD/other GPUs show name and VRAM only; live load is NVIDIA-only.
- No Windows job journal yet (`windowsJobs` is null), so lane speeds come from nothing recorded here.
- Jev shows as unknown: its triage runs through the route queue and no passive opt-in record is read.
- The Mac peer probe is inventory only (LM Studio's model listing); it never sends a prompt to the Mac.
