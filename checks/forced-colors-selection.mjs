// A selected control is marked only by the Highlight system colour, or by a
// painted difference from an unselected sibling. A transparent border has a
// width and is not a mark.

const TRANSPARENT = new Set(['', 'transparent', 'rgba(0,0,0,0)']);

function norm(color) {
  return String(color ?? '').replace(/\s+/g, '').toLowerCase();
}

function transparent(color) {
  return TRANSPARENT.has(norm(color));
}

const PAINT_KEYS = [
  'backgroundColor',
  'color',
  'borderTopColor',
  'borderRightColor',
  'borderBottomColor',
  'borderLeftColor',
];

export function selectionIsMarked(selected, sibling, system = {}) {
  const highlight = norm(system.highlight);
  if (highlight) {
    for (const key of PAINT_KEYS) {
      if (norm(selected[key]) === highlight && norm(selected[key]) !== norm(sibling[key])) return true;
    }
  }
  for (const key of ['backgroundColor', 'color']) {
    if (!transparent(selected[key]) && norm(selected[key]) !== norm(sibling[key])) return true;
  }
  for (const side of ['Top', 'Right', 'Bottom', 'Left']) {
    const width = parseFloat(selected[`border${side}Width`]) || 0;
    const color = selected[`border${side}Color`];
    if (width > 0 && !transparent(color) && norm(color) !== norm(sibling[`border${side}Color`])) return true;
  }
  const outlineWidth = parseFloat(selected.outlineWidth) || 0;
  if (selected.outlineStyle && selected.outlineStyle !== 'none' && outlineWidth > 0 && !transparent(selected.outlineColor)) {
    const siblingOutline = sibling.outlineStyle && sibling.outlineStyle !== 'none'
      && (parseFloat(sibling.outlineWidth) || 0) > 0
      && !transparent(sibling.outlineColor);
    if (!siblingOutline || norm(selected.outlineColor) !== norm(sibling.outlineColor) || selected.outlineStyle !== sibling.outlineStyle) {
      return true;
    }
  }
  return false;
}

export function focusIsMarked(style) {
  const outlineWidth = parseFloat(style.outlineWidth) || 0;
  return Boolean(style.outlineStyle && style.outlineStyle !== 'none' && outlineWidth > 0 && !transparent(style.outlineColor));
}
