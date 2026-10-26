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
if let sampleAge = snapshot.removeValue(forKey: "_sampleAge") as? Double {
    snapshot["sampledAt"] = Date().timeIntervalSince1970 - sampleAge
}
if var machines = snapshot["machines"] as? [[String: Any]] {
    for index in machines.indices {
        if let age = machines[index].removeValue(forKey: "_observedAge") as? Double {
            let format = machines[index].removeValue(forKey: "_observedFormat") as? String ?? "z"
            let formatter = ISO8601DateFormatter()
            if format == "fractional-offset" {
                formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            }
            var stamp = formatter.string(from: Date(timeIntervalSinceNow: -age))
            if format == "fractional-offset" {
                stamp = stamp.replacingOccurrences(of: "Z", with: "000+00:00")
            }
            machines[index]["observedAt"] = stamp
        }
    }
    snapshot["machines"] = machines
}
harness.updateBadge(snapshot)
let encoder = JSONEncoder()
let output = try encoder.encode([
    "title": button.title,
    "tooltip": button.toolTip ?? "",
    "accessibility": button.accessibilityValue ?? ""
])
print(String(data: output, encoding: .utf8)!)
'''


def machine(platform, busy=0, **changes):
    return {'host': platform, 'platform': platform, 'state': 'up', 'modelsComplete': True,
            'busyCount': busy, 'ageSeconds': 0, **changes}


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

    def badge(self, machines=DEFAULT_MACHINES, **changes):
        snapshot = {'schemaVersion': 1, '_sampleAge': 0,
                    'machines': [machine('mac'), machine('windows'), machine('linux')]
                    if machines is DEFAULT_MACHINES else machines, **changes}
        result = subprocess.run([str(self.executable)], input=json.dumps(snapshot), env=self.environment,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            self.fail(f'Native badge harness did not return JSON: {error}: {result.stdout!r}')

    def badge_title(self, machines=DEFAULT_MACHINES, **changes):
        return self.badge(machines, **changes)['title'].strip()

    def test_counts_busy_models_on_all_three_machines(self):
        self.assertEqual(self.badge_title([machine('mac', 1), machine('windows', 2), machine('linux', 3)]), '6')

    def test_actual_peer_hostnames_and_multiple_peers_count(self):
        rows = [machine('mac', 1), machine('windows', 2, host='winpc01'),
                machine('linux', 3, host='linux-node-1'), machine('linux', 4, host='second-linux')]
        self.assertEqual(self.badge_title(rows), '10')

    def test_fresh_complete_zero_is_known(self):
        state = self.badge()
        self.assertEqual(state['title'].strip(), '0')
        self.assertIn('0 observed working', state['tooltip'])
        self.assertIn('0 models observed working', state['accessibility'])

    def test_unknown_machine_is_not_zero(self):
        rows = [machine(platform, None, state='unknown') for platform in ('mac', 'windows', 'linux')]
        state = self.badge(rows)
        self.assertEqual(state['title'].strip(), '?')
        self.assertEqual(state['tooltip'], 'Inference Monitor · Working count unknown')
        self.assertEqual(state['accessibility'], 'Working count unknown')

    def test_unknown_peer_keeps_known_busy_count_and_uncertainty(self):
        rows = [machine('mac', 2), machine('windows'), machine('linux', None, state='unknown')]
        state = self.badge(rows)
        self.assertEqual(state['title'].strip(), '2·?')
        self.assertIn('At least 2 observed working; other activity unknown', state['tooltip'])
        self.assertEqual(state['accessibility'], 'At least 2 observed working; other activity unknown')

    def test_down_report_does_not_imply_known_zero_busy(self):
        rows = [machine('mac'), machine('windows', None, state='down'), machine('linux')]
        self.assertEqual(self.badge_title(rows), '?')

    def test_mac_expires_at_three_seconds(self):
        rows = [machine('mac', 4, ageSeconds=3.1), machine('windows'), machine('linux')]
        self.assertEqual(self.badge_title(rows), '?')

    def test_peers_use_thirty_second_threshold(self):
        rows = [machine('mac'), machine('windows', 2, ageSeconds=29), machine('linux', 3, ageSeconds=29)]
        self.assertEqual(self.badge_title(rows), '5')
        for peer in ('windows', 'linux'):
            with self.subTest(peer=peer):
                rows = [machine('mac'), machine('windows'), machine('linux')]
                next(row for row in rows if row['platform'] == peer).update(busyCount=4, ageSeconds=30.1)
                self.assertEqual(self.badge_title(rows), '?')

    def test_snapshot_elapsed_time_ages_machine_evidence(self):
        rows = [machine('mac'), machine('windows', 2), machine('linux', 3, ageSeconds=29)]
        self.assertEqual(self.badge_title(rows, _sampleAge=4), '2·?')

    def test_serving_aged_peers_count_without_adding_snapshot_age_twice(self):
        for format in ('fractional-offset', 'z'):
            with self.subTest(format=format):
                rows = [machine('mac'), machine('windows', 2, ageSeconds=29, _observedAge=29,
                                               _observedFormat=format),
                        machine('linux', 3, ageSeconds=29, _observedAge=29, _observedFormat=format)]
                self.assertEqual(self.badge_title(rows, _sampleAge=2), '5')

    def test_original_observation_expires_despite_fresh_cached_age(self):
        rows = [machine('mac'), machine('windows', 4, _observedAge=31), machine('linux')]
        self.assertEqual(self.badge_title(rows), '?')

    def test_malformed_observation_timestamps_are_unknown(self):
        for observed in ('', 'invalid', '1970-01-01T00:16:40', 1000, True):
            with self.subTest(observed=observed):
                rows = [machine('mac'), machine('windows', 4, observedAt=observed), machine('linux')]
                self.assertEqual(self.badge_title(rows), '?')

    def test_future_observation_timestamp_is_unknown(self):
        rows = [machine('mac'), machine('windows', 4, _observedAge=-5), machine('linux')]
        self.assertEqual(self.badge_title(rows), '?')

    def test_future_snapshot_is_unknown(self):
        self.assertEqual(self.badge_title(_sampleAge=-2.1), '?')

    def test_incomplete_inventory_is_unknown(self):
        rows = [machine('mac'), machine('windows', modelsComplete=False), machine('linux')]
        self.assertEqual(self.badge_title(rows), '?')

    def test_malformed_busy_counts_are_unknown(self):
        for busy in (True, False, None, '1', -1, 0.5, 257):
            with self.subTest(busy=busy):
                rows = [machine('mac'), machine('windows'), machine('linux', busy)]
                self.assertEqual(self.badge_title(rows), '?')

    def test_malformed_observation_ages_are_unknown(self):
        for age in (True, False, None, '0', -1):
            with self.subTest(age=age):
                rows = [machine('mac'), machine('windows'), machine('linux', ageSeconds=age)]
                self.assertEqual(self.badge_title(rows), '?')

    def test_missing_or_unrecognized_platform_is_unknown(self):
        for rows in ([], [machine('mac'), machine('windows')],
                     [machine('mac'), machine('windows'), machine('other')]):
            with self.subTest(rows=rows):
                self.assertEqual(self.badge_title(rows), '?')

    def test_invalid_snapshot_shape_is_unknown(self):
        for changes in ({'schemaVersion': 2}, {'machines': None}, {'machines': ['linux']}):
            with self.subTest(changes=changes):
                self.assertEqual(self.badge_title(**changes), '?')

    def test_numeric_inventory_completeness_is_not_boolean_evidence(self):
        rows = [machine('mac'), machine('windows', modelsComplete=1), machine('linux')]
        self.assertEqual(self.badge_title(rows), '?')

    def test_boolean_schema_version_is_not_supported_schema(self):
        self.assertEqual(self.badge_title(schemaVersion=True), '?')


if __name__ == '__main__':
    unittest.main()
