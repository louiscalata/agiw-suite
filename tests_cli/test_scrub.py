"""The CLI's files are public: no personal home paths, private LAN addresses or host names, private share
names or workspace links. (Patterns are assembled at run time so this file does not match itself.)"""
import re
import unittest

from support import ROOT

FILES = ["pyproject.toml", "docs/cli.md", ".github/workflows/cli.yml",
         *sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "agiw").glob("*.py")),
         *sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "tests_cli").glob("*.py"))]

PATTERNS = {
    "personal home path": re.compile("/(?:Users|home)/" + "louis" + "calata", re.I),
    "RFC 1918 address": re.compile(r"(?<![\d.])(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}"
                                   r"|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})(?![\d.])"),
    "private host name": re.compile(r"\b[A-Za-z0-9-]+\.(?:local|lan|internal|localdomain|home\.arpa)\b"),
    "private share name": re.compile("Shared" + "Chami", re.I),
    "workspace link": re.compile(r"notion\.(?:so|site)|app\.notion\.com", re.I),
}


class Scrub(unittest.TestCase):
    def test_new_files_hold_no_private_details(self):
        self.assertGreater(len(FILES), 8)
        for name in FILES:
            text = (ROOT / name).read_text(encoding="utf-8")
            for label, pattern in PATTERNS.items():
                match = pattern.search(text)
                self.assertIsNone(match, f"{label} in {name}: {match and match.group(0)!r}")


if __name__ == "__main__":
    unittest.main()
