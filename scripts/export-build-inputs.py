#!/usr/bin/env python3
"""Freeze the current Mac web inputs and Windows edition inputs for transfer.

This does not run sync-web.sh and does not write into another checkout.
scripts/sync-web.sh and scripts/appstore-distribution.sh are not in this
repository. The 6 Nov 2026 Mac receipt places them in ~/agiw-suite-appstore.
The unsigned Debug app built there used web HEAD 317aad2 on a dirty tree.
That app is not this head. Copying this bundle over that tree is not a sync.

The Mac candidate set is the web/ and jaryo/ paths named by the resources
arrays in build.sh and package-release.sh. Those two arrays must match.
The Windows set is the files windows/install.ps1 copies, plus that script
and the Windows tests. The archive contains no .git directory.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import sys
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def bash_resources(path: Path) -> list[str]:
    text = path.read_text()
    match = re.search(r"resources=\((.*?)\)", text, re.S)
    if not match:
        raise SystemExit(f"No resources array in {path}")
    names = re.findall(r"[A-Za-z0-9_./-]+", match.group(1))
    if not names:
        raise SystemExit(f"Empty resources array in {path}")
    return names


def ps_quoted_lists(path: Path) -> list[list[str]]:
    text = path.read_text()
    lists = []
    for block in re.findall(r"foreach \(\$file in ([^)]*)\)", text):
        names = re.findall(r"'([^']+)'", block)
        if names:
            lists.append(names)
    if len(lists) < 3:
        raise SystemExit(f"Could not read install file lists from {path}")
    return lists


def regular_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"Missing or non-regular input: {path.relative_to(ROOT)}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_text(*args: str) -> str:
    result = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def main() -> None:
    destination = Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/cursor/artifacts/build-inputs")
    if destination.exists():
        raise SystemExit(f"Refusing to replace {destination}")
    build = bash_resources(ROOT / "build.sh")
    release = bash_resources(ROOT / "package-release.sh")
    if build != release:
        raise SystemExit("build.sh and package-release.sh resource lists differ")
    mac_inputs = [name for name in build if name.startswith("web/") or name.startswith("jaryo/")]
    if not mac_inputs:
        raise SystemExit("No web or jaryo inputs in the resource list")
    install = ROOT / "windows" / "install.ps1"
    quoted = ps_quoted_lists(install)
    windows_rows: list[tuple[str, str]] = []
    for name in sorted((ROOT / "windows" / "agiw_win").glob("*.py")):
        windows_rows.append((str(name.relative_to(ROOT)), f"windows/agiw_win/{name.name}"))
    for name in quoted[0]:
        windows_rows.append((f"windows/{name}", f"windows/{name}"))
    for name in quoted[1]:
        windows_rows.append((f"web/{name}", f"web/{name}"))
    for name in quoted[2]:
        windows_rows.append((f"jaryo/{name.replace(chr(92), '/')}", f"jaryo/{name.replace(chr(92), '/')}"))
    for name in quoted[3]:
        windows_rows.append((name, name))
    windows_rows.append(("windows/install.ps1", "windows/install.ps1"))
    for path in sorted((ROOT / "windows" / "tests").rglob("*")):
        if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts:
            windows_rows.append((str(path.relative_to(ROOT)), str(path.relative_to(ROOT))))

    seen: set[str] = set()
    rows: list[tuple[str, str]] = []
    for source, archived in [("mac", name) for name in mac_inputs]:
        del source
        if archived not in seen:
            seen.add(archived)
            rows.append((archived, f"mac-web/{archived}"))
    for source, archived in windows_rows:
        archived_name = f"windows-edition/{archived}"
        if archived_name in seen:
            continue
        seen.add(archived_name)
        rows.append((source, archived_name))

    head = git_text("rev-parse", "HEAD")
    status = git_text("status", "--porcelain", "--untracked-files=normal")
    dirty = bool(status)
    destination.mkdir(parents=True)
    payload = destination / "payload"
    manifest_lines = []
    for source, archived in rows:
        path = ROOT / source
        regular_file(path)
        target = payload / archived
        target.parent.mkdir(parents=True, exist_ok=True)
        data = path.read_bytes()
        if stat.S_ISLNK(path.lstat().st_mode):
            raise SystemExit(f"Symlink refused: {source}")
        target.write_bytes(data)
        target.chmod(0o644)
        digest = sha256(target)
        manifest_lines.append(f"{digest}  {target.stat().st_size}  {archived}")
    manifest_lines.sort()
    manifest = destination / "manifest.sha256"
    manifest.write_text("\n".join(manifest_lines) + "\n")
    provenance = destination / "PROVENANCE.txt"
    provenance.write_text(
        "\n".join([
            "AGIW build-input bundle",
            f"head: {head or 'not a git checkout'}",
            f"dirty: {'yes' if dirty else 'no'}",
            "mac-sync: candidate web and jaryo files from build.sh resources.",
            "sync-web.sh: not in this repository. Mac receipt path is ~/agiw-suite-appstore/scripts/sync-web.sh.",
            "Do not copy this bundle onto the dirty App Store tree at HEAD 317aad2.",
            "That unsigned Debug build is not this head and is not Jaryo acceptance.",
            "appstore-distribution.sh: not in this repository. Its 27A266a pin was not changed and must not be skipped.",
            "This bundle is the Jaryo web and Windows edition inputs. It is not the Mac wrapper archive.",
            "A wrapper archive must pass scripts/check-wrapper-archive.py, including DemoGolden/demo-idle.json.",
            "The Mac restricted edition may omit online-code-mode.mjs. The Windows web keeps Fix inference.",
            "Xcode 27B52 was the only measured install. Compatibility of that pin was not verified here.",
            "windows-edition: files named by windows/install.ps1, that script, and windows/tests. No .git directory.",
            "pipe-host tests and installer/wxs-reader.test.mjs are not in this checkout.",
            "The PC listen EACCES result on %TEMP%\\*.sock stays a blocked test in that other tree. It was not skipped here.",
            "WiX is not installed by this export. A parent may install the free official toolchain. That is not MSI acceptance.",
            "PC POST ROLLBACK remains open. Mac POST 0/4 is not PASS.",
            "",
        ])
    )
    if dirty:
        (destination / "GIT-STATUS.txt").write_text(status + "\n")
    archive = destination / "agiw-build-inputs.tar"
    with tarfile.open(archive, "w") as tar:
        tar.add(provenance, arcname="PROVENANCE.txt")
        tar.add(manifest, arcname="manifest.sha256")
        tar.add(payload, arcname="payload")
    print(f"files {len(manifest_lines)}")
    print(f"dirty {dirty}")
    print(f"head {head}")
    print(f"sha256 {sha256(archive)}  {archive}")


if __name__ == "__main__":
    main()
