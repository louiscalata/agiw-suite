// Copyright 2026 Louis Calata
// SPDX-License-Identifier: Apache-2.0

// The consent boundary: what may reach the on-device model, and on what terms.
//
// A grant is an explicit, canonical, digest-bound declaration of exactly what
// may be sent, from where, how much, for how long, and what may come back. Every
// content item is checked against it individually. Revocation is immediate and
// terminal. Absence of a grant is refusal, never a default.
//
// What a grant proves, and what it does not: this record proves that a written
// declaration was presented and enforced by this process. It is NOT captured
// user-interface consent — a local process cannot witness an OS-issued user
// action — and `consentClass` records which of the two it is, so no reader can
// conflate them.
//
// The destination is the on-device Apple model. SystemLanguageModel is the local
// model; PrivateCloudComputeLanguageModel is a separate class this path never
// constructs, so a grant here authorises no network egress of content.
import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { canonicalizeJSONV1 } from '../canonical/canonical-json-v1.mjs';

// Every answer from this module carries these four, pinned false: nothing here
// authorizes, executes, promotes or certifies anything, and a reader can check.
const FLAGS = Object.freeze({ authorizing: false, modelExecuted: false,
  promotionGranted: false, certificationGranted: false });
const answer = (ok, code, fields = {}) => Object.freeze({ ...fields, ok, code, ...FLAGS });
const fail = (code, fields = {}) => answer(false, code, fields);
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const canonical = value => canonicalizeJSONV1(Buffer.from(JSON.stringify(value), 'utf8')).canonical;
const isInt = (v, min, max = Number.MAX_SAFE_INTEGER) => Number.isSafeInteger(v) && v >= min && v <= max;
const isID = v => typeof v === 'string' && v.length >= 1 && v.length <= 96 && /^[a-z][a-z0-9_.-]*$/.test(v);

export const CONTENT_CONSENT_LIMITS = Object.freeze({
  maxContentBytes: 65536,     // one artifact, bounded
  maxAdvisoryChars: 2048,     // bounded text may come back
  maxLifetimeMs: 3600000,     // a grant cannot outlive an hour
});

export const ALLOWED_CONTENT_KINDS = Object.freeze(['json', 'markdown', 'text']);

const GRANT_KEYS = ['kind', 'schemaVersion', 'generation', 'grantId', 'consentClass',
  'scopeRoot', 'allowedKinds', 'maxContentBytes', 'maxAdvisoryChars', 'lifetimeMs',
  'destination', 'networkEgress', 'contentPersisted', 'transcriptPersisted'];

/** The two things a grant can be. Nothing may report the weaker as the stronger. */
export const CONSENT_CLASSES = Object.freeze([
  'WRITTEN_DECLARATION',            // what this module can actually witness
  'CAPTURED_USER_INTERFACE_ACTION', // reserved; no code path issues it yet
]);

function grantValid(v) {
  if (v === null || typeof v !== 'object' || Array.isArray(v)) return false;
  if (Object.keys(v).sort().join('|') !== [...GRANT_KEYS].sort().join('|')) return false;
  if (v.kind !== 'nisi-content-consent-v1' || v.schemaVersion !== 1) return false;
  if (!isInt(v.generation, 1) || !isID(v.grantId)) return false;
  if (v.consentClass !== 'WRITTEN_DECLARATION') return false;
  if (typeof v.scopeRoot !== 'string' || !path.isAbsolute(v.scopeRoot) || v.scopeRoot.length > 1024) return false;
  if (!Array.isArray(v.allowedKinds) || v.allowedKinds.length === 0) return false;
  if (!v.allowedKinds.every(k => ALLOWED_CONTENT_KINDS.includes(k))) return false;
  if (new Set(v.allowedKinds).size !== v.allowedKinds.length) return false;
  if (!isInt(v.maxContentBytes, 1, CONTENT_CONSENT_LIMITS.maxContentBytes)) return false;
  if (!isInt(v.maxAdvisoryChars, 0, CONTENT_CONSENT_LIMITS.maxAdvisoryChars)) return false;
  if (!isInt(v.lifetimeMs, 1, CONTENT_CONSENT_LIMITS.maxLifetimeMs)) return false;
  // These four are the boundary itself and are not negotiable per grant.
  if (v.destination !== 'ON_DEVICE_APPLE_FOUNDATION_MODELS_ONLY') return false;
  if (v.networkEgress !== false || v.contentPersisted !== false || v.transcriptPersisted !== false) return false;
  return true;
}

class ContentConsentGrant {
  #value; #digest; #clock; #issuedAtMs; #revoked = false; #admitted = 0;

  constructor(value, digest, clock, issuedAtMs) {
    this.#value = Object.freeze({ ...value, allowedKinds: Object.freeze([...value.allowedKinds]) });
    this.#digest = digest;
    this.#clock = clock;
    this.#issuedAtMs = issuedAtMs;
  }

  get digest() { return this.#digest; }

  #live() {
    if (this.#revoked) return 'CONSENT_REVOKED';
    let now;
    try { now = this.#clock(); } catch { return 'CONSENT_CLOCK_INVALID'; }
    if (!isInt(now, 0)) return 'CONSENT_CLOCK_INVALID';
    if (now < this.#issuedAtMs) return 'CONSENT_CLOCK_REGRESSION';
    if (now >= this.#issuedAtMs + this.#value.lifetimeMs) return 'CONSENT_EXPIRED';
    return null;
  }

  /** Checks one concrete content item against the grant and, when it passes,
   *  returns the exact bytes that may be sent. Reads the file itself so no
   *  caller can present bytes that differ from what the path holds. */
  admitContent(item) {
    const notLive = this.#live();
    if (notLive) return fail(notLive);
    let filePath, kind;
    try {
      if (item === null || typeof item !== 'object' || Array.isArray(item)) return fail('CONTENT_ITEM_INVALID');
      const keys = Reflect.ownKeys(item);
      if (keys.length !== 2 || !keys.includes('filePath') || !keys.includes('kind')) return fail('CONTENT_ITEM_INVALID');
      ({ filePath, kind } = item);
    } catch { return fail('CONTENT_ITEM_INVALID'); }
    if (typeof filePath !== 'string' || !path.isAbsolute(filePath)) return fail('CONTENT_PATH_INVALID');
    if (!this.#value.allowedKinds.includes(kind)) return fail('CONTENT_KIND_REFUSED');

    // Resolve symlinks before the scope test: a link inside the approved root
    // that points outside it must not smuggle content past the boundary.
    let resolved, rootResolved;
    try {
      resolved = fs.realpathSync(filePath);
      rootResolved = fs.realpathSync(this.#value.scopeRoot);
    } catch { return fail('CONTENT_PATH_UNRESOLVABLE'); }
    const prefix = rootResolved.endsWith(path.sep) ? rootResolved : rootResolved + path.sep;
    if (!resolved.startsWith(prefix)) return fail('CONTENT_OUT_OF_SCOPE');

    let stat;
    try { stat = fs.lstatSync(resolved, { bigint: true }); } catch { return fail('CONTENT_PATH_UNRESOLVABLE'); }
    if (!stat.isFile()) return fail('CONTENT_NOT_REGULAR_FILE');
    if (stat.size === 0n) return fail('CONTENT_EMPTY');
    if (stat.size > BigInt(this.#value.maxContentBytes)) {
      return fail('CONTENT_BYTE_LIMIT', { bytes: Number(stat.size), maximum: this.#value.maxContentBytes });
    }

    let bytes, descriptor;
    const sameFile = (a, b) => a.dev === b.dev && a.ino === b.ino && a.size === b.size &&
      a.mtimeNs === b.mtimeNs && a.ctimeNs === b.ctimeNs;
    try {
      // Check and read the same opened object. This reduces replacement races;
      // stable trusted parent directories are still required for pathname scope.
      const flags = fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0) | (fs.constants.O_NONBLOCK || 0);
      descriptor = fs.openSync(resolved, flags);
      const opened = fs.fstatSync(descriptor, { bigint: true });
      if (!opened.isFile()) return fail('CONTENT_NOT_REGULAR_FILE');
      if (!sameFile(stat, opened)) return fail('CONTENT_CHANGED_DURING_READ');
      const buffer = Buffer.alloc(this.#value.maxContentBytes + 1);
      let count = 0;
      while (count < buffer.length) {
        const read = fs.readSync(descriptor, buffer, count, buffer.length - count, null);
        if (read === 0) break;
        count += read;
      }
      if (count > this.#value.maxContentBytes) {
        return fail('CONTENT_BYTE_LIMIT', { bytes: count, maximum: this.#value.maxContentBytes });
      }
      if (!sameFile(opened, fs.fstatSync(descriptor, { bigint: true })) || BigInt(count) !== opened.size) {
        return fail('CONTENT_CHANGED_DURING_READ');
      }
      bytes = Buffer.from(buffer.subarray(0, count));
    } catch (error) {
      return fail(error?.code === 'ELOOP' ? 'CONTENT_CHANGED_DURING_READ' : 'CONTENT_UNREADABLE');
    } finally {
      if (descriptor !== undefined) { try { fs.closeSync(descriptor); } catch { /* no data is admitted by cleanup */ } }
    }
    try { new TextDecoder('utf-8', { fatal: true }).decode(bytes); }
    catch { return fail('CONTENT_NOT_UTF8'); }

    this.#admitted += 1;
    return answer(true, 'CONTENT_ADMITTED', {
      bytes,
      contentSha256: sha(bytes),
      contentBytes: bytes.length,
      kind,
      consentDigest: this.#digest,
      maxAdvisoryChars: this.#value.maxAdvisoryChars,
    });
  }

  /** Immediate and terminal. A revoked grant admits nothing further. */
  revoke() {
    this.#revoked = true;
    return answer(true, 'CONSENT_REVOKED_BY_HOLDER');
  }

  status() {
    const notLive = this.#live();
    return answer(true, 'CONTENT_CONSENT_STATUS', {
      grantId: this.#value.grantId,
      consentClass: this.#value.consentClass,
      consentDigest: this.#digest,
      destination: this.#value.destination,
      networkEgress: false,
      admittedItems: this.#admitted,
      live: notLive === null,
      refusalIfNotLive: notLive,
    });
  }
}

/** Binds one canonical consent grant. Absence of a valid grant is refusal. */
export function createContentConsent(grantBytes, options) {
  try {
    const config = options && typeof options === 'object' && !Array.isArray(options) ? options : null;
    if (!config || Object.keys(config).some(k => k !== 'clock')) return fail('CONFIGURATION_REFUSED');
    const { clock } = config;
    if (typeof clock !== 'function') return fail('CONFIGURATION_REFUSED');
    if (!Buffer.isBuffer(grantBytes) || grantBytes.length === 0 || grantBytes.length > 8192) {
      return fail('CONSENT_BYTES_REFUSED');
    }
    if (grantBytes.buffer instanceof SharedArrayBuffer) return fail('CONSENT_BYTES_REFUSED');
    let text;
    try { text = new TextDecoder('utf-8', { fatal: true }).decode(grantBytes); }
    catch { return fail('CONSENT_BYTES_REFUSED'); }
    let value;
    try { value = JSON.parse(canonicalizeJSONV1(grantBytes).canonical); }
    catch { return fail('CONSENT_NONCANONICAL'); }
    if (canonical(value) !== text) return fail('CONSENT_NONCANONICAL');
    if (!grantValid(value)) return fail('CONSENT_DECLARATION_REFUSED');
    let issuedAtMs;
    try { issuedAtMs = clock(); } catch { return fail('CONSENT_CLOCK_INVALID'); }
    if (!isInt(issuedAtMs, 0)) return fail('CONSENT_CLOCK_INVALID');
    const digest = sha(Buffer.from(`nisi/content-consent/v1\0${text}`, 'utf8'));
    return answer(true, 'READY_PRIVATE_CONTENT_CONSENT_ONLY', {
      grant: new ContentConsentGrant(value, digest, clock, issuedAtMs),
      consentDigest: digest,
    });
  } catch {
    return fail('CONSTRUCTION_ERROR');
  }
}
