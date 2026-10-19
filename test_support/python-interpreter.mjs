import {spawnSync} from 'node:child_process';

// PYTHON is an executable path (including paths with spaces), never a shell command.
export function resolvePython(t, {env = process.env, platform = process.platform, probe = spawnSync} = {}) {
  const candidates = [
    ...(env.PYTHON ? [{command: env.PYTHON, args: []}] : []),
    {command: 'python3.14', args: []},
    {command: 'python3', args: []},
    ...(platform === 'win32' ? [{command: 'py', args: ['-3']}] : []),
  ];
  for (const candidate of candidates) {
    const result = probe(candidate.command, [...candidate.args, '-c',
      'import sys; sys.exit(0 if sys.version_info.major == 3 else 1)'],
    {env, encoding: 'utf8', timeout: 10000, windowsHide: true});
    if (!result.error && result.status === 0) return candidate;
  }
  t.skip('No Python 3 interpreter available: tried PYTHON, python3.14, python3' +
    (platform === 'win32' ? ', and py -3' : ''));
  return null;
}
