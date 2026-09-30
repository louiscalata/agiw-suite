// Copyright 2026 Louis Calata
// SPDX-License-Identifier: Apache-2.0
import {createHash} from 'node:crypto';
import {types as nodeTypes} from 'node:util';

const STATES = new Set(['QUEUED','RUNNING','SUCCEEDED','FAILED','CANCELLED','REVOKED']);
const ENTRY_KEYS = ['id','projectId','runId','attempt','candidateId','stage','receiptId','createdAt','ttlMs','state','heartbeatAt','retryOf','revokes','payload'];
const RECORD_KEYS = ['entry','fingerprint','retained'];
const ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const SEG = /^(?:[A-Za-z_][A-Za-z0-9_]*|0|[1-9][0-9]*)$/;
const BAD = new Set(['__proto__','prototype','constructor']);
const MARK = '[REDACTED]';

const err = (code, message = code) => { const e = new Error(message); e.code = code; return e; };
const own = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
const exact = (o, keys) => o && typeof o === 'object' && !Array.isArray(o) &&
  Object.keys(o).length === keys.length && keys.every(k => own(o, k));
const safe = n => Number.isSafeInteger(n) && n >= 0 && !Object.is(n, -0);

function copy(x, stack = new Set()) {
  // Reject ordinary and revoked Proxies before reflection, including nested data.
  if (nodeTypes.isProxy(x)) throw err('ENTRY');
  if (x === null || typeof x === 'string' || typeof x === 'boolean') {
    if (typeof x === 'string') for (let i=0;i<x.length;i++) { const n=x.charCodeAt(i); if (n>=0xd800&&n<=0xdbff) { const m=x.charCodeAt(++i); if (!(m>=0xdc00&&m<=0xdfff)) throw err('ENTRY'); } else if (n>=0xdc00&&n<=0xdfff) throw err('ENTRY'); }
    return x;
  }
  if (typeof x === 'number') { if (!Number.isSafeInteger(x) || Object.is(x, -0)) throw err('ENTRY'); return x; }
  if (!x || typeof x !== 'object' || stack.has(x)) throw err('ENTRY');
  const proto = Object.getPrototypeOf(x); if (Array.isArray(x) ? proto !== Array.prototype : proto !== Object.prototype && proto !== null) throw err('ENTRY');
  stack.add(x);
  let out;
  if (Array.isArray(x)) {
    const names = Object.getOwnPropertyNames(x);
    if (names.length !== x.length + 1 || !names.includes('length') || Reflect.ownKeys(x).length !== names.length) throw err('ENTRY');
    out = [];
    for (let i=0;i<x.length;i++) {
      const d = Object.getOwnPropertyDescriptor(x, String(i));
      if (!d || !d.enumerable || !('value' in d)) throw err('ENTRY');
      out.push(copy(d.value, stack));
    }
  } else {
    const names = Object.getOwnPropertyNames(x), keys = Object.keys(x);
    if (names.length !== keys.length || Reflect.ownKeys(x).length !== keys.length) throw err('ENTRY');
    out = {};
    for (const k of keys) {
      if (BAD.has(k) || !k.isWellFormed()) throw err('ENTRY');
      const d = Object.getOwnPropertyDescriptor(x, k);
      if (!d || !d.enumerable || !('value' in d)) throw err('ENTRY');
      out[k] = copy(d.value, stack);
    }
  }
  stack.delete(x); return out;
}

function C(x) {
  if (Array.isArray(x)) return '[' + x.map(C).join(',') + ']';
  if (x && typeof x === 'object') return '{' + Object.keys(x).sort().map(k => JSON.stringify(k)+':'+C(x[k])).join(',') + '}';
  return JSON.stringify(x);
}
function H(x) { return createHash('sha256').update(x).digest('hex'); }
function freeze(x, seen = new Set()) {
  if (x && typeof x === 'object' && !seen.has(x)) { seen.add(x); for (const v of Object.values(x)) freeze(v, seen); Object.freeze(x); }
  return x;
}
function id(x) { return typeof x === 'string' && ID.test(x); }
function configOf(input) {
  let c; try { c = copy(input); } catch (_) { throw err('CONFIG'); }
  if (!exact(c, ['projectId','maxEntries','heartbeatTtlMs','redactPaths']) || !id(c.projectId) ||
      !Number.isSafeInteger(c.maxEntries) || c.maxEntries < 1 || c.maxEntries > 10000 ||
      !Number.isSafeInteger(c.heartbeatTtlMs) || c.heartbeatTtlMs < 1 || !Array.isArray(c.redactPaths)) throw err('CONFIG');
  const paths = []; for (const p of c.redactPaths) {
    if (typeof p !== 'string' || !p.startsWith('payload.') || p.length < 9) throw err('CONFIG');
    const a = p.split('.'); if (a.some(s => !SEG.test(s) || BAD.has(s))) throw err('CONFIG');
    for (const q of paths) if (a.length === q.length ? a.every((s,i)=>s===q[i]) :
      (a.length < q.length ? a.every((s,i)=>s===q[i]) : q.every((s,i)=>s===a[i]))) throw err('CONFIG');
    paths.push(a);
  }
  c.redactPaths = c.redactPaths.slice(); c._paths = paths; return c;
}
function entryOf(input, now) {
  let e; try { e = copy(input); } catch (_) { throw err('ENTRY'); }
  if (!exact(e, ENTRY_KEYS) || !id(e.id) || !id(e.projectId) || !id(e.runId) || !id(e.candidateId) || !id(e.stage) ||
      !Number.isSafeInteger(e.attempt) || e.attempt < 0 || !safe(e.createdAt) || !Number.isSafeInteger(e.ttlMs) || e.ttlMs < 1 ||
      !safe(e.createdAt + e.ttlMs) || !STATES.has(e.state) || (e.heartbeatAt !== null && !safe(e.heartbeatAt)) ||
      (e.heartbeatAt !== null && e.heartbeatAt > e.createdAt) || (e.receiptId !== null && !id(e.receiptId)) ||
      (e.retryOf !== null && !id(e.retryOf)) || (e.revokes !== null && !id(e.revokes)) ||
      (e.state === 'REVOKED' ? e.revokes === null || e.retryOf !== null : e.revokes !== null) ||
      (now !== undefined && e.createdAt > now)) throw err('ENTRY');
  return e;
}
function redact(e, paths) {
  for (const path of paths) {
    let cur = e; for (let i=0;i<path.length-1;i++) { const k=path[i]; if (!cur || typeof cur !== 'object' || !own(cur,k) || Array.isArray(cur) && !/^(0|[1-9][0-9]*)$/.test(k)) throw err('REDACTION'); cur=cur[k]; }
    const k=path.at(-1); if (!cur || typeof cur !== 'object' || !own(cur,k) || Array.isArray(cur) && !/^(0|[1-9][0-9]*)$/.test(k)) throw err('REDACTION'); cur[k]=MARK;
  }
}
function expired(r, now) { return now >= r.entry.createdAt + r.entry.ttlMs; }

function build(config, watermark = 0, initial = [], sealed = false) {
  const records = initial.slice(), byId = new Map(), revoked = new Set();
  for (const r of records) { byId.set(r.entry.id, r); if (r.entry.revokes !== null) revoked.add(r.entry.revokes); }
  let mark = watermark;
  const prune = now => {
    const out=[]; for (const r of records) if (r.retained && expired(r,now)) { r.retained=false; r.entry.payload=null; out.push(r.entry.id); }
    let n=records.filter(r=>r.retained).length;
    for (const r of records) if (n>config.maxEntries && r.retained) { r.retained=false; r.entry.payload=null; n--; out.push(r.entry.id); }
    const pruned = new Set(out); return records.filter(r=>pruned.has(r.entry.id)).map(r=>r.entry.id);
  };
  const time = now => { if (!safe(now) || now < mark) throw err('TIME'); mark=now; };
  const view = (r, now) => {
    const terminal = new Set(['SUCCEEDED','FAILED','CANCELLED','REVOKED']);
    let l = revoked.has(r.entry.id) ? 'REVOKED' : terminal.has(r.entry.state) ? r.entry.state : expired(r,now) ? 'EXPIRED' :
      r.entry.state === 'RUNNING' && (r.entry.heartbeatAt === null || now-r.entry.heartbeatAt > config.heartbeatTtlMs) ? 'UNKNOWN' : r.entry.state;
    return freeze({entry:copy(r.entry), historical:r.historical, retained:r.retained, revoked:revoked.has(r.entry.id), expired:expired(r,now), liveness:l, authorizing:false});
  };
  const append = (input, now) => {
    if (!safe(now) || now < mark) throw err('TIME');
    const e=entryOf(input,now);
    if (sealed) return freeze({status:'REFUSED',id:e.id,reason:'SEALED'});
    if (e.projectId !== config.projectId) return freeze({status:'REFUSED',id:e.id,reason:'PROJECT'});
    // Never persist a digest of fields that the caller asked to redact.
    // Identity is intentionally defined over the retained projection.
    redact(e,config._paths);
    const fp=H('nisi-run-journal/input/v1\n'+C(e)), old=byId.get(e.id);
    if (old) return freeze(old.fingerprint===fp ? {status:'DUPLICATE',id:e.id} : {status:'CONFLICT',id:e.id,reason:'ID_CONTENT_MISMATCH'});
    if (e.retryOf !== null) {
      const t=byId.get(e.retryOf); if (!t || e.state!=='QUEUED' || t.entry.runId!==e.runId || e.attempt<=t.entry.attempt || e.createdAt<t.entry.createdAt ||
        !(new Set(['SUCCEEDED','FAILED','CANCELLED','REVOKED'])).has(t.entry.state) && !expired(t,now) && !revoked.has(t.entry.id)) return freeze({status:'REFUSED',id:e.id,reason:'RETRY'});
    }
    if (e.revokes !== null) { const t=byId.get(e.revokes); if (!t || t.entry.runId!==e.runId || t.entry.attempt!==e.attempt || t.entry.candidateId!==e.candidateId || t.entry.createdAt>e.createdAt) return freeze({status:'REFUSED',id:e.id,reason:'REVOCATION'}); }
    const r={entry:e,fingerprint:fp,retained:true,historical:false}; records.push(r); byId.set(e.id,r); if (e.revokes!==null) revoked.add(e.revokes);
    mark=now; prune(now); return freeze({status:'APPENDED',id:e.id});
  };
  const list = now => { time(now); prune(now); return freeze(records.map(r=>view(r,now))); };
  const retain = now => { time(now); return freeze(prune(now)); };
  const serialize = () => {
    if (sealed) throw err('SEALED');
    const header={type:'header',schema:'nisi-run-journal-v1',config:{projectId:config.projectId,maxEntries:config.maxEntries,heartbeatTtlMs:config.heartbeatTtlMs,redactPaths:config.redactPaths.slice()},now:mark};
    let prev=H('nisi-run-journal/header/v1\n'+C(header)), lines=[C(header)];
    records.forEach((r,i)=>{ const record={entry:r.entry,fingerprint:r.fingerprint,retained:r.retained}, env={type:'entry',seq:i+1,previousHash:prev,record,hash:H('nisi-run-journal/record/v1\n'+C({seq:i+1,previousHash:prev,record}))}; lines.push(C(env)); prev=env.hash; });
    lines.push(C({type:'footer',count:records.length,lastHash:prev})); return lines.join('\n')+'\n';
  };
  return {api:freeze({append,list,retain,serialize}), records, byId, revoked, get watermark(){return mark;}};
}

function parseLine(raw) {
  try { const v=JSON.parse(raw); return C(v)===raw ? v : null; } catch (_) { return null; }
}
function report(status, ids, line, reason) { return freeze({status,recoveredIds:ids,rejectedLine:line,reason,authorizing:false}); }

export function createRunJournal(input) { const c=configOf(input), b=build(c); return b.api; }

function reopenData(serialized) {
  if (typeof serialized !== 'string' || !serialized.includes('\n')) return {journal:null,report:report('INVALID',[],1,'HEADER')};
  const trailing=!serialized.endsWith('\n'); let ls=serialized.split('\n'); let tail=null; if (trailing) tail=ls.pop(); else ls.pop();
  const h=parseLine(ls[0]);
  if (!h || !exact(h,['type','schema','config','now']) || h.type!=='header' || h.schema!=='nisi-run-journal-v1' || !safe(h.now)) return {journal:null,report:report('INVALID',[],1,'HEADER')};
  let c; try { c=configOf(h.config); } catch (_) { return {journal:null,report:report('INVALID',[],1,'HEADER')}; }
  const b=build(c,h.now), ids=[]; let prev=H('nisi-run-journal/header/v1\n'+C(h)), footer=false, bad=null, retained=0;
  const failAt=(line,reason='RECORD',status='INVALID')=>{bad={line,reason,status};};
  for(let n=1;n<ls.length&&!bad;n++) {
    const v=parseLine(ls[n]); if (!v) { failAt(n+1); break; }
    if (v.type==='footer') {
      if (!exact(v,['type','count','lastHash']) || v.type!=='footer' || !Number.isSafeInteger(v.count) || v.count!==ids.length || v.lastHash!==prev) failAt(n+1,'FOOTER'); else footer=true;
      if (footer && (n+1<ls.length || trailing)) failAt(n+1<ls.length ? n+2 : ls.length+1,'TRAILING_DATA');
      break;
    }
    if (!exact(v,['type','seq','previousHash','record','hash']) || v.type!=='entry' || v.seq!==n || v.previousHash!==prev || typeof v.hash!=='string' || !/^[0-9a-f]{64}$/.test(v.hash) ||
        H('nisi-run-journal/record/v1\n'+C({seq:v.seq,previousHash:v.previousHash,record:v.record}))!==v.hash || !exact(v.record,RECORD_KEYS) || typeof v.record.fingerprint!=='string' || !/^[0-9a-f]{64}$/.test(v.record.fingerprint) || typeof v.record.retained!=='boolean') { failAt(n+1); break; }
    let e; try { e=entryOf(v.record.entry,h.now); } catch (_) { failAt(n+1); break; }
    if (e.projectId!==c.projectId || b.byId.has(e.id) || (!v.record.retained && e.payload!==null)) { failAt(n+1); break; }
    if (v.record.retained) { try { for(const p of c._paths){let x=e;for(const k of p){if(!x||typeof x!=='object'||!own(x,k)||Array.isArray(x)&&!/^(0|[1-9][0-9]*)$/.test(k))throw 0;x=x[k];}if(x!==MARK)throw 0;} } catch (_) { failAt(n+1); break; }
      if (v.record.fingerprint!==H('nisi-run-journal/input/v1\n'+C(e)) || expired({entry:e},h.now)) { failAt(n+1); break; }
      retained++; if(retained>c.maxEntries){failAt(n+1);break;} }
    if (e.retryOf!==null) { const t=b.byId.get(e.retryOf); if(!t || e.state!=='QUEUED' || t.entry.runId!==e.runId || e.attempt<=t.entry.attempt || e.createdAt<t.entry.createdAt || !(new Set(['SUCCEEDED','FAILED','CANCELLED','REVOKED'])).has(t.entry.state) && !expired(t,h.now) && !b.revoked.has(t.entry.id)) { failAt(n+1); break; } }
    if (e.revokes!==null) { const t=b.byId.get(e.revokes); if(!t || t.entry.runId!==e.runId || t.entry.attempt!==e.attempt || t.entry.candidateId!==e.candidateId || t.entry.createdAt>e.createdAt){failAt(n+1);break;} }
    const r={entry:e,fingerprint:v.record.fingerprint,retained:v.record.retained,historical:true}; b.records.push(r); b.byId.set(e.id,r); if(e.revokes!==null)b.revoked.add(e.revokes); prev=v.hash; ids.push(e.id);
  }
  if (!bad && !footer) bad={line:ls.length+1,reason:trailing?'TRUNCATED':'MISSING_FOOTER',status:'INCOMPLETE'};
  if (bad) { const sealed=build(c,h.now,b.records,true); return {journal:sealed.api,report:report(bad.status,ids,bad.line,bad.reason)}; }
  return {journal:b.api,report:report('COMPLETE',ids,null,null)};
}

export function reopen(serialized) { return freeze(reopenData(serialized)); }
