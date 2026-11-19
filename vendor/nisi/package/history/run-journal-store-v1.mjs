// Copyright 2026 Louis Calata
// SPDX-License-Identifier: Apache-2.0
import { createHash, randomUUID } from 'node:crypto';
import { dirname, sep } from 'node:path';
import { reopen } from './run-journal-v1.mjs';

const digest = bytes => createHash('sha256').update(bytes).digest('hex');
const hex = value => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const exact = (value, keys) => object(value) && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
const canonical = value => Array.isArray(value) ? '[' + value.map(canonical).join(',') + ']'
  : object(value) ? '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}'
    : JSON.stringify(value);
const errorCode = error => typeof error?.code === 'string' && error.code ? error.code : 'IO_ERROR';
const validPath = path => typeof path === 'string' && path.length > 0 && !path.includes('\0');
const hasMethods = (fs, names) => fs != null && names.every(name => typeof fs[name] === 'function');
const initial = () => ({ durable: false, committed: false, fsync: { file: false, directory: false }, fsyncErrors: { file: null, directory: null } });
const refusal = (state, reason) => ({ ...state, status: 'REFUSED', reason, durable: false });
// Windows needs a write-access directory handle for its flush; POSIX directories
// reject a read/write open. A failed sync still reports durable: false.
const directoryFlags = sep === '\\' ? 'r+' : 'r';

// null means the bytes are a complete journal. Any other string is a refusal reason.
// A legacy pruned fingerprint is still a refusal: the store does not return READ.
function journalProblem(serialized) {
  try {
    if (typeof serialized !== 'string' || !serialized.isWellFormed() || !serialized.endsWith('\n')) return 'INVALID_JOURNAL';
    const lines = serialized.slice(0, -1).split('\n');
    if (lines.length < 2 || lines.some(line => !line)) return 'INVALID_JOURNAL';
    const values = lines.map(line => {
      const value = JSON.parse(line);
      if (canonical(value) !== line) throw new Error('NONCANONICAL');
      return value;
    });
    const header = values[0];
    if (!exact(header, ['type', 'schema', 'config', 'now']) || header.type !== 'header' || header.schema !== 'nisi-run-journal-v1'
      || !object(header.config) || !Number.isSafeInteger(header.now) || header.now < 0) return 'INVALID_JOURNAL';
    let previousHash = digest('nisi-run-journal/header/v1\n' + lines[0]);
    for (let index = 1; index < values.length - 1; index++) {
      const value = values[index];
      if (!exact(value, ['type', 'seq', 'previousHash', 'record', 'hash']) || value.type !== 'entry'
        || value.seq !== index || value.previousHash !== previousHash || !hex(value.hash)
        || !exact(value.record, ['entry', 'fingerprint', 'retained']) || !object(value.record.entry)
        || !hex(value.record.fingerprint) || typeof value.record.retained !== 'boolean') return 'INVALID_JOURNAL';
      const expected = digest('nisi-run-journal/record/v1\n' + canonical({ seq: value.seq, previousHash: value.previousHash, record: value.record }));
      if (value.hash !== expected) return 'INVALID_JOURNAL';
      previousHash = value.hash;
    }
    const footer = values.at(-1);
    if (!(exact(footer, ['type', 'count', 'lastHash']) && footer.type === 'footer'
      && footer.count === values.length - 2 && footer.lastHash === previousHash)) return 'INVALID_JOURNAL';
    const opened = reopen(serialized);
    if (opened.report.status === 'COMPLETE') return null;
    return opened.report.reason === 'LEGACY_UNRETAINED_FINGERPRINT' ? 'LEGACY_UNRETAINED_FINGERPRINT' : 'INVALID_JOURNAL';
  } catch {
    return 'INVALID_JOURNAL';
  }
}

function decode(bytes) {
  if (!Buffer.isBuffer(bytes)) return { problem: 'INVALID_JOURNAL' };
  try {
    const serialized = bytes.toString('utf8');
    if (!Buffer.from(serialized, 'utf8').equals(bytes)) return { problem: 'INVALID_JOURNAL' };
    const problem = journalProblem(serialized);
    return problem ? { problem } : { serialized };
  } catch { return { problem: 'READ_FAILED' }; }
}

function existingBytes(fs, path) {
  try {
    const bytes = fs.readFileSync(path);
    return { bytes, absent: false };
  } catch (error) {
    return error?.code === 'ENOENT' ? { bytes: null, absent: true } : { error: 'READ_FAILED' };
  }
}

function syncFile(fs, fd, state, kind) {
  try {
    fs.fsyncSync(fd);
    state.fsync[kind] = true;
  } catch (error) {
    state.fsyncErrors[kind] = errorCode(error);
  }
}

// The read limit. A larger file is refused with FILE_TOO_LARGE; one of exactly
// this many bytes is read. Callers can pass options.maxBytes to change it.
export const MAX_READ_BYTES = 64 * 1024 * 1024;
const validLimit = value => typeof value === 'number' && value >= 0;

// The lstat size is only a first check: the file can grow or be replaced
// before it is opened. Read from one descriptor, check its fstat size, and
// never read more than maxBytes + 1 bytes, so the limit bounds what is read.
// O_NOFOLLOW refuses a link swapped in after the lstat; O_NONBLOCK keeps the
// open from waiting on a FIFO swapped in after it, and the fstat check then
// refuses it. Neither has an effect on a regular file. Descriptor requests
// are capped at 1 GiB; FILE_CHANGED covers growth and shrinkage relative to
// the initial fstat size.
function boundedRead(fs, path, maxBytes) {
  if (typeof fs.lstatSync === 'function') {
    let stat;
    try { stat = fs.lstatSync(path); }
    catch (error) { return { error: error?.code === 'ENOENT' ? 'NOT_FOUND' : 'READ_FAILED' }; }
    if (!stat || typeof stat.isSymbolicLink !== 'function' || typeof stat.isFile !== 'function') return { error: 'READ_FAILED' };
    if (stat.isSymbolicLink()) return { error: 'SYMLINK' };
    if (!stat.isFile()) return { error: 'NOT_A_FILE' };
  }
  const constants = fs.constants;
  const noFollow = constants?.O_NOFOLLOW;
  let fd;
  try { fd = fs.openSync(path, noFollow === undefined ? 'r' : constants.O_RDONLY | noFollow | (constants.O_NONBLOCK || 0)); }
  catch (error) {
    return { error: error?.code === 'ELOOP' ? 'SYMLINK' : error?.code === 'ENOENT' ? 'NOT_FOUND' : 'READ_FAILED' };
  }
  try {
    const stat = fs.fstatSync(fd);
    if (!stat || typeof stat.isFile !== 'function' || !Number.isSafeInteger(stat.size)) return { error: 'READ_FAILED' };
    if (!stat.isFile()) return { error: 'NOT_A_FILE' };
    if (stat.size > maxBytes) return { error: 'FILE_TOO_LARGE', bytes: stat.size };
    // One byte past the fstat size shows growth; early EOF shows shrinkage.
    const buffer = Buffer.alloc(stat.size + 1);
    let length = 0;
    while (length < buffer.length) {
      // Node coerces the requested length to a signed 32-bit integer.
      const count = fs.readSync(fd, buffer, length, Math.min(buffer.length - length, 2 ** 30), null);
      if (count === 0) break;
      length += count;
    }
    if (length !== stat.size) return { error: 'FILE_CHANGED' };
    return { bytes: buffer.subarray(0, length) };
  } catch { return { error: 'READ_FAILED' }; }
  finally { try { fs.closeSync(fd); } catch { /* the read result stands */ } }
}

// With openSync, fstatSync, readSync and closeSync the read is bounded and
// refuses links and non-regular files. A caller-supplied fs with only
// readFileSync reads as before, and checks the bytes it returns only when
// options.maxBytes is given.
export function readSerializedJournal(options) {
  const state = initial();
  if (!options || !validPath(options.path) || !hasMethods(options.fs, ['readFileSync'])) return refusal(state, 'INVALID_INPUT');
  if (options.maxBytes !== undefined && !validLimit(options.maxBytes)) return refusal(state, 'INVALID_INPUT');
  let bytes;
  if (hasMethods(options.fs, ['openSync', 'fstatSync', 'readSync', 'closeSync'])) {
    const bounded = boundedRead(options.fs, options.path, options.maxBytes ?? MAX_READ_BYTES);
    if (bounded.error) return { ...refusal(state, bounded.error), ...(bounded.bytes === undefined ? {} : { bytes: bounded.bytes }) };
    bytes = bounded.bytes;
  } else {
    const found = existingBytes(options.fs, options.path);
    if (found.error) return refusal(state, found.error);
    if (found.absent) return refusal(state, 'NOT_FOUND');
    if (options.maxBytes !== undefined && Buffer.isBuffer(found.bytes) && found.bytes.length > options.maxBytes) return { ...refusal(state, 'FILE_TOO_LARGE'), bytes: found.bytes.length };
    bytes = found.bytes;
  }
  const decoded = decode(bytes);
  if (decoded.problem) return refusal(state, decoded.problem);
  return { ...state, status: 'READ', serialized: decoded.serialized, sha256: digest(bytes), bytes: bytes.length, verified: true };
}

// readFileSync follows a symlink but renameSync replaces it. On a symlinked
// destination the previous-hash check would read the target while the write
// replaced the link, leaving the target unchanged and reporting success.
function symlinkDestination(fs, path) {
  try {
    const stat = fs.lstatSync(path);
    if (!stat || typeof stat.isSymbolicLink !== 'function') return { error: 'READ_FAILED' };
    return { symlink: stat.isSymbolicLink() };
  } catch (error) {
    return error?.code === 'ENOENT' ? { symlink: false } : { error: 'READ_FAILED' };
  }
}

export function writeSerializedJournal(options) {
  const state = initial();
  if (!options || !validPath(options.path) || !hasMethods(options.fs, ['lstatSync', 'readFileSync', 'openSync', 'writeSync', 'closeSync', 'renameSync', 'unlinkSync'])) return refusal(state, 'INVALID_INPUT');
  const { path, serialized, fs, expectedPreviousSha256 } = options;
  const problem = journalProblem(serialized);
  if (problem) return refusal(state, problem);
  if (expectedPreviousSha256 !== undefined && !hex(expectedPreviousSha256)) return refusal(state, 'INVALID_INPUT');
  const destination = symlinkDestination(fs, path);
  if (destination.error) return refusal(state, destination.error);
  if (destination.symlink) return refusal(state, 'SYMLINK');
  const bytes = Buffer.from(serialized, 'utf8');
  const sha256 = digest(bytes);
  const before = existingBytes(fs, path);
  if (before.error) return refusal(state, before.error);
  if (!before.absent && decode(before.bytes).problem) return refusal(state, 'EXISTING_INVALID');
  if (expectedPreviousSha256 !== undefined && (before.absent || digest(before.bytes) !== expectedPreviousSha256)) return refusal(state, 'CONFLICT');
  if (!before.absent) {
    if (before.bytes.equals(bytes)) return { ...state, status: 'UNCHANGED', sha256, bytes: bytes.length, verified: true };
    if (expectedPreviousSha256 === undefined) return refusal(state, 'CONFLICT');
  }

  // Each attempt owns a distinct temp. A process killed before cleanup may leave
  // its temp behind; the next attempt must not be blocked by that orphan.
  const temp = path + '.' + sha256 + '.' + randomUUID() + '.tmp';
  let fd = null;
  let ownedTemp = false;
  const cleanup = () => {
    if (fd !== null) {
      const closing = fd;
      fd = null;
      try { fs.closeSync(closing); } catch (error) { state.cleanupError = errorCode(error); }
    }
    if (ownedTemp) {
      ownedTemp = false;
      try { fs.unlinkSync(temp); } catch (error) { state.cleanupError = errorCode(error); }
    }
  };
  const fail = reason => {
    cleanup();
    return refusal(state, reason);
  };
  try {
    fd = fs.openSync(temp, 'wx', 0o600);
    ownedTemp = true;
    let offset = 0;
    while (offset < bytes.length) {
      const count = fs.writeSync(fd, bytes, offset, bytes.length - offset, null);
      if (!Number.isSafeInteger(count) || count <= 0 || count > bytes.length - offset) return fail('WRITE_FAILED');
      offset += count;
    }
    syncFile(fs, fd, state, 'file');
    const closing = fd;
    fd = null;
    try { fs.closeSync(closing); } catch (error) {
      state.cleanupError = errorCode(error);
      return fail('WRITE_FAILED');
    }
  } catch {
    return fail('WRITE_FAILED');
  }

  const matches = target => {
    try {
      const actual = fs.readFileSync(target);
      return Buffer.isBuffer(actual) && actual.equals(bytes);
    } catch { return false; }
  };
  if (!matches(temp)) return fail('READBACK_MISMATCH');
  const current = existingBytes(fs, path);
  if (current.error) return fail(current.error);
  if (current.absent !== before.absent || !current.absent && (!Buffer.isBuffer(current.bytes) || !current.bytes.equals(before.bytes))) return fail('CONFLICT');
  const recheck = symlinkDestination(fs, path);
  if (recheck.error) return fail(recheck.error);
  if (recheck.symlink) return fail('SYMLINK');
  try { fs.renameSync(temp, path); } catch { return fail('RENAME_FAILED'); }
  state.committed = true;
  ownedTemp = false;

  let directoryFd = null;
  try {
    directoryFd = fs.openSync(dirname(path), directoryFlags);
    syncFile(fs, directoryFd, state, 'directory');
  } catch (error) {
    state.fsyncErrors.directory = errorCode(error);
  } finally {
    if (directoryFd !== null) {
      const closing = directoryFd;
      directoryFd = null;
      try { fs.closeSync(closing); } catch (error) {
        state.fsync.directory = false;
        state.fsyncErrors.directory = [state.fsyncErrors.directory, errorCode(error)].filter(code => code !== null).join(';');
        state.cleanupError = errorCode(error);
      }
    }
  }
  if (!matches(path)) return refusal(state, 'READBACK_MISMATCH');
  return { ...state, status: 'WRITTEN', sha256, bytes: bytes.length, verified: true, durable: state.fsync.file && state.fsync.directory };
}
