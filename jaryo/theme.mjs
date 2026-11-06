/** Framework-neutral theme resolution. No DOM, no network, no option ids. */
export const THEME_MODES = ['system', 'light', 'dark'];

export function resolveTheme(choice, systemPrefersLight) {
  const mode = THEME_MODES.includes(choice) ? choice : 'system';
  const resolved = mode === 'system' ? (systemPrefersLight === true ? 'light' : 'dark') : mode;
  return {mode, resolved};
}
