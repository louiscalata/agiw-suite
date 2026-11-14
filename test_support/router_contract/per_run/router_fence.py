"""Contract fixture: the install fence writer, for Monitor tests only.

Not the router's installer.  An independent test double written from the Monitor's fence
reader (telemetry._router_install_state) and docs/router-state-contract.md.  Standard
library only.  Its presence beside pipeline_router_state.py is also what marks this
directory as a per-run generation for the tests.
"""
import json
import os
import re
import time
from pathlib import Path

FENCE_KIND = "codemode.router.install-fence.v1"
STATES = ("installing", "installed", "rolling-back", "rolled-back")
STAMP = re.compile(r"[0-9]{8}T[0-9]{6}Z\Z")
GENERATION = re.compile(r"[a-f0-9]{64}\Z")


def write(root, state, generation, stamp):
    """Publish install-fence.json under ``root``, bound to the owner.lock that is there now
    (``ownerLock`` is null when there is none)."""
    root = Path(root)
    if state not in STATES or not GENERATION.match(generation) or not STAMP.match(stamp):
        raise ValueError("invalid fence state, generation or stamp")
    try:
        lock = os.lstat(str(root / "owner.lock"))
        bound = {"dev": lock.st_dev, "ino": lock.st_ino}
    except FileNotFoundError:
        bound = None
    raw = json.dumps({"schemaVersion": 1, "kind": FENCE_KIND, "state": state, "generation": generation,
                      "stamp": stamp, "sinceUnix": time.time(), "ownerLock": bound},
                     sort_keys=True, separators=(",", ":")).encode("utf-8")
    path = root / "install-fence.json"
    staged = root / (".install-fence.json.staged-%d" % os.getpid())
    fd = os.open(str(staged), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(staged), str(path))
