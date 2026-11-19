# Inference Monitor

The Mac menu bar and the Windows tray open the same page, `/monitor`, from the local agiw-suite server. The page is `web/monitor.html` plus `web/monitor.mjs`. Neither shell draws the map itself.

The map nodes are stable ids:

| Id | What it is |
| --- | --- |
| `mac`, `mac:lmstudio`, `mac:veritas` | This Mac, LM Studio, the optional Veritas reviewer |
| `pc` | The PC machine row of the legacy PC collector |

The legacy PC collector (`inference_collect.py`) and tunnel (`pc_tunnel.py`) have no built-in lane layout. Lanes are declared in the machine registry (see [configured machine lanes](three-machine-pipeline.md)); a registry machine with `"replaces": "legacy-pc"` takes the place of the `pc` row. The earlier router (:1234) and llama-server lanes (:1245 and :1236) are retired and are not built in, so they are never probed or shown as `down`. A target whose lane id is not built in stays `not_measured` with reason `unknown_lane` and is not probed.

The Mac sampler uses the verified telemetry endpoint for `mac:lmstudio`. The Windows observer reports the absent Mac row as `not_observed` at `127.0.0.1:1234`.

A null tok/s, latency, slot, context or VRAM field means that reading was not measured, and the page shows the exact words `Not measured` for it. Every missing or unavailable reading on these pages (model, health, queue, slots, context, VRAM, observation age, lane counts and sample age) reads `Not measured`, never a dash, `N/A`, `?` or an empty cell. Health is `ok`, `degraded`, `down` or `not_measured`. The Mac observer and browser expire Mac nodes after three seconds and PC nodes after 30 seconds; missing or invalid observation times are unknown. Fetch failures clear health and measurements, and only one refresh can be in flight. **Overview** opens the Fleet ledger at `/`. The Mac native shell supplies control authorization; standalone browser launches cannot dispatch control POSTs.

The overview at `/` and the map at `/monitor` share a view-only monitor tab strip. Tabs are derived from the snapshot or inference map already on the page: machine rows, the Nisi route, client identity rows, and in-flight route or PC-job sessions. Each session tab keeps a durable workload id that does not follow the display label. A session that leaves the sample stays disconnected, or keeps a last evidenced blocked or down state, until it is dismissed. The same id returning updates that tab; a newly reported attempt does not keep the previous attempt's events or analysis. Switching tabs changes the visible monitor and keeps that monitor's filter and selection; it does not pause, cancel, restart, or stop the observer subscription. Unselected tabs still take the next sample. `?demo=1` fills the tab board from a labeled fictional sample and leaves the live map on the observer's own feed. Unknown, stale, disconnected, and unavailable stay different words. Requested and served model fields are shown separately when the sample has them. Nisi and Jev pre/post slots on a session stay unavailable until a record for that run and attempt arrives; a passed analysis is not run success. The surface is strict black and white in the system's light or dark theme, or in the one the person picks.

Repository Nisi and Jev commands for this draft, including the retrospective base-ref run, are recorded in [jaryo-nisi-jev-analysis.json](data/jaryo-nisi-jev-analysis.json). The 2026-11-06 Mac and PC receipts are in [jaryo-jev-phase-receipts.json](data/jaryo-jev-phase-receipts.json). The later authorized pre/analysis/post record is in [jaryo-authorized-0329-receipts.json](data/jaryo-authorized-0329-receipts.json). None of these files are inference telemetry or acceptance passes. Exit 0, STATUS OK, COMPLETED, and PROCEED-WITH-CHANGES do not open the Jev gate. The PC post verdict ROLLBACK is an advisory on a task that made no repository edits; it is not an instruction to revert this draft.

This checkout is the AGIW inference monitor. Its visual language is Jaryo: ink and paper for readings, text-only controls and ASCII schematics (no glyph-only buttons: zoom, fit, about, dismiss and close buttons show their words, such as **Zoom in**, **Fit**, **About** and **Close**), with the AGIW wordmark in black on a Copper `#C47A3A` fill. The binding decisions are in [jaryo-identity.md](jaryo-identity.md). The monitor pages use that chrome (`web/jaryo.css`, `web/jaryo.mjs`). The installable kit in `jaryo/` is a separate pipeline page and does not draw this map. Hermes is not used. Direction studies in `jaryo/marks/` are not the wordmark.

Run `python3 -B server.py --port 8765` from the repository root and open `http://127.0.0.1:8765/`. The same process serves `/monitor` and `/components`. `/?demo=1` is a labeled fictional tab sample; the map and vitals stay on the observer. A standalone launch cannot dispatch control POSTs. Live cross-machine acceptance is not established by these pages or by the fixture tests. A minimal reversible check on a real Mac is a loopback read of `GET /api/snapshot`, `GET /api/inference`, and `GET /api/components/jev` without `POST /api/components/jev/check`. On a real PC the existing observer default is port 8767 and `GET /api/health` plus `GET /api/snapshot`. Do not start a watcher or a hosted Jev call for that read.

## Map page

`/monitor` has one `main` landmark and one heading, "Inference map". Its tab strip lists "All inference" and one tab per fleet fact. A tab id is the fact id. A machine tab shows that machine and the lanes whose parent it is; a lane tab shows that lane only. The diagram and the list under it use the same rule, and lines to hidden nodes are hidden with them. A tab id that is no longer on the map filters nothing. The tabs name the panel they filter (`aria-controls="mapPanel"`), and the panel's label names the selected monitor. A tab that stays on the map keeps its element across repaints. On each observation the tabs that left the map are removed first, and the remaining tabs are then placed around the focused one, so the focused tab is never detached or moved and keyboard focus stays on it without a blur, including when another tab leaves, arrives or changes place. If the focused monitor itself leaves the map, focus moves to the selected tab.

Each node's state is a class on its group: `ok` is a filled mark, `down` an open mark, `not_measured` a dashed open mark and `degraded` an open mark with a heavier outline. A missing or unrecognised reading is drawn as `not_measured`. The state word stays in the text beside the mark. Lines carry their kind: solid for a machine and an endpoint configured on it (`hosts`), dashed for a router and an endpoint it lists as upstream (`upstream`), dotted for an endpoint and a client reported as its caller (`caller`), and dash-dot for any other reported kind. A text key under the graph lists the kinds that are drawn.

The line above the tabs is the collector's detail for the legacy PC forward listeners. Without a detail it says the local forward listeners are, or are not, accepting connections, and that this is local listener state, not SSH or PC reachability. Any state other than `up` or `down`, and an observation older than 30 seconds, reads as not measured.

**Pause display updates** freezes the drawing, the tabs and the listener line. While it is pressed the page shows the local time the display was paused and says newer observations are not shown; the monitor store keeps receiving them, and pressing the button again draws the newest one. Pausing does not pause, cancel or restart inference or the observer. State changes between two drawn samples are written once per update to a visible polite live region: a changed state word, a node that arrived and a node that left, at most four per update and then a count. The first sample, ages and rates are not announced. **Mute announcements** stops those messages, says so in the same region, and leaves the drawing live.

The graph is drawn at one unit per CSS pixel and is not scaled to the window. When it is wider than the page, the named region around it ("Machine and model map") scrolls and can be focused with the keyboard.

These behaviours are covered by `test_map_interaction.mjs` on a DOM double. Layout at narrow widths, 200 percent text and forced colors is not established by that test.

## PC tunnel

The PC lanes bind to 127.0.0.1. On the Mac, `python3 pc_tunnel.py` runs `ssh -N -L` when user, host, an identity file and at least one forward are configured. Set them in the environment or in `~/.config/agiw/pc-tunnel.json`:

```json
{
  "user": "pc-user",
  "host": "pc.example",
  "identityFile": "/path/to/restricted-key",
  "forwards": [
    {"name": "example-lane", "localPort": 2001, "remotePort": 3001}
  ]
}
```

`AGIW_PC_SSH_CONFIG` selects the configuration file path. Defaults are applied first, then a valid file; `AGIW_PC_SSH_USER`, `AGIW_PC_SSH_HOST` and `AGIW_PC_SSH_IDENTITY` override its fields. An invalid file returns `config_invalid` before those field overrides are applied. The key file is not read by this program. There are no default forwards. A configuration file lists them as `"forwards": [{"name": ..., "localPort": ..., "remotePort": ...}]` (1 to 8 rows; the ports in the example above are documentation placeholders, replace them with your own); choose local ports that do not collide with LM Studio or other Mac loopback bindings. Without forwards the tunnel reports `forwards_unconfigured` and does not start `ssh`. The tunnel only forwards ports. Because the collector has no built-in lanes, a forwarded lane appears on the map as `unknown_lane` and is not probed; declare the lane in the machine registry, using the forward's local port as its `localPort`, to observe it.

Admin step, not performed by this repo: if the PC key is restricted, its `authorized_keys` entry needs one `permitopen="127.0.0.1:<remotePort>"` for each configured forward. Do not assume that change is already in place.

## Veritas

`VERITAS_REVIEWER_URL` is fetched only when it is `http://127.0.0.1:<port>/...`. Anything else stays `not_measured`.

## Later

Alerts, history, spend and the live activity panel are not in this page. The activity panel will key off the node ids above. There is no PC router node in this tree, and nothing here taps one; any such tap would be a separate change.

The Suite kit converges on the Jaryo kit token file: `jaryo/vendor/jaryo.tokens.json` is a pinned copy of `tokens/jaryo.tokens.json` from the Jaryo kit repository (source commit and hash in `jaryo/vendor/SOURCE.md`), generated into `--jaryo-*` properties in `jaryo/primitives.css`; Suite spacing, type steps and the 44px control minimum read those properties where the values are equal. The colour palette is not yet converged. The full observer web pages use the `variants.web` contract in `jaryo/tokens.json`: IBM Plex Sans for text, Plex Mono for code and figures, and Copper for accent fills and focus. The shared foundation and Android keep their existing Inter and black-and-white contract. This source change is not a device or packaged-app acceptance result.
