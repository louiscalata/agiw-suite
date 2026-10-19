"""Every PowerShell Get-Command must resolve to ONE command before it is invoked.

On a Windows host with two git.exe on PATH (Git\\cmd and Git\\mingw64\\bin), `(Get-Command git.exe).Source` is a two-element
array; `& $array ...` then throws CommandNotFoundException, and a verifier that catches it reports "unverified" for a
reason unrelated to what it verifies (native PC run, 2026-10-18: twelve provenance tests and the signature control).
Production pipes through `Select-Object -First 1`; this check keeps every script and embedded test script doing the same.
A line that genuinely wants every match carries the marker `allow-multiple-commands` with the reason next to it.
"""
from pathlib import Path
import re
import unittest

REPO = Path(__file__).resolve().parents[2]
SKIP_DIRS = {'.git', 'node_modules', 'integration-evidence', '.scratch', '__pycache__'}
SUFFIXES = {'.ps1', '.psm1', '.cmd', '.py', '.mjs', '.js'}
GET_COMMAND = re.compile(r'\bGet-Command\b')
FIRST_ONLY = re.compile(r'\|\s*Select-Object\s+-First\s+1\b')
MARKER = 'allow-multiple-commands'


def bare_get_command_lines(text):
    """(line number, line) for each Get-Command not piped to `Select-Object -First 1` on the same line."""
    out = []
    for number, line in enumerate(text.splitlines(), 1):
        for match in GET_COMMAND.finditer(line):
            rest = line[match.end():]
            if MARKER in line or FIRST_ONLY.search(rest):
                continue
            # Python/JS source only matters where it embeds PowerShell; skip prose mentions in this module itself.
            out.append((number, line.strip()))
            break
    return out


def scanned_files():
    for path in REPO.rglob('*'):
        if path.suffix in SUFFIXES and path.is_file() and not (set(path.relative_to(REPO).parts) & SKIP_DIRS):
            if path.resolve() == Path(__file__).resolve():
                continue
            yield path


class CommandResolutionTests(unittest.TestCase):
    def test_every_get_command_resolves_to_one_command(self):
        offenders = []
        for path in scanned_files():
            for number, line in bare_get_command_lines(path.read_text(encoding='utf-8', errors='replace')):
                offenders.append(f'{path.relative_to(REPO)}:{number}: {line}')
        self.assertEqual(offenders, [], 'Get-Command must be piped to "Select-Object -First 1" (or marked '
                                        f'{MARKER} with a reason):\n' + '\n'.join(offenders))

    def test_detector_catches_the_native_failure_shape(self):
        # The exact pre-fix harness line from test_build_and_feed.py (native PC run, 2026-10-18).
        bad = "$realGit = (Get-Command git.exe -CommandType Application -ErrorAction Stop).Source"
        good = "$realGit = (Get-Command git.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source"
        self.assertEqual(len(bare_get_command_lines(bad)), 1)
        self.assertEqual(bare_get_command_lines(good), [])
        self.assertEqual(bare_get_command_lines(bad + f"  # {MARKER}: wants all matches"), [])

    def test_the_scan_reaches_the_known_sites(self):
        texts = {p.relative_to(REPO).as_posix(): p.read_text(encoding='utf-8', errors='replace') for p in scanned_files()}
        for site in ('windows/install.ps1', 'windows/agiw-monitor.ps1', 'windows/tests/test_build_and_feed.py'):
            self.assertIn(site, texts)
            self.assertTrue(GET_COMMAND.search(texts[site]), site)


if __name__ == '__main__':
    unittest.main()
