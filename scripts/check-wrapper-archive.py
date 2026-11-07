#!/usr/bin/env python3
"""Reject a Mac wrapper archive that omits the edition fixtures or ships the heavy runtime binary.

The c613856 native run passed only after a fresh extract whose manifest included the
Test and Fixture files. An earlier thin archive omitted them. This checker fails closed
on that shape. It does not modify App/Resources and it does not publish a remote.
"""

from __future__ import annotations

import hashlib
import io
import sys
import tarfile
from pathlib import Path


REQUIRED = (
    "Packages/AgiwCore/Tests/AgiwCoreTests/Fixtures/DemoGolden/demo-idle.json",
    "Packages/AgiwCore/Tests/AgiwCoreTests/Fixtures/DemoGolden/demo-generating.json",
    "Packages/AgiwCore/Tests/AgiwCoreTests/Fixtures/DemoGolden/demo-memory-watch.json",
    "Packages/AgiwCore/Tests/AgiwCoreTests/Fixtures/DemoGolden/demo-recovering.json",
    "scripts/appstore-distribution.sh",
    "scripts/check-web-edition.sh",
    "scripts/web-edition/test_web_edition.mjs",
    "scripts/web-edition/feature-denylist.txt",
    "scripts/web-edition/engine.mjs",
    "RUNTIME-MANIFEST.sha256",
)

# Source-only archives keep the entitlement and the runtime manifest, not the node binary.
FORBIDDEN = (
    "VendorRuntime/NisiNode",
    "App/Resources/node-runtime/bin/node",
)


def names_of(archive: Path) -> set[str]:
    raw = archive.read_bytes()
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as tar:
        found = set()
        for member in tar.getmembers():
            name = member.name.lstrip("./")
            if name:
                found.add(name)
        return found


def text_of(archive: Path, suffix: str) -> str:
    with tarfile.open(archive, mode="r:*") as tar:
        for member in tar.getmembers():
            if member.name.lstrip("./") == suffix:
                handle = tar.extractfile(member)
                if handle is None:
                    raise SystemExit(f"missing text {suffix}")
                return handle.read().decode("utf-8")
    raise SystemExit(f"missing {suffix}")


def check(archive: Path) -> list[str]:
    if not archive.is_file():
        return [f"missing archive {archive}"]
    problems = []
    found = names_of(archive)
    for path in REQUIRED:
        if path not in found:
            problems.append(f"missing {path}")
    for path in FORBIDDEN:
        if path in found:
            problems.append(f"forbidden binary {path}")
    if not problems:
        script = text_of(archive, "scripts/appstore-distribution.sh")
        if "RELEASE_XCODE_BUILD=27A266a" not in script:
            problems.append("distribution pin 27A266a is absent")
        if "skip" in script.lower() and "27A266a" in script and "SKIP" in script:
            problems.append("distribution script names a skip for the pin")
    return problems


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: check-wrapper-archive.py ARCHIVE")
    archive = Path(sys.argv[1])
    problems = check(archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest() if archive.is_file() else "absent"
    if problems:
        print(f"wrapper archive {digest} FAILED")
        for problem in problems:
            print(f"- {problem}")
        raise SystemExit(1)
    print(f"wrapper archive {digest} OK required={len(REQUIRED)} forbidden=0")


if __name__ == "__main__":
    main()
