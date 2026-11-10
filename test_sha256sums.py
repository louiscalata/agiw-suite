"""SHA256SUMS.txt matches the 1.0.0 line format."""
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "scripts" / "make_sha256sums.py"


class Sha256SumsTest(unittest.TestCase):
    def test_write_and_check_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "AGIW-example.zip").write_bytes(b"zip-bytes")
            (folder / "notes.txt").write_bytes(b"notes")
            write = subprocess.run([sys.executable, "-B", str(SCRIPT), str(folder)],
                                    capture_output=True, text=True)
            self.assertEqual(write.returncode, 0, write.stderr)
            sums = (folder / "SHA256SUMS.txt").read_text(encoding="utf-8")
            lines = sums.splitlines()
            self.assertEqual(len(lines), 2)
            self.assertTrue(sums.endswith("\n"))
            names = []
            for line in lines:
                self.assertEqual(line[64:66], "  ")
                digest, name = line[:64], line[66:]
                self.assertTrue(all(char in "0123456789abcdef" for char in digest))
                self.assertEqual(digest, hashlib.sha256((folder / name).read_bytes()).hexdigest())
                names.append(name)
            self.assertEqual(names, ["AGIW-example.zip", "notes.txt"])
            check = subprocess.run([sys.executable, "-B", str(SCRIPT), "--check", str(folder)],
                                    capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stderr)
            (folder / "notes.txt").write_bytes(b"changed")
            failed = subprocess.run([sys.executable, "-B", str(SCRIPT), "--check", str(folder)],
                                     capture_output=True, text=True)
            self.assertEqual(failed.returncode, 1)
            self.assertIn("checksum mismatch: notes.txt", failed.stderr)

    def test_v1_0_0_line_shape(self):
        line = "9a995fb8e3f6e6c5a12d76ec0d8b7f485643dd02cccf45db7c15ce1314f473a2  AGIW-Inference-Monitor-1.0.0-7-arm64.zip\n"
        self.assertEqual(len(line.splitlines()[0][:64]), 64)
        self.assertEqual(line[64:66], "  ")
        self.assertTrue(line.endswith(".zip\n"))
