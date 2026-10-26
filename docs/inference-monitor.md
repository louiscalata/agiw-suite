# Inference Monitor

The Mac menu bar and the Windows tray open the same page, `/monitor`, from the local agiw-suite server. The page is `web/monitor.html` plus `web/monitor.mjs`. Neither shell draws the map itself.

The map nodes are stable ids:

| Id | What it is |
| --- | --- |
| `mac`, `mac:lmstudio`, `mac:veritas` | This Mac, LM Studio on 127.0.0.1:1234, the Veritas reviewer |
| `pc`, `pc:router`, `pc:gpt-oss-20b`, `pc:qwen3.8-27b` | The PC, its router on :1234, and the llama-server lanes on :1245 and :1236 |

A null tok/s, latency, slot, context or VRAM field means that reading was not measured. Health is `ok`, `degraded`, `down` or `not_measured`.

## PC tunnel

The PC lanes bind to 127.0.0.1. On the Mac, `python3 pc_tunnel.py` runs `ssh -N -L` when user, host and an identity file are configured. Set them in the environment or in `~/.config/agiw/pc-tunnel.json`:

```json
{
  "user": "pc-user",
  "host": "pc.example",
  "identityFile": "/path/to/restricted-key"
}
```

`AGIW_PC_SSH_USER`, `AGIW_PC_SSH_HOST`, `AGIW_PC_SSH_IDENTITY` and `AGIW_PC_SSH_CONFIG` override that file. The key file is not read by this program. The default forwards are local 1334, 1345 and 1336 to the PC's 1234, 1245 and 1236, so they do not collide with LM Studio on the Mac. While those local ports are closed, the PC nodes stay `not_measured` with reason `tunnel_down`.

Admin step, not performed by this repo: if the PC key is restricted, its `authorized_keys` entry needs `permitopen="127.0.0.1:1234"`, `permitopen="127.0.0.1:1245"` and `permitopen="127.0.0.1:1236"`. Do not assume that change is already in place.

## Veritas

`VERITAS_REVIEWER_URL` is fetched only when it is `http://127.0.0.1:<port>/...`. Anything else stays `not_measured`.

## Later

Alerts, history, spend and the live activity panel are not in this page. The activity panel will key off the node ids above. The PC router lives outside this repo; a tap there is a separate change and is not deployed by this tree.
