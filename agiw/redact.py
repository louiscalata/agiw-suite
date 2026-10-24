"""What the CLI prints about this machine: the home folder as ~, never keys, tokens or share paths."""
from __future__ import annotations

import os
import re
from pathlib import Path, PurePath
from typing import Optional

HIDDEN_PATH = "(a folder outside your home; not shown)"

# 32+ hex characters: control tokens, link keys, nonces and proofs. Node UUIDs (dashed) are not matched.
_HEX = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{32,}(?![0-9A-Fa-f])")
_SEALED = re.compile(r"dpapi1:[A-Za-z0-9+/=]+")
_UNC = re.compile(r"\\\\[^\\\s]+\\[^\s'\"]*")                       # \\server\share\...
_NETWORK_URL = re.compile(r"(?i)\b(?:smb|cifs|afp|nfs)://[^\s'\"]+")  # smb://server/share
_MOUNTED = re.compile(r"(?:/Volumes|/mnt|/media|/run/user/\d+/gvfs)/[^\s'\"]+")  # mounted shares and volumes


def _home_variants(home: str) -> list:
    variants = {home, home.rstrip("/\\")}
    try:
        variants.add(os.path.realpath(home))
    except (OSError, ValueError):
        pass
    if "\\" in home:
        variants.add(home.replace("\\", "/"))
    return sorted((v for v in variants if len(v.strip("/\\")) >= 2), key=len, reverse=True)


def redact(text: str, home: Optional[str] = None) -> str:
    """The home folder becomes ~; secrets and network or mounted share paths are replaced."""
    home = str(Path.home()) if home is None else home
    out = str(text)
    for variant in _home_variants(home):
        flags = re.IGNORECASE if "\\" in variant or re.match(r"^[A-Za-z]:", variant) else 0
        out = re.sub(re.escape(variant) + r"(?=$|[\\/\s'\"),:;])", "~", out, flags=flags)
    out = _SEALED.sub("[sealed secret]", out)
    out = _HEX.sub("[redacted]", out)
    out = _UNC.sub("[share path]", out)
    out = _NETWORK_URL.sub("[share path]", out)
    return _MOUNTED.sub("[share path]", out)


def show_path(path: PurePath, home: Optional[str] = None) -> str:
    """A path for display: inside the home folder as ~/..., anything else described, not printed."""
    home_path = Path(str(Path.home()) if home is None else home)
    try:
        relative = Path(os.path.abspath(str(path))).relative_to(os.path.abspath(str(home_path)))
    except ValueError:
        return HIDDEN_PATH
    return "~" if str(relative) in ("", ".") else str(Path("~") / relative)
