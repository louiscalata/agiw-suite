// Readings are one rule. The BLOCKED line is not set on a hatch, and the
// typeface notes live on the identity page rather than above the components wordmark.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { cascadeStyle } from './test_support/css-cascade.mjs';

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');

test('blocked and unknown steps keep a left hatch strip outside their text', () => {
  const css = read('./jaryo/patterns.css');
  assert.match(css, /\.jp-step\[data-state="BLOCKED"\] \{[^}]*border-left: 6px double var\(--j-status-blocked\)/);
  assert.match(css, /width: var\(--j-unknown-hatch\); background: repeating-linear-gradient/);
  assert.match(css, /padding-left: calc\(var\(--j-unknown-hatch\) \+ 16px\)/);
  assert.match(css, /\.jp-step\[data-state="UNKNOWN"\] \{ border-style: dashed;/);
  assert.match(css, /aria-pressed="true"\]\[data-state="BLOCKED"\] \{ border-left-color: var\(--j-sel-blocked\)/);
});

test('typeface notes are on the identity page, and that page records the mark', () => {
  const components = read('./web/components.html');
  const identity = read('./web/identity.html');
  assert.doesNotMatch(components, /Typeface notes/);
  assert.match(components, /<main id="content" class="j-page" tabindex="-1">\s*<header>/);
  assert.match(identity, /Typeface notes/);
  assert.match(identity, /TYPE ONLY/);
  assert.match(identity, /The name is the identity/);
  assert.match(identity, /D4 Split is rejected/);
  assert.match(identity, /Inter/);
  assert.match(identity, /The favicon is the letters AG/);
  assert.doesNotMatch(identity, /sampleHeading|class="j-type j-poster"/);
  assert.match(identity, /IBM Plex Sans/);
});


test('legacy blocked and matrix readings retain their flat rule without hatching',()=>{
  const css=read('./web/jaryo.css');
  assert.doesNotMatch(css,/3px double/);
  assert.match(css,/\.j-blocked \{[^}]*background-image: none;/);
  assert.match(css,/\.j-matrix-blocked \{[^}]*background-image: none;/);
  assert.match(css,/\.j-step \{[^}]*border-bottom: 1px solid var\(--j-border\);/);
  for(const name of ['j-blocked','j-matrix-blocked']) {
    const style=cascadeStyle(css,[{tag:'body',classes:['jaryo-app']},{tag:'li',classes:['j-step',name]}]);
    assert.equal(style['background-image'],'none');
    assert.equal(style['border-bottom-style'],'solid');
    assert.equal(style['border-bottom-width'],'1px');
  }
});
for(const state of ['UNKNOWN','BLOCKED','REVIEW'])test(`web ${state} effective state style survives the base selector`,()=>{
  const css=read('./web/jaryo.css');
  const style=cascadeStyle(css,[{tag:'body',classes:['jaryo-app']},{tag:'button',classes:['jp-step','jk-station'],attrs:{'data-state':state,'aria-pressed':'false'}}]);
  if(state==='UNKNOWN') {
    assert.equal(style['border-top-style'],'dashed');
    assert.equal(style['padding-left'],'calc(var(--j-unknown-hatch) + 16px)');
  } else if(state==='BLOCKED') {
    assert.equal(style['border-left-style'],'double');assert.equal(style['border-left-width'],'6px');
    assert.equal(style['background-color'],'var(--j-blocked-bg)');
    assert.equal(style['padding-left'],'calc(var(--j-unknown-hatch) + 16px)');
  } else assert.equal(style['border-top-width'],'2px');
});
for(const mapClass of ['j-map','graph'])test(`${mapClass} excluded nodes are actually hidden without fading included text`,()=>{
  const css=read('./web/jaryo.css');
  const lineage=[{tag:'body',classes:['jaryo-app']},{tag:'div',classes:[mapClass]}];
  const included=cascadeStyle(css,[...lineage,{tag:'g',classes:['node']}]);
  const excluded=cascadeStyle(css,[...lineage,{tag:'g',classes:['node','is-outside-monitor']}]);
  assert.equal(excluded.display,'none');assert.notEqual(included.display,'none');
  assert.ok(included.opacity===undefined||included.opacity==='1');
});

test('standalone REVIEW keeps its heavier border after the shell base rule',()=>{
  const components=read('./jaryo/components.css');
  assert.match(components,/^@import url\("\.\/patterns\.css"\);/);
  const css=components.replace('@import url("./patterns.css");',read('./jaryo/patterns.css'));
  for(const selected of ['false','true']) {
    const style=cascadeStyle(css,[{tag:'main',classes:['jk-shell']},{tag:'button',classes:['jp-step','jk-station'],attrs:{'data-state':'REVIEW','data-neighbour':'','aria-pressed':selected}}]);
    for(const side of ['top','right','bottom','left']) {
      assert.equal(style[`border-${side}-width`],'2px',`${selected}: ${side} border width`);
      assert.equal(style[`border-${side}-style`],'solid');
      assert.equal(style[`border-${side}-color`],'var(--j-text)');
    }
  }
});
