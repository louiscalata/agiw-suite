# Security reporting

To report a suspected vulnerability privately, email **jlcalata@me.com**. Avoid putting vulnerability details in public issues or discussions.

Include a brief description, the affected source revision or candidate version, the expected and observed behavior, and minimal reproduction steps. Share only sanitized evidence: remove credentials, tokens, personal data, private hostnames, addresses and unrelated local state. Do not send private keys, account passwords or complete machine-state archives. If sensitive evidence is necessary, first ask how to provide it safely.

Reports are reviewed by the solo developer. This policy makes no guarantee of response time, fix time or outcome, and is not a security certification.

## Artifact status

The public v1.0.0 release contains the 1.0.0 (7) application exported through Xcode with its accepted Apple notarization ticket. Strict signatures, ticket validation, Gatekeeper assessment and the final ZIP hash were verified after extraction. All 53 application files and modes match the audited rc.2 app; only the outer installation/release notes changed. The rc.1 and rc.2 release downloads were withdrawn on 18 October 2026; their source tags remain. The repository release record lists v1.0.0 as the only binary download. The original signed candidate DMGs are unsubmitted and are not release assets. Complete native rendering/animation, a quarantined clean-Mac first install, and Jev Cancel/reopen and credential entry/save/remove remain unverified. The existing-key fixed hosted probe establishes only its synthetic connectivity check. The Mac App Store edition has its own validation, privacy and runtime requirements.

The canonical roadmap records current evidence. A historical passing test or reviewed source revision does not establish the safety of a different installed application or package.

## Known issue in v1.0.0 (Build 7): local control authentication

The shipped 1.0.0 observer checks Host and Origin but does not authenticate callers of its local control routes. Another process or account on the same Mac can supply those headers and request model load/unload, change auto-unload settings, or trigger the fixed Jev connection check under the app user's authority. A Jev check can incur provider charges when that user's credential is configured; this finding does not establish disclosure of the credential. Loopback binding and notarization do not prevent these requests. Quit the released Monitor when local callers are not trusted.

The unreleased source requires a per-launch capability on every control POST, in addition to Host/Origin and the existing action guards. The native shell creates it in memory, sends it to its child through a private stdin pipe, and injects it into its nonpersistent WebKit pages for same-origin POST headers. It is not put in the startup JSON, endpoint file, environment, URL or logs. A standalone `server.py` launch has no capability and cannot dispatch control POSTs. This is source hardening only: **v1.0.0 remains affected until a new release carries the fix**. It does not protect against compromise of the app user's processes or the native page itself.

## Network surfaces

`server.py` binds only to IPv4 loopback. Its observation GETs, including snapshots and event streams, remain readable by local clients with the accepted Host header; telemetry is not confidential from other local accounts. Control POSTs in the unreleased source require the capability described above.

The separate, default-off LAN peer link listens on `0.0.0.0`, TCP 8768 and UDP 8769, when `peerLink` is enabled. Discovery is not authentication. Pairing uses a typed PIN; status requests and replies use paired keys, signatures, timestamps and nonces. Its local pairing/control interface uses a separate owner-protected control token with challenge proofs. These protections are implemented in `peer_link.py`, independently of the Monitor HTTP capability.
