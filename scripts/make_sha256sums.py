"""Write or check SHA256SUMS.txt for a release folder.

The 1.0.0 download uses one line per file: 64 lowercase hex digits, two spaces,
then the file name. SHA256SUMS.txt itself is not listed. Only regular files in
the folder are included. Subdirectories are not walked.
"""
import argparse
import hashlib
import sys
from pathlib import Path

SUM_NAME = "SHA256SUMS.txt"


def _files(folder: Path) -> list[Path]:
    found = []
    for path in folder.iterdir():
        if path.name == SUM_NAME or path.is_symlink() or not path.is_file():
            continue
        found.append(path)
    found.sort(key=lambda item: item.name)
    return found


def _line(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"{digest}  {path.name}\n"


def write_sums(folder: Path) -> None:
    body = "".join(_line(path) for path in _files(folder))
    target = folder / SUM_NAME
    temporary = folder / f".{SUM_NAME}.tmp"
    temporary.write_text(body, encoding="utf-8")
    temporary.replace(target)


def check_sums(folder: Path) -> list[str]:
    """Return a list of problems. An empty list means the sums match."""
    target = folder / SUM_NAME
    if not target.is_file():
        return [f"{SUM_NAME} is missing"]
    text = target.read_text(encoding="utf-8")
    if text and not text.endswith("\n"):
        return [f"{SUM_NAME} does not end with a newline"]
    listed = {}
    problems = []
    for raw in text.splitlines():
        if len(raw) < 66 or raw[64:66] != "  ":
            problems.append(f"bad line: {raw!r}")
            continue
        digest, name = raw[:64], raw[66:]
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            problems.append(f"bad digest for {name}")
            continue
        if not name or "/" in name or name in (".", "..", SUM_NAME):
            problems.append(f"bad name {name!r}")
            continue
        if name in listed:
            problems.append(f"duplicate name {name}")
            continue
        listed[name] = digest
    expected = {path.name for path in _files(folder)}
    for name in sorted(expected - set(listed)):
        problems.append(f"missing from {SUM_NAME}: {name}")
    for name, digest in listed.items():
        path = folder / name
        if not path.is_file() or path.is_symlink():
            problems.append(f"missing file: {name}")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != digest:
            problems.append(f"checksum mismatch: {name}")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Write or check SHA256SUMS.txt for a release folder.")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--check", action="store_true", help="verify SHA256SUMS.txt instead of writing it")
    args = parser.parse_args(argv)
    folder = args.folder
    if not folder.is_dir():
        print(f"not a folder: {folder}", file=sys.stderr)
        return 2
    if args.check:
        problems = check_sums(folder)
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1 if problems else 0
    write_sums(folder)
    return 0


if __name__ == "__main__":
    sys.exit(main())
