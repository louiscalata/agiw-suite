// Presentation behavior tested with the existing DOM double; no browser is involved.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import {mountChoicePipeline} from './jaryo/interactions.mjs';

const choices = readFileSync(new URL('./test_jaryo_choices.mjs', import.meta.url), 'utf8');
const harness = choices.slice(choices.indexOf('class Element {'), choices.indexOf('const sample='));
const dom = runInNewContext(`${harness}\ndom;`, {DOMException});
const patterns = readFileSync(new URL('./jaryo/patterns.css', import.meta.url), 'utf8');
const web = readFileSync(new URL('./web/jaryo.css', import.meta.url), 'utf8');
const px = value => Number(/^(\d+)px$/.exec(value)[1]);
const steps = [{id:'one', name:'One', word:'OK', inputs:[]}];

for (const width of [260, 700, 701, 1199, 1200]) {
  test(`inspector uses ${width < 1200 ? 'a sheet' : 'a side column'} at ${width}px`, () => {
    const {doc, host} = dom(false);
    const matchMedia = doc.defaultView.matchMedia;
    // The double answers from the width and the query's own max-width, so the breakpoint in the source decides the case.
    doc.defaultView.matchMedia = query => {
      const max = Number(/^\(max-width: (\d+)px\)$/.exec(query)?.[1]);
      assert.ok(Number.isFinite(max), `unexpected media query ${query}`);
      const media = matchMedia(query);
      media.matches = width <= max;
      return media;
    };
    mountChoicePipeline(host, {steps});
    const inspector = host.querySelector('.jp-inspector');
    host.querySelector('.jp-step').click();
    assert.equal(inspector.open, true);
    assert.equal(inspector.modal, width < 1200);
  });
}

test('sheet and phone thumb bar have separate presentation breakpoints', () => {
  assert.match(patterns, /@media \(max-width: 1199px\)\s*\{\s*\.jp-workbench\s*\{\s*grid-template-columns:\s*minmax\(0, 1fr\)/);
  assert.match(patterns, /@media \(max-width: 1199px\)[\s\S]*?\.jp-inspector\[open\]\s*\{[^}]*position:\s*fixed/);
  const phone = patterns.slice(patterns.indexOf('@media (max-width: 700px)'), patterns.indexOf('@media (prefers-reduced-motion'));
  assert.match(phone, /\.jp-pager\s*\{[^}]*position:\s*fixed/);
  assert.doesNotMatch(patterns.slice(patterns.indexOf('@media (max-width: 1199px)'), patterns.indexOf('@media (max-width: 700px)')), /\.jp-pager\s*\{/);
});

test('the phone pager clears the bottom nav on the page and sits on the bottom edge inside the modal sheet', () => {
  const start = web.indexOf('@media (max-width:700px) {\n  /* --j-nav-clear');
  assert.ok(start > 0, 'the bottom-nav block is present');
  const phone = web.slice(start, web.indexOf('\n}\n', start));
  const clear = /body\.jaryo-app \{ --j-nav-clear:calc\((\d+px) \+ env\(safe-area-inset-bottom,0px\)\);/.exec(phone);
  const navHeight = /body\.jaryo-app > \.j-nav \{[^}]*min-height:(\d+px);/.exec(phone);
  assert.equal(px(clear[1]), px(navHeight[1]), 'the reserved clearance is the nav height');
  assert.match(phone, /body\.jaryo-app \.jp-pager \{ bottom:var\(--j-nav-clear\); \}/);
  const inPage = phone.indexOf('body.jaryo-app .jp-pager {');
  const inSheet = phone.indexOf('body.jaryo-app .jp-inspector[open] .jp-pager { bottom:0; }');
  assert.ok(inSheet > inPage, 'the sheet rule follows and out-specifies the page rule');
  // Inside the sheet the pager is 0..thumb-bar from the bottom and the sheet reserves more than that.
  const sheet = /\.jp-inspector\[open\] \{[^}]*padding:[^;]*calc\(var\(--j-thumb-bar\) \+ (\d+px) \+ env\(safe-area-inset-bottom\)\)/
    .exec(patterns.slice(patterns.indexOf('@media (max-width: 700px)')));
  assert.ok(px(sheet[1]) > 0, 'sheet content ends above the pager');
  assert.match(patterns.slice(patterns.indexOf('@media (max-width: 700px)')),
    /\.jp-pager \{[^}]*inset: auto 0 0;[^}]*height: calc\(var\(--j-thumb-bar\) \+ env\(safe-area-inset-bottom\)\)/);
  assert.doesNotMatch(web, /\.jp-pager \{ bottom:calc\(56px/, 'no pager offset bypasses the shared clearance');
});

test('on phones the pager is inside the sheet while it is modal and back in the page when it closes', () => {
  const {doc, host} = dom(true);
  mountChoicePipeline(host, {steps});
  const inspector = host.querySelector('.jp-inspector');
  const pager = host.querySelector('.jp-pager');
  assert.equal(pager.parentElement, host);
  host.querySelector('.jp-step').click();
  assert.equal(inspector.modal, true);
  assert.equal(pager.parentElement, inspector, 'the [open] .jp-pager rule applies exactly while the sheet is modal');
});
