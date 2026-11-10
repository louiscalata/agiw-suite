// The Overview still loads style.css. These overrides are what keep its panels,
// actions, legend and placeholder on the Jaryo black-and-white tokens.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const css = readFileSync(new URL('./web/jaryo.css', import.meta.url), 'utf8');
const marker = '/* Overview panels.';
const block = css.slice(css.indexOf(marker));

test('the Overview panel override is present and uses tokens only', () => {
  assert.ok(block.length > marker.length);
  assert.doesNotMatch(block, /#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(/);
});

test('light-theme surfaces and actions are named', () => {
  for (const selector of [
    'body.jaryo-app .panel-summary',
    'body.jaryo-app .tile',
    'body.jaryo-app .model-action-dock',
    'body.jaryo-app .sidebar-toggle',
    'body.jaryo-app .evidence-rail .panel-actions #onlineCodeRepair',
    'body.jaryo-app .evidence-rail .panel-actions #drawerFixButton.panel-primary',
    'body.jaryo-app #fixInferenceSubmit',
    'body.jaryo-app #modelAction:disabled',
    'body.jaryo-app .legend .legend-line.recorded',
    'body.jaryo-app .legend .cap-color.embedding',
    'body.jaryo-app .mode-tab.ready i',
    'body.jaryo-app input::placeholder',
  ]) {
    assert.ok(block.includes(selector), selector);
  }
  assert.match(block, /#modelAction:disabled[\s\S]*background: transparent;\s*color: var\(--j-text\);\s*border-style: dashed;/);
  assert.match(block, /input::placeholder \{\s*color: var\(--j-text\);\s*opacity: 1;\s*\}/);
  assert.match(css, /body\.jaryo-app \{[^}]*--sheet: var\(--j-bg\);/);
});
