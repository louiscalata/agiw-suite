import test from 'node:test';
import assert from 'node:assert/strict';
import { chromaOf } from './checks/ink-color.mjs';

const NEAR_GRAY = 'oklab(0.961073 0.0000438988 0.0000191927)';

test('Chromium 131 near-gray oklab strokes stay under the ink threshold', () => {
  const a = 0.0000438988;
  const b = 0.0000191927;
  const scaled = Math.hypot(a, b) / 0.4 * 255;
  const chroma = chromaOf(NEAR_GRAY);
  assert.ok(Number.isFinite(chroma), chroma);
  assert.ok(chroma < 18, chroma);
  assert.ok(Math.abs(chroma - scaled) < 1e-9, chroma);
});

test('oklab and oklch chroma is sqrt(a^2+b^2), scaled like an RGB span', () => {
  const saturated = chromaOf('oklab(0.6 0.2 0.1)');
  assert.ok(saturated > 18, saturated);
  assert.ok(Math.abs(saturated - Math.hypot(0.2, 0.1) / 0.4 * 255) < 1e-9);
  const lch = chromaOf('oklch(70% 0.2 30)');
  assert.ok(lch > 18, lch);
  assert.ok(Math.abs(lch - 0.2 / 0.4 * 255) < 1e-9);
  assert.equal(chromaOf('oklab(0.5 0.1 -0.1 / 0)'), 0);
  assert.equal(chromaOf('oklch(50% 40% 20deg / 0%)'), 0);
});

test('unknown color syntax still fails closed', () => {
  for (const color of ['color-mix(in srgb, red 50%, white)', 'color(srgb 1 0 0)',
    'hsl(20 100% 50%)', 'red', 'rgb(1, 2)', 'rgb(1, 2, 3) junk', 'oklab(0.5, 0.1, 0.1)',
    'oklch(70% 0.2)', 'lab(50% 20 10)']) {
    assert.equal(chromaOf(color), Infinity, color);
  }
});

test('rgb colors retain their chroma and transparent colors are ignored', () => {
  for (const color of ['', 'none', 'transparent', 'rgb(242, 242, 242)',
    'rgb(42 42 42)', 'rgb(40% 40% 40%)', 'rgba(255, 0, 0, 0)', 'rgb(255 0 0 / 0%)']) {
    assert.equal(chromaOf(color), 0, color);
  }
  assert.equal(chromaOf('rgb(255, 20, 0)'), 255);
  assert.equal(chromaOf('rgba(255, 0, 0, 0.5)'), 255);
  assert.equal(chromaOf('rgb(255 0 0 / 50%)'), 255);
});
