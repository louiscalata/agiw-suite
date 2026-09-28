#!/bin/bash
set -euo pipefail

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
for resource in server.py telemetry.py activity.py client_models.py model_control.py durable_model_journal.py auto_unload.py online_code_repair.py nisi_v02.py windows_probe.py live_feed.py gpu_probe.py local_callers.py mem_guard.py usage-format.mjs web/index.html web/app.js web/online-code-mode.mjs web/map-layout.mjs web/model-control-view.mjs web/style.css Info.plist; do
    if [[ ! -f "$project_dir/$resource" ]]; then
        echo "Missing required resource: $resource" >&2
        exit 1
    fi
done
mkdir -p "$applications_dir"
stage_dir="$(mktemp -d "$applications_dir/.inference-monitor-build.XXXXXX")"
trap 'rm -rf -- "$stage_dir"' EXIT
stage_app="$stage_dir/Inference Monitor.app"
mkdir -p "$stage_app/Contents/MacOS" "$stage_app/Contents/Resources/web"

xcrun swiftc -O -target "$(uname -m)-apple-macosx13.0" \
    -framework Cocoa -framework WebKit \
    "$project_dir/Monitor.swift" -o "$stage_app/Contents/MacOS/InferenceMonitor"
cp "$project_dir/Info.plist" "$stage_app/Contents/Info.plist"
cp "$project_dir/server.py" "$project_dir/telemetry.py" "$project_dir/activity.py" "$project_dir/client_models.py" "$project_dir/model_control.py" "$project_dir/durable_model_journal.py" "$project_dir/auto_unload.py" "$project_dir/online_code_repair.py" "$project_dir/nisi_v02.py" "$project_dir/windows_probe.py" "$project_dir/live_feed.py" "$project_dir/gpu_probe.py" "$project_dir/local_callers.py" "$project_dir/mem_guard.py" "$project_dir/usage-format.mjs" "$stage_app/Contents/Resources/"
cp "$project_dir/web/index.html" "$project_dir/web/app.js" "$project_dir/web/online-code-mode.mjs" "$project_dir/web/map-layout.mjs" "$project_dir/web/model-control-view.mjs" "$project_dir/web/style.css" "$stage_app/Contents/Resources/web/"
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
    codesign --force --sign "$sign_identity" --options runtime --timestamp=none "$stage_app"
    echo "Signing mode: explicit certificate identity ($sign_identity)."
else
    codesign --force --sign - "$stage_app"
    echo "Signing mode: ad hoc (the designated requirement is build-specific)."
fi
codesign --verify --strict "$stage_app"

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
