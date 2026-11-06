// Edition transform engine for the shared web UI. Dependency-free (Node 18+).
//
// The App Store edition is produced from the synced upstream files by explicit, fail-closed rules
// (appstore-rules.mjs): top-level JavaScript statements are dropped, replaced (pinned to the sha256 of the
// upstream statement they were written against) or edited with exact substring replacements; HTML elements
// are removed by id or by an exact start tag; CSS selectors are pruned by pattern. Every anchor must match
// exactly as often as the rule says, so upstream drift stops the sync with a list of what moved instead of
// shipping a half-stripped bundle.
import { createHash } from 'node:crypto';

export class EditionError extends Error {
  constructor(problems) {
    super(`${problems.length} edition problem(s):\n- ${problems.join('\n- ')}`);
    this.problems = problems;
  }
}

export const sha256 = text => createHash('sha256').update(text, 'utf8').digest('hex');
const short = (text, n = 90) => JSON.stringify(text.length > n ? `${text.slice(0, n)}…` : text);
const count = (haystack, needle) => {
  if (!needle) return 0;
  let n = 0;
  for (let i = haystack.indexOf(needle); i !== -1; i = haystack.indexOf(needle, i + needle.length)) n++;
  return n;
};

// ---------------------------------------------------------------------------------------------------------
// JavaScript scanning

const REGEX_BEFORE = new Set(['(', ',', '=', ':', '[', '!', '&', '|', '?', '{', '}', ';', '+', '-', '*', '%',
  '<', '>', '~', '^']);
const REGEX_KEYWORDS = new Set(['return', 'typeof', 'case', 'do', 'else', 'in', 'of', 'void', 'throw', 'delete',
  'new', 'yield', 'await', 'instanceof']);

/**
 * Scans JavaScript source and returns:
 * - `mask`: the source with the contents of strings, template text, regex literals and comments replaced by
 *   spaces (newlines kept), so identifier searches only see code;
 * - `lineStartDepth[i]`: bracket depth ({[( combined) at the start of line i;
 * - `lineStartsInCode[i]`: false when line i begins inside a string, template text or block comment.
 * Throws when brackets do not balance or a literal is unterminated.
 */
export function scanJs(source) {
  const out = source.split('');
  const blank = (from, to) => { for (let k = from; k < to; k++) if (out[k] !== '\n') out[k] = ' '; };
  const lineStartDepth = [0];
  const lineStartsInCode = [true];
  // Stack entries: 'code' frames carry their own bracket depth; 'template' frames sit between them.
  const stack = [{ mode: 'code', depth: 0, templateBrace: false }];
  let depth = 0;
  let prevSignificant = '';
  let prevWord = '';
  let i = 0;
  const n = source.length;
  const markLine = inCode => { lineStartDepth.push(depth); lineStartsInCode.push(inCode); };
  while (i < n) {
    const top = stack[stack.length - 1];
    const c = source[i];
    if (top.mode === 'template') {
      if (c === '\\') { blank(i, i + 2); if (source[i + 1] === '\n') markLine(false); i += 2; continue; }
      if (c === '`') { stack.pop(); prevSignificant = '`'; prevWord = ''; i++; continue; }
      if (c === '$' && source[i + 1] === '{') {
        stack.push({ mode: 'code', depth: 0, templateBrace: true });
        depth++;
        prevSignificant = '{'; prevWord = '';
        i += 2;
        continue;
      }
      if (c === '\n') { markLine(false); i++; continue; }
      blank(i, i + 1); i++; continue;
    }
    // code mode
    if (c === '\n') { markLine(true); i++; continue; }
    if (c === ' ' || c === '\t' || c === '\r') { i++; continue; }
    if (c === '/' && source[i + 1] === '/') {
      let j = source.indexOf('\n', i); if (j === -1) j = n;
      blank(i, j); i = j; continue;
    }
    if (c === '/' && source[i + 1] === '*') {
      const j = source.indexOf('*/', i + 2);
      if (j === -1) throw new Error(`unterminated block comment at offset ${i}`);
      for (let k = i; k < j + 2; k++) if (source[k] === '\n') markLine(false);
      blank(i, j + 2); i = j + 2; continue;
    }
    if (c === '"' || c === "'") {
      let j = i + 1;
      while (j < n && source[j] !== c) {
        if (source[j] === '\\') j++;
        else if (source[j] === '\n') throw new Error(`unterminated string at offset ${i}`);
        j++;
      }
      if (j >= n) throw new Error(`unterminated string at offset ${i}`);
      blank(i + 1, j); prevSignificant = c; prevWord = ''; i = j + 1; continue;
    }
    if (c === '`') { stack.push({ mode: 'template' }); i++; continue; }
    if (c === '/') {
      const regex = prevSignificant === '' || REGEX_BEFORE.has(prevSignificant) || REGEX_KEYWORDS.has(prevWord);
      if (regex) {
        let j = i + 1, inClass = false;
        while (j < n) {
          const d = source[j];
          if (d === '\\') { j += 2; continue; }
          if (d === '\n') throw new Error(`unterminated regex at offset ${i}`);
          if (inClass) { if (d === ']') inClass = false; }
          else if (d === '[') inClass = true;
          else if (d === '/') break;
          j++;
        }
        if (j >= n) throw new Error(`unterminated regex at offset ${i}`);
        blank(i + 1, j);
        j++;
        while (j < n && /[a-z]/i.test(source[j])) j++;
        prevSignificant = '/'; prevWord = ''; i = j; continue;
      }
      prevSignificant = '/'; prevWord = ''; i++; continue;
    }
    if (c === '{' || c === '[' || c === '(') {
      depth++; top.depth++; prevSignificant = c; prevWord = ''; i++; continue;
    }
    if (c === '}' || c === ']' || c === ')') {
      if (c === '}' && top.templateBrace && top.depth === 0) {
        stack.pop(); depth--; i++; continue; // back into the template text
      }
      depth--; top.depth--;
      if (depth < 0 || top.depth < 0) throw new Error(`unbalanced ${c} at offset ${i}`);
      prevSignificant = c; prevWord = ''; i++; continue;
    }
    if (/[A-Za-z0-9_$]/.test(c)) {
      let j = i;
      while (j < n && /[A-Za-z0-9_$]/.test(source[j])) j++;
      prevWord = source.slice(i, j); prevSignificant = 'w'; i = j; continue;
    }
    prevSignificant = c; prevWord = ''; i++;
  }
  if (stack.length !== 1) throw new Error('unterminated template literal');
  if (depth !== 0) throw new Error(`unbalanced brackets at end of file (depth ${depth})`);
  return { mask: out.join(''), lineStartDepth, lineStartsInCode };
}

/** The declared name(s) and a stable key for one top-level statement. */
export function statementKey(text) {
  const t = text.trimStart();
  let m;
  if ((m = t.match(/^import\s[^;]*?from\s*['"]([^'"]+)['"]/))) return `import:${m[1]}`;
  if ((m = t.match(/^(?:export\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z0-9_$]+)/))) return `function:${m[1]}`;
  if ((m = t.match(/^(?:export\s+)?(const|let|var)\s+([A-Za-z0-9_$]+)/))) return `${m[1]}:${m[2]}`;
  if ((m = t.match(/^\$\('([^']+)'\)\.addEventListener\('([^']+)'/))) return `on:${m[1]}:${m[2]}`;
  if ((m = t.match(/^document\.querySelectorAll\('([^']+)'\)/))) return `doc:${m[1]}`;
  if ((m = t.match(/^window\.addEventListener\('([^']+)'/))) return `window:${m[1]}`;
  if ((m = t.match(/^new\s+([A-Za-z0-9_$]+)/))) return `new:${m[1]}`;
  if (t.startsWith('{')) return 'block';
  return `stmt:${t.slice(0, 40)}`;
}

/**
 * Every binding a top-level statement declares: function names, and each name of `const a=…,b=…`, for every
 * `;`-separated declaration on the statement's lines. Reads the masked text, so strings never count.
 */
export function declaredNames(text, mask) {
  const split = (code, separator) => {
    const pieces = [];
    let depth = 0, start = 0;
    for (let i = 0; i < code.length; i++) {
      const c = code[i];
      if ('([{'.includes(c)) depth++;
      else if (')]}'.includes(c)) depth--;
      else if (depth === 0 && c === separator) { pieces.push(code.slice(start, i)); start = i + 1; }
    }
    pieces.push(code.slice(start));
    return pieces;
  };
  const names = [];
  for (const segment of split(mask, ';')) {
    const t = segment.trimStart();
    let m;
    if ((m = t.match(/^(?:export\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z0-9_$]+)/))) { names.push(m[1]); continue; }
    if (!/^(?:export\s+)?(?:const|let|var)\s/.test(t)) continue;
    const list = t.replace(/^(?:export\s+)?(?:const|let|var)\s+/, '');
    for (const piece of split(list, ',')) {
      const name = piece.trim().match(/^([A-Za-z0-9_$]+)\s*=/);
      if (name) names.push(name[1]);
    }
  }
  return names;
}

/**
 * Splits a module into top-level statements. A statement is one or more whole lines that start at bracket
 * depth 0 in code; comment-only and blank lines before it are its `lead`. Joining every statement's lead and
 * lines reproduces the source exactly (checked).
 */
export function splitJs(source) {
  const { lineStartDepth, lineStartsInCode, mask } = scanJs(source);
  const lines = source.split('\n');
  const maskLines = mask.split('\n');
  if (lineStartDepth.length !== lines.length || maskLines.length !== lines.length) {
    throw new Error(`scanner line count ${lineStartDepth.length} differs from the source's ${lines.length}`);
  }
  const statements = [];
  let lead = [];
  let current = null;
  for (let i = 0; i < lines.length; i++) {
    const blankOrComment = maskLines[i].trim() === '';
    if (current && current.closed) current = null;
    if (!current) {
      // Blank lines and comment lines (including the inside of a multi-line block comment) between statements.
      if (lineStartDepth[i] === 0 && blankOrComment) { lead.push(lines[i]); continue; }
      current = { lead, lines: [], maskLines: [], closed: false };
      lead = [];
      statements.push(current);
    }
    current.lines.push(lines[i]);
    current.maskLines.push(maskLines[i]);
    const nextDepth = i + 1 < lines.length ? lineStartDepth[i + 1] : 0;
    const nextInCode = i + 1 < lines.length ? lineStartsInCode[i + 1] : true;
    if (nextDepth === 0 && nextInCode) current.closed = true;
  }
  const trailing = lead;
  const seen = new Map();
  for (const s of statements) {
    s.text = s.lines.join('\n');
    s.mask = s.maskLines.join('\n');
    const base = statementKey(s.text);
    const k = (seen.get(base) || 0) + 1;
    seen.set(base, k);
    s.key = k === 1 ? base : `${base}#${k}`;
    s.names = declaredNames(s.text, s.mask);
  }
  const rebuilt = [...statements.flatMap(s => [...s.lead, ...s.lines]), ...trailing].join('\n');
  if (rebuilt !== source) throw new Error('statement split does not reproduce the source');
  return { statements, trailing };
}

/** Removes comment-only lines (line and block comments that start a line in code), for the edition copy. */
export function stripCommentLines(source) {
  const { mask, lineStartsInCode } = scanJs(source);
  const lines = source.split('\n'), maskLines = mask.split('\n');
  const keep = [];
  let inBlock = false;
  for (let i = 0; i < lines.length; i++) {
    const code = maskLines[i].trim() !== '';
    const trimmed = lines[i].trim();
    if (inBlock) { if (trimmed.includes('*/')) inBlock = false; continue; }
    if (!code && lineStartsInCode[i] && trimmed.startsWith('/*') && !trimmed.includes('*/')) { inBlock = true; continue; }
    if (!code && lineStartsInCode[i] && trimmed !== '') continue; // a whole-line comment
    keep.push(lines[i]);
  }
  return keep.join('\n');
}

function replaceExact(text, [from, to, expected = 1], where, problems) {
  const found = count(text, from);
  if (found !== expected) {
    problems.push(`${where}: expected ${expected} match(es) of ${short(from)}, found ${found}`);
    return text;
  }
  return text.split(from).join(to);
}

function dropLineRange(lines, { from, through, count: expected = 1 }, where, problems) {
  const starts = lines.map((line, i) => (line.startsWith(from) ? i : -1)).filter(i => i !== -1);
  if (starts.length !== expected) {
    problems.push(`${where}: expected ${expected} line(s) starting ${short(from)}, found ${starts.length}`);
    return lines;
  }
  const drop = new Set();
  for (const start of starts) {
    let end = start;
    if (through !== undefined) {
      end = lines.findIndex((line, i) => i > start && line.startsWith(through));
      if (end === -1) { problems.push(`${where}: no line starting ${short(through)} after ${short(from)}`); return lines; }
    }
    for (let i = start; i <= end; i++) drop.add(i);
  }
  return lines.filter((_, i) => !drop.has(i));
}

/**
 * Applies one file's JavaScript rules. Returns `{text, report, dropped}` where `dropped` lists the bindings of
 * every dropped statement. Collects problems instead of stopping at the first.
 */
export function transformJs(source, rules, file, problems) {
  const { statements, trailing } = splitJs(source);
  const byKey = new Map(statements.map(s => [s.key, s]));
  const known = key => {
    if (byKey.has(key)) return true;
    problems.push(`${file}: no top-level statement ${key} (upstream changed; review the rule)`);
    return false;
  };
  const drop = new Set();
  for (const key of rules.drop || []) if (known(key)) drop.add(key);
  const replaced = new Map();
  for (const [key, rule] of Object.entries(rules.replace || {})) {
    if (!known(key)) continue;
    const actual = sha256(byKey.get(key).text);
    if (actual !== rule.sha256) {
      problems.push(`${file}: ${key} changed upstream (sha256 ${actual.slice(0, 12)}, rule pinned ${rule.sha256.slice(0, 12)}); `
        + 'review the App Store replacement against the new upstream text');
      continue;
    }
    replaced.set(key, rule.text);
  }
  for (const key of Object.keys(rules.dropLines || {})) known(key);
  for (const key of Object.keys(rules.edit || {})) known(key);
  const dropped = [];
  const output = [];
  const report = { statements: statements.length, dropped: 0, replaced: 0, edited: 0, appended: 0 };
  for (const s of statements) {
    if (drop.has(s.key)) { dropped.push(...s.names); report.dropped++; continue; }
    let text = replaced.has(s.key) ? replaced.get(s.key) : s.text;
    if (replaced.has(s.key)) report.replaced++;
    const where = `${file} ${s.key}`;
    if (rules.dropLines?.[s.key]) {
      let lines = text.split('\n');
      for (const range of rules.dropLines[s.key]) lines = dropLineRange(lines, range, where, problems);
      text = lines.join('\n');
    }
    if (rules.edit?.[s.key]) {
      for (const edit of rules.edit[s.key]) text = replaceExact(text, edit, where, problems);
      report.edited++;
    }
    output.push(...s.lead, text);
  }
  for (const text of rules.append || []) { output.push(text); report.appended++; }
  output.push(...trailing);
  let text = output.join('\n');
  if (rules.stripComments) text = stripCommentLines(text);
  return { text, report, dropped };
}

/** Names exported by a module (functions, consts). */
export function exportedNames(source) {
  const names = new Set();
  for (const m of source.matchAll(/^export\s+(?:async\s+)?(?:function\s*\*?|const|let|var)\s+([A-Za-z0-9_$]+)/gm)) names.add(m[1]);
  for (const m of source.matchAll(/^export\s*\{([^}]*)\}/gm)) {
    for (const part of m[1].split(',')) { const name = part.trim().split(/\s+as\s+/).pop(); if (name) names.add(name); }
  }
  return names;
}

/**
 * Rewrites every `import {a,b} from '/x.mjs';` in `source` to the names that the edition's x.mjs still exports
 * and that the rest of the module still uses. A used name that is no longer exported is a problem (the module
 * would fail to link). An import whose module is not in the edition is a problem too.
 */
export function pruneImports(source, modules, file, problems) {
  const { statements, trailing } = splitJs(source);
  const rest = statements.filter(s => !s.key.startsWith('import:')).map(s => s.mask).join('\n');
  const used = name => new RegExp(`(?<![A-Za-z0-9_$.])${name.replace(/\$/g, '\\$')}(?![A-Za-z0-9_$])`).test(rest);
  let pruned = 0;
  for (const s of statements) {
    if (!s.key.startsWith('import:')) continue;
    const m = s.text.match(/^import\s*\{([^}]*)\}\s*from\s*(['"])([^'"]+)\2\s*;?\s*$/);
    const target = s.key.slice('import:'.length).replace(/^\//, '');
    if (!m) { problems.push(`${file}: cannot parse ${s.key}`); continue; }
    if (!modules.has(target)) { problems.push(`${file}: imports ${target}, which the edition does not ship`); continue; }
    const exported = exportedNames(modules.get(target));
    const names = m[1].split(',').map(x => x.trim()).filter(Boolean);
    const keep = [];
    for (const name of names) {
      const isUsed = used(name);
      if (isUsed && !exported.has(name)) problems.push(`${file}: uses ${name} from ${target}, which the edition no longer exports`);
      if (isUsed && exported.has(name)) keep.push(name); else pruned++;
    }
    s.text = keep.length ? `import {${keep.join(',')}} from '/${target}';` : '';
  }
  const text = [...statements.flatMap(s => (s.text === '' ? s.lead : [...s.lead, s.text])), ...trailing].join('\n');
  return { text, pruned };
}

/** Bindings dropped from a module that its remaining code still names (a ReferenceError waiting to happen). */
export function danglingReferences(source, droppedNames, file) {
  const { mask } = scanJs(source);
  const problems = [];
  for (const name of new Set(droppedNames)) {
    const re = new RegExp(`(?<![A-Za-z0-9_$.])${name.replace(/\$/g, '\\$')}(?![A-Za-z0-9_$])`);
    // `{name:…}` object keys are not references.
    const hits = mask.split('\n').filter(line => re.test(line.replace(new RegExp(`${name}\\s*:`, 'g'), ' ')));
    if (hits.length) problems.push(`${file}: dropped ${name} is still referenced: ${short(hits[0].trim())}`);
  }
  return problems;
}

// ---------------------------------------------------------------------------------------------------------
// HTML

function elementEnd(html, start, tag) {
  const open = new RegExp(`<${tag}(?=[\\s>/])`, 'g');
  const close = new RegExp(`</${tag}\\s*>`, 'g');
  const firstClose = html.indexOf('>', start);
  if (firstClose === -1) return -1;
  if (/^(input|img|br|hr|meta|link|source|wbr)$/i.test(tag) || html[firstClose - 1] === '/') return firstClose + 1;
  let depth = 1, pos = firstClose + 1;
  for (;;) {
    open.lastIndex = pos; close.lastIndex = pos;
    const o = open.exec(html), c = close.exec(html);
    if (!c) return -1;
    if (o && o.index < c.index) { depth++; pos = o.index + 1; continue; }
    depth--; pos = c.index + c[0].length;
    if (depth === 0) return pos;
  }
}

/** Finds the one element whose start tag begins with `startTag` (exact text, e.g. `<div class="x"`). */
function findElement(html, startTag, where, problems) {
  const hits = count(html, startTag);
  if (hits !== 1) { problems.push(`${where}: expected 1 element starting ${short(startTag)}, found ${hits}`); return null; }
  const start = html.indexOf(startTag);
  const tag = startTag.match(/^<([a-zA-Z0-9]+)/)?.[1];
  const end = tag ? elementEnd(html, start, tag) : -1;
  if (end === -1) { problems.push(`${where}: element ${short(startTag)} has no end tag`); return null; }
  return { start, end, openEnd: html.indexOf('>', start) + 1, tag };
}

function findById(html, id, where, problems) {
  const re = new RegExp(`<([a-zA-Z0-9]+)\\s[^>]*(?<![-\\w])id="${id}"`, 'g');
  const hits = [...html.matchAll(re)];
  if (hits.length !== 1) { problems.push(`${where}: expected 1 element with id ${id}, found ${hits.length}`); return null; }
  const start = hits[0].index, tag = hits[0][1];
  const end = elementEnd(html, start, tag);
  if (end === -1) { problems.push(`${where}: element #${id} has no end tag`); return null; }
  return { start, end, openEnd: html.indexOf('>', start) + 1, tag };
}

export function transformHtml(source, rules, file, problems) {
  let html = source;
  const where = file;
  for (const id of rules.removeIds || []) {
    const el = findById(html, id, where, problems);
    if (el) html = html.slice(0, el.start) + html.slice(el.end);
  }
  for (const startTag of rules.removeElements || []) {
    const el = findElement(html, startTag, where, problems);
    if (el) html = html.slice(0, el.start) + html.slice(el.end);
  }
  for (const [id, inner] of Object.entries(rules.replaceContent || {})) {
    const el = findById(html, id, where, problems);
    if (el) html = html.slice(0, el.openEnd) + inner + html.slice(el.end - `</${el.tag}>`.length);
  }
  for (const edit of rules.edit || []) html = replaceExact(html, edit, where, problems);
  return html;
}

export const htmlIds = html => new Set([...html.matchAll(/(?<![-\w])id="([^"]+)"/g)].map(m => m[1]));

// ---------------------------------------------------------------------------------------------------------
// CSS

/** Splits CSS into items: {kind:'text'|'comment'|'rule'|'at', text, selector?, body?, prelude?}. */
function splitCss(css) {
  const items = [];
  let i = 0;
  const n = css.length;
  while (i < n) {
    if (/\s/.test(css[i])) { let j = i; while (j < n && /\s/.test(css[j])) j++; items.push({ kind: 'text', text: css.slice(i, j) }); i = j; continue; }
    if (css.startsWith('/*', i)) {
      const j = css.indexOf('*/', i + 2);
      if (j === -1) throw new Error('unterminated CSS comment');
      items.push({ kind: 'comment', text: css.slice(i, j + 2) }); i = j + 2; continue;
    }
    // prelude up to '{' (or ';' for a statement at-rule)
    let j = i, quote = null, paren = 0;
    while (j < n) {
      const c = css[j];
      if (quote) { if (c === '\\') j++; else if (c === quote) quote = null; }
      else if (c === '"' || c === "'") quote = c;
      else if (c === '(') paren++;
      else if (c === ')') paren--;
      else if (paren === 0 && (c === '{' || c === ';')) break;
      j++;
    }
    if (j >= n) { items.push({ kind: 'text', text: css.slice(i) }); break; }
    const prelude = css.slice(i, j);
    if (css[j] === ';') { items.push({ kind: 'text', text: css.slice(i, j + 1) }); i = j + 1; continue; }
    // matching '}'
    let k = j + 1, depth = 1; quote = null;
    while (k < n && depth > 0) {
      const c = css[k];
      if (quote) { if (c === '\\') k++; else if (c === quote) quote = null; }
      else if (css.startsWith('/*', k)) { const e = css.indexOf('*/', k + 2); k = e === -1 ? n : e + 1; }
      else if (c === '"' || c === "'") quote = c;
      else if (c === '{') depth++;
      else if (c === '}') depth--;
      k++;
    }
    if (depth !== 0) throw new Error(`unbalanced CSS braces after ${short(prelude)}`);
    const body = css.slice(j + 1, k - 1);
    items.push(prelude.trimStart().startsWith('@')
      ? { kind: 'at', prelude, body, text: css.slice(i, k) }
      : { kind: 'rule', selector: prelude, body, text: css.slice(i, k) });
    i = k;
  }
  return items;
}

function splitSelectors(selector) {
  const parts = [];
  let depth = 0, start = 0;
  for (let i = 0; i < selector.length; i++) {
    const c = selector[i];
    if (c === '(' || c === '[') depth++;
    else if (c === ')' || c === ']') depth--;
    else if (c === ',' && depth === 0) { parts.push(selector.slice(start, i)); start = i + 1; }
  }
  parts.push(selector.slice(start));
  return parts;
}

/**
 * Removes every selector matching one of `patterns` (RegExps), every rule left with no selector, every
 * @media/@supports block left empty, @keyframes whose name matches, and (optionally) every comment.
 */
export function pruneCss(css, { patterns = [], keyframes = [], stripComments = true } = {}) {
  const stats = { selectorsRemoved: 0, rulesRemoved: 0, blocksRemoved: 0 };
  const walk = text => {
    const items = splitCss(text);
    const out = [];
    for (const item of items) {
      if (item.kind === 'comment') { if (!stripComments) out.push(item.text); continue; }
      if (item.kind === 'rule') {
        const selectors = splitSelectors(item.selector);
        const kept = selectors.filter(s => !patterns.some(p => p.test(s)));
        stats.selectorsRemoved += selectors.length - kept.length;
        if (!kept.length) { stats.rulesRemoved++; continue; }
        const selector = kept.length === selectors.length ? item.selector : kept.map(s => s.trim()).join(',');
        out.push(`${selector}{${item.body}}`);
        continue;
      }
      if (item.kind === 'at') {
        const name = item.prelude.trim().match(/^@([a-z-]+)/i)?.[1]?.toLowerCase();
        if (name === 'keyframes' || name === '-webkit-keyframes') {
          const frames = item.prelude.trim().split(/\s+/)[1];
          if (keyframes.some(p => p.test(frames))) { stats.blocksRemoved++; continue; }
          out.push(item.text); continue;
        }
        if (name === 'media' || name === 'supports') {
          const inner = walk(item.body);
          if (inner.trim() === '') { stats.blocksRemoved++; continue; }
          out.push(`${item.prelude}{${inner}}`); continue;
        }
        out.push(item.text); continue;
      }
      out.push(item.text);
    }
    return out.join('');
  };
  const text = walk(css).replace(/\n{3,}/g, '\n\n');
  return { text, stats };
}

// ---------------------------------------------------------------------------------------------------------
// Checks

/** `$('id')` references in app code that the page does not define. */
export function missingIds(js, html) {
  const ids = htmlIds(html);
  const refs = new Set([...js.matchAll(/\$\('([A-Za-z0-9_-]+)'\)/g)].map(m => m[1]));
  return [...refs].filter(id => !ids.has(id)).sort();
}

/** Lines of `text` matching any denylist entry: [{pattern, line, text}]. */
export function denylistHits(text, denylist) {
  const hits = [];
  text.split('\n').forEach((line, i) => {
    for (const entry of denylist) if (entry.re.test(line)) hits.push({ pattern: entry.label, line: i + 1, text: line });
  });
  return hits;
}
