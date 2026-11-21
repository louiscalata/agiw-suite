# Release checklist

Checks for a signed Mac download. See [status](status.md) for the current release
and [the roadmap](../roadmap.md) for earlier evidence.

[1.1.1 (15)](https://github.com/louiscalata/agiw-suite/releases/tag/v1.1.1) is a
Developer ID–signed DMG. It has no Apple notarization ticket. The notarization
and quarantined clean-Mac first-install checks remain open.

## Before a notarized download is published

- Record the exact source commit and its CI results.
- Match the released app's version and build in `Info.plist`. The Mac app,
  `agiw` CLI, Windows preview, and Android monitor have separate version fields.
- Verify the DMG digest, its image checksum and the mounted app's signature.
  Keep the Apple notarization acceptance and validate the stapled ticket on the
  distributed artifact. Compute the published checksum from the final bytes.

## Clean-Mac install

Use a supported Mac without the app installed. Download the candidate DMG in
a browser so macOS applies quarantine. These checks require a notarized
candidate and are **NOT_RUN** for v1.1.1.

1. Verify the DMG against its published SHA-256 file. Confirm the browser
   applied quarantine with `xattr -p com.apple.quarantine <download.dmg>`.
2. Verify the notarization ticket with `xcrun stapler validate <download.dmg>`.
3. Mount the DMG. Check the app with
   `codesign --verify --deep --strict "/path/to/Inference Monitor.app"` and
   `spctl --assess --type execute "/path/to/Inference Monitor.app"`.
   All verification and assessment commands should exit 0.
4. Copy and open the app from Finder. Record whether Gatekeeper blocks opening.
5. Record native startup and any control-route authorization checks run. Use
   NOT_RUN for checks that were skipped.

Windows installer and physical Galaxy Z Flip6 acceptance remain separate.

## Receipt

Copy this block into the pull request or the release notes when the clean-Mac install has been run. Leave it out until then. Do not put a computer name, user name, or serial number in the receipt.

```
Clean-Mac install receipt
Date:
Commit:
Download file and SHA-256:
Stapled ticket validation exit:
Quarantine attribute present:
spctl exit and summary:
codesign verify exit:
First open Gatekeeper block: yes or no
Unauthenticated control POST status:
Result:
```

Until a completed receipt is stored, status stays **NOT_RUN**.
