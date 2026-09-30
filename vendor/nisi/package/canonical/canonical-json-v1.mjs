// Copyright 2026 Louis Calata
// SPDX-License-Identifier: Apache-2.0

// An explicitly versioned raw-byte canonical JSON codec.
//
// Everything between the PURE-REGION markers below is checked by
// scripts/structural-check.mjs: it must contain no process spawn, no filesystem
// write, no network call, no dynamic code, no module load, and no process
// mutation. The checker itself is mutation-tested and false-positive-tested
// against contracts/structural-fixtures.json, so a checker that silently
// stopped detecting anything would fail its own suite.
import { createHash } from 'node:crypto';
import { types } from 'node:util';

// PURE-REGION-BEGIN

export const CANONICAL_JSON_PROFILE_V1 = 'nisi-canonical-json-v1';
export const CANONICAL_JSON_LIMITS_V1 = Object.freeze({ bytes: 1_048_576, depth: 128, objectMembers: 4_096 });

export class CanonicalJSONErrorV1 extends Error {
  constructor(code) {
    super(code); // No input content is included in diagnostics.
    this.name = 'CanonicalJSONErrorV1';
    this.code = code;
  }
}

const refuse = code => { throw new CanonicalJSONErrorV1(code); };
const typedArray = Object.getPrototypeOf(Uint8Array.prototype);
const bufferOf = Object.getOwnPropertyDescriptor(typedArray, 'buffer').get;
const lengthOf = Object.getOwnPropertyDescriptor(typedArray, 'byteLength').get;
const offsetOf = Object.getOwnPropertyDescriptor(typedArray, 'byteOffset').get;
const valuesOf = typedArray.values;

function capture(input) {
  // Native brand checks refuse proxies without invoking their property traps.
  if (!types.isUint8Array(input)) refuse('INVALID_JSON_INPUT');
  // Intrinsic iterator creation validates the view without consulting input
  // properties or iterating it. Resized-out-of-bounds views otherwise expose
  // zero offset/length and could be mistaken for a valid empty document view.
  try { valuesOf.call(input); }
  catch { refuse('INVALID_JSON_INPUT'); }
  const buffer = bufferOf.call(input);
  if (types.isSharedArrayBuffer(buffer)) refuse('INVALID_JSON_INPUT');
  const length = lengthOf.call(input);
  if (length > CANONICAL_JSON_LIMITS_V1.bytes) refuse('INVALID_JSON_SIZE');
  try {
    return Buffer.from(new Uint8Array(buffer, offsetOf.call(input), length));
  } catch {
    refuse('INVALID_JSON_INPUT');
  }
}

class Parser {
  constructor(bytes) {
    if (bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf) refuse('INVALID_JSON_BOM');
    try {
      this.text = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes);
    } catch {
      refuse('INVALID_JSON_UTF8');
    }
    this.index = 0;
  }

  whitespace() {
    while ([' ', '\t', '\n', '\r'].includes(this.text[this.index])) this.index++;
  }

  consume(character) {
    if (this.text[this.index] !== character) return false;
    this.index++;
    return true;
  }

  document() {
    this.whitespace();
    const value = this.value(0);
    this.whitespace();
    if (this.index !== this.text.length) refuse('INVALID_JSON_SYNTAX');
    return value;
  }

  value(depth) {
    this.whitespace();
    const next = this.text[this.index];
    if (next === '{' || next === '[') {
      if (depth >= CANONICAL_JSON_LIMITS_V1.depth) refuse('INVALID_JSON_DEPTH');
      return next === '{' ? this.object(depth + 1) : this.array(depth + 1);
    }
    if (next === '"') return JSON.stringify(this.string());
    for (const literal of ['true', 'false', 'null']) {
      if (this.text.startsWith(literal, this.index)) {
        this.index += literal.length;
        return literal;
      }
    }
    if (next === '-' || (next >= '0' && next <= '9') || ['N', 'I', '+'].includes(next)) return this.number();
    refuse('INVALID_JSON_SYNTAX');
  }

  object(depth) {
    this.index++;
    this.whitespace();
    if (this.consume('}')) return '{}';
    const members = [];
    const seen = new Set();
    while (true) {
      this.whitespace();
      if (this.text[this.index] !== '"') refuse('INVALID_JSON_SYNTAX');
      const key = this.string();
      if (key.includes('\0')) refuse('INVALID_JSON_DUPLICATE_KEY');
      if (key.normalize('NFC') !== key) refuse('INVALID_JSON_KEY_NOT_NFC');
      if (seen.has(key)) refuse('INVALID_JSON_DUPLICATE_KEY');
      if (members.length >= CANONICAL_JSON_LIMITS_V1.objectMembers) refuse('INVALID_JSON_OBJECT_SIZE');
      seen.add(key);
      this.whitespace();
      if (!this.consume(':')) refuse('INVALID_JSON_SYNTAX');
      members.push({ key, order: Buffer.from(key, 'utf8'), value: this.value(depth) });
      this.whitespace();
      if (this.consume('}')) break;
      if (!this.consume(',')) refuse('INVALID_JSON_SYNTAX');
    }
    members.sort((left, right) => Buffer.compare(left.order, right.order));
    // A sorted object passed to JSON.stringify would reorder numeric-looking
    // keys. Direct member emission also preserves the literal __proto__ key.
    return '{' + members.map(member => JSON.stringify(member.key) + ':' + member.value).join(',') + '}';
  }

  array(depth) {
    this.index++;
    this.whitespace();
    if (this.consume(']')) return '[]';
    const values = [];
    while (true) {
      values.push(this.value(depth));
      this.whitespace();
      if (this.consume(']')) return '[' + values.join(',') + ']';
      if (!this.consume(',')) refuse('INVALID_JSON_SYNTAX');
    }
  }

  string() {
    if (!this.consume('"')) refuse('INVALID_JSON_STRING');
    let result = '';
    while (this.index < this.text.length) {
      const c = this.text[this.index++];
      if (c === '"') return result;
      if (c.charCodeAt(0) < 0x20) refuse('INVALID_JSON_STRING');
      if (c !== '\\') { result += c; continue; }
      const escaped = this.text[this.index++];
      if (escaped === 'u') {
        let scalar = this.hex4();
        if (scalar >= 0xd800 && scalar <= 0xdbff) {
          if (!this.consume('\\') || !this.consume('u')) refuse('INVALID_JSON_STRING_SURROGATE');
          const low = this.hex4();
          if (low < 0xdc00 || low > 0xdfff) refuse('INVALID_JSON_STRING_SURROGATE');
          scalar = 0x10000 + ((scalar - 0xd800) << 10) + low - 0xdc00;
        } else if (scalar >= 0xdc00 && scalar <= 0xdfff) {
          refuse('INVALID_JSON_STRING_SURROGATE');
        }
        result += String.fromCodePoint(scalar);
      } else {
        switch (escaped) {
          case '"': result += '"'; break;
          case '\\': result += '\\'; break;
          case '/': result += '/'; break;
          case 'b': result += '\b'; break;
          case 'f': result += '\f'; break;
          case 'n': result += '\n'; break;
          case 'r': result += '\r'; break;
          case 't': result += '\t'; break;
          default: refuse('INVALID_JSON_STRING');
        }
      }
    }
    refuse('INVALID_JSON_STRING');
  }

  hex4() {
    const digits = this.text.slice(this.index, this.index + 4);
    if (!/^[0-9a-fA-F]{4}$/.test(digits)) refuse('INVALID_JSON_STRING');
    this.index += 4;
    return Number.parseInt(digits, 16);
  }

  number() {
    const start = this.index;
    while (this.index < this.text.length && ![' ', '\t', '\r', '\n', ',', ']', '}'].includes(this.text[this.index])) this.index++;
    const token = this.text.slice(start, this.index);
    if (!/^-?(0|[1-9][0-9]*)$/.test(token) || token === '-0') refuse('INVALID_JSON_NUMBER');
    const magnitude = token.startsWith('-') ? token.slice(1) : token;
    if (magnitude.length > 16 || (magnitude.length === 16 && magnitude > '9007199254740991')) refuse('INVALID_JSON_NUMBER');
    // Source digits, not a rounded Number: no fraction/exponent normalization.
    return token;
  }
}

/** Raw bytes only. Callers must explicitly bind this profile before using its
 * digest; this function never rewrites its input and has no fallback path. */
export function canonicalizeJSONV1(input) {
  const bytes = capture(input);
  const canonical = new Parser(bytes).document();
  if (Buffer.byteLength(canonical, 'utf8') > CANONICAL_JSON_LIMITS_V1.bytes) refuse('INVALID_JSON_SIZE');
  return Object.freeze({
    profile: CANONICAL_JSON_PROFILE_V1,
    canonical,
    sha256: createHash('sha256').update(canonical, 'utf8').digest('hex'),
  });
}
// PURE-REGION-END
