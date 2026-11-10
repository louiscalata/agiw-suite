# Inference Monitor

The Mac menu bar and the Windows tray open the same page, `/monitor`, from the local agiw-suite server. The page is `web/monitor.html` plus `web/monitor.mjs`. Neither shell draws the map itself.

The map nodes are stable ids:

| Id | What it is |
| --- | --- |
| `mac`, `mac:lmstudio`, `mac:veritas` | This Mac, LM Studio, the optional Veritas reviewer |
| `pc`, `pc:router`, `pc:gpt-oss-20b`, `pc:qwen3.8-27b` | The PC, its router on :1234, and the llama-server lanes on :1245 and :1236 |

The Mac sampler uses the verified telemetry endpoint for `mac:lmstudio`. The Windows observer reports the absent Mac row as `not_observed` at `127.0.0.1:1234`.

A null tok/s, latency, slot, context or VRAM field means that reading was not measured. Health is `ok`, `degraded`, `down` or `not_measured`. The Mac observer and browser expire Mac nodes after three seconds and PC nodes after 30 seconds; missing or invalid observation times are unknown. Fetch failures clear health and measurements, and only one refresh can be in flight. **Full dashboard** opens the existing view at `/`. The Mac native shell supplies control authorization; standalone browser launches cannot dispatch control POSTs.

The overview at `/` and the map at `/monitor` share a view-only monitor tab strip. Tabs are derived from the snapshot or inference map already on the page: machine rows, the Nisi route, client identity rows, and in-flight route or PC-job sessions. Each session tab keeps a durable workload id that does not follow the display label. A session that leaves the sample stays disconnected, or keeps a last evidenced blocked or down state, until it is dismissed. The same id returning updates that tab; a newly reported attempt does not keep the previous attempt's events or analysis. Switching tabs changes the visible monitor and keeps that monitor's filter and selection; it does not pause, cancel, restart, or stop the observer subscription. Unselected tabs still take the next sample. `?demo=1` fills the tab board from a labeled fictional sample and leaves the live map on the observer's own feed. Unknown, stale, disconnected, and unavailable stay different words. Requested and served model fields are shown separately when the sample has them. Nisi and Jev pre/post slots on a session stay unavailable until a record for that run and attempt arrives; a passed analysis is not run success. The surface is strict black and white in the system's light or dark theme, or in the one the person picks.

Repository Nisi and Jev commands for this draft, including the retrospective base-ref run, are recorded in [jaryo-nisi-jev-analysis.json](data/jaryo-nisi-jev-analysis.json). The 2026-11-06 Mac and PC receipts are in [jaryo-jev-phase-receipts.json](data/jaryo-jev-phase-receipts.json). The later authorized pre/analysis/post record is in [jaryo-authorized-0329-receipts.json](data/jaryo-authorized-0329-receipts.json). None of these files are inference telemetry or acceptance passes. Exit 0, STATUS OK, COMPLETED, and PROCEED-WITH-CHANGES do not open the Jev gate. The PC post verdict ROLLBACK is an advisory on a task that made no repository edits; it is not an instruction to revert this draft.

This checkout is the AGIW inference monitor. Its visual language is Jaryo: ink and paper for readings, booklet red `#C21A27` and Newsreader for the wordmark and headlines, Jost for text and labels, and text-only controls. The binding decisions are in [jaryo-identity.md](jaryo-identity.md). The monitor pages use that chrome (`web/jaryo.css`, `web/jaryo.mjs`). The installable kit in `jaryo/` is a separate pipeline page and does not draw this map. Hermes is not used. Direction studies in `jaryo/marks/` are not the wordmark.

Run `python3 -B server.py --port 8765` from the repository root and open `http://127.0.0.1:8765/`. The same process serves `/monitor` and `/components`. `/?demo=1` is a labeled fictional tab sample; the map and vitals stay on the observer. A standalone launch cannot dispatch control POSTs. Live cross-machine acceptance is not established by these pages or by the fixture tests. A minimal reversible check on a real Mac is a loopback read of `GET /api/snapshot`, `GET /api/inference`, and `GET /api/components/jev` without `POST /api/components/jev/check`. On a real PC the existing observer default is port 8767 and `GET /api/health` plus `GET /api/snapshot`. Do not start a watcher or a hosted Jev call for that read.

## PC tunnel

The PC lanes bind to 127.0.0.1. On the Mac, `python3 pc_tunnel.py` runs `ssh -N -L` when user, host and an identity file are configured. Set them in the environment or in `~/.config/agiw/pc-tunnel.json`:

```json
{
  "user": "pc-user",
  "host": "pc.example",
  "identityFile": "/path/to/restricted-key"
}
```

`AGIW_PC_SSH_CONFIG` selects the configuration file path. Defaults are applied first, then a valid file; `AGIW_PC_SSH_USER`, `AGIW_PC_SSH_HOST` and `AGIW_PC_SSH_IDENTITY` override its fields. An invalid file returns `config_invalid` before those field overrides are applied. The key file is not read by this program. The default forwards are local 1334, 1345 and 1336 to the PC's 1234, 1245 and 1236, so they do not collide with LM Studio on the Mac. A PC node's Endpoint displays the local URL actually fetched. For a forwarded llama lane, Upstream retains the remote service URL so router edges still resolve. While those local ports are closed, the PC nodes stay `not_measured` with reason `tunnel_down`.

Admin step, not performed by this repo: if the PC key is restricted, its `authorized_keys` entry needs `permitopen="127.0.0.1:1234"`, `permitopen="127.0.0.1:1245"` and `permitopen="127.0.0.1:1236"`. Do not assume that change is already in place.

## Veritas

`VERITAS_REVIEWER_URL` is fetched only when it is `http://127.0.0.1:<port>/...`. Anything else stays `not_measured`.

## Later

Alerts, history, spend and the live activity panel are not in this page. The activity panel will key off the node ids above. The PC router lives outside this repo; a tap there is a separate change and is not deployed by this tree.
