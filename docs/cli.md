# agiw command-line tool (v0.1)

`agiw` connects your Mac, Linux and Windows machines on the same LAN. Each machine runs a small peer link that
announces itself, pairs with your other machines through a 6-digit PIN, and answers paired machines with a
signed, read-only status: health and model names. It is standard-library Python 3.9+ with no dependencies, and
it reuses the suite's `peer_link.py` (protocol `agiw-peer/2`) unchanged apart from the host-kind default.

**What v0.1 does not do.** Status is **signed but not encrypted**: anyone capturing LAN traffic can read the
model names and states your machines report. v0.1 **dispatches no jobs**: it discovers, pairs and reads
status, and no route starts, loads or changes anything on another machine. IPv4 LAN only.

## Install

Available once published (replace `<tag>` with a release tag):

```sh
pipx install "git+https://github.com/louiscalata/agiw-suite@<tag>"
# or
uv tool install "git+https://github.com/louiscalata/agiw-suite@<tag>"
```

From a checkout: `pipx install .` (or `python3 -m pip install --user .`). Check it with `agiw --version`.

## First run and pairing

Do steps 1–2 on every machine, then pair each pair of machines once.

1. **Allow the two ports** (see [Firewall](#firewall)): tcp/8768 for signed status and pairing, udp/8769
   for discovery.
2. **Start the peer link**, in a terminal you keep open: `agiw serve`. Or start it at every login:
   `agiw service install` (user level, no admin rights). What it reports depends on the machine:
   - **Mac:** the local LM Studio model list (`--lmstudio URL` to change it; default `http://127.0.0.1:1234`).
   - **Linux:** the Linux observer's status file (`~/.local/state/agiw/linux-status.json`, or under an
     absolute `$XDG_STATE_HOME`) while it exists, otherwise a minimal status whose health and models are
     `unknown`.
   - **Windows:** the Windows observer's status file, which you name:
     `agiw serve --status-file "<observer folder>\state\peer-link-status.json"` (turn on the observer's
     `peerLinkStatus` option). Without it the peer link refuses to start, as before.
3. **Pair.** On machine A, list what it sees, then invite B by the name shown:

   ```sh
   agiw peers
   agiw pair studio-pc
   ```

   A shows a PIN, valid for 5 minutes. On B, run `agiw accept` and type it (add A's name if several
   invites wait). Three wrong PINs end the invite. Nothing pairs without a typed PIN.
4. **Check:** `agiw status` on either machine.

The PIN feeds SPAKE2, so a LAN attacker gets one online guess per attempt and nothing to brute-force later.

### Mac

```sh
pipx install "git+https://github.com/louiscalata/agiw-suite@<tag>"
agiw doctor
agiw service install        # or: agiw serve
agiw peers && agiw pair <linux or windows machine>
```

### Linux

```sh
pipx install "git+https://github.com/louiscalata/agiw-suite@<tag>"
agiw doctor
agiw service install        # systemd --user; see the lingering note below
agiw accept                 # when the other machine shows a PIN
```

A `systemd --user` service stops when you log out unless lingering is on for your account
(`loginctl enable-linger $USER`; some systems ask for an administrator). agiw does not change this.

### Windows

```powershell
pipx install "git+https://github.com/louiscalata/agiw-suite@<tag>"
agiw doctor
agiw service install --status-file "<observer folder>\state\peer-link-status.json"
agiw accept
```

## Commands

| Command | What it does |
| --- | --- |
| `agiw serve` | Run the peer link in the foreground (what the service runs). `--host`, `--name`, `--status-file`, `--lmstudio`. |
| `agiw pair <name>` | Invite a discovered machine; shows the PIN here. |
| `agiw accept [<name>]` | Type the PIN another machine shows. |
| `agiw unpair <name>` | Forget a machine. Run it on the other machine too. |
| `agiw peers [--json]` | Discovered and paired machines. |
| `agiw status [--json]` | `up`, `down` or `unknown` for each paired machine. |
| `agiw doctor` | Local read-only checks: Python, port occupancy, firewall hints, state folder and token privacy, service definition. |
| `agiw service install\|uninstall\|status` | User-level login service. |
| `agiw uninstall [--purge-keys [--yes]]` | Remove the service; optionally list, then delete, pairing keys. |

Global options: `--state-dir` (default below) and `--status-port` (default 8768). Exit codes: 0 done,
1 failed, 2 usage, 3 the peer link is not running here, 75 the state-folder lock is held by another serve.

**Status meanings.** `up`: a signed status reply was verified in the last 15 seconds. `down`: the machine
was heard on the LAN earlier but has been silent for over 60 seconds with no signed reply. `unknown`:
anything else, with the reason, such as not heard from since this peer link started, or heard but no
fresh signed reply (a refused request, or a machine without a status source).

All commands except `serve`, `doctor`, `service` and `uninstall` talk to your own peer link on
127.0.0.1. It must first prove that it holds the control token in your state folder, so another program on
the port learns nothing.

`agiw doctor` never contacts the peer link. It observes local file metadata and whether the status TCP and
discovery UDP ports are in use. A busy status port is reported as probably the peer link, without proving
which program owns it. Use `agiw status` for the live check.

## Firewall

agiw never changes firewall settings; `agiw doctor` prints these hints.

- **macOS:** with the firewall on, allow incoming connections for the Python that runs agiw
  (System Settings > Network > Firewall > Options).
- **Linux (ufw):** allow your LAN only, for example
  `sudo ufw allow from <your LAN>/24 to any port 8768 proto tcp` and the same for `port 8769 proto udp`.
- **Windows:** add inbound rules yourself, Private profile, in an administrator PowerShell:

  ```powershell
  New-NetFirewallRule -DisplayName "AGIW peer link TCP" -Direction Inbound -Protocol TCP -LocalPort 8768 -Profile Private -Action Allow
  New-NetFirewallRule -DisplayName "AGIW peer link UDP" -Direction Inbound -Protocol UDP -LocalPort 8769 -Profile Private -Action Allow
  ```

## Service management

`agiw service install` writes one definition under your own folders and starts it; nothing asks for admin
rights, sudo or UAC. The resolved absolute state folder is always stored in the definition, including when
you use the default. Options you pass (`--status-file`, `--name` and so on) go into it too.

| OS | Definition | Started with |
| --- | --- | --- |
| macOS | `~/Library/LaunchAgents/io.github.louiscalata.agiw.peer-link.plist`; log in `~/Library/Logs/agiw/` | `launchctl bootstrap gui/<uid>` |
| Linux | `~/.config/systemd/user/agiw-peer-link.service` | `systemctl --user enable`, then `restart` (also applies changed options on reinstall) |
| Windows | Task `\AGIW\PeerLink-<SID>` (your account's SID, logon trigger, least privilege); XML copy under `%LOCALAPPDATA%\AGIW\service\` | `schtasks /Create /XML`, then `/Run` |

On Windows, agiw reads your SID with `whoami /user /fo csv /nh` and uses that same account-specific task
name for install, status and uninstall. Installation refuses if the SID cannot be read. After `/Run`,
installation checks the locale-independent ScheduledTasks State enum through PowerShell for up to about
five seconds. It reports running only after observing `Running`; otherwise it reports the installed task
as not confirmed running, gives the last state or query failure, and exits 1. Status and uninstall use the
same State query; a failed query leaves status unknown and keeps the definition during uninstall.

`agiw service status` reports whether the definition exists and whether the service manager has it loaded
and running. `agiw service uninstall` stops it and checks that launchd no longer holds the job on macOS,
or that the service is stopped on Linux and Windows, before removing the definition. A waiting or exited
LaunchAgent is still loaded. If removal cannot be confirmed, agiw keeps the definition and reports how to stop it.

## State folder

Identity, control token and one trust record per paired machine, private to your account:

- macOS: `~/Library/Application Support/AGIW/peer-link`
- Linux: `~/.local/state/agiw/peer-link` (under an absolute `$XDG_STATE_HOME` when set)
- Windows: `%LOCALAPPDATA%\AGIW\peer-link`, with link keys sealed by DPAPI and pairing generations in
  `HKCU\Software\AGIW\PeerLink`

`agiw doctor` checks the folder's owner and permissions and the privacy of existing key files without
reading or unsealing their contents. It never repairs state or contacts the peer link; `agiw status` is the
live check. `agiw serve` repairs the state when started.

`agiw serve` holds an exclusive process lock for its whole run and refuses to start if that state folder is
already locked. The lock file sits next to the state folder, with `.agiw-lock` appended to its name (for
example, `peer-link.agiw-lock` beside `peer-link`), so it does not enter the peer link's state-file checks.
Lock refusal exits 75; systemd does not restart that exit and limits other failures to five starts in 300
seconds, with 30 seconds between restarts. The LaunchAgent and Windows task pass `serve --service`,
which prints the same lock-refusal message and exits 0. The LaunchAgent throttles crashes to 60 seconds;
the Windows task retries failures up to three times, one minute apart.

## Uninstall

```sh
agiw uninstall                       # remove the login service; keys stay
agiw uninstall --purge-keys          # removes service; key deletion is a dry run
agiw uninstall --purge-keys --yes    # deletes them (stop any running `agiw serve` first)
pipx uninstall agiw                  # or: uv tool uninstall agiw
```

`--purge-keys --yes` deletes only files the peer link creates and leaves links, junctions, reparse points and
unrecognized files in place. It first removes the login service, then must acquire the same process lock as
`agiw serve` before deleting keys. If the lock is held, it reports the service manager's result (removed,
none installed, or confirmed not running) and that no key file was deleted, even when that process uses
custom ports. It holds the
lock through deletion and removes the lock file after the state folder is gone. The selected and default
status TCP ports and the discovery UDP port must also all be free. Existing control tokens must pass the
privacy and validity checks; unsafe tokens and busy or unknown ports prevent key deletion. These checks
make no peer-link exchange. `--yes` requires `--purge-keys`.

Purge protects against `agiw serve` and the login service; if you started peer_link.py directly, stop it before purging.

If a key file cannot be deleted, purge reports the file and reason, continues deleting the others and
exits 1. On Windows it still attempts to revoke this account's pairing generations in the registry. If
revocation fails, it prints the file-deletion summary first, then the registry error and reason, and exits 1.

If you started the peer link with a non-default `--status-port`, pass the same `--status-port` to uninstall,
for example `agiw --status-port 8878 uninstall --purge-keys --yes`.
Your other machines keep their side: run `agiw unpair <this machine>` there.

## What leaves the machine

Discovery hellos (unsigned): node id, machine name, host kind and port. Signed status, to paired machines
only, after cutting it down to an allowlist: host name, platform, health, timestamps, model ids with
loaded/activity state, and a few GPU and memory numbers when the status source provides them. No
files, prompts, keys or paths are sent. `agiw doctor` prints your home folder as `~` and never prints keys,
tokens or share paths.
