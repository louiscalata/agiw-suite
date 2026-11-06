/** Surface tokens for the Jaryo draft. Graphite is the draft default, not a chosen product palette. */
const THEMES = ['graphite', 'paper', 'mono'];
const KEY = 'jaryo-theme';

export function applyTheme(name) {
  const theme = THEMES.includes(name) ? name : 'graphite';
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem(KEY, theme); }
  catch (_) { /* private mode */ }
  return theme;
}

export function initTheme(select) {
  let saved = 'graphite';
  try { saved = localStorage.getItem(KEY) || 'graphite'; }
  catch (_) { saved = 'graphite'; }
  const theme = applyTheme(saved);
  if (!select) return theme;
  select.value = theme;
  select.addEventListener('change', () => applyTheme(select.value));
  return theme;
}

if (typeof document !== 'undefined' && typeof document.querySelector === 'function') {
  const themeSelect = document.querySelector('#jaryoTheme');
  if (themeSelect) initTheme(themeSelect);
}
