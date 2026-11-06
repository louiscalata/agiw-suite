/** AGIW adapter for the shared Jaryo theme. The product opens on Overview. */
import {THEME_MODES, resolveTheme} from '/jaryo/theme.mjs';

const KEY = 'agiw-theme';

function systemPrefersLight() {
  return window.matchMedia('(prefers-color-scheme: light)').matches === true;
}

export function applyTheme(choice) {
  const theme = resolveTheme(choice, systemPrefersLight());
  document.documentElement.dataset.theme = theme.resolved;
  document.documentElement.dataset.themeChoice = theme.mode;
  try { localStorage.setItem(KEY, theme.mode); }
  catch (_) { /* private mode */ }
  return theme.mode;
}

export function initTheme(select) {
  let saved = 'system';
  try { saved = localStorage.getItem(KEY) || 'system'; }
  catch (_) { saved = 'system'; }
  if (!THEME_MODES.includes(saved)) saved = 'system';
  const mode = applyTheme(saved);
  const media = window.matchMedia('(prefers-color-scheme: light)');
  const onChange = () => {
    if (document.documentElement.dataset.themeChoice === 'system') applyTheme('system');
  };
  if (typeof media.addEventListener === 'function') media.addEventListener('change', onChange);
  if (!select) return mode;
  select.value = mode;
  select.addEventListener('change', () => applyTheme(select.value));
  return mode;
}

if (typeof document !== 'undefined' && typeof document.querySelector === 'function') {
  document.documentElement.dataset.tokenSource = 'black-white-system';
  document.documentElement.dataset.provisionalMark = 'd1-stamp';
  document.documentElement.dataset.logoSelection = 'pending';
  const themeSelect = document.querySelector('#jaryoTheme');
  if (themeSelect) initTheme(themeSelect);
}
