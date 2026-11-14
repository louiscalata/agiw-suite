// Source contract for the native shell: landing pages, kept preferences, window frame, page recovery.
// The document-start script is executed here against stand-ins. The Swift helpers are read as text
// and type-checked separately; nothing here runs the app or proves behaviour in a real window.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {THEME_MODES} from './jaryo/theme.mjs';

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');
const swift = read('./Monitor.swift');
const between = (begin, end) => {
  const start = swift.indexOf(begin);
  assert.notEqual(start, -1, `Monitor.swift must contain ${begin}`);
  const stop = swift.indexOf(end, start);
  assert.notEqual(stop, -1, `Monitor.swift must contain ${end}`);
  return swift.slice(start, stop);
};
const policy = between('// BEGIN MONITOR SHELL POLICY', '// END MONITOR SHELL POLICY');
const list = name => {
  const match = policy.match(new RegExp(`static let ${name} = \\[([^\\]]*)\\]`));
  assert.ok(match, `the shell policy must declare ${name}`);
  return [...match[1].matchAll(/"([^"]*)"/g)].map(entry => entry[1]);
};
const literal = name => {
  const match = policy.match(new RegExp(`static let ${name} = "([^"]*)"`));
  assert.ok(match, `the shell policy must declare ${name}`);
  return match[1];
};

const ORIGIN = 'http://127.0.0.1:8765';

/** Runs the production document-start script in a page stand-in and returns what it touched. */
function page({theme = 'dark', textSize = '125', origin = ORIGIN, failing = false} = {}) {
  const match = policy.match(/let seed = """\n([\s\S]*?)\n        """/);
  assert.ok(match, 'the shell policy must build the preference script');
  const settings = {origin: ORIGIN, themeKey: literal('themeKey'), textSizeKey: literal('textSizeKey'), theme, textSize};
  const script = match[1].replace('\\(settings)', JSON.stringify(settings));
  assert.doesNotMatch(script, /\\\(/, 'only the settings object is interpolated into the script');
  class Storage {
    constructor() { this.values = new Map(); }
    getItem(key) { return this.values.has(String(key)) ? this.values.get(String(key)) : null; }
    setItem(key, value) {
      if (failing) throw new Error('storage refused');
      this.values.set(String(key), String(value));
    }
  }
  const original = Storage.prototype.setItem;
  const posted = [];
  const window = {localStorage: new Storage(), sessionStorage: new Storage(),
    webkit: {messageHandlers: {monitor: {postMessage: message => posted.push(message)}}}};
  vm.runInNewContext(script, {window, Storage, location: {origin}, String});
  return {window, posted, hooked: Storage.prototype.setItem !== original, settings};
}

test('the shell keeps the same two keys and offered values as the pages', () => {
  assert.equal(literal('themeKey'), 'agiw-theme');
  assert.equal(literal('textSizeKey'), 'agiw-text-size');
  assert.match(read('./web/jaryo.mjs'), /const KEY = 'agiw-theme';/);
  assert.match(read('./web/simple-view.mjs'), /localStorage\?\.setItem\('agiw-text-size', size\)/);
  assert.deepEqual(list('themes'), THEME_MODES);
  const sizes = read('./web/simple-view.mjs').match(/export const TEXT_SIZES = \[([^\]]*)\];/);
  assert.ok(sizes, 'the pages must declare their offered text sizes');
  assert.deepEqual(list('textSizes'), [...sizes[1].matchAll(/'([^']*)'/g)].map(entry => entry[1]));
});

test('the script writes the kept pair before the page reads it and reports nothing for that', () => {
  const {window, posted, hooked} = page({theme: 'light', textSize: '150'});
  assert.equal(window.localStorage.getItem('agiw-theme'), 'light');
  assert.equal(window.localStorage.getItem('agiw-text-size'), '150');
  assert.equal(hooked, true);
  assert.deepEqual(posted, []);
});

test('a later write to either key reports the whole pair; other writes report nothing', () => {
  const {window, posted} = page({theme: 'system', textSize: '100'});
  window.localStorage.setItem('agiw-theme', 'dark');
  window.localStorage.setItem('agiw-text-size', '200');
  window.localStorage.setItem('agiw-other', 'x');
  window.sessionStorage.setItem('agiw-theme', 'light');
  assert.equal(window.localStorage.getItem('agiw-other'), 'x');
  assert.equal(window.sessionStorage.getItem('agiw-theme'), 'light');
  assert.deepEqual(JSON.parse(JSON.stringify(posted)), [
    {action: 'preferences', theme: 'dark', textSize: '100', previousTheme: 'system', previousTextSize: '100'},
    {action: 'preferences', theme: 'dark', textSize: '200', previousTheme: 'dark', previousTextSize: '100'},
  ]);
});

test('a stale page rewriting the value it already holds reports nothing', () => {
  // The popover page loaded with 'system' repaints 'system' on an appearance change after the
  // window chose Dark; that rewrite must not reach the shell and replace the kept Dark.
  const {window, posted} = page({theme: 'system', textSize: '100'});
  window.localStorage.setItem('agiw-theme', 'system');
  window.localStorage.setItem('agiw-text-size', '100');
  assert.deepEqual(posted, []);
  window.localStorage.setItem('agiw-theme', 'light');
  window.localStorage.setItem('agiw-theme', 'light');
  assert.equal(posted.length, 1, 'only the real change is reported');
  assert.equal(posted[0].theme, 'light');
});

test('the docs match the v1.0.0 tag, the frame restore and the disclosed recovery design', () => {
  const architecture = read('./docs/architecture.md');
  assert.doesNotMatch(architecture, /1\.0\.0 \(7\) download opens `\/monitor`/);
  assert.match(architecture, /At tag `v1\.0\.0` \(build 7\) the shell loads the observer root `\/`/);
  const status = read('./docs/status.md');
  assert.doesNotMatch(status, /A saved frame is moved onto an attached screen/);
  assert.match(status, /a frame that overlaps no screen is ignored and the window is centred/);
  assert.match(status, /a failed later navigation replaces the page the user was on with the waiting view/);
});

test('a page on another origin is neither seeded nor observed', () => {
  const {window, posted, hooked} = page({origin: 'http://127.0.0.1:9999'});
  assert.equal(window.localStorage.getItem('agiw-theme'), null);
  assert.equal(hooked, false);
  window.localStorage.setItem('agiw-theme', 'light');
  assert.deepEqual(posted, []);
});

test('storage that refuses writes leaves the page untouched', () => {
  const {posted, hooked} = page({failing: true});
  assert.equal(hooked, false);
  assert.deepEqual(posted, []);
});

test('a missing native bridge does not break the page write', () => {
  const {window} = page();
  delete window.webkit;
  window.localStorage.setItem('agiw-theme', 'light');
  assert.equal(window.localStorage.getItem('agiw-theme'), 'light');
});

test('the dashboard window opens Overview and the compact popover keeps the map', () => {
  assert.match(policy, /case \.compact: return "\/monitor"/);
  assert.match(policy, /case \.window: return "\/"/);
  assert.match(swift, /popoverDashboard = DashboardController\(owner: self, surface: \.compact, preferences: preferences\)/);
  assert.match(swift, /let controller = DashboardController\(owner: self, surface: \.window, preferences: preferences\)/);
  const load = between('    func loadDashboard(_ origin: URL) {', '    func setControlCapability(');
  assert.match(load, /MonitorShellPolicy\.landingURL\(surface, origin: origin\)/);
  assert.match(load, /webView\.load\(URLRequest\(url: url,/);
  // One observer origin literal: the badge poll and both surfaces resolve against it.
  assert.equal(swift.match(/URL\(string: "http:\/\/127\.0\.0\.1:\\\(port\)[^"]*"\)/g).length, 1);
});

test('preferences are kept by the shell, never by a persistent web store', () => {
  assert.equal(swift.match(/websiteDataStore = \.nonPersistent\(\)/g).length, 1);
  assert.doesNotMatch(swift, /WKWebsiteDataStore\.default\(\)|websiteDataStore = \.default\(\)/);
  const install = between('    private func installUserScripts() {', '    func navigationFailed() {');
  assert.match(install, /removeAllUserScripts\(\)/);
  assert.match(install, /MonitorShellPolicy\.preferenceScript\(preferences, origin: \$0\)/);
  assert.match(install, /for source in \[seed, capabilityScript\]/);
  assert.match(install, /injectionTime: \.atDocumentStart, forMainFrameOnly: true/);
  assert.match(swift, /savedTheme: defaults\.string\(forKey: Self\.themeDefault\)/);
  assert.match(swift, /savedTextSize: defaults\.string\(forKey: Self\.textSizeDefault\)/);
});

test('a reported pair is adopted only when both values are offered', () => {
  const reported = between('    static func reported(', '\n}\n');
  assert.match(reported, /let theme = theme as\? String, themes\.contains\(theme\)/);
  assert.match(reported, /let textSize = textSize as\? String, textSizes\.contains\(textSize\)/);
  assert.match(reported, /else \{ return nil \}/);
  const handler = between('    func userContentController(', '        switch action {');
  assert.match(handler, /sender\.protocol == "http", sender\.host == "127\.0\.0\.1", sender\.port == origin\.port/);
  const adopt = handler.slice(handler.indexOf('if action == "preferences" {'));
  assert.match(adopt, /guard body\.count == 5, let reported = MonitorPreferences\.reported\(/);
  // Only a report whose previous pair differs is a choice; a same-value rewrite is ignored.
  assert.match(adopt, /MonitorPreferences\.isChange\(reported, previousTheme: body\["previousTheme"\],\s+previousTextSize: body\["previousTextSize"\]\)/);
  assert.match(adopt, /reported != preferences else \{ return \}/);
  const change = between('    static func isChange(', '\n}\n');
  assert.match(change, /return theme != reported\.theme \|\| textSize != reported\.textSize/);
  // The controller no longer keeps a page URL it never reads.
  assert.doesNotMatch(swift, /expectedURL/);
  assert.match(adopt, /defaults\.set\(reported\.theme, forKey: Self\.themeDefault\)/);
  assert.match(adopt, /defaults\.set\(reported\.textSize, forKey: Self\.textSizeDefault\)/);
  assert.match(adopt, /popoverDashboard\?\.setPreferences\(reported\)/);
  assert.match(adopt, /windowDashboard\?\.setPreferences\(reported\)/);
  // Every other action still carries exactly one field.
  assert.match(adopt, /return\n        \}\n        guard body\.count == 1 else \{ return \}/);
});

test('the window reopens at a saved frame clamped to an attached screen', () => {
  const clamp = between('    static func restoredFrame(', '    /// A cancelled load');
  assert.match(clamp, /guard let saved = saved, usable\(saved\) else \{ return nil \}/);
  assert.match(clamp, /screen\.intersection\(saved\)/);
  assert.match(clamp, /guard let screen = best else \{ return nil \}/);
  assert.match(clamp, /min\(max\(saved\.width, minimum\.width\), screen\.width\)/);
  assert.match(clamp, /min\(max\(saved\.height, minimum\.height\), screen\.height\)/);
  assert.match(clamp, /min\(max\(saved\.minX, screen\.minX\), screen\.maxX - width\)/);
  assert.match(clamp, /min\(max\(saved\.minY, screen\.minY\), screen\.maxY - height\)/);
  const open = between('    @objc func openDashboardWindow(', '    private func rememberWindowFrame(');
  const order = ['window.center()', 'MonitorShellPolicy.restoredFrame(', 'NSScreen.screens.map { $0.visibleFrame }',
    'window.setFrame(restored, display: false)', 'window.delegate = self'].map(text => open.indexOf(text));
  assert.ok(order.every(index => index !== -1), 'the window is centred, restored, then observed');
  assert.deepEqual(order, [...order].sort((a, b) => a - b));
  const remember = between('    private func rememberWindowFrame(', '    @objc func quitMonitor(');
  assert.match(remember, /window === dashboardWindow/);
  assert.match(remember, /!window\.styleMask\.contains\(\.fullScreen\), !window\.isMiniaturized/);
  assert.match(remember, /defaults\.set\(NSStringFromRect\(window\.frame\), forKey: Self\.windowFrameDefault\)/);
  assert.match(remember, /func windowDidMove\(_ notification: Notification\) \{ rememberWindowFrame\(notification\) \}/);
  assert.match(remember, /func windowDidResize\(_ notification: Notification\) \{ rememberWindowFrame\(notification\) \}/);
});

test('later navigation failures and a lost web process recover; cancelled and policy-stopped loads do not', () => {
  const rule = between('    static func recoversFromNavigationFailure(', '    /// Document-start script');
  assert.match(rule, /if code == NSURLErrorCancelled \{ return false \}/);
  assert.match(rule, /if domain == "WebKitErrorDomain" && code == 102 \{ return false \}/);
  const failed = between('    private func navigationFailed(_ webView: WKWebView', '    private func controller(for webView');
  assert.match(failed, /MonitorShellPolicy\.recoversFromNavigationFailure\(domain: failure\.domain, code: failure\.code\)/);
  // The first-load guard is gone from failure handling: any main-frame navigation may recover.
  assert.doesNotMatch(failed, /activeNavigation/);
  assert.match(failed, /func webViewWebContentProcessDidTerminate\(_ webView: WKWebView\) \{ recoverPage\(webView\) \}/);
  assert.match(failed, /controller\.expectedOrigin == origin,\n\s+origin != nil, serviceError == nil, !terminating/);
  assert.match(failed, /controller\.navigationFailed\(\)\n\s+if observerHealthy, let origin = origin \{ controller\.retryIfNeeded\(origin\) \}/);
  // Showing the page still requires the load the shell itself started.
  const finished = between('didFinish navigation: WKNavigation!) {', 'didFailProvisionalNavigation');
  assert.match(finished, /controller\.activeNavigation === navigation/);
});
