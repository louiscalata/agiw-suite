"""Worker LaunchAgent pins node/git and a minimal PATH; never writes a token value."""
import os
import plistlib
import tempfile
import unittest
from pathlib import Path

from agiw import worker_service as ws

ARGS = ['--worker-id', 'monterey-mac-pro', '--bind', '127.0.0.1', '--port', '18791',
        '--token-file', '/secure/worker.token', '--model-endpoint', 'http://127.0.0.1:1234/v1/chat/completions',
        '--model', 'm', '--disable-thinking']
TOOLS = {'node': '/Users/x/.local/bin/node', 'git': '/usr/bin/git'}


class WorkerServiceTests(unittest.TestCase):
    def render(self, args=ARGS, env=None, which=TOOLS.get):
        return ws.render_plist(args, home='/Users/x', python='/usr/bin/python3', env=env or {}, which=which,
                               repo_root='/repo')

    def test_plist_pins_tools_and_minimal_path(self):
        worker, data = self.render()
        plist = plistlib.loads(data)
        envs = plist['EnvironmentVariables']
        self.assertEqual(worker, 'monterey-mac-pro')
        self.assertEqual(envs['AGIW_NODE'], '/Users/x/.local/bin/node')
        self.assertEqual(envs['AGIW_GIT'], '/usr/bin/git')
        self.assertEqual(envs['PATH'].split(':')[:2], ['/Users/x/.local/bin', '/usr/bin'])
        self.assertIn('/bin', envs['PATH'].split(':'))
        self.assertEqual(plist['ProgramArguments'][:4], ['/usr/bin/python3', '-B', '-m', 'agiw.windows_worker'])
        self.assertEqual(plist['ProgramArguments'][4:], ARGS)
        self.assertEqual(plist['WorkingDirectory'], '/repo')
        self.assertEqual(plist['Label'], ws.LABEL_PREFIX + 'monterey-mac-pro')
        self.assertTrue(ws.LABEL_PREFIX.endswith('.agiw.fleet-worker.'))

    def test_env_overrides_win(self):
        _, data = self.render(env={'AGIW_NODE': '/opt/node/bin/node', 'AGIW_GIT': '/opt/git/bin/git'})
        envs = plistlib.loads(data)['EnvironmentVariables']
        self.assertEqual((envs['AGIW_NODE'], envs['AGIW_GIT']), ('/opt/node/bin/node', '/opt/git/bin/git'))

    def test_missing_tools_fail_closed(self):
        from unittest import mock
        with mock.patch.object(ws.pilot, 'NODE_FALLBACK_DIRS', ()):
            with self.assertRaises(ws.ServiceError) as cm:
                self.render(which=lambda t: None if t == 'node' else '/usr/bin/git')
        self.assertEqual(str(cm.exception), 'NODE_NOT_FOUND')
        with self.assertRaises(ws.ServiceError) as cm:
            self.render(which=lambda t: TOOLS['node'] if t == 'node' else None)
        self.assertEqual(str(cm.exception), 'GIT_NOT_FOUND')

    def test_token_value_unknown_worker_and_missing_token_file_refused(self):
        for args, code in ((ARGS + ['--token', 'abc'], 'TOKEN_VALUE_REFUSED_USE_TOKEN_FILE'),
                           ([a if a != 'monterey-mac-pro' else 'evil-box' for a in ARGS], 'UNKNOWN_WORKER_ID'),
                           ([a for a in ARGS if a not in ('--token-file', '/secure/worker.token')], 'TOKEN_FILE_REQUIRED')):
            with self.subTest(code=code), self.assertRaises(ws.ServiceError) as cm:
                self.render(args=args)
            self.assertEqual(str(cm.exception), code)

    def test_install_writes_plist_and_only_prints_launchctl(self):
        calls = []
        with tempfile.TemporaryDirectory() as home:
            target, command = ws.install(ARGS, home=home, runner=lambda *a, **k: calls.append(a),
                                         python='/usr/bin/python3', env={}, which=TOOLS.get, repo_root='/repo')
            self.assertTrue(target.is_file())
            self.assertEqual(target.parent, Path(home) / 'Library' / 'LaunchAgents')
            self.assertEqual(oct(target.stat().st_mode & 0o777), '0o644')
            self.assertNotIn(b'abc', target.read_bytes())
        self.assertEqual(calls, [])
        self.assertEqual(command[:2], ['launchctl', 'bootstrap'])


if __name__ == '__main__':
    unittest.main()
