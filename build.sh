#!/bin/bash
set -euo pipefail

# Installation requires a native Apple silicon shell; refuse before creating paths.
if [[ "$(uname -s)" != Darwin || "$(uname -m)" != arm64 ]]; then
    echo "Apple silicon is required. Run this installer in a native arm64 macOS terminal (not Rosetta)." >&2
    exit 2
fi

project_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
applications_dir="$HOME/Applications"
app_path="$applications_dir/Inference Monitor.app"
python_path="/opt/homebrew/bin/python3"
if [[ ! -x "$python_path" ]]; then python_path="/usr/local/bin/python3"; fi
if [[ ! -x "$python_path" ]]; then python_path="/usr/bin/python3"; fi
if [[ ! -x "$python_path" ]]; then
    echo "Python 3 is required to safely install Inference Monitor." >&2
    exit 1
fi
resources=(
    server.py telemetry.py machine_state.py agiw_status.py agiw-status activity.py client_models.py model_control.py
    durable_model_journal.py auto_unload.py online_code_repair.py nisi_v02.py
    windows_probe.py live_feed.py gpu_probe.py local_callers.py mem_guard.py peer_feed.py peer_link.py
    pair_status.py usage-format.mjs web/index.html web/app.js web/online-code-mode.mjs
    web/map-layout.mjs web/model-control-view.mjs web/style.css
    bundle_nisi.py bundled_components.py jev_connection.py web/components.html web/components.js web/components.css
)
for resource in "${resources[@]}" Info.plist Monitor.swift JevKeychain.swift freeze_build_inputs.py; do
    if [[ ! -f "$project_dir/$resource" || -L "$project_dir/$resource" ]]; then
        echo "Missing or symlinked build input: $resource" >&2
        exit 1
    fi
done
mkdir -p "$applications_dir"
stage_dir="$(mktemp -d "$applications_dir/.inference-monitor-build.XXXXXX")"
stage_dir="$(cd -- "$stage_dir" && pwd -P)"
trap 'rm -rf -- "$stage_dir"' EXIT
input_dir="$stage_dir/inputs"
# Retain the root and the helper itself before reading executable source.
"$python_path" - "$project_dir" "$input_dir" "${resources[@]}" Info.plist Monitor.swift JevKeychain.swift vendor/nisi <<'PYFREEZE'
import os
import stat
import sys

root = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
helper = os.open('freeze_build_inputs.py', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
with os.fdopen(helper, 'rb') as source:
    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
        raise SystemExit('Nonregular build input helper')
    namespace = {'__name__': 'freeze_build_inputs'}
    exec(compile(source.read(), 'freeze_build_inputs.py', 'exec'), namespace)
namespace['freeze_inputs'](root, sys.argv[2], sys.argv[3:])
PYFREEZE
stage_app="$stage_dir/Inference Monitor.app"
mkdir -p "$stage_app/Contents/MacOS" "$stage_app/Contents/Resources/web"
"$python_path" "$input_dir/bundle_nisi.py" --destination "$stage_app/Contents/Resources/nisi"

xcrun swiftc -O -target arm64-apple-macosx13.0 \
    -framework Cocoa -framework WebKit -framework Metal \
    "$input_dir/Monitor.swift" -o "$stage_app/Contents/MacOS/InferenceMonitor"
xcrun swiftc -O -target arm64-apple-macosx13.0 -framework Cocoa -framework Security \
    "$input_dir/JevKeychain.swift" -o "$stage_app/Contents/MacOS/JevKeychain"
if [[ "$(lipo -archs "$stage_app/Contents/MacOS/InferenceMonitor")" != arm64 ]]; then
    echo "Compiled app must contain only arm64; installation refused." >&2
    exit 1
fi
if [[ "$(lipo -archs "$stage_app/Contents/MacOS/JevKeychain")" != arm64 ]]; then
    echo "Jev helper must contain only arm64; installation refused." >&2
    exit 1
fi
cp "$input_dir/Info.plist" "$stage_app/Contents/Info.plist"
for resource in "${resources[@]}"; do
    cp "$input_dir/$resource" "$stage_app/Contents/Resources/$resource"
done
plutil -lint "$stage_app/Contents/Info.plist"
sign_identity="${MONITOR_SIGN_IDENTITY:-}"
if [[ -n "$sign_identity" ]]; then
    if [[ ! "$sign_identity" =~ ^[0-9a-fA-F]{40}$ ]]; then
        echo "MONITOR_SIGN_IDENTITY must be an exact 40-character certificate SHA-1." >&2
        exit 1
    fi
    # Local installation only. The explicit identity gives macOS a stable
    # certificate-based requirement across source rebuilds; it does not
    # notarize or publish the app.
    codesign --force --sign "$sign_identity" --options runtime --timestamp=none "$stage_app/Contents/MacOS/JevKeychain"
    codesign --force --sign "$sign_identity" --options runtime --timestamp=none "$stage_app"
    echo "Signing mode: explicit certificate identity ($sign_identity)."
else
    codesign --force --sign - "$stage_app/Contents/MacOS/JevKeychain"
    codesign --force --sign - "$stage_app"
    echo "Signing mode: ad hoc (the designated requirement is build-specific)."
fi
codesign --verify --deep --strict "$stage_app"

# Hold the same observer-only lock across replacement. A running application is
# never overwritten, and a simultaneous launch cannot overlap installation.
"$python_path" - "$stage_app" "$app_path" <<'PY'
import fcntl
import os
from pathlib import Path
import shutil
import sys
import tempfile

source, destination = map(Path, sys.argv[1:])
state = Path.home() / '.local/state/inference-monitor'
state.mkdir(mode=0o700, parents=True, exist_ok=True)
with (state / 'app.lock').open('a+') as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Inference Monitor is running. Quit it before rebuilding; the installed app was not changed.')
    if destination.is_symlink():
        raise SystemExit('Refusing to replace a symlink at the application destination.')
    backup = None
    if destination.exists():
        backup_root = Path(tempfile.mkdtemp(prefix='.inference-monitor-backup.', dir=destination.parent))
        backup = backup_root / destination.name
        os.rename(destination, backup)
    try:
        os.rename(source, destination)
    except BaseException:
        if backup is not None:
            os.rename(backup, destination)
            backup.parent.rmdir()
        raise
    if backup is not None:
        shutil.rmtree(backup.parent)
print(f'Built, signed, and installed: {destination}')
print('The app has not been launched.')
PY
