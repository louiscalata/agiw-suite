// Screen contract for the Jaryo faces. Tokens stay in jaryo/; this checks the pages that consume them.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');

const SCREENS = [
  ['Overview', './web/index.html', '/'],
  ['Map', './web/monitor.html', '/monitor'],
  ['Machines', './web/machines.html', '/machines'],
  ['Components', './web/components.html', '/components'],
  ['Identity', './web/identity.html', '/identity'],
  ['Setup', './web/setup.html', '/setup'],
];

const NAV = [
  ['/', 'Overview'],
  ['/monitor', 'Map'],
  ['/machines', 'Machines'],
  ['/components', 'Components'],
  ['/identity', 'Identity'],
  ['/setup', 'Setup'],
];

function navLinks(html) {
  const nav = html.slice(html.indexOf('<nav'), html.indexOf('</nav>'));
  return [...nav.matchAll(/<a href="([^"]+)"[^>]*>([^<]+)<\/a>/g)].map(match => [match[1], match[2].trim()]);
}

test('every screen uses the same Jaryo chrome', () => {
  for (const [name, path, href] of SCREENS) {
    const html = read(path);
    assert.match(html, /class="jaryo-app"/, name);
    assert.match(html, /href="\/jaryo\.css"/, name);
    assert.match(html, /src="\/jaryo\.mjs"/, name);
    assert.match(html, /href="\/jaryo\/marks\/favicon\.svg"/, name);
    assert.match(html, /<a class="j-skip" href="#content">Skip to content<\/a>/, name);
    assert.match(html, /id="content"/, name);
    assert.match(html, /id="jaryoTheme"/, name);
    assert.match(html, /<option value="system">System<\/option>/, name);
    assert.doesNotMatch(html, /hermes|Hermes|graphite|copper/i, name);
    assert.doesNotMatch(html, /<img[^>]+agiw|brand-monogram|>AS</, name);
    const links = navLinks(html);
    assert.deepEqual(links, NAV, name);
    const current = html.match(new RegExp(`<a href="${href.replace('/', '\\/')}"[^>]*>`));
    assert.match(current[0], /aria-current="page"/, name);
  }
});

test('the wordmark is type, and demo copy is labeled', () => {
  const overview = read('./web/index.html');
  assert.match(overview, /<strong class="j-type">AGIW<\/strong><small>Inference Monitor<\/small>/);
  for (const path of ['./web/monitor.html', './web/machines.html', './web/components.html', './web/identity.html', './web/setup.html']) {
    assert.match(read(path), /<strong class="j-type">AGIW<\/strong>/, path);
  }
  assert.match(read('./web/identity.html'), /The name is the identity/);
  assert.match(overview, /id="jaryoDemo"/);
  assert.match(overview, /DEMO · fictional monitors · not a live observation/);
  assert.match(read('./web/monitor.html'), /id="jaryoDemo"/);
  assert.match(read('./web/monitor.html'), /Reading the monitor feed\./);
});

test('empty machine rows and setup states stay words, not invented readings', () => {
  const machines = read('./web/machines.mjs');
  assert.match(machines, /No machine rows in this sample\. A missing read stays unknown\./);
  assert.match(machines, /The snapshot could not be read, so every machine stays unknown\./);
  const setup = read('./web/setup.mjs');
  assert.match(setup, /UNKNOWN: ' j-unknown'/);
  assert.match(setup, /FAILED: ' j-blocked'/);
  assert.match(setup, /OPTIONAL: ' j-unavailable'/);
});

test('the shell keeps a desktop sidebar, a cover width, and reduced motion', () => {
  const css = read('./web/jaryo.css');
  assert.match(css, /grid-row: 2 \/ span 8/);
  assert.match(css, /grid-template-rows: auto auto minmax\(0, 1fr\) auto/);
  assert.match(css, /@media \(max-width: 22\.5rem\)/);
  assert.match(css, /prefers-reduced-motion: reduce/);
  assert.match(css, /\.j-skip:focus/);
  assert.match(css, /p\.j-type-sample\.j-plate/);
  assert.match(css, /body\.jaryo-app \.connection \{\s*display: inline;/);
  assert.match(css, /body\.jaryo-app > header \{\s*margin: 0;/);
  assert.match(css, /body\.jaryo-app \.tools #pause::before \{ content: none; \}/);
  assert.match(css, /body\.jaryo-app \.pc-switch-copy \{ display: flex; \}/);
  assert.match(css, /body\.jaryo-app \.pc-switch-copy strong \{ display: block; \}/);
  assert.match(read('./web/simple-view.css'), /@media\(max-width:22\.5rem\)/);
  assert.match(read('./jaryo/primitives.css'), /prefers-reduced-motion: reduce/);
  const edition = read('./wrapper-patch/web-edition/appstore-rules.mjs');
  assert.match(edition, /@media\(max-width:22\.5rem\)/);
});

test('Mac waiting chrome and Android covers stay black and white type', () => {
  const swift = read('./Monitor.swift');
  assert.match(swift, /effectiveAppearance/);
  assert.match(swift, /NSColor\.black/);
  assert.match(swift, /NSColor\.white/);
  assert.doesNotMatch(swift, /calibratedWhite: 0\.72/);
  for (const path of [
    './android/app/src/main/res/values/colors.xml',
    './android/app/src/main/res/values-night/colors.xml',
  ]) {
    const colors = read(path);
    const hexes = colors.match(/#[0-9A-Fa-f]{3,8}/g) || [];
    assert.deepEqual([...new Set(hexes)].sort(), ['#000000', '#FFFFFF'].sort(), path);
  }
  const cover = read('./android/app/src/main/res/layout/activity_cover.xml');
  const inner = read('./android/app/src/main/res/layout/activity_inner.xml');
  const widget = read('./android/app/src/main/res/layout/widget_status.xml');
  for (const layout of [cover, inner, widget]) assert.match(layout, /text="AGIW"/);
  assert.ok(widget.indexOf('widgetAcceptance') < widget.indexOf('widgetDetail'), 'the acceptance line stays above the detail that can clip');
  assert.match(widget, /android:maxLines="3"/);
  assert.match(cover, /android:textColor="@color\/agiw_ink"/);
});
