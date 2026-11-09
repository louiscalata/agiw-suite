/** Copy the kit to a clean directory and serve it. The source of a copy is that copy. */
import { createServer } from 'node:http';
import { mkdirSync, readFileSync, realpathSync, statSync, cpSync } from 'node:fs';
import { dirname, extname, join, resolve, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));

export const KIT_FILES = [
  'tokens.json',
  'theme.mjs',
  'primitives.css',
  'components.css',
  'layout.css',
  'motion.css',
  'behavior.mjs',
  'boot.js',
  'cli.mjs',
  'mcp.mjs',
  'install.mjs',
  'contract.mjs',
  'README.md',
  'demo/index.html',
  'demo/pipeline.json',
  'demo/demo.css',
  'demo/smoke.mjs',
  'marks/d1-stamp.svg',
  'marks/d2-ledger.svg',
  'marks/d3-schematic.svg',
  'marks/d4-split.svg',
  'marks/d4-split-before.svg',
  'marks/d4-split-wordmark.svg',
  'marks/d4-split-on-dark.svg',
  'marks/d4-split-on-light.svg',
  'marks/d5-signal.svg',
  'marks/favicon.svg',
];

const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.md': 'text/markdown; charset=utf-8',
};

export function kitRoot() {
  return here;
}

/** True when this module is the program Node was asked to run, after symlinks. macOS temp dirs are symlinks. */
export function isDirectRun(moduleUrl) {
  const entry = process.argv[1];
  if (!entry || !moduleUrl) return false;
  try {
    return realpathSync(fileURLToPath(moduleUrl)) === realpathSync(entry);
  } catch {
    try { return pathToFileURL(entry).href === moduleUrl; }
    catch { return false; }
  }
}

export function assertInstallDest(source, dest) {
  if (typeof dest !== 'string' || dest.trim() === '') throw new Error('install needs --dest <directory>');
  const target = resolve(dest);
  const from = resolve(source);
  if (target === from || target.startsWith(from + sep)) {
    throw new Error(`Refusing to install into the kit source: ${target}`);
  }
  return target;
}

export function installKit(dest, source = here) {
  const target = assertInstallDest(source, dest);
  mkdirSync(target, { recursive: true });
  for (const rel of KIT_FILES) {
    const from = join(source, rel);
    const to = join(target, rel);
    mkdirSync(dirname(to), { recursive: true });
    cpSync(from, to);
  }
  return target;
}

function fileForRequest(root, pathname) {
  const rel = decodeURIComponent(pathname).replace(/^\/+/, '');
  if (rel.includes('\0') || rel.split('/').includes('..')) return null;
  const file = resolve(root, rel);
  if (file !== root && !file.startsWith(root + sep)) return null;
  return file;
}

export function createKitServer(root = here) {
  return createServer((req, res) => {
    const url = new URL(req.url || '/', 'http://127.0.0.1');
    if (url.pathname === '/') {
      res.writeHead(302, { Location: '/demo/index.html' });
      res.end();
      return;
    }
    const file = fileForRequest(root, url.pathname);
    if (!file) {
      res.writeHead(403);
      res.end('Forbidden');
      return;
    }
    let body;
    try {
      if (!statSync(file).isFile()) throw new Error('not a file');
      body = readFileSync(file);
    } catch (_) {
      res.writeHead(404);
      res.end('Not found');
      return;
    }
    res.writeHead(200, {
      'Content-Type': TYPES[extname(file)] || 'application/octet-stream',
      'Cache-Control': 'no-store',
    });
    res.end(body);
  });
}

export function startServer(port = 0, root = here) {
  const server = createKitServer(root);
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, '127.0.0.1', () => {
      const address = server.address();
      resolve({ server, port: address.port, root });
    });
  });
}
