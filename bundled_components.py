"""Inspect AGIW's pinned public Nisi payload and run its explicit offline check.

This is independent of the owner's externally installed Nisi/Jev router.
Neither app startup nor a status read runs a workflow or loads a model.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading

from bundle_nisi import verify_payload


ROOT = Path(__file__).resolve().parent
NODE_PATHS = (Path('/opt/homebrew/bin/node'), Path('/usr/local/bin/node'))


def payload_path():
    bundled = ROOT / 'nisi'
    return bundled if bundled.exists() else ROOT / 'vendor' / 'nisi'


def node_path():
    return next((path for path in NODE_PATHS if path.is_file()
                 and os.access(path, os.X_OK)), None)


class BundledComponents:
    def __init__(self):
        self.lock = threading.Lock()
        self.check = {'state': 'not-run', 'message': 'The bundled Nisi self-check has not run.'}

    def status(self):
        try:
            verify_payload(payload_path())
            integrity = 'verified'
        except (OSError, ValueError, KeyError, TypeError):
            integrity = 'unavailable'
        node = node_path()
        with self.lock:
            check = dict(self.check)
        checked_node = check.get('nodePath')
        if check['state'] == 'passed' and checked_node in {str(path) for path in NODE_PATHS}:
            candidate = Path(checked_node)
            if candidate.is_file() and os.access(candidate, os.X_OK):
                node = candidate
        return {
            'nisi': {'version': '0.2.0', 'integrity': integrity,
                     'license': 'Apache-2.0', 'includedByDefault': integrity == 'verified',
                     'entry': str(payload_path() / 'package' / 'bin' / 'nisi.mjs'),
                     'source': 'https://github.com/louiscalata/nisi/releases/tag/v0.2.0'},
            'node': {'found': node is not None, 'path': str(node) if node else None,
                     'requirement': 'Node.js 22 or newer (Apple silicon)'},
            'check': check,
        }

    def request_check(self):
        with self.lock:
            if self.check['state'] == 'running':
                return dict(self.check)
            self.check = {'state': 'running', 'message': 'Checking the bundled Nisi workflow. No model call.'}
        try:
            threading.Thread(target=self._run_check, daemon=True, name='nisi-bundle-check').start()
        except RuntimeError:
            with self.lock:
                self.check = {'state': 'failed', 'message': 'The Nisi self-check could not start. Try again.'}
                return dict(self.check)
        return {'state': 'running', 'message': 'Checking the bundled Nisi workflow. No model call.'}

    def _run_check(self):
        result = {'state': 'failed', 'message': 'Nisi self-check failed; the external router was not changed.'}
        try:
            verify_payload(payload_path())
            node = node_path()
            if node is None:
                result = {'state': 'needs-node', 'message': 'Install Node.js 22 or newer for Apple silicon, then run this check again.'}
                return
            # Exclude NODE_OPTIONS, NODE_PATH, credentials and owner router settings.
            # Commands and paths are fixed; user input never becomes executable code.
            with tempfile.TemporaryDirectory(prefix='agiw-nisi-check-') as scratch:
                env = {'PATH': '/usr/bin:/bin', 'HOME': scratch, 'TMPDIR': scratch,
                       'LANG': 'en_US.UTF-8'}
                version = None
                for candidate in NODE_PATHS:
                    if not candidate.is_file() or not os.access(candidate, os.X_OK):
                        continue
                    try:
                        probe = subprocess.run(
                            [str(candidate), '-p', 'JSON.stringify({version:process.versions.node,arch:process.arch})'],
                            env=env, cwd=scratch, capture_output=True, text=True, timeout=5, check=True)
                        runtime = json.loads(probe.stdout)
                        found = runtime.get('version', '')
                        if (isinstance(found, str) and re.fullmatch(r'\d+\.\d+\.\d+', found)
                                and int(found.split('.')[0]) >= 22 and runtime.get('arch') == 'arm64'):
                            node, version = candidate, found
                            break
                    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
                        continue
                if version is None:
                    result = {'state': 'needs-node', 'message': 'Nisi requires Node.js 22 or newer running natively on Apple silicon.'}
                    return
                completed = subprocess.run(
                    [str(node), str(payload_path() / 'package' / 'bin' / 'nisi.mjs'), 'demo'],
                    env=env, cwd=scratch, capture_output=True, text=True, timeout=20, check=True)
                report = json.loads(completed.stdout)
                if (report.get('outcome') != 'COMPLETED' or report.get('reportStored') is not True
                        or type(report.get('modelCalls')) is not int or report['modelCalls'] != 0):
                    raise ValueError('Unexpected deterministic demo result')
                result = {'state': 'passed', 'message': 'Nisi 0.2.0 passed its fixed workflow self-check. No model was called.',
                          'nodeVersion': version, 'nodePath': str(node), 'modelCalls': 0}
        except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
            pass
        finally:
            with self.lock:
                self.check = result
