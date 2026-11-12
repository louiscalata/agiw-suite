/** Kit behaviour: theme, pipeline stations, and tabs. No network except the pipeline file. */
import { THEME_MODES, resolveTheme } from './theme.mjs';
import { mountChoicePipeline, mountCommandPalette } from './interactions.mjs';

export const THEME_KEY = 'jaryo-kit-theme';

const ESCAPES = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
};

export function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (ch) => ESCAPES[ch]);
}

export function readThemeChoice(search, saved) {
  const query = new URLSearchParams(search.startsWith('?') || search === '' ? search : `?${search}`).get('theme');
  if (THEME_MODES.includes(query)) return query;
  if (THEME_MODES.includes(saved)) return saved;
  return 'system';
}

export function applyTheme(doc, choice, systemPrefersLight) {
  const theme = resolveTheme(choice, systemPrefersLight);
  doc.documentElement.dataset.theme = theme.resolved;
  doc.documentElement.dataset.themeChoice = theme.mode;
  return theme;
}

export function nextIndex(index, count, key) {
  if (!Number.isInteger(count) || count <= 0) return null;
  if (!Number.isInteger(index) || index < 0 || index >= count) return null;
  switch (key) {
    case 'ArrowRight':
    case 'ArrowDown':
      return (index + 1) % count;
    case 'ArrowLeft':
    case 'ArrowUp':
      return (index - 1 + count) % count;
    case 'Home':
      return 0;
    case 'End':
      return count - 1;
    default:
      return null;
  }
}

export function stationDetail(station) {
  return `<p class="jk-eyebrow">${escapeHtml(station.name)}</p>
    <p class="jk-detail-word">${escapeHtml(station.word)}</p>
    <span class="jk-detail-rule" aria-hidden="true"></span>
    <p>${escapeHtml(station.detail)}</p>`;
}

export function pipelineMarkup(pipeline) {
  const name = pipeline && typeof pipeline.name === 'string' ? pipeline.name : 'Pipeline';
  const note = pipeline && typeof pipeline.note === 'string' ? pipeline.note : '';
  const acceptance = pipeline && typeof pipeline.acceptance === 'string' ? pipeline.acceptance : 'Blocked';
  const stations = pipeline && Array.isArray(pipeline.stations) ? pipeline.stations : [];
  if (stations.length === 0) {
    return {
      name,
      note,
      acceptance,
      strip: '<div class="jk-empty"><p class="jk-empty-title">Empty</p><p>No stations on this lane. Add a declared station to pipeline.json.</p></div>',
      detail: '',
    };
  }
  const buttons = stations.map((station, index) => {
    const selected = index === 0;
    const number = String(index + 1).padStart(2, '0');
    return `<button type="button" class="jk-station" id="jk-station-${escapeHtml(station.id)}" data-station="${escapeHtml(station.id)}" aria-pressed="${selected ? 'true' : 'false'}" aria-controls="jk-station-detail" tabindex="${selected ? '0' : '-1'}">
      <span class="jk-station-index">${number}</span>
      <span class="jk-station-name">${escapeHtml(station.name)}</span>
      <span class="jk-station-word">${escapeHtml(station.word)}</span>
    </button>`;
  }).join('');
  return {
    name,
    note,
    acceptance,
    strip: `<div class="jk-stations" role="toolbar" aria-label="Pipeline stations">${buttons}</div>`,
    detail: `<div class="jk-detail" id="jk-station-detail" aria-live="polite">${stationDetail(stations[0])}</div>`,
  };
}

function systemPrefersLight(matchMedia) {
  try { return matchMedia('(prefers-color-scheme: light)').matches === true; }
  catch (_) { return false; }
}

function readSaved(storage) {
  try { return storage.getItem(THEME_KEY); }
  catch (_) { return null; }
}

function writeSaved(storage, choice) {
  try { storage.setItem(THEME_KEY, choice); }
  catch (_) { /* private mode */ }
}

export function mountPipeline(root, pipeline) {
  const view = pipelineMarkup(pipeline);
  const title = root.ownerDocument.getElementById('jk-pipeline-title');
  const note = root.ownerDocument.getElementById('jk-pipeline-note');
  const acceptance = root.ownerDocument.getElementById('jk-acceptance-word');
  if (title) title.textContent = view.name;
  if (note) note.textContent = view.note;
  if (acceptance) acceptance.textContent = view.acceptance;
  root.innerHTML = `${view.strip}${view.detail}`;
  const stations = pipeline && Array.isArray(pipeline.stations) ? pipeline.stations : [];
  if (stations.length === 0) return;
  mountChoicePipeline(root, {steps: stations, selectedId: stations[0]?.id});
}

function hostDocument(root) {
  return root.nodeType === 9 ? root : root.ownerDocument;
}

export function mountTabs(root) {
  const doc = hostDocument(root);
  const lists = [...root.querySelectorAll('[data-jk-tabs]')];
  lists.forEach((set) => {
    const tabs = [...set.querySelectorAll('[role="tab"]')];
    const panels = tabs.map((tab) => doc.getElementById(tab.getAttribute('aria-controls')));
    const select = (index, focus) => {
      tabs.forEach((tab, item) => {
        const on = item === index;
        tab.setAttribute('aria-selected', on ? 'true' : 'false');
        tab.tabIndex = on ? 0 : -1;
        if (panels[item]) panels[item].hidden = !on;
      });
      if (focus) tabs[index].focus();
    };
    const list = set.querySelector('[role="tablist"]');
    list.addEventListener('keydown', (event) => {
      const index = tabs.indexOf(doc.activeElement);
      const next = nextIndex(index, tabs.length, event.key);
      if (next === null) return;
      event.preventDefault();
      select(next, true);
    });
    tabs.forEach((tab, index) => {
      tab.addEventListener('click', () => select(index, true));
    });
    const current = Math.max(0, tabs.findIndex((tab) => tab.getAttribute('aria-selected') === 'true'));
    select(current, false);
  });
}

export function mountTheme(doc, { storage, matchMedia, search }) {
  const select = doc.querySelector('[data-jk-theme]');
  const choice = readThemeChoice(search, readSaved(storage));
  const apply = (next, persist) => {
    applyTheme(doc, next, systemPrefersLight(matchMedia));
    if (select) select.value = THEME_MODES.includes(next) ? next : 'system';
    if (persist) writeSaved(storage, next);
  };
  apply(choice, false);
  if (!select) return;
  select.addEventListener('change', () => apply(select.value, true));
  try {
    const media = matchMedia('(prefers-color-scheme: light)');
    const onChange = () => {
      if (doc.documentElement.dataset.themeChoice === 'system') apply('system', false);
    };
    if (typeof media.addEventListener === 'function') media.addEventListener('change', onChange);
  } catch (_) { /* matchMedia missing */ }
}

export function mountGuards(root) {
  root.addEventListener('click', (event) => {
    const button = event.target.closest('button[aria-disabled="true"]');
    if (!button || !root.contains(button)) return;
    event.preventDefault();
    event.stopPropagation();
  });
  root.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    const button = event.target.closest?.('button[aria-disabled="true"]');
    if (!button || !root.contains(button)) return;
    event.preventDefault();
    event.stopPropagation();
  });
}

export async function mount(doc, env) {
  const root = doc.querySelector('.jk-shell');
  if (!root) return;
  mountGuards(root);
  mountTheme(doc, env);
  const slot = doc.querySelector('[data-jk-pipeline]');
  if (slot) {
    try {
      const response = await env.fetch(new URL('pipeline.json', env.pageUrl));
      if (!response.ok) throw new Error(String(response.status));
      const pipeline = await response.json();
      mountPipeline(slot, pipeline);
    } catch (_) {
      slot.innerHTML = '<div class="jk-alert" role="alert"><p class="jk-alert-title">Error</p><p>The pipeline file did not load. Check that pipeline.json is available.</p></div>';
    }
  }
  mountTabs(doc);
  const commands = doc.querySelector('[data-jk-commands]');
  if (commands) {
    const palette = mountCommandPalette(doc, commands, [...doc.querySelectorAll('.jk-nav a')].map(link => ({label:link.textContent, run:() => { link.click(); }})));
    doc.addEventListener('keydown', event => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k' && !doc.querySelector('dialog:modal')) { event.preventDefault(); palette.open(); }
    });
  }
}

if (typeof document !== 'undefined' && typeof document.querySelector === 'function' && document.querySelector('[data-jk-pipeline]')) {
  const storage = {
    getItem: (key) => { try { return localStorage.getItem(key); } catch (_) { return null; } },
    setItem: (key, value) => { try { localStorage.setItem(key, value); } catch (_) { /* private */ } },
  };
  mount(document, {
    storage,
    matchMedia: (query) => window.matchMedia(query),
    search: window.location.search,
    pageUrl: window.location.href,
    fetch: (url) => fetch(url),
  });
}
