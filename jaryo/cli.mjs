#!/usr/bin/env node
/** Jaryo kit command. Install copies this folder. Demo serves the pipeline page. */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { checkKit } from './contract.mjs';
import { installKit, isDirectRun, kitRoot, startServer } from './install.mjs';
import { serveMcp } from './mcp.mjs';

export function parseArgs(argv) {
  const [command = 'help', ...rest] = argv;
  const opts = { help: false };
  for (let i = 0; i < rest.length; i += 1) {
    const token = rest[i];
    switch (token) {
      case '--dest':
        opts.dest = rest[i + 1];
        i += 1;
        break;
      case '--port':
        opts.port = rest[i + 1];
        i += 1;
        break;
      case '--help':
      case '-h':
        opts.help = true;
        break;
      default: {
        const unknown = token;
        throw new Error(`Unknown argument: ${unknown}`);
      }
    }
  }
  return { command, opts };
}

export function helpText() {
  return `Jaryo kit

  node jaryo/cli.mjs install --dest <directory>
  node <directory>/cli.mjs demo --port 8731
  node jaryo/cli.mjs check
  node jaryo/cli.mjs tokens
  node jaryo/cli.mjs mcp

install copies the kit. demo serves http://127.0.0.1:<port>/ which redirects to the pipeline page.
check reads the kit files. tokens prints tokens.json. mcp speaks the stdio tool protocol.`;
}

export async function run(argv, io = {}) {
  const stdout = io.stdout || ((line) => console.log(line));
  const stderr = io.stderr || ((line) => console.error(line));
  let parsed;
  try { parsed = parseArgs(argv); }
  catch (error) {
    stderr(error.message);
    stderr(helpText());
    return 2;
  }
  const { command, opts } = parsed;
  if (opts.help || command === 'help') {
    stdout(helpText());
    return 0;
  }
  switch (command) {
    case 'install': {
      try {
        const dest = installKit(opts.dest);
        stdout(`Installed Jaryo kit in ${dest}`);
        stdout(`Run: node ${dest}/cli.mjs demo --port 8731`);
        return 0;
      } catch (error) {
        stderr(error.message);
        return 2;
      }
    }
    case 'demo': {
      const port = opts.port === undefined ? 8731 : Number(opts.port);
      if (!Number.isInteger(port) || port < 0 || port > 65535) {
        stderr('demo needs --port <0-65535>');
        return 2;
      }
      const running = await startServer(port, kitRoot());
      stdout(`Jaryo demo at http://127.0.0.1:${running.port}/`);
      stdout(`Pipeline page http://127.0.0.1:${running.port}/demo/index.html`);
      await new Promise((resolve) => {
        const stop = () => running.server.close(() => resolve());
        process.once('SIGINT', stop);
        process.once('SIGTERM', stop);
      });
      return 0;
    }
    case 'check': {
      const problems = checkKit(kitRoot());
      if (problems.length === 0) {
        stdout('ok');
        return 0;
      }
      problems.forEach((problem) => stderr(problem));
      return 1;
    }
    case 'tokens': {
      stdout(readFileSync(join(kitRoot(), 'tokens.json'), 'utf8').trimEnd());
      return 0;
    }
    case 'mcp': {
      await serveMcp();
      return 0;
    }
    default: {
      const unknown = command;
      stderr(`Unknown command: ${unknown}`);
      stderr(helpText());
      return 2;
    }
  }
}

if (isDirectRun(import.meta.url)) {
  run(process.argv.slice(2)).then((code) => {
    if (code !== 0) process.exitCode = code;
  });
}
