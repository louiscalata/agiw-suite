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
Return to the [README](../README.md).
