"""AGIW peer link: discover AGIW machines on the LAN, pair them with a 6-digit PIN, auto-connect.

Protocol agiw-peer/2 (stdlib only; macOS and Windows). The user experience follows NVIDIA PAIR's
cluster pairing (nvpair-cluster-manager spec, sections 7.4 and 7.5). Its documented weakness is closed:
PAIR's 6-digit PIN feeds EAP-NOOB and can be brute-forced offline by an active LAN attacker. Here the
PIN feeds SPAKE2, a password-authenticated key exchange, so an attacker gets one online guess per
attempt and nothing to brute-force afterwards.

  * Identity. A random UUID per install (identity.json). The UUID is the principal, never the hostname.
  * Discovery. Every 5 s each node broadcasts an unsigned hello on UDP 8769: uuid, name, host kind,
    port. A hello proves nothing. It only puts a machine in the "available" list or suggests a new
    address for a paired peer, and that address is used only after an authenticated status reply
    comes back from it.
  * Pairing. On machine A the user asks to pair with B. A shows a 6-digit PIN for 5 minutes and
    tells B an invite is waiting. On B the user types the PIN. B and A run SPAKE2 (RFC 3526 2048-bit
    group) and exchange key-confirmation MACs bound to both UUIDs, names and host kinds. Each side
    then stores trusted/<peer-uuid>.json with a fresh 256-bit link key. 3 wrong attempts end the
    invite. Nothing pairs without a typed PIN.
  * Status. GET /agiw-peer/v2/status, signed with the requester's link key. The reply is signed
    over the request nonce. Read-only: there is no route that changes models or settings.
  * Unpair. Delete the trust file; the peer's next request gets 403. A known UUID with a different
    key is refused until the user pairs again.
  * Control. Invite, accept, unpair and the peer list are served only to 127.0.0.1 callers that
    prove they hold the control token in the state directory, so a web page cannot drive pairing.
    The daemon re-reads that token for every exchange, so replacing the file revokes the old token.
  * Secrets. Key material is verified before each use: POSIX owner and mode plus macOS extended ACLs
    (cached by inode and ctime, which any permission change alters), or on Windows the owner SID and
    every allow-ACE SID, read with the Win32 security API each time. Before a widened Windows folder is
    tightened (which re-inherits into everything below it and would hide the exposure), every secret
    below it that others could read is retired on disk: the token deleted, trust records quarantined.
    That decision lives in the files, so a restart cannot bring an exposed secret back.

Signatures authenticate but do not encrypt: someone capturing LAN traffic can read model names and
states. Outgoing status is cut down to an allowlist (project_status), so nothing else leaves the machine. A LAN host that relays a paired peer's traffic can only pass on that peer's own
signed replies, or drop them, which shows as an error. IPv4 only.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import math
import ntpath
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.request
import uuid as uuidlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

PROTOCOL = "agiw-peer/2"
BEACON_PORT = 8769
STATUS_PORT = 8768
PREFIX = "/agiw-peer/v2"
BEACON_INTERVAL = 5.0
POLL_INTERVAL = 5.0
CONNECTED_WITHIN = 15.0
FORGET_AVAILABLE_AFTER = 60.0
CLOCK_WINDOW = 30.0
INVITE_TTL = 300.0
INVITE_ATTEMPTS = 3
MAX_DATAGRAM = 1024
MAX_STATUS_BYTES = 65536
MAX_BODY = 4096
MAX_MODELS = 32
MAX_PEERS = 32
MAX_INBOUND_INVITES = 4
MAX_CONNECTIONS = 8
PER_SOURCE_CONNECTIONS = 2
CONTROL_CHALLENGE_TTL = 10.0
REQUEST_DEADLINE = 6.0   # absolute, from accept to close, so drip-fed headers cannot hold a slot
FETCH_DEADLINE = 4.0     # absolute, from connect to last byte
STATUS_MAX_AGE = 60.0    # a status whose observedAt is older (or further ahead) than this is refused
HOST_KINDS = ("mac", "windows", "linux")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")

StatusProvider = Callable[[], dict]

# ---------- SPAKE2 over the RFC 3526 2048-bit MODP group (prime-order subgroup of quadratic residues) ----------

P = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
    "15728E5A8AACAA68FFFFFFFFFFFFFFFF", 16)
Q = (P - 1) // 2
G = 4  # 2^2 generates the order-Q subgroup


def _hash_to_group(label: bytes) -> int:
    """A subgroup element with unknown discrete log: expand the label to 2304 bits, reduce, square."""
    stream = b"".join(hashlib.sha512(label + i.to_bytes(4, "big")).digest() for i in range(5))
    return pow(int.from_bytes(stream[:288], "big") % P, 2, P)


M = _hash_to_group(b"agiw-peer/2 SPAKE2 M")
N = _hash_to_group(b"agiw-peer/2 SPAKE2 N")
ELEMENT_HEX = 512


def _in_group(value: int) -> bool:
    return 1 < value < P - 1 and pow(value, Q, P) == 1


def _password_scalar(pin: str, inviter: str, joiner: str, invite: str) -> int:
    """w is bound to both UUIDs and this invite, so one invite's transcript says nothing about another."""
    salt = f"{PROTOCOL} w|{inviter}|{joiner}|{invite}".encode()
    return int.from_bytes(hashlib.pbkdf2_hmac("sha512", pin.encode(), salt, 10000), "big") % Q


def _lp(*parts: bytes) -> bytes:
    return b"".join(len(p).to_bytes(4, "big") + p for p in parts)


def _hex(value: int) -> str:
    return format(value, "0512x")


def _element(text: Any) -> int:
    if not isinstance(text, str) or len(text) != ELEMENT_HEX or not re.fullmatch(r"[0-9a-f]+", text):
        raise ValueError("bad group element")
    value = int(text, 16)
    if not _in_group(value):
        raise ValueError("element outside the group")
    return value


class Spake:
    """One side of SPAKE2 (RFC 9382 structure on a MODP group). role 'joiner' sends X = g^x M^w,
    role 'inviter' sends Y = g^y N^w. Python's pow() is not constant time; that is accepted because a
    PIN lives for one 5-minute invite with at most 3 attempts, and nothing secret outlives it."""

    def __init__(self, role: str, pin: str, inviter: dict, joiner: dict, invite: str):
        self.role, self.inviter, self.joiner, self.invite = role, inviter, joiner, invite
        self.w = _password_scalar(pin, inviter["node"], joiner["node"], invite)
        self.secret = secrets.randbelow(Q - 1) + 1
        blind = M if role == "joiner" else N
        self.public = pow(G, self.secret, P) * pow(blind, self.w, P) % P

    def finish(self, other_hex: Any) -> dict:
        other = _element(other_hex)
        unblind = N if self.role == "joiner" else M
        k = pow(other * pow(unblind, -self.w, P) % P, self.secret, P)
        x, y = (self.public, other) if self.role == "joiner" else (other, self.public)
        info = lambda d: canonical({"node": d["node"], "name": d["name"], "host": d["host"]})  # noqa: E731
        transcript = _lp(PROTOCOL.encode(), self.invite.encode(), info(self.joiner), info(self.inviter),
                         _hex(x).encode(), _hex(y).encode(), _hex(k).encode(), self.w.to_bytes(256, "big"))
        digest = hashlib.sha512(transcript).digest()
        ke, ka = digest[:32], digest[32:]
        kdf = lambda key, label: hmac.new(key, label, hashlib.sha256).digest()  # noqa: E731
        kc_inviter, kc_joiner = kdf(ka, b"ConfirmationKeys inviter"), kdf(ka, b"ConfirmationKeys joiner")
        # Ke becomes the link key only after both confirmations verify; callers pin nothing before that.
        return {"link": kdf(ke, b"agiw-peer link key"),
                "inviterConfirm": hmac.new(kc_inviter, transcript, hashlib.sha256).hexdigest(),
                "joinerConfirm": hmac.new(kc_joiner, transcript, hashlib.sha256).hexdigest(),
                # Sent by the inviter only after it has pinned; the joiner pins only after verifying it.
                "finishAck": hmac.new(kc_inviter, b"pinned\n" + transcript, hashlib.sha256).hexdigest()}


# ---------- identity and trust store ----------

def default_state_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "AGIW" / "peer-link"
    if sys.platform.startswith("linux"):
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
        if not base.is_absolute():
            base = Path.home() / ".local" / "state"
        return base / "agiw" / "peer-link"
    return Path.home() / "Library" / "Application Support" / "AGIW" / "peer-link"


class LooseSecret(PermissionError):
    """Key material that someone else could have read. It is never used again: tokens are rotated and
    trust records quarantined (re-pairing required), because tightening permissions cannot un-read it."""


_verified: dict[str, tuple] = {}  # POSIX only: path -> (inode, ctime_ns); any chmod or ACL edit changes ctime
# One control-token read or rotation, or one Windows folder tightening, at a time in this process.
# Reentrant: load_control_token holds it while _restrict_dir and _write_private tighten under it.
_token_lock = threading.RLock()
_quarantine_lock = threading.Lock()
_quarantined: dict[str, list[str]] = {}  # trusted folder -> node UUIDs this process quarantined, newest last

# Windows: owners and DACLs are read with the Win32 security API (ctypes, stdlib) by SID, never by
# parsing account names, and on every use. Nothing is cached there: Windows st_ctime is the creation
# time, so an access-list edit leaves (inode, ctime) unchanged.
_SYSTEM_SID, _ADMINS_SID = "S-1-5-18", "S-1-5-32-544"
_OWNER_RIGHTS_SID = "S-1-3-4"       # Refers to the object owner, which must be this account first.
_ALLOW_ACES = (0x0, 0x9)            # ACCESS_ALLOWED, ACCESS_ALLOWED_CALLBACK: the SID follows the mask
_ALLOW_OBJECT_ACES = (0x5, 0xB)     # ACCESS_ALLOWED_OBJECT, ACCESS_ALLOWED_CALLBACK_OBJECT
_DENY_ACES = (0x1, 0x6, 0xA, 0xC)   # the deny forms of the four above
_win_api_cache: list = []
_win_user: list[str] = []
_win_tightened: set[str] = set()    # folders this process has set an exact DACL on (still verified every call)


def _is_reparse(info: Any) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def _ace_allow_sid(ace: bytes) -> bytes | None:
    """The SID an allow ACE grants access to, or None for a deny ACE. Any other entry type, or one
    that does not parse, raises: an access list that cannot be read cannot be vouched for."""
    size = int.from_bytes(ace[2:4], "little") if len(ace) >= 4 else 0
    if not 8 <= size <= len(ace):
        raise LooseSecret("malformed access entry")
    kind, ace = ace[0], ace[:size]
    if kind in _DENY_ACES:
        return None
    if kind in _ALLOW_ACES:
        offset = 8
    elif kind in _ALLOW_OBJECT_ACES:
        if size < 12:
            raise LooseSecret("malformed access entry")
        flags = int.from_bytes(ace[8:12], "little")
        offset = 12 + (16 if flags & 1 else 0) + (16 if flags & 2 else 0)  # ObjectType, InheritedObjectType
    else:
        raise LooseSecret(f"unrecognised access entry type {kind}")
    if size < offset + 8 or ace[offset] != 1 or ace[offset + 1] > 15:  # SID revision 1, <= 15 sub-authorities
        raise LooseSecret("malformed SID in an access entry")
    end = offset + 8 + 4 * ace[offset + 1]
    if end > size:
        raise LooseSecret("malformed SID in an access entry")
    return ace[offset:end]


def _win_api():
    """advapi32 and kernel32 with explicit prototypes. Only ever called on Windows."""
    if not _win_api_cache:
        import ctypes
        from ctypes import wintypes
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        out = ctypes.POINTER(ctypes.c_void_p)
        for fn, args, result in (
                (kernel32.GetCurrentProcess, [], wintypes.HANDLE),
                (kernel32.CloseHandle, [wintypes.HANDLE], wintypes.BOOL),
                (kernel32.LocalFree, [ctypes.c_void_p], ctypes.c_void_p),
                (kernel32.GetSystemDirectoryW, [wintypes.LPWSTR, wintypes.UINT], wintypes.UINT),
                (advapi32.OpenProcessToken, [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)],
                 wintypes.BOOL),
                (advapi32.GetTokenInformation, [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                                ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
                (advapi32.ConvertSidToStringSidW, [ctypes.c_void_p, out], wintypes.BOOL),
                (advapi32.GetSecurityInfo, [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD, out, out, out, out, out],
                 wintypes.DWORD),
                (advapi32.GetNamedSecurityInfoW, [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, out, out, out,
                                                  out, out], wintypes.DWORD),
                (advapi32.GetAclInformation, [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_int],
                 wintypes.BOOL),
                (advapi32.GetAce, [ctypes.c_void_p, wintypes.DWORD, out], wintypes.BOOL),
                (crypt32.CryptProtectData, [ctypes.POINTER(_DataBlob.get()), wintypes.LPCWSTR,
                                            ctypes.POINTER(_DataBlob.get()), ctypes.c_void_p, ctypes.c_void_p,
                                            wintypes.DWORD, ctypes.POINTER(_DataBlob.get())], wintypes.BOOL),
                (crypt32.CryptUnprotectData, [ctypes.POINTER(_DataBlob.get()), ctypes.c_void_p,
                                              ctypes.POINTER(_DataBlob.get()), ctypes.c_void_p, ctypes.c_void_p,
                                              wintypes.DWORD, ctypes.POINTER(_DataBlob.get())], wintypes.BOOL)):
            fn.argtypes, fn.restype = args, result
        _win_api_cache.append((ctypes, wintypes, advapi32, kernel32))
        _win_crypt32.append(crypt32)
    return _win_api_cache[0]


class _DataBlob:
    """DATA_BLOB {DWORD cbData; BYTE *pbData}, built lazily so the module imports on any OS."""
    _cls: list = []

    @classmethod
    def get(cls):
        if not cls._cls:
            import ctypes
            from ctypes import wintypes

            class DATA_BLOB(ctypes.Structure):
                _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
            cls._cls.append(DATA_BLOB)
        return cls._cls[0]


_win_crypt32: list = []
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


def _win_dpapi(data: bytes, entropy: bytes, protect: bool) -> bytes:
    """CryptProtectData / CryptUnprotectData for the current Windows account, no UI. Only on Windows."""
    ctypes, _wintypes, _advapi32, kernel32 = _win_api()
    crypt32 = _win_crypt32[0]
    blob_type = _DataBlob.get()

    def blob(raw: bytes):
        buffer = ctypes.create_string_buffer(raw, len(raw))
        return blob_type(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer

    data_in, keep_in = blob(data)
    extra, keep_extra = blob(entropy)
    result = blob_type()
    call = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = call(ctypes.byref(data_in), None, ctypes.byref(extra), None, None, _CRYPTPROTECT_UI_FORBIDDEN,
              ctypes.byref(result))
    del keep_in, keep_extra
    if not ok:
        raise LooseSecret("this secret was not sealed by this Windows account") if not protect \
            else PermissionError("DPAPI could not seal the secret")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


def _win_protect(data: bytes, entropy: bytes) -> bytes:
    return _win_dpapi(data, entropy, True)


def _win_unprotect(blob: bytes, entropy: bytes) -> bytes:
    return _win_dpapi(blob, entropy, False)


SEAL_PREFIX = "dpapi1:"

# Windows: each trust record also carries a random generation that must match the one kept for that peer
# in this account's registry (HKCU, outside the state folder, writable only by this account and admins).
# Unpairing or quarantining deletes the registry value, so restoring an exact old record file - even a
# genuine sealed one whose link key a former peer still knows - can never bring that trust back.
_GENERATIONS = r"Software\AGIW\PeerLink\TrustGenerations"


def _gen_get(node: str) -> str | None:
    if os.name != "nt":
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _GENERATIONS) as key:
            value, kind = winreg.QueryValueEx(key, node)
    except FileNotFoundError:
        return None
    if kind != winreg.REG_SZ or not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        return None
    return value


def _gen_set(node: str, generation: str) -> None:
    if os.name != "nt":
        return
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _GENERATIONS, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, node, 0, winreg.REG_SZ, generation)


def _gen_revoke_all() -> None:
    """Every pairing's generation, at once. Used when a state folder was found open to others: a record
    hidden there under any name and put back later can no longer match. Raises if it cannot finish, and
    then nothing is tightened or loaded."""
    if os.name != "nt":
        return
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _GENERATIONS, 0, winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE)
    except FileNotFoundError:
        return
    with key:
        while True:
            try:
                name = winreg.EnumValue(key, 0)[0]
            except OSError as exc:
                if getattr(exc, "winerror", None) == 259:  # ERROR_NO_MORE_ITEMS: every value is gone
                    break
                raise  # anything else (access denied, key deleted...) is not "done": nothing gets tightened
            winreg.DeleteValue(key, name)


def _gen_delete(node: str) -> None:
    if os.name != "nt":
        return
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _GENERATIONS, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, node)
    except FileNotFoundError:
        pass


def _seal(secret: str, purpose: str) -> str:
    """At rest on Windows a secret is a DPAPI blob for this account, bound to its purpose. Another account
    that can read the file learns nothing; one that writes or restores a file cannot make a blob this
    account accepts with a value it knows. POSIX keeps the plain value: there another account cannot
    write a file this user owns, and owner + mode are checked on every read."""
    if os.name != "nt":
        return secret
    return SEAL_PREFIX + base64.b64encode(_win_protect(secret.encode("ascii"), purpose.encode("ascii"))).decode()


def _unseal(stored: str, purpose: str) -> str:
    if os.name != "nt":
        return stored
    if not isinstance(stored, str) or not stored.startswith(SEAL_PREFIX):
        raise LooseSecret("a Windows secret that is not sealed to this account is never used")
    try:
        raw = base64.b64decode(stored[len(SEAL_PREFIX):], validate=True)
    except (ValueError, TypeError):
        raise LooseSecret("sealed secret is malformed")
    try:
        return _win_unprotect(raw, purpose.encode("ascii")).decode("ascii")
    except UnicodeDecodeError:
        raise LooseSecret("sealed secret is malformed")


def _win_sid_text(sid: Any) -> str:
    ctypes, _wintypes, advapi32, kernel32 = _win_api()
    text = ctypes.c_void_p()
    if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        raise PermissionError("unreadable security identifier")
    try:
        return ctypes.wstring_at(text.value)
    finally:
        kernel32.LocalFree(text)


def _win_system_dir() -> str:
    """System32 as Windows reports it. Tools are started from here by full path, never found through PATH,
    the working folder or Python's folder, which another account may be able to write to."""
    ctypes, _wintypes, _advapi32, kernel32 = _win_api()
    buffer = ctypes.create_unicode_buffer(1024)
    size = kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if not 0 < size < len(buffer):
        raise PermissionError("cannot locate the Windows system folder")
    return buffer.value


def _win_current_sid() -> str:
    """This process's user SID (TokenUser): the only acceptable owner of the peer-link folder and secrets."""
    if not _win_user:
        ctypes, wintypes, advapi32, kernel32 = _win_api()
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            raise PermissionError("cannot identify the current Windows account")
        try:
            size = wintypes.DWORD(0)
            advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser: learn the size
            buffer = ctypes.create_string_buffer(max(size.value, ctypes.sizeof(ctypes.c_void_p)))
            if not size.value or not advapi32.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
                raise PermissionError("cannot identify the current Windows account")
            # TOKEN_USER begins with SID_AND_ATTRIBUTES, whose first member is the PSID.
            _win_user.append(_win_sid_text(ctypes.c_void_p.from_buffer(buffer).value))
        finally:
            kernel32.CloseHandle(token)
    return _win_user[0]


def _win_owner_and_allow_sids(target: int | str) -> tuple[str, list[str] | None]:
    """(owner SID, SID of every allow ACE) of an open file descriptor, read through its own handle so the
    file checked is the file read, or of a path (folders). The SID list is None for a NULL DACL."""
    ctypes, _wintypes, advapi32, kernel32 = _win_api()
    owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    wanted = 0x1 | 0x4  # OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION; object type 1 = SE_FILE_OBJECT
    if isinstance(target, int):
        import msvcrt
        rc = advapi32.GetSecurityInfo(msvcrt.get_osfhandle(target), 1, wanted, ctypes.byref(owner), None,
                                      ctypes.byref(dacl), None, ctypes.byref(descriptor))
    else:
        rc = advapi32.GetNamedSecurityInfoW(str(target), 1, wanted, ctypes.byref(owner), None,
                                            ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if rc != 0:
        raise PermissionError(f"could not read the access list (Windows error {rc})")
    try:
        if not owner.value:
            raise LooseSecret("no owner is recorded")
        owner_sid = _win_sid_text(owner.value)
        if not dacl.value:
            return owner_sid, None
        info = (ctypes.c_uint32 * 3)()  # ACL_SIZE_INFORMATION: AceCount, AclBytesInUse, AclBytesFree
        if not advapi32.GetAclInformation(dacl, info, ctypes.sizeof(info), 2):  # AclSizeInformation
            raise PermissionError("could not read the access list")
        sids = []
        for index in range(info[0]):
            ace = ctypes.c_void_p()
            if not advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                raise PermissionError("could not read the access list")
            size = ctypes.c_uint16.from_address(ace.value + 2).value  # ACE_HEADER.AceSize
            sid = _ace_allow_sid(ctypes.string_at(ace.value, size))
            if sid is not None:
                sids.append(_win_sid_text(ctypes.create_string_buffer(sid, len(sid))))
        return owner_sid, sids
    finally:
        kernel32.LocalFree(descriptor)


def _win_check_private(target: int | str, label: str, dacl: bool = True) -> None:
    """Owned by this account and, with dacl=True, open to nobody but it, SYSTEM and Administrators."""
    me = _win_current_sid()
    owner, sids = _win_owner_and_allow_sids(target)
    if owner != me:
        # Another account's ownership does not establish that this secret was private to our account,
        # whatever today's access list says. OWNER RIGHTS is accepted only after this owner check.
        raise LooseSecret(f"{label} is owned by {owner}, not by this account")
    if dacl:
        if sids is None:
            raise LooseSecret(f"{label} has no access list, so anyone can open it")
        others = [sid for sid in sids if sid not in (me, _SYSTEM_SID, _ADMINS_SID, _OWNER_RIGHTS_SID)]
        if others:
            raise LooseSecret(f"{label} is also accessible to {others[0]}")


def _secret_kind(path: str) -> str | None:
    """'token' for a control token, 'trust' for a trust record (trusted/<uuid>.json), else None.
    Case-insensitive on purpose: Windows (and a default macOS volume) open 'Control-Token' or
    'TRUSTED/<UUID>.JSON' for the canonical lowercase path, so a case variant is the same secret."""
    name = os.path.basename(path).casefold()
    if name == "control-token":
        return "token"
    if name.endswith(".json") and UUID_RE.match(name[:-5]):
        # Wherever it sits: a 'trusted' folder renamed to anything else can still be reached through an
        # alias (a Windows 8.3 short name TRUSTED), so a record is retired by its own name, not its folder's.
        return "trust"
    return None


def _trust_folder_genuine(folder: Path) -> bool:
    """The trust folder is exactly `<state>/trusted`: no case-variant sibling, and when it exists the name
    the file system really stores for it (realpath resolves links, and on Windows long names behind 8.3
    aliases via GetFinalPathNameByHandle) is 'trusted' inside the real state folder."""
    if _case_variants(folder.parent, folder.name):
        return False
    try:
        os.lstat(folder)
    except FileNotFoundError:
        return True
    real, parent = os.path.realpath(folder), os.path.realpath(folder.parent)
    return os.path.basename(real) == folder.name and os.path.dirname(real) == parent


def _case_variants(folder: Path, canonical: str) -> list[str]:
    """Directory entries that are `canonical` except for letter case. On a case-insensitive volume the
    canonical path opens such an entry, so it must be treated as planted, never read as ours."""
    try:
        names = os.listdir(folder)
    except FileNotFoundError:
        return []
    return [n for n in names if n.casefold() == canonical.casefold() and n != canonical]


def _quarantine_trust_record(path: str) -> None:
    """Rename a trust record out of use (kept for inspection); the user pairs that machine again."""
    folder, name = os.path.split(path)
    _gen_delete(name[:-len(".json")].casefold())  # revoked first: a restored copy can never be trusted again
    os.replace(path, os.path.join(folder, f"{name}.quarantined-{int(time.time())}"))
    with _quarantine_lock:
        nodes = _quarantined.setdefault(folder, [])
        nodes.append(name[:-len(".json")])
        del nodes[:-16]


def _win_retire_exposed(directory: Path, unconditional: bool = False) -> None:
    """Runs under _token_lock before a folder is tightened. Tightening re-inherits into every file and
    folder below it that does not block inheritance, which would make an exposed secret look private
    again, in this process and after any restart. So every secret anywhere below (links and reparse
    points are not followed) whose owner or access list is not private now is retired on disk: a
    control token is deleted, so a fresh one is made; a trust record is quarantined. Anything that
    cannot be retired raises, and then nothing is tightened, so the exposure is still visible next time.
    unconditional: the folder itself was found open to others. Then a secret's current owner and access
    list prove nothing (whoever could write the folder could also have written the file and then made its
    access list look private), so every secret below is retired, whatever it looks like now."""
    stack = [str(directory)]
    while stack:
        with os.scandir(stack.pop()) as entries:
            found = list(entries)
        for entry in found:
            try:
                info = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue  # gone since it was listed (a temporary file renamed into place)
            if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
                continue
            if stat.S_ISDIR(info.st_mode):
                stack.append(entry.path)
                continue
            kind = _secret_kind(entry.path) if stat.S_ISREG(info.st_mode) else None
            if kind is None:
                continue
            if not unconditional:
                try:
                    _win_check_private(entry.path, entry.name)
                    continue
                except OSError:  # readable by others, or its access list cannot be read: never used again
                    pass
            try:
                if kind == "token":
                    os.unlink(entry.path)
                else:
                    _quarantine_trust_record(entry.path)
            except FileNotFoundError:
                pass


def _win_restrict_dir(directory: Path, info: Any) -> None:
    if _is_reparse(info):
        raise PermissionError(f"{directory} is a reparse point")
    key, me = str(directory), _win_current_sid()
    system = _win_system_dir()
    with _token_lock:
        # Observed under the lock: a decision taken on an access list read before another thread tightened
        # the folder and paired a peer would revoke that fresh pairing.
        owner, sids = _win_owner_and_allow_sids(key)
        if owner != me:
            raise PermissionError(f"{directory} belongs to another account ({owner})")
        private = sids is not None and all(sid in (me, _SYSTEM_SID, _ADMINS_SID, _OWNER_RIGHTS_SID) for sid in sids)
        if private and key in _win_tightened:
            return
        # Every tightening is preceded by retiring the secrets below that others could read, under the
        # same lock that guards control-token reads and trust-record writes, so no exposure is laundered
        # by any caller. A folder that was open to others retires every secret below it.
        if not private:
            # A trust record may be hidden under any name and put back after tightening through a handle opened
            # earlier; only revoking every pairing's generation makes such a copy worthless. If this raises,
            # nothing is tightened, so the next attempt sees the open folder again.
            _gen_revoke_all()
        _win_retire_exposed(directory, unconditional=not private)
        # icacls (by full path) only sets the restriction (no inherited entries, full control for this
        # account's SID); what is accepted is decided by reading the security descriptor back.
        result = subprocess.run([ntpath.join(system, "icacls.exe"), key, "/inheritance:r", "/grant:r",
                                 f"*{me}:(OI)(CI)F", "/Q"], capture_output=True, timeout=30, cwd=system)
        if result.returncode != 0:
            raise PermissionError("icacls could not make the peer-link folder private")
        _win_check_private(key, directory.name)
        if not private:
            # Nothing of ours was written since the first pass (this lock is held), so any secret now
            # present was put there by someone else before the folder closed: retire it as well.
            _win_retire_exposed(directory, unconditional=True)
        _win_tightened.add(key)


def _only_me(path: Path, is_dir: bool) -> None:
    """Fail closed unless nobody but this user (and, on Windows, SYSTEM and Administrators) has access,
    and on Windows unless this account owns it. POSIX mode bits and owners are checked by the caller;
    this covers macOS extended ACLs and Windows owners and DACLs."""
    if sys.platform == "darwin":
        out = subprocess.run(["/bin/ls", "-led" if is_dir else "-le", str(path)], capture_output=True, text=True,
                             timeout=10)
        if out.returncode != 0:
            raise PermissionError(f"could not read the access list of {path.name}")
        if any(re.match(r"^\s*\d+: ", line) for line in out.stdout.splitlines()[1:]):
            raise LooseSecret(f"{path.name} has an extended ACL granting other access")
    elif os.name == "nt":
        if _is_reparse(os.lstat(path)):
            raise PermissionError(f"{path.name} is a reparse point")
        _win_check_private(str(path), path.name)


def _restrict_dir(directory: Path) -> None:
    """Create or verify a private folder. POSIX: owned by this user, 0700, no extended ACL (macOS); the
    result is cached by (inode, ctime), which any chmod or ACL edit changes. Windows: not a reparse
    point, owned by this account, DACL limited to it, SYSTEM and Administrators, checked on every call."""
    directory.mkdir(parents=True, exist_ok=True)
    info = os.lstat(directory)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise PermissionError(f"{directory} is not a plain folder")
    if os.name == "nt":
        _win_restrict_dir(directory, info)
        return
    key = str(directory)
    if _verified.get(key) == (info.st_ino, info.st_ctime_ns):
        return
    if info.st_uid != os.getuid():
        raise PermissionError(f"{directory} belongs to another user")
    os.chmod(directory, 0o700)  # the folder holds no secret itself; each secret file is checked on read
    if sys.platform == "darwin":
        subprocess.run(["/bin/chmod", "-N", key], capture_output=True, timeout=10)
        _only_me(directory, True)
    info = os.lstat(directory)
    _verified[key] = (info.st_ino, info.st_ctime_ns)


def _write_private(path: Path, text: str) -> None:
    _restrict_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        _only_me(tmp, False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_identity(state_dir: Path) -> str:
    path = state_dir / "identity.json"
    try:
        value = strict_json(_read_private(path, secret=False)).get("nodeUuid")
        if isinstance(value, str) and UUID_RE.match(value):
            return value
    except (OSError, ValueError, AttributeError):
        pass
    value = str(uuidlib.uuid4())
    _write_private(path, json.dumps({"nodeUuid": value, "createdAt": int(time.time() * 1000)}))
    return value


def _read_private(path: Path, secret: bool = True) -> str:
    """Read a regular file this user owns. For key material (secret=True) any sign that someone else
    could have read it raises LooseSecret; the caller rotates or quarantines, never reuses it."""
    windows = os.name == "nt"
    if windows and _is_reparse(os.lstat(path)):
        raise PermissionError(f"{path.name} is a reparse point")
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PermissionError(f"{path.name} is not a regular file")
        if windows:
            # No cache on Windows (ctime there is the creation time): the owner, and for key material the
            # DACL, are read from this open handle on every read. A secret that was readable before its
            # folder was tightened is not here any more: _win_retire_exposed removed it from disk first.
            _win_check_private(fd, path.name, dacl=secret)
        else:
            if info.st_uid != os.getuid():
                raise LooseSecret(f"{path.name} belongs to another user")
            if secret:
                if info.st_mode & 0o077:
                    raise LooseSecret(f"{path.name} was readable by other users")
                if _verified.get(str(path)) != (info.st_ino, info.st_ctime_ns):
                    _only_me(path, False)
                    _verified[str(path)] = (info.st_ino, info.st_ctime_ns)
        with os.fdopen(os.dup(fd), "r", encoding="utf-8") as handle:
            return handle.read(MAX_STATUS_BYTES)
    finally:
        os.close(fd)


def load_control_token(state_dir: Path) -> str:
    """The control token on disk now, checked as key material; an exposed, unreadable or malformed one is
    replaced. The running peer link calls this for every control exchange, so a rotation revokes the old
    token. Only 64 lowercase hex characters (what secrets.token_hex(32) writes) are accepted."""
    with _token_lock:
        _restrict_dir(state_dir)
        path = state_dir / "control-token"
        for variant in _case_variants(state_dir, "control-token"):
            # Not a name this code ever writes: whatever it holds was put there by someone else.
            try:
                os.unlink(state_dir / variant)
            except FileNotFoundError:
                pass
        try:
            value = _unseal(_read_private(path).strip(), "agiw-peer control-token")
            if re.fullmatch(r"[0-9a-f]{64}", value):
                return value
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            # Possibly exposed, unreadable, or not text (UnicodeDecodeError is a ValueError): never reuse
            # it. A fresh token invalidates any copy.
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        value = secrets.token_hex(32)
        _write_private(path, _seal(value, "agiw-peer control-token") + "\n")
        return value


class TrustStore:
    """trusted/<uuid>.json, one file per paired peer, like PAIR's trusted-node store."""

    def __init__(self, state_dir: Path):
        self.dir = state_dir / "trusted"
        self._lock = threading.Lock()

    @property
    def quarantined(self) -> list[str]:
        """Peers whose records this process quarantined, whether found here or while a folder was tightened."""
        with _quarantine_lock:
            return list(_quarantined.get(str(self.dir), []))

    def load(self) -> dict[str, dict]:
        # Same lock as pin() and remove(), taken first in all three, so a check that finds a stale record can
        # never quarantine a newer pairing written in between.
        with _token_lock:
            return self._load()

    def _load(self) -> dict[str, dict]:
        pins = {}
        self.registry_error: str | None = None
        try:
            os.lstat(self.dir)
        except OSError:
            return pins  # nothing paired yet; reading never creates the folder
        if not _trust_folder_genuine(self.dir):
            return pins  # 'TRUSTED', a short-name alias or a link is not a folder this code made: no trust
        try:
            # Owner, permissions, and no link or reparse point, checked before anything in it is listed.
            _restrict_dir(self.dir)
        except (OSError, subprocess.SubprocessError):
            return pins  # a folder we cannot vouch for yields no trust at all
        try:
            # Listed by hand, matched without regard to case, so every platform sees the same entries
            # (glob is case-sensitive on macOS but not on Windows) and planted variants are set aside.
            files = sorted(self.dir / n for n in os.listdir(self.dir) if n.casefold().endswith(".json"))
        except OSError:
            return pins
        for path in files:
            if path.name != path.name.casefold() and _secret_kind(str(path)) == "trust":
                # '<UUID>.JSON' and similar: never written by this code, so planted; set aside, never trusted.
                try:
                    _quarantine_trust_record(str(path))
                except OSError:
                    pass
                continue
            try:
                pin = strict_json(_read_private(path))
            except LooseSecret:
                # Someone else could have read this link key: stop trusting it; the user pairs again.
                try:
                    _quarantine_trust_record(str(path))
                except OSError:
                    pass
                continue
            except (OSError, ValueError, subprocess.SubprocessError):
                continue
            try:
                if path.stem != pin["nodeUuid"] or not UUID_RE.match(path.stem) or pin.get("host") not in HOST_KINDS:
                    continue
                try:
                    # Bound to this record's UUID, so a sealed key cannot be moved to another peer's record.
                    opened = _unseal(pin["linkKey"], "agiw-peer link " + path.stem)
                    if os.name == "nt":
                        key_hex, _, generation = opened.partition(":")
                        try:
                            current = _gen_get(path.stem)
                        except OSError as exc:
                            # Unreadable registry: no trust from this record now, and nothing is revoked or moved;
                            # it loads again once the registry answers.
                            self.registry_error = type(exc).__name__
                            continue
                        if current is None or not hmac.compare_digest(generation, current):
                            raise LooseSecret("trust record is not the current pairing (revoked or replayed)")
                        opened = key_hex
                    key = bytes.fromhex(opened)
                except LooseSecret:
                    try:
                        _quarantine_trust_record(str(path))
                    except OSError:
                        pass
                    continue
                if len(key) != 32:
                    continue
                pins[path.stem] = {**pin, "key": key}
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
        return pins

    def pin(self, node: str, name: str, host: str, key: bytes) -> None:
        with _token_lock, self._lock:
            path = self.dir / f"{node}.json"
            if not _trust_folder_genuine(self.dir):
                raise PermissionError("the pairing folder is not the one this program made (a differently "
                                      "cased, aliased or linked 'trusted'); remove it, then pair again")
            _restrict_dir(self.dir)  # create or validate before anything inside it is listed or removed
            if not _trust_folder_genuine(self.dir):
                raise PermissionError("the pairing folder changed while it was being checked; pair again")
            for variant in _case_variants(self.dir, path.name):
                try:  # a case variant would capture the canonical write on a case-insensitive volume
                    os.unlink(self.dir / variant)
                except FileNotFoundError:
                    pass
            if path.exists():
                old = self.load().get(node)
                if old and not hmac.compare_digest(old["key"], key):
                    raise PermissionError("this machine is already paired with a different key; unpair it first")
            stored = key.hex()
            if os.name == "nt":
                generation = secrets.token_hex(16)
                _gen_set(node, generation)
                stored = f"{stored}:{generation}"
            _write_private(path, json.dumps({"nodeUuid": node, "name": name[:64], "host": host,
                                             "linkKey": _seal(stored, "agiw-peer link " + node),
                                             "pinnedAt": int(time.time() * 1000)}))

    def remove(self, node: str) -> bool:
        with _token_lock, self._lock:
            _gen_delete(node)  # revoked even if someone later puts the old file back
            try:
                (self.dir / f"{node}.json").unlink()
                return True
            except FileNotFoundError:
                return False


class Subkeys:
    def __init__(self, link: bytes):
        self.request = hmac.new(link, b"agiw-peer request", hashlib.sha256).digest()
        self.reply = hmac.new(link, b"agiw-peer reply", hashlib.sha256).digest()


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def _no_constants(name: str):
    raise ValueError("non-finite number " + name)


def strict_json(raw: bytes | str) -> Any:
    return json.loads(raw, parse_constant=_no_constants)


def lan_source(address: str, allow_loopback: bool = False) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return allow_loopback
    return (ip.is_private or ip.is_link_local) and not ip.is_multicast and not ip.is_unspecified


class NonceCache:
    def __init__(self, ttl: float = 2 * CLOCK_WINDOW, limit: int = 4096):
        self.ttl, self.limit, self._seen, self._lock = ttl, limit, {}, threading.Lock()

    def fresh(self, nonce: str, now: float) -> bool:
        with self._lock:
            if len(self._seen) >= self.limit:
                self._seen = {n: t for n, t in self._seen.items() if now - t < self.ttl}
                if len(self._seen) >= self.limit:
                    return False
            if nonce in self._seen and now - self._seen[nonce] < self.ttl:
                return False
            self._seen[nonce] = now
            return True


# ---------- hello beacons ----------

def make_hello(node: str, host: str, name: str, port: int, now: float) -> bytes:
    return canonical({"p": PROTOCOL, "node": node, "host": host, "name": name[:64], "port": port, "ts": round(now, 3)})


def read_hello(data: bytes) -> dict | None:
    """Shape-checked hello, or None. Never raises. A hello is a hint, never a credential."""
    if len(data) > MAX_DATAGRAM:
        return None
    try:
        body = strict_json(data.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(body, dict) or body.get("p") != PROTOCOL:
        return None
    node, port, name = body.get("node"), body.get("port"), body.get("name")
    if (not isinstance(node, str) or not UUID_RE.match(node) or body.get("host") not in HOST_KINDS
            or not isinstance(name, str) or not name or len(name) > 64 or not name.isprintable()
            or not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535):
        return None
    return {"node": node, "host": body["host"], "name": name, "port": port}


# ---------- status documents ----------

def _parse_iso(text: str) -> float:
    import datetime as _dt
    value = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("observedAt needs a timezone")
    return value.timestamp()


def validate_status(doc: Any, host: str, now: float | None = None) -> dict:
    """Schema-1 status: windows-status.json shape (host = kind) or the agiw-peer presence shape
    (kind "agiw-peer", host = machine name, platform = kind)."""
    kind = doc.get("platform") if isinstance(doc, dict) and doc.get("kind") == "agiw-peer" else (
        doc.get("host") if isinstance(doc, dict) else None)
    if (not isinstance(doc, dict) or doc.get("schemaVersion") != 1 or kind != host
            or not isinstance(doc.get("observedAt"), str) or not isinstance(doc.get("models"), list)
            or len(doc["models"]) > MAX_MODELS):
        raise ValueError("status document invalid")
    if now is not None:
        try:
            observed = _parse_iso(doc["observedAt"])
        except (ValueError, OverflowError):
            raise ValueError("status observedAt is not a time")
        if not math.isfinite(observed) or abs(now - observed) > STATUS_MAX_AGE:
            raise ValueError("status is not fresh")
    for model in doc["models"]:
        if (not isinstance(model, dict) or not isinstance(model.get("id"), str) or not model["id"]
                or model.get("loadedState") not in ("loaded", "inactive", "unknown")):
            raise ValueError("status model invalid")
    return doc


def _scalar(value: Any, kind: type, limit: int = 128) -> Any:
    if kind is str:
        return value[:limit] if isinstance(value, str) and value.isprintable() else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(float(value)) else None


def project_status(doc: dict) -> dict:
    """What leaves this machine: only the documented fields, nothing a provider happened to include."""
    out: dict[str, Any] = {"schemaVersion": 1, "observedAt": doc["observedAt"], "models": []}
    for key in ("kind", "host", "platform", "edition", "health", "producer"):
        if _scalar(doc.get(key), str, 64) is not None:
            out[key] = _scalar(doc[key], str, 64)
    if _scalar(doc.get("observedUnix"), float) is not None:
        out["observedUnix"] = doc["observedUnix"]
    if type(doc.get("handsOff")) is bool:
        out["handsOff"] = doc["handsOff"]
    # The publisher's uncapped model count, so a reader can tell a whole list from a capped one.
    if type(doc.get("modelsTotal")) is int and 0 <= doc["modelsTotal"] <= 1 << 16:
        out["modelsTotal"] = doc["modelsTotal"]
    if type(doc.get("loadedListComplete")) is bool:
        out["loadedListComplete"] = (doc["loadedListComplete"]
                                     and len(doc["models"]) <= MAX_MODELS)
    for model in doc["models"][:MAX_MODELS]:
        row = {"id": model["id"][:256], "loadedState": model["loadedState"]}
        for key in ("host", "activity", "lane", "servedModel"):
            if _scalar(model.get(key), str, 64) is not None:
                row[key] = _scalar(model[key], str, 64)
        for key in ("slotsBusy", "slotsTotal", "liveTokensPerSecond"):
            if _scalar(model.get(key), float) is not None:
                row[key] = model[key]
        out["models"].append(row)
    gpus = doc.get("gpus") if isinstance(doc.get("gpus"), list) else []
    out["gpus"] = [{k: g[k] for k in ("name", "utilizationPercent", "memoryUsedMiB", "memoryTotalMiB", "temperatureC")
                    if _scalar(g.get(k), str if k == "name" else float, 80) is not None}
                   for g in gpus[:4] if isinstance(g, dict)]
    memory = doc.get("memory") if isinstance(doc.get("memory"), dict) else {}
    out["memory"] = {k: memory[k] for k in ("level", "availablePercent")
                     if _scalar(memory.get(k), str if k == "level" else float, 16) is not None}
    if _scalar(doc.get("version"), str, 32) is not None:
        out["version"] = _scalar(doc["version"], str, 32)
    # Route components (Nisi, Jev): only known ids, short printable words and one age; never free text blobs.
    components = doc.get("components") if isinstance(doc.get("components"), list) else []
    rows = []
    for item in components[:4]:
        if not isinstance(item, dict) or item.get("id") not in ("nisi", "jev"):
            continue
        row = {k: _scalar(item.get(k), str, 128) for k in ("id", "label", "state", "detail")}
        row = {k: v for k, v in row.items() if v is not None}
        age = _scalar(item.get("lastJudgedAgeSeconds"), float)
        if age is not None and age >= 0:
            row["lastJudgedAgeSeconds"] = age
        rows.append(row)
    if rows:
        out["components"] = rows
    return out


def lmstudio_status(base: str = "http://127.0.0.1:1234", host: str = "mac") -> StatusProvider:
    """Status straight from a local LM Studio REST listing."""

    def provide() -> dict:
        with urllib.request.urlopen(base + "/api/v0/models", timeout=2) as response:
            payload = json.loads(response.read(MAX_STATUS_BYTES + 1)[:MAX_STATUS_BYTES])
        models = []
        for item in (payload.get("data") or [])[:MAX_MODELS]:
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item.get("type") in (None, "llm", "vlm"):
                state = {"loaded": "loaded", "not-loaded": "inactive"}.get(item.get("state"), "unknown")
                models.append({"id": item["id"][:256], "host": host, "loadedState": state})
        now = time.time()
        return {"schemaVersion": 1, "kind": "agiw-peer", "host": socket.gethostname().split(".")[0].lower()[:63],
                "platform": host, "edition": "peer-link", "observedAt": _iso(now), "observedUnix": round(now, 3),
                "health": "ok", "producer": "agiw-peer-link lmstudio", "models": models}

    return provide


def file_status(path: Path, max_age: float = 30.0) -> StatusProvider:
    def provide() -> dict:
        if time.time() - path.stat().st_mtime > max_age:
            raise ValueError("status file is stale")
        return json.loads(path.read_bytes()[:MAX_STATUS_BYTES])

    return provide


def detect_host_kind(os_name: str | None = None, platform: str | None = None) -> str:
    """This machine's kind for `run --host`: windows, linux, and mac otherwise (the earlier default)."""
    os_name = os.name if os_name is None else os_name
    platform = sys.platform if platform is None else platform
    if os_name == "nt":
        return "windows"
    if platform.startswith("linux"):
        return "linux"
    return "mac"


def linux_status_file() -> Path:
    """The Linux observer's private heartbeat (linux/README.md): ${XDG_STATE_HOME:-~/.local/state}/agiw."""
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    if not base.is_absolute():
        base = Path.home() / ".local" / "state"
    return base / "agiw" / "linux-status.json"


def minimal_status(host: str) -> StatusProvider:
    """No local observer: this peer link answers, and its models and health stay unknown."""

    def provide() -> dict:
        now = time.time()
        return {"schemaVersion": 1, "kind": "agiw-peer", "host": socket.gethostname().split(".")[0].lower()[:63],
                "platform": host, "edition": "peer-link", "observedAt": _iso(now), "observedUnix": round(now, 3),
                "health": "unknown", "producer": "agiw-peer-link minimal", "models": [], "loadedListComplete": False}

    return provide


def linux_status(path: Path | None = None) -> StatusProvider:
    """The Linux observer's status file while it exists (refused when stale, like file_status), else the
    minimal status. Checked on every request, so an observer started later is picked up."""
    path = linux_status_file() if path is None else path
    observed, fallback = file_status(path), minimal_status("linux")

    def provide() -> dict:
        return observed() if path.exists() else fallback()

    return provide


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + f".{int(ts % 1 * 1000):03d}Z"


# ---------- HTTP plumbing ----------

def _exclusive(sock: socket.socket) -> None:
    """Windows SO_REUSEADDR lets another process bind the same port and steal traffic; claim it exclusively."""
    if os.name == "nt":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)


def _hard_close(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


class LinkServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16
    allow_reuse_address = os.name != "nt"

    def __init__(self, address, link: "PeerLink"):
        self.link = link
        # Loopback control gets its own pool, so LAN connection floods cannot lock the owner out,
        # and no single LAN address may hold more than PER_SOURCE_CONNECTIONS of the LAN pool.
        self.pools = {"local": threading.BoundedSemaphore(4), "lan": threading.BoundedSemaphore(MAX_CONNECTIONS)}
        self.per_source: dict[str, int] = {}
        self.source_lock = threading.Lock()
        super().__init__(address, LinkHandler)

    def _pool(self, client_address) -> str:
        return "local" if client_address[0] == "127.0.0.1" and not self.link.allow_loopback_lan else "lan"

    def _admit(self, client_address) -> bool:
        pool = self._pool(client_address)
        if pool == "lan" and client_address[0] != "127.0.0.1":
            with self.source_lock:
                if self.per_source.get(client_address[0], 0) >= PER_SOURCE_CONNECTIONS:
                    return False
                self.per_source[client_address[0]] = self.per_source.get(client_address[0], 0) + 1
        if self.pools[pool].acquire(blocking=False):
            return True
        self._release_source(client_address)
        return False

    def _release_source(self, client_address) -> None:
        if self._pool(client_address) == "lan" and client_address[0] != "127.0.0.1":
            with self.source_lock:
                left = self.per_source.get(client_address[0], 1) - 1
                if left > 0:
                    self.per_source[client_address[0]] = left
                else:
                    self.per_source.pop(client_address[0], None)

    def _release(self, client_address) -> None:
        self.pools[self._pool(client_address)].release()
        self._release_source(client_address)

    def server_bind(self):
        _exclusive(self.socket)
        super().server_bind()

    def process_request(self, request, client_address):
        if not self._admit(client_address):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._release(client_address)
            raise

    def process_request_thread(self, request, client_address):
        watchdog = threading.Timer(REQUEST_DEADLINE, _hard_close, (request,))
        watchdog.daemon = True
        watchdog.start()
        try:
            super().process_request_thread(request, client_address)
        finally:
            watchdog.cancel()
            self._release(client_address)


class LinkHandler(BaseHTTPRequestHandler):
    server_version = "agiw-peer"
    sys_version = ""
    timeout = 5
    protocol_version = "HTTP/1.0"

    def log_message(self, *_):
        pass

    def _send(self, code: int, body: bytes = b"", headers: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, code: int, obj: Any):
        self._send(code, canonical(obj))

    def _raw_body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return None
        if not 0 < length <= MAX_BODY:
            return None
        try:
            return self.rfile.read(length)
        except OSError:
            return None

    def _body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return None
        if not 0 < length <= MAX_BODY:
            return None
        try:
            body = strict_json(self.rfile.read(length))
        except (ValueError, OSError):
            return None
        return body if isinstance(body, dict) else None

    def _loopback_plain(self) -> bool:
        return self.client_address[0] == "127.0.0.1" and self.headers.get("Origin") is None

    def _local_control(self, body: bytes) -> bool:
        """The client proves it knows the token for this one request, after the server proved it first.
        The token is the one on disk now, and must be the one the challenge was issued under."""
        link: PeerLink = self.server.link
        if not self._loopback_plain():
            return False
        ns, proof = self.headers.get("X-Agiw-Control-Nonce", ""), self.headers.get("X-Agiw-Control-Proof", "")
        challenge = link.take_control_challenge(ns)  # single use: consumed even if what follows fails
        if challenge is None:
            return False
        nc, issued_under = challenge
        token = link.current_control_token()
        if token is None or not hmac.compare_digest(issued_under, control_token_id(token)):
            return False  # rotated since the challenge was issued: the old token is revoked mid-exchange too
        return hmac.compare_digest(proof, control_client_proof(token, ns, nc, self.command, self.path, body))

    def do_GET(self):
        link: PeerLink = self.server.link
        if self.path == PREFIX + "/status" and lan_source(self.client_address[0], link.allow_loopback):
            return self._status()
        if self.path == "/control/hello" and self._loopback_plain():
            nc = self.headers.get("X-Agiw-Client-Nonce", "")
            if not re.fullmatch(r"[0-9a-f]{32}", nc):
                return self._send(400)
            token = link.current_control_token()  # read and checked now; never a copy from startup
            if token is None:
                return self._send(503)
            ns = link.new_control_challenge(nc, token)
            if ns is None:
                return self._send(429)
            return self._json(200, {"ns": ns, "proof": control_server_proof(token, nc, ns)})
        if self.path == "/control/peers" and self._local_control(b""):
            return self._json(200, link.snapshot())
        self._send(404)

    def do_POST(self):
        link: PeerLink = self.server.link
        if self.path.startswith("/control/"):
            raw = self._raw_body()
            if raw is None or not self._local_control(raw):
                return self._send(404)
            try:
                body = strict_json(raw)
            except ValueError:
                body = None
            if not isinstance(body, dict):
                return self._json(400, {"error": "bad body"})
            try:
                if self.path == "/control/invite":
                    return self._json(200, link.invite(str(body.get("peer", ""))))
                if self.path == "/control/accept":
                    return self._json(200, link.accept(str(body.get("peer", "")), str(body.get("pin", ""))))
                if self.path == "/control/unpair":
                    return self._json(200, link.unpair(str(body.get("peer", ""))))
            except (LookupError, ValueError, PermissionError, ConnectionError, OSError) as exc:
                return self._json(409, {"error": str(exc)[:200]})
            return self._send(404)
        if not lan_source(self.client_address[0], link.allow_loopback):
            return self._send(404)
        body = self._body()
        if body is None:
            return self._send(400)
        if self.path == PREFIX + "/pair/notify":
            return self._json(*link.on_notify(body, self.client_address[0]))
        if self.path == PREFIX + "/pair/start":
            return self._json(*link.on_start(body))
        if self.path == PREFIX + "/pair/finish":
            return self._json(*link.on_finish(body))
        self._send(404)

    def do_PUT(self):
        self._send(405)

    do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = do_PUT

    def _status(self):
        link: PeerLink = self.server.link
        node, ts, nonce = (self.headers.get(h, "") for h in ("X-Agiw-Node", "X-Agiw-Ts", "X-Agiw-Nonce"))
        sig = self.headers.get("X-Agiw-Sig", "")
        pin = link.trusted().get(node)
        try:
            stamp = float(ts)
        except ValueError:
            return self._send(403)
        now = time.time()
        if pin is None or len(nonce) != 32 or not math.isfinite(stamp) or abs(now - stamp) > CLOCK_WINDOW:
            return self._send(403)
        keys = Subkeys(pin["key"])
        signed = "\n".join(("GET", PREFIX + "/status", node, link.node, ts, nonce)).encode()
        if (not hmac.compare_digest(sig, hmac.new(keys.request, signed, hashlib.sha256).hexdigest())
                or not link.nonces_for(node).fresh(nonce, time.monotonic())):
            return self._send(403)
        try:
            body = canonical(project_status(validate_status(link.status_provider(), link.host, time.time())))
        except Exception:  # noqa: BLE001 - provider failure is reported as unavailable, never as stale data
            return self._send(503)
        if len(body) > MAX_STATUS_BYTES:
            return self._send(503)
        reply = hmac.new(keys.reply, _reply_header(nonce, link.node, node) + body, hashlib.sha256)
        self._send(200, body, {"X-Agiw-Node": link.node, "X-Agiw-Sig": reply.hexdigest()})


def control_token_id(token: str) -> str:
    """What a control challenge remembers about the token it was issued under (never the token itself)."""
    return hashlib.sha256(f"agiw-control token id\n{token}".encode()).hexdigest()


def control_server_proof(token: str, nc: str, ns: str) -> str:
    return hmac.new(token.encode(), f"agiw-control server\n{nc}\n{ns}".encode(), hashlib.sha256).hexdigest()


def control_client_proof(token: str, ns: str, nc: str, method: str, path: str, body: bytes) -> str:
    text = f"agiw-control client\n{ns}\n{nc}\n{method}\n{path}\n{hashlib.sha256(body).hexdigest()}"
    return hmac.new(token.encode(), text.encode(), hashlib.sha256).hexdigest()


def control_request(state_dir: Path, port: int, method: str, path: str, body: dict | None = None,
                    limit: int = (MAX_PEERS + 1) * MAX_STATUS_BYTES, deadline: float = 20.0) -> tuple[int, Any]:
    """Talk to this user's own peer link on 127.0.0.1. The listener must first prove it holds the
    control token; the token itself is never sent, so whatever squats on the port learns nothing."""
    token = load_control_token(state_dir)
    nc = secrets.token_hex(16)
    status, _h, raw = _http("127.0.0.1", port, "GET", "/control/hello", {"X-Agiw-Client-Nonce": nc}, None,
                            1024, deadline=min(deadline, 3.0))
    hello = strict_json(raw) if status == 200 and raw else {}
    ns = hello.get("ns") if isinstance(hello, dict) else None
    if (not isinstance(ns, str) or not re.fullmatch(r"[0-9a-f]{32}", ns)
            or not hmac.compare_digest(str(hello.get("proof", "")), control_server_proof(token, nc, ns))):
        raise PermissionError(f"the process on 127.0.0.1:{port} is not your peer link; nothing was sent to it")
    payload = canonical(body) if body is not None else b""
    status, _h, raw = _http("127.0.0.1", port, method, path, {
        "X-Agiw-Control-Nonce": ns, "Content-Type": "application/json",
        "X-Agiw-Control-Proof": control_client_proof(token, ns, nc, method, path, payload)},
        payload if body is not None else None, limit, deadline=deadline)
    return status, (strict_json(raw) if raw else {})


def _http(address: str, port: int, method: str, path: str, headers: dict, body: bytes | None,
          limit: int, deadline: float = FETCH_DEADLINE) -> tuple[int, dict, bytes]:
    started = time.monotonic()
    conn = http.client.HTTPConnection(address, port, timeout=deadline)
    expired = threading.Event()
    held: list[socket.socket] = []  # getresponse() hands the socket to the response and clears conn.sock

    def expire():
        expired.set()
        for sock in held:
            _hard_close(sock)

    watchdog = threading.Timer(deadline, expire)
    watchdog.daemon = True
    watchdog.start()
    response = None
    try:
        conn.connect()
        held.append(conn.sock)
        if expired.is_set():
            raise TimeoutError("peer exceeded the deadline")
        conn.request(method, path, body=body, headers={**headers, "Connection": "close"})
        response = conn.getresponse()
        chunks, size = [], 0
        while True:
            if time.monotonic() - started > deadline:
                raise TimeoutError("peer exceeded the deadline")
            chunk = response.read(8192)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise ValueError("peer reply too large")
            chunks.append(chunk)
        if expired.is_set():
            raise TimeoutError("peer exceeded the deadline")
        return response.status, {k.lower(): v for k, v in response.getheaders()}, b"".join(chunks)
    except OSError:
        if expired.is_set():
            raise TimeoutError("peer exceeded the deadline")
        raise
    finally:
        watchdog.cancel()
        if response is not None:
            response.close()
        conn.close()


def _reply_header(nonce: str, server: str, client: str) -> bytes:
    return "\n".join(("200", PREFIX + "/status", server, client, nonce, "")).encode()


def fetch_status(link_key: bytes, me: str, address: str, port: int, peer_node: str, peer_host: str,
                 now: float) -> dict:
    keys = Subkeys(link_key)
    nonce, ts = secrets.token_hex(16), f"{now:.3f}"
    signed = "\n".join(("GET", PREFIX + "/status", me, peer_node, ts, nonce)).encode()
    status, headers, body = _http(address, port, "GET", PREFIX + "/status", {
        "X-Agiw-Node": me, "X-Agiw-Ts": ts, "X-Agiw-Nonce": nonce,
        "X-Agiw-Sig": hmac.new(keys.request, signed, hashlib.sha256).hexdigest()}, None, MAX_STATUS_BYTES)
    if status != 200:
        raise ConnectionError(f"peer answered {status}")
    expected = hmac.new(keys.reply, _reply_header(nonce, peer_node, me) + body, hashlib.sha256)
    if headers.get("x-agiw-node") != peer_node or not hmac.compare_digest(headers.get("x-agiw-sig", ""),
                                                                           expected.hexdigest()):
        raise PermissionError("peer reply signature invalid")
    return validate_status(strict_json(body), peer_host, time.time())


# ---------- the link ----------

class PeerLink:
    def __init__(self, state_dir: Path, host: str, name: str, status_provider: StatusProvider,
                 status_port: int = STATUS_PORT, beacon_port: int = BEACON_PORT, bind: str = "0.0.0.0",
                 broadcast: tuple[str, ...] = ("255.255.255.255",), allow_loopback: bool = False,
                 clock: Callable[[], float] = time.monotonic, on_change: Callable[[dict], None] | None = None):
        if host not in HOST_KINDS:
            raise ValueError("host must be one of " + ", ".join(HOST_KINDS))
        self.state_dir = state_dir
        self.node = load_identity(state_dir)
        load_control_token(state_dir)  # created or rotated now; read again for every control exchange
        self.store = TrustStore(state_dir)
        self.host, self.name = host, name[:64]
        self.status_provider, self.status_port, self.beacon_port = status_provider, status_port, beacon_port
        self.bind, self.broadcast, self.allow_loopback = bind, broadcast, allow_loopback
        self.clock, self.on_change = clock, on_change
        self._nonces: dict[str, NonceCache] = {}
        # server nonce -> (client nonce, expiry, id of the control token it was issued under)
        self._control_challenges: dict[str, tuple[str, float, str]] = {}
        self.allow_loopback_lan = allow_loopback
        self._seen: dict[str, dict] = {}       # uuid -> last hello facts (address, port, lastHello, ...)
        self._live: dict[str, dict] = {}       # uuid -> status / statusAt / error for paired peers
        self._outbound: dict[str, dict] = {}   # invite id -> invite this node shows a PIN for
        self._inbound: dict[str, dict] = {}    # peer uuid -> invite waiting for the user to type a PIN
        self._completed: dict[str, dict] = {}  # invite id -> finished exchange, for idempotent finish retries
        self._trusted_cache: tuple[float, dict] | None = None  # None = reload (monotonic may start near 0)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self.server: LinkServer | None = None
        self.udp: socket.socket | None = None
        self.poll_error: str | None = None

    @property
    def control_token(self) -> str:
        """The control token on disk right now. Never cached, so rotating the file revokes the old one."""
        return load_control_token(self.state_dir)

    def current_control_token(self) -> str | None:
        try:
            return load_control_token(self.state_dir)
        except Exception:  # noqa: BLE001 - a token that cannot be vouched for serves no control request (503)
            return None

    def new_control_challenge(self, nc: str, token: str) -> str | None:
        now = time.monotonic()
        with self._lock:
            self._control_challenges = {k: v for k, v in self._control_challenges.items() if v[1] > now}
            if len(self._control_challenges) >= 64:
                return None
            ns = secrets.token_hex(16)
            self._control_challenges[ns] = (nc, now + CONTROL_CHALLENGE_TTL, control_token_id(token))
            return ns

    def take_control_challenge(self, ns: str) -> tuple[str, str] | None:
        """(client nonce, token id) of a live challenge, removed so it is single use."""
        with self._lock:
            entry = self._control_challenges.pop(ns, None)
        return (entry[0], entry[2]) if entry and entry[1] > time.monotonic() else None

    def nonces_for(self, node: str) -> NonceCache:
        """One bounded replay cache per paired peer, so one machine cannot exhaust another's."""
        with self._lock:
            cache = self._nonces.get(node)
            if cache is None:
                cache = self._nonces[node] = NonceCache(limit=512)
            return cache

    def me(self) -> dict:
        return {"node": self.node, "name": self.name, "host": self.host}

    def trusted(self) -> dict[str, dict]:
        now = time.monotonic()
        with self._lock:
            if self._trusted_cache is None or now - self._trusted_cache[0] > 1.0:
                self._trusted_cache = (now, self.store.load())
            return self._trusted_cache[1]

    def _invalidate(self):
        with self._lock:
            self._trusted_cache = None

    # lifecycle
    def start(self) -> "PeerLink":
        self.server = LinkServer((self.bind, self.status_port), self)
        self.status_port = self.server.server_address[1]
        try:
            self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if os.name == "nt":
                _exclusive(self.udp)
            else:
                self.udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            self.udp.bind((self.bind, self.beacon_port))
            self.beacon_port = self.udp.getsockname()[1]
            self.udp.settimeout(1.0)
        except OSError:
            # Nothing is serving yet, so close directly; shutdown() would wait for a loop that never ran.
            self.server.server_close()
            if self.udp is not None:
                self.udp.close()
            self.server = self.udp = None
            raise
        for target in (self.server.serve_forever, self._listen, self._announce, self._poll):
            threading.Thread(target=target, name=f"agiw-peer-{target.__name__}", daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.udp:
            self.udp.close()

    # discovery
    def _announce(self):
        while not self._stop.is_set():
            data = make_hello(self.node, self.host, self.name, self.status_port, time.time())
            try:
                with self._lock:
                    unicast = {p["address"] for n, p in self._seen.items()
                               if n in self.trusted() and p.get("address")}
            except Exception as exc:  # noqa: BLE001 - announcing must outlive any trust-store failure
                self.poll_error = f"announce: {type(exc).__name__}"[:200]
                unicast = set()
            for target in (*self.broadcast, *unicast):
                try:
                    self.udp.sendto(data, (target, self.beacon_port))
                except OSError:
                    pass
            self._stop.wait(BEACON_INTERVAL)

    def _listen(self):
        while not self._stop.is_set():
            try:
                data, (address, _port) = self.udp.recvfrom(MAX_DATAGRAM + 1)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                continue
            try:
                self.handle_datagram(data, address)
            except Exception:  # noqa: BLE001 - one hostile datagram must not stop discovery
                continue

    def handle_datagram(self, data: bytes, address: str) -> None:
        if not lan_source(address, self.allow_loopback):
            return
        hello = read_hello(data)
        if not hello or hello["node"] == self.node:
            return
        now = self.clock()
        trusted = self.trusted()
        paired = hello["node"] in trusted
        with self._lock:
            seen = self._seen.get(hello["node"])
            if seen is None:
                if len(self._seen) >= MAX_PEERS:
                    unpaired = [n for n in self._seen if n not in trusted]
                    if not unpaired:
                        return
                    del self._seen[min(unpaired, key=lambda n: self._seen[n]["lastHello"])]
                seen = self._seen[hello["node"]] = {"address": None, "candidates": []}
            seen["lastHello"] = now
            if paired:
                # A hello cannot rename or move a paired peer: name and kind come from the trust file,
                # and a new endpoint is only tried, then adopted after an authenticated reply from it.
                endpoint = (address, hello["port"])
                if endpoint != (seen["address"], seen.get("port")) and endpoint not in seen["candidates"]:
                    seen["candidates"] = (seen["candidates"] + [endpoint])[-4:]
            else:
                seen.update(host=hello["host"], name=hello["name"], port=hello["port"], address=address)

    # polling
    def _poll(self):
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 - a crashed poller would freeze the table at its last state
                self.poll_error = f"{type(exc).__name__}: {exc}"[:200]
            self._stop.wait(POLL_INTERVAL)

    def poll_once(self) -> None:
        now = self.clock()
        trusted = self.trusted()
        with self._lock:
            for node in [n for n, s in self._seen.items()
                         if n not in trusted and now - s["lastHello"] > FORGET_AVAILABLE_AFTER]:
                del self._seen[node]
            for invite_id in [i for i, v in self._outbound.items() if now > v["expires"]]:
                del self._outbound[invite_id]
            for node in [n for n, v in self._inbound.items() if now > v["expires"]]:
                del self._inbound[node]
            targets = [(n, dict(self._seen[n], candidates=list(self._seen[n]["candidates"])))
                       for n in trusted if n in self._seen]
        # Peers are polled in parallel so one dead machine cannot age a healthy one; each peer tries its
        # known endpoint and at most the newest candidate, so a cycle stays within 2 x FETCH_DEADLINE.
        threads = [threading.Thread(target=self._poll_peer, args=(node, trusted[node], seen), daemon=True)
                   for node, seen in targets]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2 * FETCH_DEADLINE + 1)
        if self.on_change:
            self.on_change(self.snapshot())

    def _poll_peer(self, node: str, pin: dict, seen: dict) -> None:
        tries = [(seen["address"], seen.get("port"))] if seen.get("address") and seen.get("port") else []
        tries += seen["candidates"][-1:]
        update = {"status": None, "statusAt": None, "error": "no known endpoint"}
        adopted = None
        for address, port in tries:
            try:
                status = fetch_status(pin["key"], self.node, address, port, node, pin["host"], time.time())
                update, adopted = {"status": status, "statusAt": self.clock(), "error": None}, (address, port)
                break
            except Exception as exc:  # noqa: BLE001 - a failed poll demotes the peer at once, with the reason
                update = {"status": None, "statusAt": None, "error": f"{type(exc).__name__}: {exc}"[:200]}
        with self._lock:
            current = self.trusted().get(node)
            if current is None or not hmac.compare_digest(current["key"], pin["key"]):
                return  # unpaired or re-paired while this poll was in flight: its result belongs to nobody
            if adopted and node in self._seen:
                self._seen[node].update(address=adopted[0], port=adopted[1],
                                        candidates=[c for c in self._seen[node]["candidates"] if c != adopted])
            self._live.setdefault(node, {}).update(update)

    def snapshot(self) -> dict:
        now = self.clock()
        trusted = self.trusted()
        rows = []
        with self._lock:
            for node in sorted(set(trusted) | set(self._seen)):
                seen, live, pin = self._seen.get(node, {}), self._live.get(node, {}), trusted.get(node)
                row = {"node": node, "name": (pin or seen).get("name"), "host": (pin or seen).get("host"),
                       "paired": pin is not None, "address": seen.get("address"),
                       "lastHello": seen.get("lastHello"), "error": live.get("error") if pin else None,
                       "status": None, "inviteWaiting": node in self._inbound}
                good = live.get("statusAt")
                if pin is None:
                    row["state"] = "available"
                elif good is not None and now - good <= CONNECTED_WITHIN:
                    row["state"], row["status"] = "connected", live.get("status")
                elif not seen or now - seen.get("lastHello", 0) > FORGET_AVAILABLE_AFTER:
                    row["state"] = "offline"
                else:
                    row["state"] = "stale" if good is not None else "connecting"
                rows.append(row)
            outbound = [{"peer": v["peer"]["node"], "name": v["peer"]["name"], "expires": v["expires"]}
                        for v in self._outbound.values()]
        return {"schemaVersion": 1, "protocol": PROTOCOL, "observedAt": _iso(time.time()), "self": self.me(),
                "peers": rows, "invitesShowingPin": outbound, "pollError": self.poll_error,
                "quarantined": list(self.store.quarantined)}

    # pairing: inviter side
    def _resolve(self, peer: str) -> tuple[str, dict]:
        with self._lock:
            matches = [(n, s) for n, s in self._seen.items() if peer in (n, s.get("name"))]
        if len(matches) != 1:
            raise LookupError("no single discovered machine matches " + repr(peer[:64]))
        return matches[0][0], dict(matches[0][1])

    def invite(self, peer: str) -> dict:
        node, seen = self._resolve(peer)
        if node in self.trusted():
            raise ValueError("already paired; unpair first to pair again")
        pin = f"{secrets.randbelow(1_000_000):06d}"
        invite_id = secrets.token_hex(16)
        expires = self.clock() + INVITE_TTL
        record = {"id": invite_id, "pin": pin, "expires": expires, "attempts": 0,
                  "peer": {"node": node, "name": seen["name"], "host": seen["host"]}, "spake": None}
        with self._lock:
            for old in [i for i, v in self._outbound.items() if v["peer"]["node"] == node]:
                del self._outbound[old]
            self._outbound[invite_id] = record
        status, _h, _b = _http(seen["address"], seen["port"], "POST", PREFIX + "/pair/notify",
                               {"Content-Type": "application/json"},
                               canonical({**self.me(), "invite": invite_id, "port": self.status_port}), 512)
        if status != 200:
            with self._lock:
                self._outbound.pop(invite_id, None)
            raise ConnectionError(f"{seen['name']} did not accept the invite notice ({status})")
        return {"pin": pin, "peer": seen["name"], "expiresInSeconds": int(INVITE_TTL),
                "say": f"Type {pin} on {seen['name']} within 5 minutes."}

    def on_start(self, body: dict) -> tuple[int, dict]:
        with self._lock:
            record = self._outbound.get(body.get("invite"))
            joiner = record and record["peer"]
            if (not record or self.clock() > record["expires"] or body.get("node") != joiner["node"]
                    or body.get("host") != joiner["host"] or body.get("name") != joiner["name"]):
                return 404, {"error": "no such invite"}
            record["attempts"] += 1
            if record["attempts"] > INVITE_ATTEMPTS:
                del self._outbound[record["id"]]
                return 410, {"error": "too many attempts; invite cancelled"}
            attempt = secrets.token_hex(16)
            record["attempt"], record["spake"] = attempt, None  # a newer start supersedes older ones
            spake = Spake("inviter", record["pin"], self.me(), joiner, record["id"])
        try:
            keys = spake.finish(body.get("x"))
        except ValueError:
            return 400, {"error": "bad exchange"}
        with self._lock:
            if record.get("attempt") != attempt:
                return 409, {"error": "superseded by a newer attempt"}
            record["spake"] = keys
        return 200, {"y": _hex(spake.public), "confirm": keys["inviterConfirm"], "attempt": attempt}

    def on_finish(self, body: dict) -> tuple[int, dict]:
        confirm = str(body.get("confirm", ""))
        with self._lock:
            # One critical section decides between "retry of a finished exchange" and "this request
            # finishes it", so two identical finishes cannot both miss the completion and one get a 404.
            done = self._completed.get(body.get("invite"))
            if done is None:
                record = self._outbound.get(body.get("invite"))
                if (not record or not record.get("spake") or self.clock() > record["expires"]
                        or body.get("attempt") != record.get("attempt")):
                    return 404, {"error": "no such invite"}
                keys, joiner = record["spake"], record["peer"]
                if not hmac.compare_digest(confirm, keys["joinerConfirm"]):
                    record["spake"] = None
                    return 403, {"error": "wrong PIN"}
                del self._outbound[record["id"]]
                self._completed = {k: v for k, v in self._completed.items() if self.clock() <= v["expires"]}
                done = self._completed[record["id"]] = {"joinerConfirm": keys["joinerConfirm"],
                                                        "finishAck": keys["finishAck"], "saved": threading.Event(),
                                                        "ok": False, "expires": self.clock() + INVITE_TTL}
                retry = False
            else:
                retry = True
        if retry:
            # The joiner retrying after a lost reply (or a duplicate in flight): same exchange, same answer,
            # nothing new pinned. Wait outside the lock for the first finish to save; the ack is only ever
            # sent once pinned.
            if not hmac.compare_digest(confirm, done["joinerConfirm"]):
                return 403, {"error": "wrong PIN"}
            done["saved"].wait(5.0)
            if done.get("ok") and self.clock() <= done["expires"]:
                return 200, {"paired": True, "ack": done["finishAck"]}
            return 409, {"error": "pairing could not be saved on the inviting machine"}
        try:
            self.store.pin(joiner["node"], joiner["name"], joiner["host"], keys["link"])
            done["ok"] = True
        except PermissionError as exc:
            return 409, {"error": str(exc)}
        finally:
            done["saved"].set()
        self._invalidate()
        return 200, {"paired": True, "ack": keys["finishAck"]}

    # pairing: joiner side
    def on_notify(self, body: dict, address: str) -> tuple[int, dict]:
        node, invite_id, port = body.get("node"), body.get("invite"), body.get("port")
        if (not isinstance(node, str) or not UUID_RE.match(node) or node == self.node
                or body.get("host") not in HOST_KINDS or not isinstance(body.get("name"), str)
                or not body["name"] or len(body["name"]) > 64 or not body["name"].isprintable()
                or not isinstance(invite_id, str) or not re.fullmatch(r"[0-9a-f]{32}", invite_id)
                or not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535):
            return 400, {"error": "bad invite"}
        with self._lock:
            now = self.clock()
            self._inbound = {n: v for n, v in self._inbound.items() if now <= v["expires"]}
            seen = self._seen.get(node)
            if (not seen or seen.get("address") != address or now - seen.get("lastHello", -1e9) > FORGET_AVAILABLE_AFTER):
                return 403, {"error": "invite from a machine this one has not discovered"}
            if node not in self._inbound and len(self._inbound) >= MAX_INBOUND_INVITES:
                return 429, {"error": "too many pending invites"}
            self._inbound[node] = {"peer": {"node": node, "name": body["name"], "host": body["host"]},
                                   "invite": invite_id, "address": address, "port": port,
                                   "expires": self.clock() + INVITE_TTL}
        return 200, {"waiting": True}

    def accept(self, peer: str, pin: str) -> dict:
        if not re.fullmatch(r"\d{6}", pin):
            raise ValueError("the PIN is six digits")
        with self._lock:
            matches = [v for n, v in self._inbound.items() if peer in (n, v["peer"]["name"]) or
                       (not peer and len(self._inbound) == 1)]
            if len(matches) != 1:
                raise LookupError("no single pending invite matches " + repr(peer[:64]))
            invite = dict(matches[0])
        spake = Spake("joiner", pin, invite["peer"], self.me(), invite["invite"])
        status, _h, raw = _http(invite["address"], invite["port"], "POST", PREFIX + "/pair/start",
                                {"Content-Type": "application/json"},
                                canonical({**self.me(), "invite": invite["invite"], "x": _hex(spake.public)}), 2048)
        if status != 200:
            raise ConnectionError(f"{invite['peer']['name']} refused the pairing ({status})")
        reply = strict_json(raw)
        keys = spake.finish(reply.get("y"))
        attempt = reply.get("attempt")
        if not hmac.compare_digest(str(reply.get("confirm", "")), keys["inviterConfirm"]):
            _http(invite["address"], invite["port"], "POST", PREFIX + "/pair/finish",
                  {"Content-Type": "application/json"},
                  canonical({"invite": invite["invite"], "attempt": attempt, "confirm": "0"}), 512)
            raise PermissionError("wrong PIN (or not the machine that showed it); try again")
        finish = canonical({"invite": invite["invite"], "attempt": attempt, "confirm": keys["joinerConfirm"]})
        status, ack = None, ""
        for _ in range(3):  # the inviter answers a repeated finish idempotently, so a lost reply is retried
            try:
                status, _h, raw_ack = _http(invite["address"], invite["port"], "POST", PREFIX + "/pair/finish",
                                            {"Content-Type": "application/json"}, finish, 512)
                ack = str(strict_json(raw_ack).get("ack", "")) if status == 200 else ""
                break
            except (OSError, TimeoutError, ValueError, AttributeError):
                continue
        if status != 200:
            raise ConnectionError(f"{invite['peer']['name']} did not confirm the pairing ({status})")
        if not hmac.compare_digest(ack, keys["finishAck"]):
            # A bare 200 proves nothing: only the machine holding the PIN-derived keys can make this ack.
            raise PermissionError(f"{invite['peer']['name']}'s confirmation was not authentic; nothing was paired")
        self.store.pin(invite["peer"]["node"], invite["peer"]["name"], invite["peer"]["host"], keys["link"])
        self._invalidate()
        with self._lock:
            self._inbound.pop(invite["peer"]["node"], None)
            self._seen.setdefault(invite["peer"]["node"], {"address": invite["address"], "candidates": [],
                                                           "lastHello": self.clock(), **invite["peer"],
                                                           "port": invite["port"]})
        return {"paired": True, "peer": invite["peer"]["name"]}

    def unpair(self, peer: str) -> dict:
        trusted = self.trusted()
        matches = [n for n, p in trusted.items() if peer in (n, p.get("name"))]
        if len(matches) != 1:
            raise LookupError("no single paired machine matches " + repr(peer[:64]))
        self.store.remove(matches[0])
        self._invalidate()
        with self._lock:
            self._live.pop(matches[0], None)
        return {"unpaired": matches[0]}


def directed_broadcasts() -> tuple[str, ...]:
    """255.255.255.255 leaves by one interface only on a multihomed Mac, so also send to each local
    IPv4 address's /24 broadcast. A wrong guess costs one ignored datagram."""
    targets = ["255.255.255.255"]
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        infos = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private and not ip.is_loopback:
            targets.append(str(ipaddress.ip_network(f"{ip}/24", strict=False).broadcast_address))
    return tuple(dict.fromkeys(targets))


def write_snapshot(path: Path) -> Callable[[dict], None]:
    def write(doc: dict) -> None:
        try:
            _write_private(path, json.dumps(doc, indent=1))
        except OSError:
            pass

    return write


# ---------- CLI ----------

def _control(state_dir: Path, port: int, method: str, path: str, body: dict | None = None) -> dict:
    status, doc = control_request(state_dir, port, method, path, body)
    if status != 200:
        raise SystemExit((doc.get("error") if isinstance(doc, dict) else None) or f"peer link answered {status}")
    return doc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="peer_link", description=__doc__.split("\n")[0])
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    parser.add_argument("--status-port", type=int, default=STATUS_PORT)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="announce this machine and auto-connect to paired machines")
    run.add_argument("--host", choices=HOST_KINDS, default=detect_host_kind())
    run.add_argument("--name", default=socket.gethostname().split(".")[0])
    source = run.add_mutually_exclusive_group()
    source.add_argument("--status-file", type=Path, help="schema-1 status JSON kept fresh by the local monitor")
    source.add_argument("--lmstudio", help="local LM Studio base URL, e.g. http://127.0.0.1:1234 (Mac default)")
    run.add_argument("--beacon-port", type=int, default=BEACON_PORT)
    sub.add_parser("peers", help="list discovered and paired machines")
    invite = sub.add_parser("invite", help="pair with a discovered machine: shows a 6-digit PIN here")
    invite.add_argument("peer", help="its name or uuid, as listed by `peers`")
    accept = sub.add_parser("accept", help="type the PIN another machine is showing")
    accept.add_argument("peer", nargs="?", default="", help="needed only when several invites wait")
    unpair = sub.add_parser("unpair", help="forget a paired machine")
    unpair.add_argument("peer")
    args = parser.parse_args(argv)

    if args.command == "run":
        if args.status_file:
            provider = file_status(args.status_file)
        elif args.lmstudio or args.host == "mac":
            provider = lmstudio_status(args.lmstudio or "http://127.0.0.1:1234", args.host)
        elif args.host == "linux":
            provider = linux_status()
        else:
            parser.error("--status-file is required on " + args.host + ": serve the observer's own status, "
                         "not whatever LM Studio happens to list")
        link = PeerLink(args.state_dir, args.host, args.name, provider, status_port=args.status_port,
                        beacon_port=args.beacon_port, broadcast=directed_broadcasts())
        link.on_change = write_snapshot(args.state_dir / "peers.json")
        link.start()
        print(f"agiw peer link: {link.name} {link.node} ({args.host}); tcp/{link.status_port} udp/{link.beacon_port}; "
              f"{len(link.trusted())} paired", flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            link.stop()
        return 0
    if args.command == "peers":
        doc = _control(args.state_dir, args.status_port, "GET", "/control/peers")
        for row in doc["peers"]:
            extra = " (invite waiting: run `accept`)" if row["inviteWaiting"] else ""
            print(f"{row['state']:<10} {row['name'] or '?':<24} {row['host'] or '?':<8} {row['node']}{extra}")
        if not doc["peers"]:
            print("no AGIW machines seen yet")
        return 0
    if args.command == "invite":
        doc = _control(args.state_dir, args.status_port, "POST", "/control/invite", {"peer": args.peer})
        print(f"PIN {doc['pin']}  -  {doc['say']}")
        return 0
    if args.command == "accept":
        import getpass
        pin = getpass.getpass("PIN shown on the other machine: ").strip()
        doc = _control(args.state_dir, args.status_port, "POST", "/control/accept", {"peer": args.peer, "pin": pin})
        print(f"paired with {doc['peer']}")
        return 0
    doc = _control(args.state_dir, args.status_port, "POST", "/control/unpair", {"peer": args.peer})
    print(f"unpaired {doc['unpaired']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
