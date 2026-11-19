// Copyright 2026 Louis Calata
// SPDX-License-Identifier: Apache-2.0
// Host-held anchor for a run journal (review item R1; docs/design/journal-anchor.md).
// The anchor is a small file the host keeps outside the journal's directory. It names
// the exact bytes of the journal the host last wrote, its entry count, a chain over
// the payload-free form of each entry and a generation counter. The anchor adds no
// journal format or reopen changes; it writes journal bytes through the store.
// It does not stop an attacker who can also write the anchor, and it authenticates
// no writer. Results carry reason codes, counts and hashes only: no payload, no path.
import { createHash, randomUUID } from 'node:crypto';
import { basename, dirname, isAbsolute, parse, relative, resolve, sep } from 'node:path';
import { reopen } from './run-journal-v1.mjs';
import { readSerializedJournal, writeSerializedJournal } from './run-journal-store-v1.mjs';

export const ANCHOR_SCHEMA = 'nisi-journal-anchor-v1';
export const MAX_ANCHOR_BYTES = 4096;
export const RENAME_ATTEMPTS = 5;
export const RENAME_RETRY_DELAYS_MS = Object.freeze([10, 20, 40, 80]);

const JOURNAL_SCHEMA = 'nisi-run-journal-v1';
const ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const HEX = /^[0-9a-f]{64}$/;
const ZERO = '0'.repeat(64);
const MAX_RECORDS = 10000;
const MAX_KEY_LENGTH = 1024;
const STATE_KEYS = ['count', 'fileSha256', 'generation', 'journalSchema', 'lastHash', 'projectId', 'stemTip'];
const SHARING_CODES = new Set(['EPERM', 'EBUSY', 'EACCES']);
const POLICIES = new Set(['require', 'unanchored-ok']);
const FS_METHODS = ['lstatSync', 'openSync', 'fstatSync', 'readSync', 'writeSync', 'closeSync', 'readFileSync', 'renameSync', 'unlinkSync'];
// Windows needs a write-access directory handle for its flush; POSIX directories reject it.
const directoryFlags = sep === '\\' ? 'r+' : 'r';

const digest = bytes => createHash('sha256').update(bytes).digest('hex');
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const exact = (value, keys) => object(value) && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
const canonical = value => Array.isArray(value) ? '[' + value.map(canonical).join(',') + ']'
  : object(value) ? '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}'
    : JSON.stringify(value);
const hasMethods = (fs, names) => fs != null && names.every(name => typeof fs[name] === 'function');
const validPath = path => typeof path === 'string' && path.length > 0 && !path.includes('\0');
const validKey = key => typeof key === 'string' && key.length > 0 && key.length <= MAX_KEY_LENGTH && key.isWellFormed();
const generationOf = value => Number.isSafeInteger(value) && value >= 1;
const codeError = code => Object.assign(new Error(code), { code });
const codeOf = error => typeof error?.code === 'string' && /^[A-Z0-9_]{1,32}$/.test(error.code) ? error.code : 'IO_ERROR';

// The anchor file name is the hash of the host's key, never the key or a path.
export function anchorFileName(key) {
  if (!validKey(key)) throw codeError('INVALID_INPUT');
  return digest(Buffer.from(key, 'utf8')) + '.anchor.json';
}

// Stem chain over the payload-free form of each entry (design, section 3). The journal
// must be COMPLETE under reopen. `chain[i]` is S_i, so `chain[count]` is the tip.
export function describeJournal(serialized) {
  if (typeof serialized !== 'string' || !serialized.isWellFormed() || !serialized.endsWith('\n')) throw codeError('INVALID_JOURNAL');
  if (reopen(serialized).report.status !== 'COMPLETE') throw codeError('JOURNAL_NOT_COMPLETE');
  const lines = serialized.slice(0, -1).split('\n');
  const header = JSON.parse(lines[0]);
  const footer = JSON.parse(lines.at(-1));
  const projectId = header.config.projectId;
  const seed = digest('nisi-journal-anchor/seed/v1\n' + canonical({ projectId }));
  const stems = [];
  const chain = [seed];
  const rows = [];
  for (let index = 1; index < lines.length - 1; index++) {
    const record = JSON.parse(lines[index]).record;
    const stem = digest('nisi-journal-anchor/stem/v1\n' + canonical({ ...record.entry, payload: null }));
    stems.push(stem);
    chain.push(digest('nisi-journal-anchor/step/v1\n' + chain.at(-1) + '\n' + stem));
    rows.push({ retained: record.retained, fingerprint: record.fingerprint });
  }
  return {
    projectId, count: stems.length, lastHash: footer.lastHash, seed, stems, chain, rows,
    stemTip: chain.at(-1), fileSha256: digest(Buffer.from(serialized, 'utf8')),
  };
}

const stateOf = (description, generation) => ({
  count: description.count, fileSha256: description.fileSha256, generation, journalSchema: JOURNAL_SCHEMA,
  lastHash: description.lastHash, projectId: description.projectId, stemTip: description.stemTip,
});
// The state before the first journal exists. No file has this digest.
const genesisOf = projectId => ({
  count: 0, fileSha256: ZERO, generation: 1, journalSchema: JOURNAL_SCHEMA, lastHash: ZERO, projectId,
  stemTip: digest('nisi-journal-anchor/seed/v1\n' + canonical({ projectId })),
});
const isGenesis = state => state.count === 0 && state.fileSha256 === ZERO;
const encode = (current, pending) => Buffer.from(canonical({ current, pending, schema: ANCHOR_SCHEMA }) + '\n', 'utf8');

function validState(state) {
  return exact(state, STATE_KEYS)
    && Number.isSafeInteger(state.count) && state.count >= 0 && state.count <= MAX_RECORDS
    && typeof state.fileSha256 === 'string' && typeof state.lastHash === 'string' && typeof state.stemTip === 'string'
    && HEX.test(state.fileSha256) && HEX.test(state.lastHash) && HEX.test(state.stemTip)
    && generationOf(state.generation) && state.journalSchema === JOURNAL_SCHEMA
    && typeof state.projectId === 'string' && ID.test(state.projectId);
}

function decode(bytes) {
  const text = bytes.toString('utf8');
  if (!Buffer.from(text, 'utf8').equals(bytes)) return null;
  let value;
  try { value = JSON.parse(text.slice(0, -1)); } catch { return null; }
  if (canonical(value) + '\n' !== text) return null;
  if (!exact(value, ['current', 'pending', 'schema']) || value.schema !== ANCHOR_SCHEMA || !validState(value.current)) return null;
  if (value.pending !== null) {
    if (!validState(value.pending) || value.pending.projectId !== value.current.projectId
      || value.pending.generation !== value.current.generation + 1) return null;
  }
  return { current: value.current, pending: value.pending };
}

// One descriptor, fstat size checked, never more than max + 1 bytes read, links and
// non-regular files refused. This reader handles only the capped anchor file;
// the store additionally caps each request and refuses journal shrinkage.
function boundedRead(fs, path, maxBytes) {
  let stat;
  try { stat = fs.lstatSync(path); } catch (error) { return { error: error?.code === 'ENOENT' ? 'NOT_FOUND' : 'READ_FAILED' }; }
  if (!stat || typeof stat.isSymbolicLink !== 'function' || typeof stat.isFile !== 'function') return { error: 'READ_FAILED' };
  if (stat.isSymbolicLink()) return { error: 'SYMLINK' };
  if (!stat.isFile()) return { error: 'NOT_A_FILE' };
  const constants = fs.constants;
  const noFollow = constants?.O_NOFOLLOW;
  let fd;
  try { fd = fs.openSync(path, noFollow === undefined ? 'r' : constants.O_RDONLY | noFollow | (constants.O_NONBLOCK || 0)); }
  catch (error) { return { error: error?.code === 'ELOOP' ? 'SYMLINK' : error?.code === 'ENOENT' ? 'NOT_FOUND' : 'READ_FAILED' }; }
  try {
    const opened = fs.fstatSync(fd);
    if (!opened || typeof opened.isFile !== 'function' || !Number.isSafeInteger(opened.size)) return { error: 'READ_FAILED' };
    if (!opened.isFile()) return { error: 'NOT_A_FILE' };
    if (opened.size > maxBytes) return { error: 'FILE_TOO_LARGE' };
    const buffer = Buffer.alloc(opened.size + 1);
    let length = 0;
    while (length < buffer.length) {
      const count = fs.readSync(fd, buffer, length, buffer.length - length, null);
      if (count === 0) break;
      length += count;
    }
    if (length > opened.size) return { error: 'FILE_CHANGED' };
    return { bytes: buffer.subarray(0, length) };
  } catch { return { error: 'READ_FAILED' }; }
  finally { try { fs.closeSync(fd); } catch { /* the read result stands */ } }
}

function readAnchor(fs, path) {
  const read = boundedRead(fs, path, MAX_ANCHOR_BYTES);
  if (read.error === 'NOT_FOUND') return { found: false };
  if (read.error) return { invalid: read.error };
  const anchor = decode(read.bytes);
  return anchor ? { found: true, anchor, bytes: read.bytes } : { invalid: 'ANCHOR_FORMAT' };
}

const sleepMs = ms => Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);

// Temp file in the anchor directory, fsync, rename, directory sync. `guard` runs just
// before the rename and says whether the anchor is still what the caller saw. The
// rename is retried a few times on a sharing violation (EPERM, EBUSY, EACCES), which
// Windows reports while a scanner holds the target open.
function writeAnchorFile(fs, path, bytes, guard, sleep) {
  const temp = path + '.' + randomUUID() + '.tmp';
  const result = { committed: false, fileSynced: false, directorySynced: false };
  let fd = null;
  let owned = false;
  const cleanup = () => {
    if (fd !== null) { const closing = fd; fd = null; try { fs.closeSync(closing); } catch { /* temp is removed next */ } }
    if (owned) { owned = false; try { fs.unlinkSync(temp); } catch { /* an orphan temp is harmless */ } }
  };
  const fail = reason => { cleanup(); return { ...result, reason }; };
  try {
    fd = fs.openSync(temp, 'wx', 0o600);
    owned = true;
    let offset = 0;
    while (offset < bytes.length) {
      const count = fs.writeSync(fd, bytes, offset, bytes.length - offset, null);
      if (!Number.isSafeInteger(count) || count <= 0 || count > bytes.length - offset) return fail('ANCHOR_WRITE_FAILED');
      offset += count;
    }
    try { fs.fsyncSync(fd); result.fileSynced = true; } catch { /* durable stays false */ }
    const closing = fd;
    fd = null;
    fs.closeSync(closing);
    const back = fs.readFileSync(temp);
    if (!Buffer.isBuffer(back) || !back.equals(bytes)) return fail('ANCHOR_WRITE_FAILED');
  } catch { return fail('ANCHOR_WRITE_FAILED'); }
  try {
    if (!guard()) return fail('CONFLICT');
    try {
      if (fs.lstatSync(path).isSymbolicLink()) return fail('ANCHOR_WRITE_FAILED');
    } catch (error) { if (error?.code !== 'ENOENT') return fail('ANCHOR_WRITE_FAILED'); }
    for (let attempt = 1; ; attempt++) {
      try { fs.renameSync(temp, path); break; }
      catch (error) {
        if (attempt >= RENAME_ATTEMPTS || !SHARING_CODES.has(error?.code)) return fail('ANCHOR_WRITE_FAILED');
        sleep(RENAME_RETRY_DELAYS_MS[attempt - 1]);
      }
    }
  } catch { return fail('ANCHOR_WRITE_FAILED'); }
  owned = false;
  result.committed = true;
  let directoryFd = null;
  try {
    directoryFd = fs.openSync(dirname(path), directoryFlags);
    fs.fsyncSync(directoryFd);
    result.directorySynced = true;
  } catch { /* durable stays false */ }
  finally {
    if (directoryFd !== null) { try { fs.closeSync(directoryFd); } catch { result.directorySynced = false; } }
  }
  return { ...result, durable: result.fileSynced && result.directorySynced };
}

// All traversal is bounded and uses only the caller's filesystem. Resolve a link
// before consuming the next component: neither Windows normalization nor realpath
// given a whole link/.. spelling preserves the physical parent in every case.
const MAX_DIRECTORY_LINKS = 40;
const MAX_DIRECTORY_COMPONENTS = 4096;
const appendComponent = (parent, part) => parent.endsWith(sep) ? parent + part : parent + sep + part;
const componentsOf = path => path.split(sep === '\\' ? /[\\/]/ : /\//);
const unresolvedPath = () => ({ problem: 'ANCHOR_PATH_UNRESOLVED' });

function resolveExisting(fs, path) {
  // Drive-relative Windows names depend on per-drive process state. Do not guess.
  if (!isAbsolute(path) && parse(path).root !== '') return unresolvedPath();
  const absolute = isAbsolute(path) ? path : appendComponent(resolve('.'), path);
  const root = parse(absolute).root;
  let probe = sep === '\\' ? resolve(root) : root;
  let parts = componentsOf(absolute.slice(root.length));
  let index = 0;
  let links = 0;
  let steps = 0;
  let missing = false;
  let canonical = true;
  let useRealpath = typeof fs.realpathSync === 'function';
  while (index < parts.length) {
    const part = parts[index++];
    if (part === '' || part === '.') continue;
    if (++steps > MAX_DIRECTORY_COMPONENTS) return unresolvedPath();
    // Some virtual aliases (volfs) have missing spelled ancestors but a usable
    // complete directory name. Observe that directory before accepting its '..';
    // a genuinely missing/.. path must never be lexically rescued.
    if (missing && part === '..') {
      let parent;
      try { parent = fs.lstatSync(probe, { bigint: true }); } catch { return unresolvedPath(); }
      if (!parent || typeof parent.isDirectory !== 'function' || !parent.isDirectory()
        || typeof parent.isSymbolicLink !== 'function' || parent.isSymbolicLink()) return unresolvedPath();
      missing = false;
      canonical = false;
    }
    const next = appendComponent(probe, part);
    if (useRealpath) {
      try {
        const resolved = fs.realpathSync(next);
        if (validPath(resolved) && isAbsolute(resolved)) {
          probe = resolved;
          canonical = true;
          continue;
        }
      } catch { /* try the supplied component/link capabilities */ }
      // A later non-native realpath could collapse a physical alias/.. spelling.
      useRealpath = false;
    }
    let stat;
    try { stat = fs.lstatSync(next, { bigint: true }); }
    catch (error) {
      if (error?.code !== 'ENOENT') return unresolvedPath();
      missing = true;
      probe = next;
      continue;
    }
    if (!stat || typeof stat.isSymbolicLink !== 'function' || typeof stat.isDirectory !== 'function') return unresolvedPath();
    const parentMissing = missing;
    missing = false;
    if (stat.isSymbolicLink()) {
      if (++links > MAX_DIRECTORY_LINKS || typeof fs.readlinkSync !== 'function') return unresolvedPath();
      let target;
      try { target = fs.readlinkSync(next); } catch { return unresolvedPath(); }
      if (!validPath(target)) return unresolvedPath();
      if (isAbsolute(target)) {
        const targetRoot = parse(target).root;
        // A rooted Windows link without a drive uses the link's current volume.
        probe = sep === '\\' && /^[\\/]$/.test(targetRoot) ? parse(probe).root : targetRoot;
        parts = componentsOf(target.slice(targetRoot.length)).concat(parts.slice(index));
      } else {
        if (parentMissing || parse(target).root !== '') return unresolvedPath();
        parts = componentsOf(target).concat(parts.slice(index));
      }
      index = 0;
      continue;
    }
    if (!stat.isDirectory()) return unresolvedPath();
    canonical = false;
    // On POSIX, literal /.. retains the physical parent of non-symlink aliases
    // such as volfs. Windows can use string parents only after links are expanded.
    probe = part === '..' && sep === '\\' ? dirname(probe) : next;
  }
  return { path: probe, canonical };
}
// Exact device/inode identities only; imprecise or missing metadata proves nothing.
function identityOf(fs, path) {
  try {
    const stat = fs.lstatSync(path, { bigint: true });
    if (!stat || typeof stat.isDirectory !== 'function' || !stat.isDirectory()) return null;
    for (const value of [stat.dev, stat.ino]) {
      if (typeof value !== 'bigint' && !(typeof value === 'number' && Number.isSafeInteger(value))) return null;
      if (value < 0) return null;
    }
    const ino = String(stat.ino);
    return ino === '0' || stat.ino === undefined || stat.dev === undefined ? null : String(stat.dev) + ':' + ino;
  } catch { return null; }
}
function insideJournalDirectory(fs, journalPath, anchorDir, canonical) {
  const journalDir = dirname(journalPath);
  const target = anchorDir;
  const relation = relative(journalDir, target);
  // '' is the same directory; a path that leaves it, or is on another drive, is outside.
  if (relation !== '..' && !relation.startsWith('..' + sep) && !isAbsolute(relation)) return true;
  const identity = identityOf(fs, journalDir);
  if (identity === null) return canonical ? false : null;
  const rootIdentity = identityOf(fs, parse(target).root);
  // POSIX physical parents also catch volfs aliases. Windows paths have already
  // had every directory link expanded, so their string parents are now usable.
  const seen = new Set();
  for (let probe = target, steps = 0; steps < MAX_DIRECTORY_COMPONENTS; steps++) {
    const current = identityOf(fs, probe);
    if (current === identity) return true;
    if (current === null) {
      if (!canonical) {
        // Only an ordinary missing suffix may be skipped without an identity.
        try { fs.lstatSync(probe, { bigint: true }); return null; }
        catch (error) { if (error?.code !== 'ENOENT') return null; }
      }
      const parent = dirname(probe);
      if (parent === probe) return canonical ? false : null;
      probe = parent;
    } else {
      if (seen.has(current)) return canonical || current === rootIdentity ? false : null;
      seen.add(current);
      if (sep === '\\') {
        const parent = dirname(probe);
        if (parent === probe) return canonical || current === rootIdentity ? false : null;
        probe = parent;
      } else {
        probe += sep + '..';
      }
    }
  }
  return null;
}

// A trailing separator names a directory. POSIX refuses a journal path such as
// 'journal.jsonl/' that is not a directory (ENOTDIR). Node on Windows strips the
// separator before the call (path.toNamespacedPath), so the regular file would be
// read or replaced. On Windows the journal calls see the same refusal instead.
const notDirectory = () => Object.assign(new Error('not a directory'), { code: 'ENOTDIR' });
function directoryPathFs(fs, journalPath) {
  const writeBits = ['O_CREAT', 'O_WRONLY', 'O_RDWR'].reduce((bits, name) => bits | (fs.constants?.[name] | 0), 0);
  const creates = flags => typeof flags === 'string' ? /[wa+]/.test(flags) : typeof flags === 'number' && (flags & writeBits) !== 0;
  const refuses = (name, args) => {
    let stat;
    try { stat = fs.lstatSync(journalPath); } catch (error) {
      // A missing path is reported by the call itself, unless the call would create it.
      if (error?.code !== 'ENOENT') return false;
      return name === 'renameSync' || (name === 'openSync' && creates(args[1]));
    }
    return !(stat && typeof stat.isDirectory === 'function' && stat.isDirectory());
  };
  const view = {};
  const names = new Set([...FS_METHODS, 'constants', 'fsyncSync', 'statSync', 'realpathSync', 'readlinkSync']);
  for (const name in fs) names.add(name);
  for (const name of names) {
    if (!(name in fs)) continue;
    const value = fs[name];
    view[name] = typeof value !== 'function' ? value : (...args) => {
      if ((args[0] === journalPath || (name === 'renameSync' && args[1] === journalPath)) && refuses(name, args)) throw notDirectory();
      return value.apply(fs, args);
    };
  }
  return view;
}

// Shared argument checks. Returns { problem } or { context }.
function contextOf(options) {
  if (!object(options)) return { problem: 'INVALID_INPUT' };
  const { journalPath, anchorDir, key, fs, policy, maxBytes, expectations, expectedGeneration, sleep } = options;
  if (!validPath(journalPath) || !validPath(anchorDir) || !validKey(key) || !hasMethods(fs, FS_METHODS)) return { problem: 'INVALID_INPUT' };
  if (policy !== undefined && !POLICIES.has(policy)) return { problem: 'INVALID_INPUT' };
  if (maxBytes !== undefined && !(typeof maxBytes === 'number' && maxBytes >= 0)) return { problem: 'INVALID_INPUT' };
  if (expectedGeneration !== undefined && !generationOf(expectedGeneration)) return { problem: 'INVALID_INPUT' };
  if (sleep !== undefined && typeof sleep !== 'function') return { problem: 'INVALID_INPUT' };
  if (expectations !== undefined) {
    try { reopen('\n', expectations); } catch { return { problem: 'INVALID_INPUT' }; }
  }
  const trailingSeparator = journalPath.endsWith(sep) || (sep === '\\' && journalPath.endsWith('/')) ? sep : '';
  const journalDirectory = resolveExisting(fs, dirname(journalPath));
  const anchorDirectory = resolveExisting(fs, anchorDir);
  if (journalDirectory.problem || anchorDirectory.problem) return unresolvedPath();
  const resolvedJournalPath = appendComponent(journalDirectory.path, basename(journalPath)) + trailingSeparator;
  const resolvedAnchorDir = anchorDirectory.path;
  const inside = insideJournalDirectory(fs, resolvedJournalPath, resolvedAnchorDir, journalDirectory.canonical && anchorDirectory.canonical);
  if (inside === true) return { problem: 'ANCHOR_INSIDE_JOURNAL_DIR' };
  if (inside === null) return unresolvedPath();
  return {
    context: {
      fs: trailingSeparator === '\\' ? directoryPathFs(fs, resolvedJournalPath) : fs,
      journalPath: resolvedJournalPath, expectations, expectedGeneration, maxBytes,
      anchorPath: resolvedAnchorDir + sep + anchorFileName(key), policy: policy ?? 'require', sleep: sleep ?? sleepMs,
    },
  };
}

// The journal at the path, read through the store and checked against the host's
// expectations. Returns { problem: cause } or { serialized, description }.
function readJournal(context) {
  const read = readSerializedJournal({ path: context.journalPath, fs: context.fs, ...(context.maxBytes === undefined ? {} : { maxBytes: context.maxBytes }) });
  if (read.status !== 'READ') return { problem: read.reason };
  if (context.expectations !== undefined) {
    const report = reopen(read.serialized, context.expectations).report;
    if (report.status !== 'COMPLETE') return { problem: report.reason };
  }
  try { return { serialized: read.serialized, description: describeJournal(read.serialized) }; }
  catch { return { problem: 'INVALID_JOURNAL' }; }
}

// Section 4, steps 5 and 6 of the verification algorithm.
function judge(anchor, description) {
  const current = anchor.current;
  if (description.projectId !== current.projectId) return 'ANCHOR_PROJECT';
  if (description.fileSha256 === current.fileSha256) return 'OK';
  if (anchor.pending !== null && description.fileSha256 === anchor.pending.fileSha256) return 'OK_PENDING';
  if (description.count < current.count) return 'ROLLED_BACK';
  if (description.chain[current.count] === current.stemTip) return description.count === current.count ? 'CONTENT_DIFFERS' : 'AHEAD';
  return 'DIVERGED';
}

const verdictOf = (status, anchor, description) => ({
  status, ok: status === 'OK' || status === 'OK_PENDING',
  generation: anchor.current.generation, anchorCount: anchor.current.count,
  journalCount: description.count, sha256: description.fileSha256,
});

function verifyInternal(context) {
  const found = readAnchor(context.fs, context.anchorPath);
  if (found.invalid) return { status: 'ANCHOR_INVALID', ok: false, cause: found.invalid };
  if (!found.found) return { status: context.policy === 'require' ? 'ANCHOR_MISSING' : 'UNANCHORED', ok: false };
  const journal = readJournal(context);
  if (journal.problem) return { status: 'JOURNAL_NOT_COMPLETE', ok: false, cause: journal.problem };
  return verdictOf(judge(found.anchor, journal.description), found.anchor, journal.description);
}

// Never throws: an error from the caller's fs is reported by code only.
const guarded = (run, failure) => options => {
  try { return run(options); } catch { return { ...failure }; }
};

export const verifyAnchoredJournal = guarded(options => {
  const { problem, context } = contextOf(options);
  if (problem) return { status: problem, ok: false };
  return verifyInternal(context);
}, { status: 'REFUSED', ok: false, reason: 'IO_ERROR' });

// Continuity of the new journal over the anchored state (design, decision 5): same
// project, no fewer entries, the same stem chain over the anchored entries, and the
// same retained fingerprints; a retired row stays retired.
function extension(current, previous, next) {
  // The seed of the stem chain binds the project, so a journal of another project fails the tip check.
  if (next.count < current.count) return false;
  if (next.chain[current.count] !== current.stemTip) return false;
  if (previous === null) return true;
  for (let index = 0; index < current.count; index++) {
    const before = previous.rows[index];
    const after = next.rows[index];
    if (!before.retained && after.retained) return false;
    if (before.retained && after.retained && before.fingerprint !== after.fingerprint) return false;
  }
  return true;
}

const refused = (reason, extra = {}) => ({ status: 'REFUSED', reason, ...extra });

export const writeAnchoredJournal = guarded(options => {
  const { problem, context } = contextOf(options);
  if (problem) return refused(problem);
  const { fs, anchorPath, sleep } = context;
  const { serialized } = options;
  let next;
  try { next = describeJournal(serialized); } catch (error) { return refused('JOURNAL_NOT_COMPLETE', { cause: codeOf(error) }); }
  if (context.expectations !== undefined) {
    const report = reopen(serialized, context.expectations).report;
    if (report.status !== 'COMPLETE') return refused('JOURNAL_NOT_COMPLETE', { cause: report.reason });
  }

  // Step 1: the anchor and the journal it describes must agree.
  const found = readAnchor(fs, anchorPath);
  if (found.invalid) return refused('ANCHOR_INVALID', { cause: found.invalid });
  let anchor;
  let previous = null;
  let onDisk;
  let durable = true;
  if (!found.found) {
    if (context.expectedGeneration !== undefined) return refused('CONFLICT');
    try { fs.lstatSync(context.journalPath); return refused('ANCHOR_MISSING'); }
    catch (error) { if (error?.code !== 'ENOENT') return refused('JOURNAL_NOT_COMPLETE', { cause: 'READ_FAILED' }); }
    anchor = { current: genesisOf(next.projectId), pending: null };
    onDisk = null;
  } else {
    anchor = found.anchor;
    onDisk = found.bytes;
    if (context.expectedGeneration !== undefined && context.expectedGeneration !== anchor.current.generation) return refused('CONFLICT');
    const journal = readJournal(context);
    if (journal.problem) {
      if (!(journal.problem === 'NOT_FOUND' && isGenesis(anchor.current))) return refused('JOURNAL_NOT_COMPLETE', { cause: journal.problem });
    } else {
      const status = judge(anchor, journal.description);
      if (status !== 'OK' && status !== 'OK_PENDING') return refused(status);
      previous = journal.description;
      // The generation after this write must stay a safe integer: refuse before anything changes.
      const top = status === 'OK_PENDING' ? anchor.pending : anchor.current;
      if (next.fileSha256 !== top.fileSha256 && !generationOf(top.generation + 1)) return refused('GENERATION_EXHAUSTED');
      if (status === 'OK_PENDING') {
        const promoted = encode(anchor.pending, null);
        const guard = () => { const again = readAnchor(fs, anchorPath); return again.found === true && again.bytes.equals(onDisk); };
        const wrote = writeAnchorFile(fs, anchorPath, promoted, guard, sleep);
        if (!wrote.committed) return refused(wrote.reason);
        durable = durable && wrote.durable;
        anchor = { current: anchor.pending, pending: null };
        onDisk = promoted;
      }
    }
  }
  const current = anchor.current;
  if (previous !== null && next.fileSha256 === current.fileSha256) {
    return { status: 'UNCHANGED', committed: false, durable, generation: current.generation, sha256: next.fileSha256, bytes: Buffer.byteLength(serialized, 'utf8') };
  }

  // Step 3: the continuity check.
  if (!extension(current, previous, next)) return refused('NOT_EXTENSION');

  // Step 4: record the intended state before the journal changes.
  if (!generationOf(current.generation + 1)) return refused('GENERATION_EXHAUSTED');
  const target = stateOf(next, current.generation + 1);
  const unchangedOnDisk = () => {
    const again = readAnchor(fs, anchorPath);
    return onDisk === null ? again.found === false && !again.invalid : again.found === true && again.bytes.equals(onDisk);
  };
  const pendingBytes = encode(current, target);
  const first = writeAnchorFile(fs, anchorPath, pendingBytes, unchangedOnDisk, sleep);
  if (!first.committed) return refused(first.reason);
  durable = durable && first.durable;
  onDisk = pendingBytes;
  const unchangedSince = () => { const again = readAnchor(fs, anchorPath); return again.found === true && again.bytes.equals(onDisk); };

  // Step 5: the journal, through the store, with the digest the anchor holds.
  const stored = writeSerializedJournal({
    path: context.journalPath, serialized, fs,
    ...(previous === null ? {} : { expectedPreviousSha256: current.fileSha256 }),
  });
  if (stored.status !== 'WRITTEN' && stored.status !== 'UNCHANGED') {
    if (stored.committed === true) return refused(stored.reason, { committed: true, durable: false });
    // Nothing changed on disk: the pending slot is cleared.
    const cleared = writeAnchorFile(fs, anchorPath, encode(current, null), unchangedSince, sleep);
    return refused(stored.reason, { committed: false, durable: false, cleared: cleared.committed });
  }

  // Step 6: promote.
  const promoted = writeAnchorFile(fs, anchorPath, encode(target, null), unchangedSince, sleep);
  if (!promoted.committed) return refused('ANCHOR_WRITE_FAILED', { committed: true, durable: false });
  return {
    status: stored.status, committed: true, durable: durable && stored.durable && promoted.durable,
    generation: target.generation, sha256: next.fileSha256, bytes: stored.bytes,
  };
}, refused('IO_ERROR'));

// Trust on first use: records whatever complete journal is on disk at this moment.
export const adoptJournal = guarded(options => {
  const { problem, context } = contextOf(options);
  if (problem) return refused(problem);
  const { fs, anchorPath, sleep } = context;
  const found = readAnchor(fs, anchorPath);
  if (found.found || found.invalid) return refused('ANCHOR_EXISTS');
  const journal = readJournal(context);
  if (journal.problem) return refused('JOURNAL_NOT_COMPLETE', { cause: journal.problem });
  const state = stateOf(journal.description, 1);
  const wrote = writeAnchorFile(fs, anchorPath, encode(state, null), () => readAnchor(fs, anchorPath).found === false, sleep);
  if (!wrote.committed) return refused(wrote.reason);
  return { status: 'ADOPTED', durable: wrote.durable, generation: 1, count: state.count, sha256: state.fileSha256 };
}, refused('IO_ERROR'));

// The explicit decision for a journal that is AHEAD of its anchor. Nothing else may
// be re-anchored, and the host decides: this helper never runs by itself.
export const reanchorJournal = guarded(options => {
  const { problem, context } = contextOf(options);
  if (problem) return refused(problem);
  const { fs, anchorPath, sleep } = context;
  const found = readAnchor(fs, anchorPath);
  if (found.invalid) return refused('ANCHOR_INVALID', { cause: found.invalid });
  if (!found.found) return refused('ANCHOR_MISSING');
  if (context.expectedGeneration !== undefined && context.expectedGeneration !== found.anchor.current.generation) return refused('CONFLICT');
  const journal = readJournal(context);
  if (journal.problem) return refused('JOURNAL_NOT_COMPLETE', { cause: journal.problem });
  const status = judge(found.anchor, journal.description);
  if (status !== 'AHEAD') return refused('NOT_AHEAD', { observed: status });
  if (!generationOf(found.anchor.current.generation + 1)) return refused('GENERATION_EXHAUSTED');
  const state = stateOf(journal.description, found.anchor.current.generation + 1);
  const wrote = writeAnchorFile(fs, anchorPath, encode(state, null), () => { const again = readAnchor(fs, anchorPath); return again.found === true && again.bytes.equals(found.bytes); }, sleep);
  if (!wrote.committed) return refused(wrote.reason);
  return {
    status: 'REANCHORED', durable: wrote.durable, generation: state.generation, previousGeneration: found.anchor.current.generation,
    count: state.count, previousCount: found.anchor.current.count, sha256: state.fileSha256,
  };
}, refused('IO_ERROR'));
