# Release checklist

Checks for a signed Mac download. This page does not replace [status](status.md). No receipt is stored in this repository. The clean-Mac install below is **NOT_RUN**.

The published app to install is still [1.0.0 (7)](https://github.com/louiscalata/agiw-suite/releases/tag/v1.0.0). The [v1.1.0](https://github.com/louiscalata/agiw-suite/releases/tag/v1.1.0) tag is source only and has no app attached.

## Before a download is published

- The commit is on `main` and its source checks are green.
- Version strings in `Info.plist`, `pyproject.toml`, `agiw/__init__.py`, `windows/agiw_win/__init__.py`, and `android/app/build.gradle.kts` match the release.
- `SHA256SUMS.txt` is produced for the release folder. The 1.0.0 file is one line: 64 lowercase hex digits, two spaces, then the ZIP name.
- The ZIP is Developer ID signed and notarized. Building this source tree does not reproduce the 1.0.0 ZIP.

## Clean-Mac install

Run this on a Mac that does not already have the app installed. Download the ZIP in a browser so macOS sets the quarantine attribute. Copying the file with `scp` or `curl` does not set it.

1. In the download folder, confirm quarantine is present:

   ```sh
   xattr -p com.apple.quarantine AGIW-Inference-Monitor-1.0.0-7-arm64.zip
   ```

   The command prints an attribute value. A missing attribute means the file was not downloaded by a browser.

2. Check Gatekeeper assessment and the signature. Run these on the app after the ZIP is extracted:

   ```sh
   spctl -a -vvv -t install "/path/to/Inference Monitor.app"
   codesign --verify --deep --strict "/path/to/Inference Monitor.app"
   ```

   Both commands exit 0. `spctl` prints `accepted` and a Developer ID authority.

3. Open the app from Finder. The first open has no Gatekeeper block.

4. With the app running, an unauthenticated control POST returns 403. Replace the port with the observer port:

   ```sh
   curl -s -o /dev/null -w '%{http_code}\n' \
     -X POST \
     -H 'Content-Type: application/json' \
     -H 'Origin: http://127.0.0.1:PORT' \
     --data '{"action":"load","modelId":"example"}' \
     "http://127.0.0.1:PORT/api/models/control"
   ```

   Expect `403`. The 1.0.0 (7) download does not have this check. The v1.1.0 source does. This step applies to a build of the v1.1.0 source, not to the 1.0.0 (7) ZIP.

Native macOS GUI, a Windows installer, and a physical Galaxy Z Flip6 are **NOT_RUN** here. This checklist does not perform those checks.

## Receipt

Copy this block into the pull request or the release notes when the clean-Mac install has been run. Leave it out until then. Do not put a computer name, user name, or serial number in the receipt.

```
Clean-Mac install receipt
Date:
Commit:
Download file:
Quarantine attribute present:
spctl exit and summary:
codesign verify exit:
First open Gatekeeper block: yes or no
Unauthenticated control POST status:
Result:
```

Until a completed receipt is stored, status stays **NOT_RUN**.
