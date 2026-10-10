# Advanced configuration

Optional routing, recovery and memory controls require separately configured components. Start with the [release installation guide](../README.md#first-run).

## Resident model budget

For separately configured external routing, the Mac Nisi route selects two distinct LLMs already resident in LM Studio. It prefers Gemma 4 as author when present and chooses another resident LLM as reviewer; it does not load an extra model for those roles. Jev uses a separate remote adapter and does not need a Mac model resident. Core 6 is a catalog view, not a request to hold six models in RAM.

Automatic unloading is off by default. When explicitly enabled, it protects the current resident Nisi pair, considers only other observed idle Mac models, and refuses to unload anything if the pair cannot be established from a fresh snapshot. These policies do not establish that a live routed task succeeded.

## Optional hardware access for nearby workers

For setup or recovery of nearby headless machines, an [Openterface Mini-KVM](https://openterface.com/minikvm/) or compatible KVM is recommended for direct keyboard, video and mouse access through USB and HDMI. See the [Mini-KVM FAQ](https://docs.openterface.com/products/minikvm/faq/) for connection requirements. Network inference still requires configured workers/model endpoints and their network transport; a KVM is optional and does not pool RAM or GPUs or increase token throughput. Native AGIW KVM integration has not been verified.

## Share recovery settings

The source contains no address or login name for the owner's actual PC. To enable the explicit Fix Route remount on a Mac, set both environment variables in the Inference Monitor process environment before launch:

```sh
AGIW_SHARE_HOSTS='10.222.33.10,10.222.33.20,pc.example.invalid'
AGIW_SHARE_USERNAME='registered-user'
```

Those addresses and the name are examples; replace them with the owner's actual LAN values. `AGIW_SHARE_HOSTS` accepts one or two numeric IPv4 addresses in the `10/8`, `172.16/12`, or `192.168/16` private ranges and optionally a third trusted DNS name or address for matching the mounted share. `AGIW_SHARE_USERNAME` is the registered SMB account; this repository stores no password. Guest and Anonymous are refused. If `login-agent.sh install` runs with both variables exported, it stores only these non-password settings in the private per-user LaunchAgent. A partial pair is refused. A share already mounted as Guest or from an unconfigured host is not treated as healthy; Fix Route stops without remounting it.

## Memory readings for sandboxed callers

When the Mac Monitor can read the kernel memory counters, its sampler writes a short-lived `level.json` under `~/.local/state/agiw/mem-guard/`. The directory is owner-only and the file is mode `0600`. A sandboxed memory-guard caller can use this file for up to three seconds when it cannot read the counters directly. The sampler never renews the file from an older file reading or fallback.

`mem-guard status` reports one sample. When high swap use is its only TIGHT signal, `mem-guard admit` can take 5.1 seconds for a second direct kernel sample, even with `--wait 0`. Heavy work is admitted in that special case only if pressure and headroom remain healthy and swap use and swapouts do not grow. Missing counters, fallback data, or another TIGHT reason still refuse admission. Use the `admit` exit code as the gate; `status` is informational.

## Linking with the Windows edition

The [Windows edition](../windows/README.md) and this Mac meet on SharedChami. Reading the PC is always on and read-only: the Windows PC inspector shows **Live from <host>** from the paired LAN peer link when it is connected, otherwise from the newest `llm-lab/status/agiw-peers/<host>.json` or `windows-status.json` observed in the last 30 s. The map's Windows PC node then leads with **LINKED VIA LAN** (paired, signed) or **REPORTED VIA SHARE** (unsigned file), the PC edition's words for its Mac node, and with neither once the feed is stale. Inside that same 30 s window the PC chip leads with the PC's own report (**Reports 2 loaded · idle**, or **Reports idle** when space is short), and the node puts it after the link word in place of "worker advertised · inference unknown" while no dispatcher job or recent answer speaks for the PC. A report never makes the chip or node pulse; only a job in flight does. When the PC reports itself **down** or **degraded**, that word stays even in the shortest chip form (**Reports degraded**), an OK chip turns to its warning tone, and the node says **PC REPORTS DOWN** (or **PC REPORTS DEGRADED**) so it never reads as the link being down. The **Live from <host>** panel also lists every graphics adapter the PC reports (the AMD card as well as the NVIDIA one), as whole-card load, not a running model.

Options (`~/Library/Application Support/AGIW/config.json`):

```json
{ "publishStatus": true, "peerLink": true }
```

`publishStatus` writes this Mac's presence to `llm-lab/status/agiw-peers/<host>.json` every 10 s, where the PC edition shows the Mac node as reported via the share. It never writes `mac-status.json`, which belongs to the Mac status script. `peerLink` publishes the same presence and also starts the LAN peer link: this Mac announces itself on the LAN and serves its status, read-only, to machines paired with a typed PIN. Both are off by default. `--publish-status` and `--peer-link` turn an option on whatever the file says; `--config <path>` reads another file. The file must be a strict JSON object of at most 16 KB with `true` or `false` values; a UTF-8 byte order mark is accepted, as on the PC. A malformed file, or a symbolic link, leaves both off and shows an **AGIW config** error under Sources; the monitor still starts. The LAN link starts after the monitor is serving; if it cannot start (a busy port, a damaged file in the peer-link state folder), Sources shows an **AGIW peer link (LAN)** error and everything else keeps running.

The presence file uses the PC's schema 1 shape (`kind: "agiw-peer"`, `platform: "macos"`, `edition: "mac"`): short host name, app version, observation time, health, Mac model IDs with loaded state and verified activity, GPU load, memory level, the loopback dashboard port and the files this Mac reads. It also carries `components`: this Mac's **Nisi** and **Jev** states as the monitor sees them (for example Nisi `ready`, `partial` or `needs-action`, Jev `configured` or `unavailable`, plus how long ago Jev last judged a route), each with a fixed description; a state the monitor cannot establish is published as `unknown`, never left out. Components reach both the share file and the paired LAN link (whose status allowlist carries only the `nisi` and `jev` entries, as short words and one age); the PC edition does not show them yet. The presence file reports Nisi and Jev; it does not start or route through them. It holds no paths, prompts or keys. When neither LM Studio inventory source answers, the Mac's model list is unknown, so no presence is written (the last file ages out after 30 s) rather than a list of zero models. Each write must first pass the LAN link's status rules and the PC reader's rules, then goes to a temp file in the same folder and is renamed over the old one, so a refused or failed write leaves the previous file. Temp files this Mac left behind more than five minutes earlier (a dropped share, a killed process) are removed before the next write. `llm-lab/status` must already exist on the mounted share, and a symbolically linked folder is refused. Writes run on one background thread: if the share hangs, Sources shows **AGIW presence (share)** as stuck and no further write is queued behind it.

Return to the [README](../README.md).
