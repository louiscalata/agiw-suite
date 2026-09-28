#!/bin/bash
set -euo pipefail

# Keep the user-owned menu bar application available after login and unexpected
# application failure. A normal Quit exits successfully and is not restarted.
action="${1:-}"
label="local.codemode.inference-monitor.login"
app_path="$HOME/Applications/Inference Monitor.app"
binary="$app_path/Contents/MacOS/InferenceMonitor"
agent_dir="$HOME/Library/LaunchAgents"
agent_path="$agent_dir/$label.plist"
state_dir="$HOME/.local/state/inference-monitor"
domain="gui/$(id -u)"
service="$domain/$label"
python_bin="/usr/bin/python3"
if [[ -x /opt/homebrew/bin/python3 ]]; then
  python_bin="/opt/homebrew/bin/python3"
fi

case "$action" in
  install)
    if [[ ! -x "$binary" ]]; then
      echo "Install the signed Inference Monitor app before enabling login recovery." >&2
      exit 1
    fi
    codesign --verify --strict "$app_path"
    mkdir -p "$agent_dir" "$state_dir"
    chmod 700 "$state_dir"
    if launchctl print "$service" >/dev/null 2>&1; then
      echo "The login agent is already loaded: $service" >&2
      exit 1
    fi
    if pgrep -f "$binary" >/dev/null 2>&1; then
      echo "Quit the existing Inference Monitor app before installing login recovery." >&2
      exit 1
    fi
    "$python_bin" - "$binary" "$agent_path" "$state_dir" <<'PY'
import os
from pathlib import Path
import plistlib
import stat
import sys
import tempfile

binary, destination, state = map(Path, sys.argv[1:])
config = {
    'Label': 'local.codemode.inference-monitor.login',
    'ProgramArguments': [str(binary)],
    'RunAtLoad': True,
    'KeepAlive': {'SuccessfulExit': False},
    'ThrottleInterval': 10,
    'LimitLoadToSessionType': 'Aqua',
    'ProcessType': 'Interactive',
    'StandardOutPath': str(state / 'launchd.stdout.log'),
    'StandardErrorPath': str(state / 'launchd.stderr.log'),
}
share_hosts = os.environ.get('AGIW_SHARE_HOSTS', '')
share_username = os.environ.get('AGIW_SHARE_USERNAME', '')
if bool(share_hosts) != bool(share_username):
    raise SystemExit('Set both AGIW_SHARE_HOSTS and AGIW_SHARE_USERNAME, or neither.')
if share_hosts:
    # Local account and LAN settings persist for launchd restarts; no password
    # is stored. The app validates them again before any share recovery action.
    config['EnvironmentVariables'] = {
        'AGIW_SHARE_HOSTS': share_hosts,
        'AGIW_SHARE_USERNAME': share_username,
    }
raw = plistlib.dumps(config, sort_keys=True)
if destination.exists() or destination.is_symlink():
    try:
        existing = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise SystemExit('Refusing to open an unsafe login agent')
    try:
        info = os.fstat(existing)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise SystemExit('Refusing to replace an unsafe login agent')
        with os.fdopen(os.dup(existing), 'rb') as source:
            if source.read() != raw:
                raise SystemExit('A different login agent already exists; inspect it before replacing')
        os.fchmod(existing, 0o600)
    finally:
        os.close(existing)
else:
    fd, temp = tempfile.mkstemp(prefix='.inference-monitor-login.', dir=destination.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, destination)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
PY
    plutil -lint "$agent_path"
    launchctl bootstrap "$domain" "$agent_path"
    launchctl print "$service" >/dev/null
    echo "Installed and loaded the per-user Inference Monitor login agent: $service"
    ;;
  status)
    if [[ -f "$agent_path" ]] && launchctl print "$service" >/dev/null 2>&1; then
      service_detail="$(launchctl print "$service")"
      if [[ "$service_detail" == *"state = running"* ]]; then
        echo "Login agent and app running: $service"
      elif [[ "$service_detail" == *"last exit code = 0"* ]]; then
        echo "Login agent loaded; app stopped after a normal Quit. Use launchctl kickstart $service to start it again."
        exit 1
      else
        echo "Login agent loaded but app is not running: $service" >&2
        exit 1
      fi
    elif [[ -f "$agent_path" ]]; then
      echo "Login agent installed but not loaded: $agent_path"
      exit 1
    else
      echo "Login agent not installed"
      exit 1
    fi
    ;;
  uninstall)
    if launchctl print "$service" >/dev/null 2>&1; then
      launchctl bootout "$service"
    fi
    if [[ -L "$agent_path" ]]; then
      echo "Refusing to remove a symlinked login agent." >&2
      exit 1
    fi
    if [[ -f "$agent_path" ]]; then
      rm -- "$agent_path"
    fi
    echo "Removed the per-user Inference Monitor login agent."
    ;;
  *)
    echo "Usage: $0 install|status|uninstall" >&2
    exit 2
    ;;
esac
