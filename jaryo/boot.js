/* First paint, before the module loads. Keep the choice order in step with readThemeChoice. */
(function () {
  var modes = ['system', 'light', 'dark'];
  var query = null;
  var saved = null;
  try { query = new URLSearchParams(window.location.search).get('theme'); }
  catch (error) { query = null; }
  try { saved = window.localStorage.getItem('jaryo-kit-theme'); }
  catch (error) { saved = null; }
  var choice = modes.indexOf(query) !== -1 ? query : (modes.indexOf(saved) !== -1 ? saved : 'system');
  var light = false;
  try { light = window.matchMedia('(prefers-color-scheme: light)').matches === true; }
  catch (error) { light = false; }
  var resolved = choice === 'system' ? (light ? 'light' : 'dark') : choice;
  document.documentElement.dataset.theme = resolved;
  document.documentElement.dataset.themeChoice = choice;
})();
