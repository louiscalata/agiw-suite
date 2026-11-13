"""Compile and execute the production badge-counting logic without starting the app."""
import json
import os
import platform
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


SOURCE = Path(__file__).resolve().with_name('Monitor.swift')
BEGIN = '    private func updateBadge(_ snapshot: [String: Any]) {'
DEFAULT_MACHINES = object()
DRIVER = r'''
let input = FileHandle.standardInput.readDataToEndOfFile()
var snapshot = try JSONSerialization.jsonObject(with: input) as! [String: Any]
if let base = snapshot.removeValue(forKey: "_fleetNow") as? Double,
   var fleet = snapshot["fleetState"] as? [String: Any],
   var facts = fleet["facts"] as? [[String: Any]] {
    let delta = Date().timeIntervalSince1970 - base
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    let whole = ISO8601DateFormatter()
    for index in facts.indices {
        for field in ["observedAt", "expiresAt"] {
            if let stamp = facts[index][field] as? String,
               let date = formatter.date(from: stamp) ?? whole.date(from: stamp) {
                facts[index][field] = formatter.string(from: date.addingTimeInterval(delta))
            }
        }
    }
    fleet["facts"] = facts
    snapshot["fleetState"] = fleet
}
harness.updateBadge(snapshot)
let output = try JSONEncoder().encode([
    "title": button.title,
    "tooltip": button.toolTip ?? "",
    "accessibility": button.accessibilityValue ?? ""
])
print(String(data: output, encoding: .utf8)!)
'''


@unittest.skipUnless(platform.system() == 'Darwin' and shutil.which('xcrun'), 'Requires macOS Swift toolchain')
class NativeBadgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='badge-native-', dir=Path(tempfile.gettempdir()).resolve())
        cls.addClassCleanup(cls.temp.cleanup)
        stage = Path(cls.temp.name)
        cls.environment = dict(os.environ, TMPDIR=str(stage), CFFIXED_USER_HOME=str(stage),
                               CLANG_MODULE_CACHE_PATH=str(stage / 'clang-cache'),
                               SWIFT_MODULECACHE_PATH=str(stage / 'swift-cache'))
        source = SOURCE.read_text()
        if source.count(BEGIN) != 1:
            raise AssertionError('Expected one production updateBadge method')
        begin = source.index(BEGIN)
        end = source.index('\n    /// Amber when memory is tight', begin)
        methods = source[begin:end]
        methods = methods.replace('private func updateBadge', 'func updateBadge', 1)
        methods = methods.replace('private func showUnknownBadge', 'func showUnknownBadge', 1)
        swift = stage / 'main.swift'
        swift.write_text('''import Foundation
import CoreFoundation

final class BadgeButton {
    var title = ""
    var toolTip: String?
    var accessibilityValue: String?
    func setAccessibilityValue(_ value: String) { accessibilityValue = value }
}
final class BadgeItem { let button: BadgeButton? = BadgeButton() }
final class BadgeHarness {
    var statusItem: BadgeItem! = BadgeItem()
    var button: BadgeButton { statusItem!.button! }
    func applyMemoryTint(_ level: String?) {}
    static func memoryBadge(_ snapshot: [String: Any], elapsed: Double) -> (level: String, summary: String)? { nil }
''' + methods + '\n}\nlet harness = BadgeHarness()\nlet button = harness.button\n' + DRIVER)
        cls.executable = stage / 'badge-test'
        compiled = subprocess.run(['xcrun', 'swiftc', '-module-cache-path', str(stage / 'swift-cache'),
                                   str(swift), '-o', str(cls.executable)], env=cls.environment,
                                  capture_output=True, text=True, timeout=60)
        if compiled.returncode:
            raise AssertionError('Native badge harness compilation failed:\n' + compiled.stderr)

    def badge(self, fleet=None, base=None):
        fixture = json.loads(SOURCE.with_name('test_support').joinpath('fleet-state.json').read_text())
        snapshot = {'schemaVersion': 1, '_fleetNow': base if base is not None else fixture['now'],
                    'fleetState': fixture['fleetState'] if fleet is None else fleet}
        result = subprocess.run([str(self.executable)], input=json.dumps(snapshot), env=self.environment,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def fixture(self):
        return json.loads(SOURCE.with_name('test_support').joinpath('fleet-state.json').read_text())

    def test_same_fixture_membership_and_state_words_as_machine_ui(self):
        fixture = self.fixture()
        state = self.badge()
        self.assertEqual(state['title'].strip(), '3/4')
        self.assertIn('3 of 4 lanes answering', state['tooltip'])
        for fact in fixture['fleetState']['facts']:
            self.assertIn('{} ({}): {}'.format(fact['label'], fact['id'], fact['state']), state['tooltip'])
        self.assertIn('host health and fleet activity unknown', state['accessibility'])
        self.assertNotIn('observed working', state['tooltip'])

    def test_independent_expiry_and_original_age(self):
        fixture = self.fixture()
        # Four seconds after the fixture local evidence expires; registry lanes
        # keep their independent six-second window, pending never becomes zero.
        state = self.badge(base=fixture['now'] + 4)
        self.assertEqual(state['title'].strip(), '2/4')
        self.assertIn('Local endpoint (mac:lmstudio): stale (5 s old)', state['tooltip'])
        self.assertIn('PC fast (registry:pc:fast): endpoint answering', state['tooltip'])
        self.assertIn('unknown', state['tooltip'])

    def test_expired_fact_does_not_borrow_fleet_timestamp(self):
        fixture = self.fixture()
        fixture['fleetState']['observedAt'] = '2099-01-01T00:00:00Z'
        state = self.badge(fixture['fleetState'], base=fixture['now'] + 40)
        self.assertEqual(state['title'].strip(), '?/4')
        self.assertIn('Answering count unknown', state['tooltip'])
        self.assertIn('stale', state['tooltip'])
        self.assertNotIn('endpoint answering', state['tooltip'])

    def test_missing_and_malformed_contract_is_unknown(self):
        for fleet in ({}, {'schemaVersion': True, 'facts': []}, {'schemaVersion': 1, 'facts': None},
                      {'schemaVersion': 1, 'facts': ['invalid']}, {'schemaVersion': 1, 'facts': []}):
            with self.subTest(fleet=fleet):
                state = self.badge(fleet)
                self.assertEqual(state['title'].strip(), '?')
                self.assertEqual(state['accessibility'], 'Fleet coverage unknown')

    def test_missing_or_invalid_fact_clocks_are_unknown(self):
        for field, value in (('observedAt', None), ('expiresAt', None),
                             ('observedAt', 'bad'), ('expiresAt', 'bad'),
                             ('expiresAt', '1970-01-01T00:00:00Z'),
                             ('observedAt', '2099-01-01T00:00:00Z')):
            fixture = self.fixture()
            fleet = fixture['fleetState']
            for fact in fleet['facts']:
                if fact['scope'] == 'lane':
                    fact[field] = value
            with self.subTest(field=field, value=value):
                state = self.badge(fleet)
                self.assertEqual(state['title'].strip(), '?/4')
                self.assertNotIn(': endpoint answering', state['tooltip'])

    def test_duplicate_ids_are_invalid(self):
        fleet = self.fixture()['fleetState']
        fleet['facts'].append(fleet['facts'][0])
        self.assertEqual(self.badge(fleet)['title'].strip(), '?')

    def test_no_lane_facts_is_unknown_and_never_zero_working(self):
        fleet = self.fixture()['fleetState']
        fleet['facts'] = [fact for fact in fleet['facts'] if fact['scope'] == 'machine']
        state = self.badge(fleet)
        self.assertEqual(state['title'].strip(), '?')
        self.assertIn('Lane coverage unknown', state['tooltip'])
        self.assertNotIn('0 observed', state['tooltip'])

    def test_invalid_registry_cannot_show_complete_zero_in_native_badge(self):
        fleet = self.fixture()['fleetState']
        for fact in fleet['facts']:
            if fact['scope'] == 'lane':
                fact.update(state='endpoint unavailable', observedAt='2026-11-12T11:59:59Z',
                            expiresAt='2026-11-12T12:00:05Z')
        fleet['registry'] = {'state': 'unknown', 'reason': 'registry_invalid', 'completeness': 'unknown'}
        state = self.badge(fleet)
        self.assertEqual(state['title'].strip(), '?/4')
        self.assertIn('Partial coverage; registry invalid', state['tooltip'])


if __name__ == '__main__':
    unittest.main()
