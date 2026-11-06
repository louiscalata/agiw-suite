/**
 * Review gallery for five original AGIW marks.
 * It does not write a choice and it does not change the running monitor.
 */
const DIRECTIONS = [
  {
    id: 'd1',
    file: '/jaryo/marks/d1-stamp.svg',
    name: 'D1 Stamp',
    note: 'Provisional candidate on the chrome. A square index with AG drawn as strokes. Not a selected identity.',
  },
  {
    id: 'd2',
    file: '/jaryo/marks/d2-ledger.svg',
    name: 'D2 Ledger',
    note: 'Three rules of different length and a small index. A record, not a status lamp.',
  },
  {
    id: 'd3',
    file: '/jaryo/marks/d3-schematic.svg',
    name: 'D3 Schematic',
    note: 'Corner ticks and a center mark, like a title block. Those dashes are the drawing, not the unknown state.',
  },
  {
    id: 'd4',
    file: '/jaryo/marks/d4-split.svg',
    name: 'D4 Split',
    note: 'Two blocks, one filled and one open. Two machines, not a health color.',
  },
  {
    id: 'd5',
    file: '/jaryo/marks/d5-signal.svg',
    name: 'D5 Signal',
    note: 'A reticle in a dashed frame. The frame belongs to the mark. Product unknown state stays the word UNKNOWN and the glyph ?.',
  },
];

function plate(kind, svg, name) {
  const wrap = document.createElement('div');
  wrap.className = `j-plate j-plate-${kind}`;
  const mark = document.createElement('div');
  mark.className = 'j-plate-mark';
  mark.append(svg.cloneNode(true));
  const word = document.createElement('span');
  word.className = 'j-lockup';
  word.textContent = 'AGIW';
  wrap.append(mark, word);
  wrap.setAttribute('role', 'img');
  wrap.setAttribute('aria-label', `${name}, ${kind} treatment`);
  return wrap;
}

function safeSvg(text) {
  const trimmed = text.trim();
  if (!trimmed.startsWith('<svg') || /<script|onload=|javascript:/i.test(trimmed)) return null;
  const doc = new DOMParser().parseFromString(trimmed, 'image/svg+xml');
  const svg = doc.querySelector('svg');
  if (!svg || doc.querySelector('parsererror')) return null;
  return document.importNode(svg, true);
}

async function mount() {
  const host = document.getElementById('identityGallery');
  if (!host) return;
  for (const direction of DIRECTIONS) {
    const card = document.createElement('article');
    card.className = 'j-id-card';
    card.dataset.direction = direction.id;
    const title = document.createElement('h2');
    title.textContent = direction.name;
    const note = document.createElement('p');
    note.textContent = direction.note;
    const row = document.createElement('div');
    row.className = 'j-plates';
    let svg = null;
    try {
      const response = await fetch(direction.file, {cache: 'no-store'});
      if (response.ok) svg = safeSvg(await response.text());
    } catch (_) {
      svg = null;
    }
    if (!svg) {
      const missing = document.createElement('p');
      missing.className = 'j-unavailable j-callout';
      missing.textContent = `○ UNAVAILABLE · ${direction.file} did not load`;
      card.append(title, note, missing);
      host.append(card);
      continue;
    }
    svg.setAttribute('aria-hidden', 'true');
    for (const kind of ['dark', 'light', 'narrow']) row.append(plate(kind, svg, direction.name));
    const file = document.createElement('p');
    file.className = 'j-note';
    file.textContent = direction.file;
    card.append(title, note, row, file);
    host.append(card);
  }
}

mount();
