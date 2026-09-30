# Security reporting

To report a suspected vulnerability privately, email **jlcalata@me.com**. Avoid putting vulnerability details in public issues or discussions.

Include a brief description, the affected source revision or candidate version, the expected and observed behavior, and minimal reproduction steps. Share only sanitized evidence: remove credentials, tokens, personal data, private hostnames, addresses and unrelated local state. Do not send private keys, account passwords or complete machine-state archives. If sensitive evidence is necessary, first ask how to provide it safely.

Reports are reviewed by the solo developer. This policy makes no guarantee of response time, fix time or outcome, and is not a security certification.

## Artifact status

The public v1.0.0 release contains the 1.0.0 (7) application exported through Xcode with its accepted Apple notarization ticket. Strict signatures, ticket validation, Gatekeeper assessment and the final ZIP hash were verified after extraction. All 53 application files and modes match the audited rc.2 app; only the outer installation/release notes changed. Build 5 rc.1 remains available. The original signed candidate DMGs are unsubmitted and are not release assets. Complete native rendering/animation, a quarantined clean-Mac first install, and Jev Cancel/reopen and credential entry/save/remove remain unverified. The existing-key fixed hosted probe establishes only its synthetic connectivity check. The Mac App Store edition has its own validation, privacy and runtime requirements.

The canonical roadmap records current evidence. A historical passing test or reviewed source revision does not establish the safety of a different installed application or package.
