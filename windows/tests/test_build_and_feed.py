"""PLAN A3: VERSION 0.2.0 with build.json from install.ps1, the shipped peer link and its local status feed, and
test discovery from the repository root."""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

WINDOWS = Path(__file__).resolve().parents[1]
REPO = WINDOWS.parent
sys.path.insert(0, str(WINDOWS))
from agiw_win import SERVICE, VERSION, buildinfo, probes  # noqa: E402
from agiw_win.collect import WORKER_VERSION  # noqa: E402
from agiw_win import server  # noqa: E402
from tests.install_helpers import install_root  # noqa: E402

COMMIT = "af39471a9e7c0d210ae4e7520c0a2436d130c52c"
INSTALLER = (WINDOWS / "install.ps1").read_text(encoding="utf-8")


def signed_fixture_repo(root, signature=True, missing_module=False, custom_filter=False):
    """A disposable Git SSH signing identity and trust store, never the user's Git config."""
    git = shutil.which('git')
    keygen = shutil.which('ssh-keygen')
    if not keygen and git:
        bundled = Path(git).resolve().parent.parent / 'usr/bin/ssh-keygen.exe'
        if bundled.is_file():
            keygen = str(bundled)
    if not git or not keygen:
        raise AssertionError('signed fixture requires git and ssh-keygen (including Git for Windows OpenSSH)')
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('GIT_')}
    empty_config = root / 'empty.gitconfig'
    empty_config.write_text('', encoding='utf-8')
    env.update(GIT_CONFIG_GLOBAL=str(empty_config), GIT_CONFIG_NOSYSTEM='1', GIT_ATTR_NOSYSTEM='1',
               GIT_TERMINAL_PROMPT='0')
    key = root / 'test-signing-key'
    generated = subprocess.run([keygen, '-q', '-t', 'ed25519', '-N', '', '-f', str(key)],
                               env=env, capture_output=True, text=True, timeout=30)
    if generated.returncode:
        raise AssertionError('throwaway SSH key generation failed: ' + generated.stderr)
    allowed = root / 'allowed-signers'
    allowed.write_text('agiw-fixture@example.invalid ' + key.with_suffix('.pub').read_text().strip() + '\n',
                       encoding='utf-8')
    repo = root / 'source-repo'
    repo.mkdir()
    def run(*args):
        return subprocess.run([git, '-C', str(repo), *args], env=env, check=True,
                              capture_output=True, text=True, timeout=30).stdout.strip()
    run('init', '-q', '--template=')
    for name, value in {
        'user.name': 'AGIW test fixture', 'user.email': 'agiw-fixture@example.invalid',
        'user.signingkey': str(key), 'gpg.format': 'ssh',
        'gpg.ssh.program': str(keygen),
        'gpg.ssh.allowedSignersFile': str(allowed), 'core.autocrlf': 'false',
        'core.attributesFile': str(empty_config), 'core.hooksPath': str(root / 'no-hooks'),
    }.items():
        run('config', '--local', name, value)
    module = repo / 'windows/agiw_win/source.py'
    module.parent.mkdir(parents=True)
    module.write_bytes(b'VALUE = 1\n')
    if missing_module:
        module.with_name('deleted.py').write_bytes(b'DELETED = True\n')
    (repo / '.gitattributes').write_text('*.py text eol=lf\n' +
        ('windows/agiw_win/source.py filter=unsafe\n' if custom_filter else ''), encoding='utf-8')
    run('add', '--', '.gitattributes', 'windows')
    run('-c', 'commit.gpgsign=' + ('true' if signature else 'false'), 'commit', '-qm', 'throwaway fixture')
    return repo, env, run('rev-parse', 'HEAD')


def write_build(path: Path, **over):
    doc = {"schemaVersion": 1, "service": SERVICE, "version": VERSION, "commit": COMMIT, "dirty": False,
           "installedAt": "2026-10-17T19:30:00Z", "source": "install.ps1",
           "signatureVerified": True, "filesMatchHead": True}
    doc.update(over)
    path.write_text(json.dumps(doc), encoding="utf-8")


class Version(unittest.TestCase):
    def test_version_is_0_2_0_and_fits_the_worker_pattern(self):
        self.assertEqual(VERSION, "0.2.0")
        self.assertEqual(WORKER_VERSION, "win-0.2.0")
        self.assertLessEqual(len(WORKER_VERSION), 16)

    def test_the_installer_can_read_the_version_line(self):
        # install.ps1 parses VERSION with this exact pattern; a reformatted __init__.py must not break installs.
        pattern = re.search(r"\[regex\]::Match\(\$initText, '(.+?)'\)", INSTALLER).group(1)
        init = (WINDOWS / "agiw_win" / "__init__.py").read_text(encoding="utf-8")
        match = re.search(pattern.replace("(?m)", ""), init, re.MULTILINE)
        self.assertEqual(match.group(1), VERSION)


class BuildRecord(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "build.json"

    def test_recorded_commit_and_dirty_flag(self):
        write_build(self.path)
        build = buildinfo.read_build(self.path)
        self.assertEqual((build["state"], build["commit"], build["dirty"], build["installedAt"]),
                         ("recorded", COMMIT, False, "2026-10-17T19:30:00Z"))
        self.assertEqual(buildinfo.producer(build), f"agiw-win-observer 0.2.0+{COMMIT[:12]}")
        write_build(self.path, dirty=True)
        self.assertEqual(buildinfo.producer(buildinfo.read_build(self.path)), f"agiw-win-observer 0.2.0+{COMMIT[:12]}.dirty.unverified")

    def test_unknown_status_signature_or_bytes_never_looks_like_a_clean_signed_commit(self):
        for over in ({"dirty": None}, {"dirty": "false"}, {"signatureVerified": False},
                     {"signatureVerified": None}, {"filesMatchHead": False}, {"filesMatchHead": None},
                     {"signatureVerified": 1}, {"filesMatchHead": 1}):
            with self.subTest(over=over):
                write_build(self.path, **over)
                build = buildinfo.read_build(self.path)
                self.assertEqual(build["provenance"], "unverified")
                self.assertIn("unverified", build["detail"])
                self.assertEqual(buildinfo.producer(build), f"agiw-win-observer 0.2.0+{COMMIT[:12]}.unverified")
                self.assertEqual(buildinfo.build_public(build)["provenance"], "unverified")

    def test_legacy_record_has_unverified_provenance_even_if_dirty_is_false(self):
        write_build(self.path)
        doc = json.loads(self.path.read_text())
        del doc["signatureVerified"]
        del doc["filesMatchHead"]
        self.path.write_text(json.dumps(doc))
        build = buildinfo.read_build(self.path)
        self.assertEqual(build["provenance"], "unverified")
        self.assertTrue(buildinfo.producer(build).endswith(".unverified"))

    def test_producer_does_not_trust_missing_or_nonboolean_clean_status(self):
        for dirty in (None, 0, "false"):
            build = {"version": VERSION, "commit": COMMIT, "dirty": dirty, "provenance": "signed-head"}
            self.assertTrue(buildinfo.producer(build).endswith(".unverified"))
            self.assertEqual(buildinfo.build_public(build)["provenance"], "unverified")

    def test_absent_invalid_and_mismatched_records_never_claim_a_commit(self):
        self.assertEqual(buildinfo.read_build(self.path)["state"], "absent")
        cases = [("{", "invalid"), ("[]", "invalid"), (json.dumps({"schemaVersion": 1}), "invalid")]
        for text, state in cases:
            with self.subTest(text=text):
                self.path.write_text(text, encoding="utf-8")
                self.assertEqual(buildinfo.read_build(self.path)["state"], state)
        for over, state in (({"service": "other"}, "invalid"), ({"schemaVersion": True}, "invalid"),
                            ({"version": "0.1.0"}, "mismatch"), ({"version": None}, "mismatch"),
                            ({"commit": "AF39471A9E7C0D210AE4E7520C0A2436D130C52C"}, "invalid"),
                            ({"commit": "af39471"}, "invalid"), ({"commit": 7}, "invalid"),
                            ({"commit": None}, "no-commit")):
            with self.subTest(over=over):
                write_build(self.path, **over)
                build = buildinfo.read_build(self.path)
                self.assertEqual((build["state"], build["commit"], build["dirty"]), (state, None, None))
                self.assertEqual(buildinfo.producer(build), "agiw-win-observer 0.2.0")
        write_build(self.path, installedAt="yesterday", dirty="no")
        build = buildinfo.read_build(self.path)
        self.assertEqual((build["installedAt"], build["dirty"]), (None, None))

    def test_links_oversize_and_nonfinite_files_are_refused(self):
        target = self.path.with_name("real.json")
        write_build(target)
        os.symlink(target, self.path)
        self.assertEqual(buildinfo.read_build(self.path)["state"], "invalid")
        self.path.unlink()
        write_build(self.path, padding="x" * 5000)
        self.assertEqual(buildinfo.read_build(self.path)["state"], "invalid")
        self.path.write_text('{"schemaVersion": 1, "service": "agiw-win-observer", "version": "0.2.0", "x": NaN}')
        self.assertEqual(buildinfo.read_build(self.path)["state"], "invalid")

    def test_producer_and_public_identity_are_bounded(self):
        self.assertEqual(buildinfo.producer(None, "0.2.0"), "agiw-win-observer 0.2.0")
        self.assertEqual(buildinfo.producer({"version": "x" * 40}), "agiw-win-observer 0.2.0")
        self.assertEqual(buildinfo.producer({"version": "0.2.0", "commit": "../etc"}), "agiw-win-observer 0.2.0")
        self.assertEqual(buildinfo.build_public({"version": "0.2.0", "commit": COMMIT, "dirty": True}),
                         {"version": "0.2.0", "commit": COMMIT[:12], "dirty": True, "provenance": "unverified"})
        self.assertEqual(buildinfo.build_public({"commit": "nope", "dirty": True}), {"version": None, "commit": None, "dirty": None, "provenance": "unverified"})

    def test_default_path_is_the_install_root(self):
        self.assertEqual(buildinfo.default_build_path(), WINDOWS / "build.json")

    def test_published_documents_carry_the_build(self):
        build = {"version": VERSION, "commit": COMMIT, "dirty": False, "state": "recorded", "provenance": "signed-head"}
        snapshot = {"observedAt": "2026-10-17T19:30:00Z", "sampledAt": 1792265400.0, "observerVersion": VERSION,
                    "build": build, "lanes": [{"id": "fast", "expectedModel": "openai/gpt-oss-20b", "status": "idle",
                                               "phase": "idle"}]}
        status = probes.pc_status(snapshot)
        presence = probes.peer_presence(snapshot, "test-pc")
        self.assertEqual(status["producer"], f"agiw-win-observer 0.2.0+{COMMIT[:12]}")
        self.assertEqual((presence["producer"], presence["version"]), (status["producer"], "0.2.0"))
        self.assertEqual(presence["build"], {"version": "0.2.0", "commit": COMMIT[:12], "dirty": False, "provenance": "signed-head"})


class Installer(unittest.TestCase):
    """Static checks of install.ps1 that run on every OS; the Windows-only run is below."""

    def test_ships_the_peer_link_and_a_build_record(self):
        self.assertRegex(INSTALLER, r"foreach \(\$file in [^\n]*'peer_link\.py'")
        self.assertIn("'build.json'", INSTALLER)
        self.assertIn("rev-parse --verify HEAD", INSTALLER)
        self.assertIn("$sourcePaths = @($files | ForEach-Object { $_[0] })", INSTALLER)
        self.assertIn("[Globalization.CultureInfo]::InvariantCulture", INSTALLER)
        self.assertTrue((REPO / "peer_link.py").is_file())

    def test_build_json_is_staged_and_swapped_with_every_file(self):
        loop = INSTALLER[INSTALLER.index("foreach ($relative in $installed)"):INSTALLER.index("Set-Content -LiteralPath $marker")]
        self.assertIn("Join-Path $backup $relative", loop)
        self.assertNotIn("$pair", loop, "the swap loop must use its own loop variable")
        self.assertLess(INSTALLER.index("Join-Path $stage 'build.json'"), INSTALLER.index("foreach ($relative in $installed)"))

    def test_installing_starts_nothing_new(self):
        # peer_link.py is copied, never run; no firewall rule, task or service is created by the installer.
        self.assertNotRegex(INSTALLER, r"(?im)^[^#\n]*peer_link\.py['\"]?\s+run")
        self.assertNotRegex(INSTALLER, r"(?i)New-NetFirewallRule|netsh|Register-ScheduledTask|New-Service")

    def test_private_enrollment_is_inventory_data_not_a_shipped_blob(self):
        inventory = INSTALLER[INSTALLER.index("function Test-InstalledInventory("):INSTALLER.index("function Test-StagedProvenance(")]
        names = re.search(r"foreach \(\$name in (.*?)\) \{ \[void\]\$expected.Add\(\$name\) \}", inventory).group(1)
        self.assertEqual(set(re.findall(r"'([^']+)'", names)),
                         {".agiw-win-install", "build.json", "config.json", "lane-processes.json"})
        package = INSTALLER[INSTALLER.index("$files = New-Object"):INSTALLER.index("function Test-InstalledInventory(")]
        self.assertNotIn("lane-processes.json", package)


@unittest.skipUnless(shutil.which("git"), "git is needed to exercise installer source status")
class InstallerSourceStatus(unittest.TestCase):
    """Run the installer's actual git status flags against scoped, untracked source fixtures."""

    def status(self, source):
        command = re.search(r"\$git.Source -C \$repo status (.+?) 2>\$null", INSTALLER).group(1)
        arguments = [arg for arg in shlex.split(command) if arg != "$sourcePaths"]
        if "--" not in arguments:
            arguments.append("--")
        result = subprocess.run([shutil.which("git"), "-C", str(REPO), "status", *arguments, str(source)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_untracked_installed_module_marks_the_build_dirty(self):
        with tempfile.TemporaryDirectory(prefix=".agiw-build-test-", dir=REPO) as tmp:
            source = Path(tmp) / "extra_module.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            self.assertTrue(self.status(source).startswith("?? "), "an untracked copied module is not clean source")

    def test_ignored_installed_module_also_marks_the_build_dirty(self):
        with tempfile.TemporaryDirectory(prefix=".agiw-build-test-", dir=REPO) as tmp:
            (Path(tmp) / ".gitignore").write_text("ignored_module.py\n", encoding="utf-8")
            source = Path(tmp) / "ignored_module.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            self.assertTrue(self.status(source).startswith("!! "), "ignored files are still copied by the installer")


@unittest.skipUnless(os.name == "nt", "Windows installer only")
class InstallerOnWindows(unittest.TestCase):
    def test_install_writes_a_build_record_the_observer_accepts(self):
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        with install_root(self) as tmp:
            destination = Path(tmp) / "agiw-win"
            result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                     "-File", str(WINDOWS / "install.ps1"), "-Destination", str(destination)],
                                    capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((destination / "peer_link.py").is_file())
            build = buildinfo.read_build(destination / "build.json")
            self.assertIn(build["state"], ("recorded", "no-commit"))
            self.assertEqual(build["version"], VERSION)
            sources = [(path, Path("agiw_win") / path.name) for path in (WINDOWS / "agiw_win").glob("*.py")]
            sources += [(WINDOWS / name, Path(name)) for name in
                        ("agiw_observer.py", "agiw-monitor.ps1", "AGIW Monitor.vbs", "AGIW Monitor.cmd", "README.md")]
            sources += [(REPO / "web" / name, Path("web") / name) for name in
                        ("index.html", "app.js", "style.css", "map-layout.mjs", "model-control-view.mjs", "online-code-mode.mjs")]
            sources += [(REPO / name, Path(name)) for name in
                        ("usage-format.mjs", "client_models.py", "peer_link.py", "LICENSE", "NOTICE")]
            for source, relative in sources:
                with self.subTest(installed=str(relative)):
                    self.assertEqual((destination / relative).read_bytes(), source.read_bytes())
                    if build["provenance"] == "signed-head":
                        repo_path = source.relative_to(REPO).as_posix()
                        blob = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--verify",
                                               f"{build['commit']}:{repo_path}"],
                                              capture_output=True, timeout=30)
                        actual = subprocess.run(["git", "-C", str(REPO), "hash-object", f"--path={repo_path}",
                                                 "--", str(destination / relative)], capture_output=True, timeout=30)
                        self.assertEqual(blob.returncode, 0, blob.stderr)
                        self.assertEqual(actual.returncode, 0, actual.stderr)
                        self.assertEqual(actual.stdout.strip(), blob.stdout.strip())
            if build["provenance"] == "signed-head":
                verified = subprocess.run(["git", "-C", str(REPO), "verify-commit", build["commit"]],
                                          capture_output=True, timeout=30)
                self.assertEqual(verified.returncode, 0, verified.stderr)
            else:
                self.assertIn(".unverified", buildinfo.producer(build))

    def staged_provenance(self, payload=b"VALUE = 1\n", dirty="$false", signature=True,
                          missing_module=False, unexpected=None, custom_filter=False):
        # Exercise the actual verifier against a real signed temporary repo, with no host trust.
        helper = INSTALLER[INSTALLER.index("Add-Type -TypeDefinition @'"):INSTALLER.index("$Destination = $full")]
        function = INSTALLER[INSTALLER.index("function Test-InstalledInventory("):INSTALLER.index("# A commit identifies")]
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        with install_root(self) as tmp:
            root = Path(tmp)
            repo, env, commit = signed_fixture_repo(root, signature, missing_module, custom_filter)
            (root / "stage" / "agiw_win").mkdir(parents=True)
            (root / "stage" / "agiw_win" / "source.py").write_bytes(payload)
            (root / "install").mkdir()
            if unexpected:
                stale = root / "install" / unexpected
                stale.parent.mkdir(parents=True, exist_ok=True)
                stale.write_bytes(b"stale executable")
            quoted = str(root).replace("'", "''")
            repo_quoted = str(repo).replace("'", "''")
            command = helper + function + f"""
$ErrorActionPreference = 'Stop'
$realGit = (Get-Command git.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
$files = New-Object System.Collections.Generic.List[object]
$files.Add(@('{repo_quoted}\\windows\\agiw_win\\source.py', 'agiw_win\\source.py'))
$guard = New-Object AgiwInstallGuard
try {{
    Test-StagedProvenance $files '{quoted}\\stage' '{repo_quoted}' '{commit}' {dirty} $realGit '{quoted}\\install' $guard | ConvertTo-Json -Compress
}} finally {{ $guard.Dispose() }}
"""
            result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-Command", command],
                                    env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

    def assert_unsigned_rejected(self, **options):
        unsigned = self.staged_provenance(signature=False, **options)
        self.assertIs(unsigned['signatureVerified'], False)
        self.assertIs(unsigned['filesMatchHead'], True)
        self.assertEqual(unsigned['provenance'], 'unverified')

    def test_staged_bytes_are_checked_against_head_even_when_size_is_unchanged(self):
        verified = self.staged_provenance()
        self.assertEqual(verified["provenance"], "signed-head")
        tampered = self.staged_provenance(payload=b"VALUE = 2\n")
        self.assertIs(tampered["filesMatchHead"], False)
        self.assertEqual(tampered["provenance"], "unverified")
        self.assert_unsigned_rejected()

    def test_crlf_staged_payload_uses_declared_git_text_normalization(self):
        build = self.staged_provenance(payload=b"VALUE = 1\r\n")
        self.assertIs(build["filesMatchHead"], True)
        self.assertEqual(build["provenance"], "signed-head")
        self.assert_unsigned_rejected(payload=b'VALUE = 1\r\n')

    def test_custom_clean_filter_is_not_run_or_trusted(self):
        build = self.staged_provenance(custom_filter=True)
        self.assertIs(build["filesMatchHead"], False)
        self.assertEqual(build["provenance"], "unverified")

    def test_unknown_status_or_failed_signature_cannot_claim_signed_head(self):
        for dirty, signature in (("$null", True), ("$false", False)):
            with self.subTest(dirty=dirty, signature=signature):
                self.assertEqual(self.staged_provenance(dirty=dirty, signature=signature)["provenance"], "unverified")

    def test_deleted_tracked_module_is_not_a_complete_signed_build(self):
        build = self.staged_provenance(missing_module=True)
        self.assertIs(build["filesMatchHead"], False)
        self.assertEqual(build["provenance"], "unverified")

    def test_stale_python_and_extension_modules_make_installed_inventory_unverified(self):
        for relative in ("agiw_win/stale.py", "agiw_win/stale.pyc", "agiw_win/probes.cp314-win_amd64.pyd",
                         "json/__init__.py", "lane-processes.py", "agiw_win/lane-processes.json",
                         "lane-processes.json/extra.py"):
            with self.subTest(relative=relative):
                build = self.staged_provenance(unexpected=relative)
                self.assertIs(build["filesMatchHead"], False)
                self.assertEqual(build["provenance"], "unverified")

    def test_private_enrollment_does_not_invalidate_a_signed_package(self):
        build = self.staged_provenance(unexpected="lane-processes.json")
        self.assertIs(build["filesMatchHead"], True)
        self.assertEqual(build["provenance"], "signed-head")
        tampered = self.staged_provenance(payload=b"VALUE = 2\n", unexpected="lane-processes.json")
        self.assertIs(tampered["filesMatchHead"], False)
        self.assertEqual(tampered["provenance"], "unverified")
        self.assert_unsigned_rejected(unexpected='lane-processes.json')


class PeerLinkFeed(unittest.TestCase):
    def snapshot(self):
        return {"observedAt": "2026-10-17T19:30:00Z", "sampledAt": 1792265400.0, "observerVersion": VERSION,
                "dashboardPort": 8767, "build": {"version": VERSION, "commit": COMMIT},
                "lanes": [{"id": "fast", "expectedModel": "openai/gpt-oss-20b", "status": "idle", "phase": "idle",
                           "slotsBusy": 0, "slotsTotal": 2, "servedModel": r"C:\models\gpt-oss-20b.gguf"}],
                "components": [{"id": "nisi", "label": "Nisi gate", "state": "ready", "detail": "x", "authorModel": "q"},
                               {"id": "jev", "label": "Jev", "state": "unknown", "detail": "y"},
                               {"id": "pair", "label": "NVIDIA PAIR", "state": "running", "listeners": []}]}

    def test_the_feed_passes_the_pairing_services_own_validation(self):
        import_path = sys.path[:]
        try:
            sys.path.append(str(REPO))
            import peer_link  # noqa: PLC0415
        finally:
            sys.path[:] = import_path
        doc = probes.peer_link_status_doc(self.snapshot(), "test-pc")
        self.assertEqual([c["id"] for c in doc["components"]], ["nisi", "jev"])
        peer_link.validate_status(doc, "windows")
        projected = peer_link.project_status(doc)
        self.assertEqual((projected["kind"], projected["platform"], projected["producer"]),
                         ("agiw-peer", "windows", f"agiw-win-observer 0.2.0+{COMMIT[:12]}.unverified"))
        self.assertEqual([c["id"] for c in projected["components"]], ["nisi", "jev"])
        self.assertNotIn("models\\\\", json.dumps(projected))

    def test_sample_writes_the_private_file_and_reports_it(self):
        from types import SimpleNamespace
        from copy import deepcopy
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "state" / probes.PEER_LINK_STATUS_FILE_NAME
            snapshot = self.snapshot()
            collector = SimpleNamespace(share_root=Path(tmp), host="test-pc", collect=lambda: deepcopy(snapshot))
            stop = mock.Mock()
            stop.is_set.side_effect = [False, False, True]
            store = server.SnapshotStore()
            # Two samples: the first writes the file, the second reports it (well inside the 10 s refresh).
            # Python 3.9 on macOS starts monotonic near zero; this fixture must not depend on process uptime.
            with mock.patch.object(server.time, "monotonic", return_value=100.0):
                server.sample(store, collector, stop, interval=0.0, peer_link_status_to=target)
            written = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual((written["kind"], written["host"], written["platform"]), ("agiw-peer", "test-pc", "windows"))
            if os.name != "nt":
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertFalse((Path(tmp) / "llm-lab").exists(), "the feed never goes to the share")
            feed = next(s for s in store.read()["sources"] if s["id"] == "peer-link-feed")
            self.assertEqual(feed["state"], "live")

    def test_a_failed_write_is_reported_and_keeps_sampling(self):
        outcome = {"ok": None, "error": None}
        with mock.patch.object(probes, "write_json_atomic", side_effect=PermissionError(13, "denied")), \
                mock.patch("sys.stderr"):
            server.write_peer_link_status(Path("/nonexistent/state/x.json"), self.snapshot(), "test-pc", outcome)
        self.assertEqual(outcome["error"], "PermissionError 13")
        source = server.peer_link_feed_source(outcome, 5.0)
        self.assertEqual(source["state"], "unavailable")
        self.assertIn("failed (PermissionError 13)", source["detail"])
        self.assertEqual(server.peer_link_feed_source({"ok": None, "error": None}, 5.0)["state"], "unavailable")

    def test_only_a_literal_true_turns_an_option_on(self):
        for value, expected in ((True, True), (False, False), ("true", False), (1, False), (None, False)):
            with self.subTest(value=value):
                options = server.observer_options({"peerLinkStatus": value, "publishStatus": value})
                self.assertEqual((options["peerLinkStatus"], options["publishStatus"]), (expected, expected))
        self.assertEqual(server.observer_options([], publish_status=True, peer_link=True),
                         {"publishStatus": True, "peerLink": True, "peerLinkReader": False, "peerLinkStatus": False})

    def test_main_passes_the_private_feed_path_to_the_sampler(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"peerLinkStatus": True}), encoding="utf-8")
            started = []
            fake_server = mock.Mock(server_address=("127.0.0.1", 8767))
            fake_server.serve_forever.side_effect = lambda **_: None

            class Thread:
                def __init__(self, target=None, args=(), kwargs=None, **_):
                    self.target, self.kwargs = target, kwargs or {}

                def start(self):
                    started.append((self.target, self.kwargs))
            with mock.patch.object(server, "Collector") as collector_cls, \
                    mock.patch.object(server, "bind", return_value=fake_server), \
                    mock.patch.object(server, "find_web_root", return_value=Path(tmp)), \
                    mock.patch.object(server.threading, "Thread", Thread), \
                    mock.patch.object(server.signal, "signal"), mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                collector_cls.return_value.build = {"state": "absent"}
                collector_cls.return_value.host = "test-pc"
                self.assertEqual(server.main(["--config", str(config)]), 0)
        sampler = next(kwargs for target, kwargs in started if target is server.sample)
        self.assertEqual(sampler["peer_link_status_to"],
                         Path(server.__file__).resolve().parent.parent / "state" / probes.PEER_LINK_STATUS_FILE_NAME)
        self.assertIsNone(sampler["publish_to"])

    def test_no_lan_is_still_accepted_and_ignored(self):
        with mock.patch.object(server, "Collector") as collector_cls, mock.patch("sys.stdout"):
            collector_cls.return_value.collect.return_value = {"host": "windows"}
            self.assertEqual(server.main(["--once", "--no-lan", "--config", "/nonexistent.json"]), 0)
        self.assertNotIn("mac_peer", collector_cls.call_args.kwargs)


class Discovery(unittest.TestCase):
    def run_python(self, code):
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run([sys.executable, "-B", "-c", code], cwd=REPO, capture_output=True, text=True,
                              timeout=120, env=env)

    def test_the_suite_is_a_package_reachable_from_the_repository_root(self):
        self.assertTrue((WINDOWS / "tests" / "__init__.py").is_file())
        result = self.run_python(
            "import unittest; s = unittest.defaultTestLoader.discover('windows/tests', pattern='test_winsys.py', "
            "top_level_dir='windows'); print(s.countTestCases(), sorted({t.id().split('.')[0] for g in s for c in g for t in c}))")
        self.assertEqual(result.returncode, 0, result.stderr)
        count, roots = result.stdout.split(" ", 1)
        self.assertGreater(int(count), 0)
        self.assertEqual(roots.strip(), "['tests']", "modules are qualified, so they cannot clash with root tests")

    def test_root_discovery_still_does_not_descend_into_windows(self):
        result = self.run_python(
            "import unittest; s = unittest.defaultTestLoader.discover('.', pattern='test_winsys.py', top_level_dir='.');"
            " print(s.countTestCases())")
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "0"), result.stderr)


if __name__ == "__main__":
    unittest.main()
