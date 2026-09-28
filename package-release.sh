#!/bin/bash
# Build a Developer ID signed GitHub-download candidate without changing the
# installed app or its login agent. Notarization is a separate release gate.
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
sign_identity="${MONITOR_RELEASE_SIGN_IDENTITY:-}"
if [[ ! "$sign_identity" =~ ^[0-9a-fA-F]{40}$ ]]; then
    echo "Set MONITOR_RELEASE_SIGN_IDENTITY to an exact Developer ID Application certificate SHA-1." >&2
    exit 2
fi
if [[ "$(uname -s)" != Darwin ]]; then
    echo "A macOS host is required to build the release candidate." >&2
    exit 2
fi
for required_cmd in xcrun xcodebuild xcode-select codesign hdiutil plutil shasum git cmp lipo /usr/bin/python3; do
    if ! command -v "$required_cmd" >/dev/null 2>&1; then
        echo "Missing required command: $required_cmd" >&2
        exit 2
    fi
done
source_dirty=0
if [[ -n "$(git -C "$project_dir" status --porcelain --untracked-files=normal)" ]]; then
    source_dirty=1
fi
if [[ "${MONITOR_RELEASE_ALLOW_DIRTY:-0}" != 1 && "$source_dirty" == 1 ]]; then
    echo "Release source is dirty. Commit and review it before building a distributable candidate." >&2
    exit 2
fi

resources=(
    server.py telemetry.py activity.py client_models.py model_control.py
    durable_model_journal.py auto_unload.py online_code_repair.py nisi_v02.py
    windows_probe.py live_feed.py gpu_probe.py local_callers.py mem_guard.py
    usage-format.mjs web/index.html web/app.js web/online-code-mode.mjs
    web/map-layout.mjs web/model-control-view.mjs web/style.css
)
for resource in "${resources[@]}" Info.plist Monitor.swift; do
    if [[ ! -f "$project_dir/$resource" ]]; then
        echo "Missing release input: $resource" >&2
        exit 2
    fi
done

version="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$project_dir/Info.plist")"
build="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$project_dir/Info.plist")"
bundle_id="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$project_dir/Info.plist")"
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || [[ ! "$build" =~ ^[1-9][0-9]*$ ]]; then
    echo "Release Info.plist needs a three-part version and a positive integer build." >&2
    exit 2
fi
if [[ "$bundle_id" != local.codemode.inference-monitor ]]; then
    echo "Unexpected bundle ID: $bundle_id" >&2
    exit 2
fi
arch="$(uname -m)"
if [[ "$arch" != arm64 && "$arch" != x86_64 ]]; then
    echo "Unsupported release architecture: $arch" >&2
    exit 2
fi
source_commit="$(git -C "$project_dir" rev-parse HEAD)"
script_sha256="$(shasum -a 256 "$project_dir/package-release.sh" | awk '{print $1}')"
developer_dir="$(xcode-select -p)"
xcode_version="$(xcodebuild -version)"
sdk_version="$(xcrun --sdk macosx --show-sdk-version)"
swiftc_path="$(xcrun --find swiftc)"
swiftc_version="$(xcrun swiftc --version 2>/dev/null | head -n 1)"

output_dir="${MONITOR_RELEASE_OUTPUT_DIR:-$project_dir/dist}"
if [[ -L "$output_dir" ]]; then
    echo "Refusing a symlinked release output directory." >&2
    exit 2
fi
mkdir -p "$output_dir"
output_dir="$(cd -- "$output_dir" && pwd -P)"
release_name="AGIW-Inference-Monitor-${version}-${arch}"
if [[ "$source_dirty" == 1 ]]; then
    release_name="$release_name-dirty"
fi
release_dmg="$output_dir/$release_name.dmg"
release_manifest="$output_dir/$release_name.manifest.json"
if [[ -e "$release_dmg" || -L "$release_dmg" || -e "$release_manifest" || -L "$release_manifest" ]]; then
    echo "Release output already exists; choose a new version or output directory." >&2
    exit 2
fi

stage_dir="$(mktemp -d "$output_dir/.release-build.XXXXXX")"
mounted=0
mount_dir="$stage_dir/mounted"
cleanup() {
    if [[ "$mounted" == 1 ]]; then
        hdiutil detach "$mount_dir" >/dev/null 2>&1 || true
    fi
    rm -rf -- "$stage_dir"
}
trap cleanup EXIT
input_dir="$stage_dir/inputs"
payload_dir="$stage_dir/payload"
stage_app="$payload_dir/Inference Monitor.app"
stage_dmg="$stage_dir/$release_name.dmg"
mkdir -p "$input_dir/web" "$stage_app/Contents/MacOS" "$stage_app/Contents/Resources/web" "$mount_dir"
for resource in "${resources[@]}" Info.plist Monitor.swift; do
    cp "$project_dir/$resource" "$input_dir/$resource"
done

xcrun swiftc -O -target "$arch-apple-macosx13.0" \
    -framework Cocoa -framework WebKit \
    "$input_dir/Monitor.swift" -o "$stage_app/Contents/MacOS/InferenceMonitor"
cp "$input_dir/Info.plist" "$stage_app/Contents/Info.plist"
for resource in "${resources[@]}"; do
    cp "$input_dir/$resource" "$stage_app/Contents/Resources/$resource"
done
plutil -lint "$stage_app/Contents/Info.plist"

codesign --force --sign "$sign_identity" --options runtime --timestamp "$stage_app"
codesign --verify --strict --verbose=2 "$stage_app"
app_signature="$(codesign -dv --verbose=4 "$stage_app" 2>&1)"
if ! grep -q '^Timestamp=' <<< "$app_signature" ||
   ! grep -q '^Authority=Developer ID Application:' <<< "$app_signature"; then
    echo "App lacks a secure Developer ID Application signature; release candidate rejected." >&2
    exit 1
fi
if [[ "$(lipo -archs "$stage_app/Contents/MacOS/InferenceMonitor")" != "$arch" ]]; then
    echo "Compiled app architecture differs from the selected release architecture." >&2
    exit 1
fi

cat > "$payload_dir/INSTALL.txt" <<'EOF'
AGIW Suite Inference Monitor — GitHub download candidate

Copy Inference Monitor.app to Applications, then open it. This build observes
local inference on this Mac. Model serving, Nisi/Jev routing, Windows worker,
SharedChami recovery, and optional controls require separately installed and
configured components.
EOF
hdiutil create -volname "AGIW Inference Monitor $version" -srcfolder "$payload_dir" \
    -format UDZO -fs HFS+ "$stage_dmg" >/dev/null
codesign --force --sign "$sign_identity" --timestamp \
    --identifier "$bundle_id.dmg" "$stage_dmg"
codesign --verify --verbose=2 "$stage_dmg"
dmg_signature="$(codesign -dv --verbose=4 "$stage_dmg" 2>&1)"
if ! grep -q '^Timestamp=' <<< "$dmg_signature" ||
   ! grep -q '^Authority=Developer ID Application:' <<< "$dmg_signature"; then
    echo "DMG lacks a secure Developer ID Application signature; release candidate rejected." >&2
    exit 1
fi

hdiutil attach -readonly -nobrowse -mountpoint "$mount_dir" "$stage_dmg" >/dev/null
mounted=1
mounted_app="$mount_dir/Inference Monitor.app"
codesign --verify --strict --verbose=2 "$mounted_app"
if [[ "$(lipo -archs "$mounted_app/Contents/MacOS/InferenceMonitor")" != "$arch" ]]; then
    echo "Mounted app architecture does not match the release label." >&2
    exit 1
fi
if ! cmp -s "$input_dir/Info.plist" "$mounted_app/Contents/Info.plist"; then
    echo "Mounted app version metadata differs from the frozen input." >&2
    exit 1
fi
for resource in "${resources[@]}"; do
    if ! cmp -s "$input_dir/$resource" "$mounted_app/Contents/Resources/$resource"; then
        echo "Mounted app resource differs from the frozen input: $resource" >&2
        exit 1
    fi
done
hdiutil detach "$mount_dir" >/dev/null
mounted=0

if [[ "$(git -C "$project_dir" rev-parse HEAD)" != "$source_commit" ]] ||
   [[ "$(shasum -a 256 "$project_dir/package-release.sh" | awk '{print $1}')" != "$script_sha256" ]] ||
   [[ "$(xcode-select -p)" != "$developer_dir" ]] ||
   [[ "$(xcodebuild -version)" != "$xcode_version" ]] ||
   [[ "$(xcrun --sdk macosx --show-sdk-version)" != "$sdk_version" ]] ||
   [[ "$(xcrun --find swiftc)" != "$swiftc_path" ]] ||
   [[ "$(xcrun swiftc --version 2>/dev/null | head -n 1)" != "$swiftc_version" ]]; then
    echo "Source revision, release script, or selected toolchain changed during the build." >&2
    exit 1
fi
for resource in "${resources[@]}" Info.plist Monitor.swift; do
    if ! cmp -s "$project_dir/$resource" "$input_dir/$resource"; then
        echo "Release input changed during the build: $resource" >&2
        exit 1
    fi
done
if [[ "$source_dirty" == 0 && -n "$(git -C "$project_dir" status --porcelain --untracked-files=normal)" ]]; then
    echo "Source became dirty during the build." >&2
    exit 1
fi

dmg_sha256="$(shasum -a 256 "$stage_dmg" | awk '{print $1}')"
/usr/bin/python3 - "$stage_dir/manifest.json" "$input_dir" "$source_commit" "$source_dirty" "$script_sha256" "$version" "$build" "$bundle_id" "$arch" "$developer_dir" "$xcode_version" "$sdk_version" "$swiftc_path" "$swiftc_version" "$dmg_sha256" "${resources[@]}" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

out, frozen, commit, dirty, script_hash, version, build, bundle_id, arch, developer_dir, xcode_version, sdk_version, swiftc_path, swiftc_version, dmg_hash, *resources = sys.argv[1:]
frozen = Path(frozen)
inputs = resources + ['Info.plist', 'Monitor.swift']
# The manifest records exact relative inputs. The caller passes source files by
# name so no owner's absolute path is written into the distributable metadata.
record = {
    'kind': 'agiw.monitor.release-candidate.v1',
    'sourceCommit': commit,
    'sourceDirty': dirty == '1',
    'releaseScriptSha256': script_hash,
    'version': version,
    'build': build,
    'bundleId': bundle_id,
    'architecture': arch,
    'developerDir': developer_dir,
    'xcodeVersion': xcode_version,
    'macosSdkVersion': sdk_version,
    'swiftcPath': swiftc_path,
    'swiftcVersion': swiftc_version,
    'dmgSha256BeforeNotarization': dmg_hash,
    'candidateStatus': 'signed_unnotarized',
    'inputSha256': {
        name: hashlib.sha256((frozen / name).read_bytes()).hexdigest()
        for name in inputs
    },
}
Path(out).write_text(json.dumps(record, indent=2, sort_keys=True) + '\n')
PY
mv "$stage_dmg" "$release_dmg"
mv "$stage_dir/manifest.json" "$release_manifest"
echo "Built timestamped, signed release candidate: $release_dmg"
echo "SHA-256 before notarization: $dmg_sha256"
echo "Notarization, staple, clean-Mac assessment, and publication remain pending."
