# Three-machine port roles

The remote-machine registry file is the single place a user declares **registry remote lanes**: `~/.config/agiw/remote-machines.json`, or the file selected by `AGIW_REMOTE_MACHINES_CONFIG`. Each lane's `localPort` is an existing Mac loopback binding; the registry does not create a tunnel or configure a remote server. See [the registry guide](three-machine-pipeline.md) for validation and the example file.

The following roles are defined by the current source, not a claim about what is running on any machine. Ports can be configured where noted.

| Port | Role in code |
| --- | --- |
| Configured `localPort` | Registry lane loopback endpoint; unique per registry and restricted to 1024–65535, excluding the protected ports below (`remote_machines.py`). |
| 1234 | Default Mac LM Studio API port (`telemetry.py`). Registry-protected. |
| 1235 | Registry-protected; the Windows observer does not use it as a fallback lane port. |
| 8080, 8081, 11434 | Registry-protected. The Windows observer's PAIR listener inventory recognizes these ports without assuming ownership (`windows/agiw_win/probes.py`). |
| 18082 | Registry-protected (`remote_machines.py`). |
| 8768 TCP, 8769 UDP | Default opt-in peer-link status and discovery ports (`peer_link.py`); registry-protected. |
| 8767 | Default Windows loopback observer port (`windows/agiw_win/server.py`). |
| OS-selected | Default Mac observer port; `server.py` and the native launcher request port 0. |
| Configured `localPort` → `remotePort` | Forwards for the separate legacy PC SSH tunnel. There are no built-in forwards and no built-in legacy PC lanes; the earlier 1234, 1245 and 1236 PC lanes are retired (`pc_tunnel.py`, `inference_collect.py`). |
| 14318–14323 | Windows PAIR listener inventory recognizes this range without assuming ownership (`windows/agiw_win/probes.py`). |

The Windows observer's **local** fast/deep lane probes use `windows-llm-pipeline/config.json` on its configured share, not registry ports; missing or unreadable configuration leaves those lanes unconfigured and unprobed. The legacy PC collector and tunnel have no built-in lanes or forwards; the tunnel keeps its own configuration independently of the registry. A registry entry can explicitly replace legacy PC visibility as described in the registry guide; it does not migrate those legacy consumers.
