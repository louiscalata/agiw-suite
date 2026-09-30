# Security reporting

To report a suspected vulnerability privately, email **jlcalata@me.com**. Avoid putting vulnerability details in public issues or discussions.

Include a brief description, the affected source revision or candidate version, the expected and observed behavior, and minimal reproduction steps. Share only sanitized evidence: remove credentials, tokens, personal data, private hostnames, addresses and unrelated local state. Do not send private keys, account passwords or complete machine-state archives. If sensitive evidence is necessary, first ask how to provide it safely.

Reports are reviewed by the solo developer. This policy makes no guarantee of response time, fix time or outcome, and is not a security certification.

## Artifact status

Passing source checks and Developer ID signing are distinct from distribution acceptance. The GitHub Build 7 rc.2 prerelease is a notarized ZIP containing an app exported through Xcode; its accepted ticket, strict signature, Gatekeeper assessment and final ZIP hash were verified after extraction. Build 5 rc.1 remains available. The original signed candidate DMGs are unsubmitted and are not release assets. Complete native rendering/animation and a quarantined clean-Mac first install remain stable-release gates. Build 7 Jev Cancel/reopen and credential entry/save/remove acceptance remain unverified; an existing-key fixed hosted probe is separate from those actions and from task routing. The Mac App Store edition is a separate application with its own validation, privacy and runtime requirements.

The canonical roadmap records current evidence. A historical passing test or reviewed source revision does not establish the safety of a different installed application or package.
