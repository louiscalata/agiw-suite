"""Record saved inputs against their pinned source archive (Python 3.12 or later)."""

import argparse
import hashlib
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

SOURCE = "windows/agiw_win/collect.py"


def _git(repo, *arguments):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_CONFIG_")}
    return subprocess.run(
        ["git", "-C", str(repo), *arguments], check=True, capture_output=True, timeout=30, env=environment
    ).stdout


def _record(repo, document, revision):
    original = _git(repo, "show", f"{revision}:{SOURCE}")
    source_hash = hashlib.sha256(original).hexdigest()
    if document["generated_from"]["source"] != SOURCE or document["generated_from"]["source_sha256"] != source_hash:
        raise ValueError("Pinned source differs from the corpus source")
    if (repo / SOURCE).read_bytes() != original:
        raise ValueError("Working source differs from the pinned source")
    with tempfile.TemporaryDirectory(prefix="windows-collect-record-") as folder:
        exported = Path(folder)
        archive = exported / "source.tar"
        archive.write_bytes(_git(repo, "archive", revision))
        with tarfile.open(archive) as bundle:
            bundle.extractall(exported, filter="data")
        if (exported / SOURCE).read_bytes() != original:
            raise ValueError("Archive source differs from the pinned source")
        sys.path[:0] = [str(exported / "windows"), str(exported)]
        module = importlib.import_module("agiw_win.collect")
        spec = importlib.util.spec_from_file_location(
            "collect_recording", repo / "windows/tests/test_collect_golden.py"
        )
        harness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(harness)
        if Path(module.__file__).resolve() != exported / SOURCE or harness.collect is not module:
            raise ValueError("Recording imported an unexpected source")
        for case in document["cases"]:
            case["expected"] = harness.run_case(module, case)
    document["generated_from"].update(revision=revision, generator="test_support/windows_collect_golden/record.py")
    return document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--revision", help="Use a different revision with identical source bytes")
    args = parser.parse_args()
    if sys.version_info < (3, 12):
        parser.error("Recording requires Python 3.12 or later for safe archive extraction")
    repo = Path(__file__).resolve().parents[2]
    document = json.loads(Path(__file__).with_name("cases.json").read_text(encoding="utf-8"))
    revision = _git(repo, "rev-parse", args.revision or document["generated_from"]["revision"]).decode().strip()
    document = _record(repo, document, revision)
    args.output.write_text(json.dumps(document, indent=2, ensure_ascii=True, allow_nan=False) + "\n", encoding="utf-8")
    sys.stdout.write(f"Recorded {len(document['cases'])} cases from {revision}\n")


if __name__ == "__main__":
    main()
