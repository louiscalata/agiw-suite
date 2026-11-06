"""The wrapper handoff must include DemoGolden fixtures and must not carry the node binary."""

import importlib.util
import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "check_wrapper_archive", ROOT / "scripts" / "check-wrapper-archive.py")
ARCHIVE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ARCHIVE)
FORBIDDEN = ARCHIVE.FORBIDDEN
REQUIRED = ARCHIVE.REQUIRED
check = ARCHIVE.check


def archive_with(paths, texts=None):
    texts = texts or {}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path in paths:
            data = texts.get(path, b"ok\n")
            if isinstance(data, str):
                data = data.encode()
            info = tarfile.TarInfo(path)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class WrapperArchiveTests(unittest.TestCase):
    def test_missing_demo_idle_is_rejected(self):
        paths = [path for path in REQUIRED if not path.endswith("demo-idle.json")]
        texts = {"scripts/appstore-distribution.sh": "RELEASE_XCODE_BUILD=27A266a\n"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "thin.tgz"
            path.write_bytes(archive_with(paths, texts))
            problems = check(path)
        self.assertTrue(any("demo-idle.json" in problem for problem in problems), problems)

    def test_forbidden_node_binary_is_rejected(self):
        texts = {"scripts/appstore-distribution.sh": "RELEASE_XCODE_BUILD=27A266a\n"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "heavy.tgz"
            path.write_bytes(archive_with((*REQUIRED, FORBIDDEN[0]), texts))
            problems = check(path)
        self.assertTrue(any("NisiNode" in problem for problem in problems), problems)

    def test_pin_must_stay_27a266a(self):
        texts = {"scripts/appstore-distribution.sh": "RELEASE_XCODE_BUILD=27B52\n"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pin.tgz"
            path.write_bytes(archive_with(REQUIRED, texts))
            problems = check(path)
        self.assertTrue(any("27A266a" in problem for problem in problems), problems)

    def test_complete_source_archive_passes_when_present(self):
        archive = os.environ.get("AGIW_WRAPPER_ARCHIVE")
        if not archive:
            self.skipTest("AGIW_WRAPPER_ARCHIVE is not set")
        self.assertEqual(check(Path(archive)), [])


if __name__ == "__main__":
    unittest.main()
