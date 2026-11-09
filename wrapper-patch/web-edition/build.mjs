#!/usr/bin/env node
// Builds the App Store edition of the shared web UI from a directory of synced upstream files.
//
//   node scripts/web-edition/build.mjs <upstream-dir> <out-dir>
//
// <upstream-dir> holds the nine synced files (flat). <out-dir> must be empty or missing; the edition's eight
// files are written there only when every rule applied and every check passed:
//   - the rules (appstore-rules.mjs) matched exactly (missing statements, anchor counts, sha256 pins);
//   - no dropped binding is still referenced, and every import names a function the edition still exports;
//   - every $('id') in app.js exists in index.html, and index.html loads only shipped files;
//   - no line of any shipped file matches feature-denylist.txt (release gate G3);
//   - `node --check` accepts every JavaScript file.
// Exit 0 and a one-line summary per file on success; exit 1 with every problem listed otherwise.
import { readFileSync, writeFileSync, mkdirSync, readdirSync, existsSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  EditionError, transformJs, transformHtml, pruneCss, pruneImports, danglingReferences, missingIds, denylistHits,
} from './engine.mjs';
import * as rules from './appstore-rules.mjs';
import { adaptEdition } from './jaryo-adapt.mjs';

const here = dirname(fileURLToPath(import.meta.url));

export function loadDenylist(file = join(here, 'feature-denylist.txt')) {
  // Drop a checkout CR before the tab split. Otherwise the last alternative, or the whole
  // pattern, includes that CR and a line ending in LF no longer matches.
  return readFileSync(file, 'utf8').split('\n').map(line => line.endsWith('\r') ? line.slice(0, -1) : line).filter(line => line.trim() && !line.startsWith('#')).map(line => {
    const [flags, label, pattern] = line.split('\t');
    if (!pattern) throw new Error(`feature-denylist.txt: malformed line ${JSON.stringify(line)}`);
    return { label, re: new RegExp(pattern, flags.includes('i') ? 'i' : '') };
  });
}

/** Applies the App Store rules to the upstream files. Returns {files, reports}; throws EditionError. */
export function buildEdition(upstream, { denylist = loadDenylist() } = {}) {
  adaptEdition(rules, upstream);
  const problems = [];
  const read = name => {
    if (!upstream.has(name)) { problems.push(`upstream file ${name} is missing`); return ''; }
    return upstream.get(name);
  };
  const files = new Map();
  const reports = {};
  const dropped = {};
  for (const name of rules.ship) {
    const source = read(name);
    if (rules.html[name]) {
      files.set(name, transformHtml(source, rules.html[name], name, problems));
    } else if (rules.css[name]) {
      const { text, stats } = pruneCss(source, rules.css[name]);
      files.set(name, `${text.trimEnd()}\n${rules.css[name].append ? `${rules.css[name].append}\n` : ''}`);
      reports[name] = stats;
    } else if (rules.js[name]) {
      try {
        const { text, report, dropped: names } = transformJs(source, rules.js[name], name, problems);
        files.set(name, text);
        reports[name] = report;
        dropped[name] = names;
      } catch (error) {
        problems.push(`${name}: ${error.message}`);
      }
    } else {
      files.set(name, source);
    }
  }
  if (problems.length) throw new EditionError(problems);

  // Imports: keep what the shipped modules export and app.js still uses.
  const modules = new Map([...files].filter(([name]) => name.endsWith('.mjs')));
  for (const [name, text] of [...files]) {
    if (!/\.(m?js)$/.test(name)) continue;
    const { text: pruned, pruned: count } = pruneImports(text, modules, name, problems);
    files.set(name, pruned);
    if (reports[name]) reports[name].importsPruned = count;
  }
  for (const [name, names] of Object.entries(dropped)) problems.push(...danglingReferences(files.get(name), names, name));

  const index = files.get('index.html');
  for (const id of missingIds(files.get('app.js'), index)) problems.push(`app.js: $('${id}') has no element in index.html`);
  for (const m of index.matchAll(/(?:src|href)="\/([^"#?]+)"/g)) {
    if (!rules.ship.includes(m[1])) problems.push(`index.html loads /${m[1]}, which the edition does not ship`);
  }
  for (const [name, text] of files) {
    for (const hit of denylistHits(text, denylist)) {
      problems.push(`${name}:${hit.line}: private-edition feature (${hit.pattern}): ${JSON.stringify(hit.text.trim().slice(0, 100))}`);
    }
  }
  if (problems.length) throw new EditionError(problems);
  return { files, reports };
}

function main(argv) {
  const [src, out] = argv;
  if (!src || !out) {
    console.error('usage: node scripts/web-edition/build.mjs <upstream-dir> <out-dir>');
    return 2;
  }
  if (existsSync(out) && readdirSync(out).length) {
    console.error(`web-edition: ${out} is not empty`);
    return 2;
  }
  const upstream = new Map();
  for (const name of readdirSync(src)) upstream.set(name, readFileSync(join(src, name), 'utf8'));
  let built;
  try {
    built = buildEdition(upstream);
  } catch (error) {
    if (!(error instanceof EditionError)) throw error;
    console.error(`web-edition: FAILED\n- ${error.problems.join('\n- ')}`);
    return 1;
  }
  mkdirSync(out, { recursive: true });
  for (const [name, text] of built.files) writeFileSync(join(out, name), text);
  const failures = [];
  for (const name of built.files.keys()) {
    if (!/\.(m?js)$/.test(name)) continue;
    try {
      execFileSync(process.execPath, ['--check', join(out, name)], { stdio: 'pipe' });
    } catch (error) {
      failures.push(`${name}: node --check: ${String(error.stderr || error.message).trim().split('\n')[0]}`);
    }
  }
  if (failures.length) {
    console.error(`web-edition: FAILED\n- ${failures.join('\n- ')}`);
    return 1;
  }
  for (const [name, text] of built.files) {
    const r = built.reports[name];
    const detail = !r ? 'unchanged' : 'statements' in r
      ? `${r.statements} statements: ${r.dropped} dropped, ${r.replaced} replaced, ${r.edited} edited, ${r.appended} appended, ${r.importsPruned ?? 0} imports pruned`
      : `css: ${r.selectorsRemoved} selectors, ${r.rulesRemoved} rules, ${r.blocksRemoved} blocks removed`;
    const upstreamBytes = upstream.get(name)?.length ?? 0;
    console.log(`web-edition: ${name} ${upstreamBytes} -> ${text.length} bytes (${detail})`);
  }
  console.log(`web-edition: appstore edition OK (${built.files.size} files; not shipped: ${[...upstream.keys()].filter(n => !built.files.has(n)).join(', ') || 'none'})`);
  return 0;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) process.exit(main(process.argv.slice(2)));
