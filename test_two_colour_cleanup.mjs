import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const css = readFileSync(new URL('./web/jaryo.css', import.meta.url), 'utf8');
const components = readFileSync(new URL('./web/components.css', import.meta.url), 'utf8');
const style = readFileSync(new URL('./web/style.css', import.meta.url), 'utf8');
const tail = css.slice(css.indexOf('/* Two-colour and square-corner stragglers.'));

test('the Components page rules that carry literal colours are overridden with tokens', () => {
  // The literals are in components.css; Jaryo must win for each of them.
  for (const literal of ['#243040', '#c0d9c9', '#cddbf0', '#9ca8b7']) assert.ok(components.includes(literal), `${literal} still in components.css`);
  assert.match(tail, /body\.jaryo-app dl,\s*body\.jaryo-app details \{ border-color: var\(--j-border\); \}/);
  assert.match(tail, /body\.jaryo-app #result,\s*body\.jaryo-app footer \{ color: var\(--j-text\); \}/);
  const pre = tail.slice(tail.indexOf('body.jaryo-app pre#command'));
  assert.match(pre.slice(0, pre.indexOf('}')), /color: var\(--j-text\);\s*background: var\(--j-bg\);\s*border: 1px solid var\(--j-border\);\s*border-radius: 0;/);
});

test('every toolbar and panel control that style.css rounds is squared again', () => {
  for (const sel of ['.tools .mode-tab', '.tools .fix-tab', '.tools .pc-switch', '.tools .pc-switch-track', '.tools #pause', '.tools #expand', '.auto-unload-control', '#autoUnloadToggle', '.map-controls', '#memoryBanner']) {
    assert.ok(tail.includes(`body.jaryo-app ${sel}`), sel);
  }
  assert.match(tail, /#memoryBanner \{ border-radius: 0; \}/);
  // The status dots stay round: filled and hollow circles are how state is drawn, so they must not be in the squaring list.
  assert.doesNotMatch(tail.slice(tail.indexOf('body.jaryo-app .tools .mode-tab')), /\.mode-tab i|\.vital i/);
  assert.match(style, /\.tools \.mode-tab,\.tools \.fix-tab,\.tools \.pc-switch\{border-radius:0/);
});

test('the block uses tokens only', () => {
  assert.doesNotMatch(tail, /#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(/);
});
