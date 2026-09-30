"""Black-box legal-input refusal checks; never compile, sign, or create a DMG."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform == "darwin", "release packaging requires macOS")
class LegalReleaseInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = Path(__file__).resolve().parent
        cls.git_env = {key: value for key, value in os.environ.items()
                       if not key.startswith("GIT_")}
        cls.git_env.update({"GIT_CONFIG_GLOBAL": "/dev/null",
                            "GIT_CONFIG_SYSTEM": "/dev/null",
                            "GIT_CONFIG_NOSYSTEM": "1"})
        paths = subprocess.check_output(
            ["git", "-C", str(cls.repo), "ls-files", "-z"], env=cls.git_env
        ).decode().split("\0")
        # Exercise the current checkout rather than a reconstructed input list.
        # No parsing or reconstruction of the script's input list is involved.
        cls.inputs = {
            name: (cls.repo / name).read_bytes()
            for name in paths if name and (cls.repo / name).is_file()
        }
        for name in ("package-release.sh", "LICENSE", "NOTICE"):
            cls.inputs[name] = (cls.repo / name).read_bytes()

    def check_refusal(self, legal_name, defect, existing_output):
        with tempfile.TemporaryDirectory(prefix="agiw-package-test-") as temp:
            root = Path(temp)
            isolated_env = self.git_env.copy()
            source = root / "source"
            source.mkdir()
            for name, data in self.inputs.items():
                destination = source / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
            subprocess.run(["git", "init", "--template=", "-q", str(source)],
                           env=isolated_env, check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(
                ["git", "-C", str(source), "-c", "user.name=Packaging Test",
                 "-c", "user.email=packaging-test@example.invalid",
                 "-c", "commit.gpgSign=false", "-c", "core.hooksPath=/dev/null",
                 "commit", "--allow-empty", "-qm", "Isolated test baseline"],
                env=isolated_env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            legal = source / legal_name
            legal.unlink()
            if defect == "empty":
                legal.touch()
            elif defect == "symlink":
                target = root / "otherwise-valid-legal.txt"
                target.write_bytes(self.inputs[legal_name])
                legal.symlink_to(target)

            output = root / "output"
            if existing_output:
                output.mkdir()
                (output / "sentinel").write_bytes(b"preserve existing output\x00\n")
            before = self.output_state(output)
            tools = root / "tools"
            tools.mkdir()
            log = root / "unexpected-tools.log"
            for name in ("xcrun", "xcodebuild", "xcode-select", "codesign",
                         "hdiutil", "plutil", "lipo"):
                shim = tools / name
                shim.write_text('#!/bin/bash\n'
                                'printf "%s\\n" "$0" >> "$PACKAGE_TEST_TOOL_LOG"\n'
                                'exit 97\n')
                shim.chmod(0o700)
            environment = isolated_env.copy()
            environment.update({
                "PATH": str(tools) + os.pathsep + "/usr/bin:/bin:/usr/sbin:/sbin",
                "MONITOR_RELEASE_SIGN_IDENTITY": "0" * 40,
                "MONITOR_RELEASE_ALLOW_DIRTY": "1",
                "MONITOR_RELEASE_OUTPUT_DIR": str(output),
                "PACKAGE_TEST_TOOL_LOG": str(log),
            })
            result = subprocess.run(
                ["/bin/bash", str(source / "package-release.sh")],
                cwd=source, env=environment, capture_output=True, text=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn(
                f"Missing, empty or symlinked legal release input: {legal_name}",
                result.stderr,
            )
            self.assertEqual(self.output_state(output), before)
            self.assertFalse(log.exists(), log.read_text() if log.exists() else "")

    @staticmethod
    def output_state(output):
        if not output.exists():
            return None
        paths = [output, *output.rglob("*")]
        return {
            str(path.relative_to(output)): (
                path.stat().st_ino, path.stat().st_mode, path.stat().st_mtime_ns,
                None if path.is_dir() else path.read_bytes(),
            )
            for path in paths
        }

    def test_invalid_legal_input_preserves_existing_output(self):
        for name in ("LICENSE", "NOTICE"):
            for defect in ("missing", "empty", "symlink"):
                with self.subTest(legal=name, defect=defect):
                    self.check_refusal(name, defect, existing_output=True)

    def test_invalid_legal_input_does_not_create_output(self):
        for name in ("LICENSE", "NOTICE"):
            for defect in ("missing", "empty", "symlink"):
                with self.subTest(legal=name, defect=defect):
                    self.check_refusal(name, defect, existing_output=False)


if __name__ == "__main__":
    unittest.main()
