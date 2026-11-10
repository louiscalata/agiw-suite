import test from 'node:test';
import assert from 'node:assert/strict';
import { focusIsMarked, selectionIsMarked } from './checks/forced-colors-selection.mjs';

function box(overrides = {}) {
  return {
    backgroundColor: 'rgba(0, 0, 0, 0)',
    color: 'canvastext',
    borderTopWidth: '0px',
    borderRightWidth: '0px',
    borderBottomWidth: '2px',
    borderLeftWidth: '0px',
    borderTopColor: 'transparent',
    borderRightColor: 'transparent',
    borderBottomColor: 'transparent',
    borderLeftColor: 'transparent',
    outlineStyle: 'none',
    outlineWidth: '0px',
    outlineColor: 'transparent',
    ...overrides,
  };
}

test('a transparent 2px border does not mark a selected nav link', () => {
  const selected = box();
  const sibling = box();
  assert.equal(selectionIsMarked(selected, sibling, { highlight: 'rgb(0, 0, 128)' }), false);
});

test('Highlight that differs from the unselected sibling is a mark', () => {
  const selected = box({ backgroundColor: 'rgb(0, 0, 128)', color: 'rgb(255, 255, 255)' });
  const sibling = box({ backgroundColor: 'rgb(255, 255, 255)', color: 'rgb(0, 0, 0)' });
  assert.equal(selectionIsMarked(selected, sibling, { highlight: 'rgb(0, 0, 128)' }), true);
});

test('an opaque border that differs from the sibling is a mark', () => {
  const selected = box({ borderLeftWidth: '2px', borderLeftColor: 'rgb(0, 0, 0)' });
  const sibling = box({ borderLeftWidth: '2px', borderLeftColor: 'transparent' });
  assert.equal(selectionIsMarked(selected, sibling, {}), true);
});

test('the same opaque border on both controls is not a selection mark', () => {
  const selected = box({ borderBottomColor: 'rgb(0, 0, 0)' });
  const sibling = box({ borderBottomColor: 'rgb(0, 0, 0)' });
  assert.equal(selectionIsMarked(selected, sibling, { highlight: 'rgb(0, 0, 128)' }), false);
});

test('focus requires a visible outline, not a text colour', () => {
  assert.equal(focusIsMarked(box({ color: 'rgb(0, 0, 0)' })), false);
  assert.equal(focusIsMarked(box({
    outlineStyle: 'dashed',
    outlineWidth: '3px',
    outlineColor: 'rgb(0, 0, 0)',
  })), true);
});
