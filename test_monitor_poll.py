"""Execute the actual native startup and poll methods with an in-memory transport."""
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(platform.system() == 'Darwin' and shutil.which('xcrun'), 'Requires macOS Swift toolchain')
class NativePollTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='native-poll-')
        cls.addClassCleanup(cls.temp.cleanup)
        stage = Path(cls.temp.name)
        source = Path('Monitor.swift').read_text()
        methods = source[source.index('    private func consumeStartup('):source.index('    private func updateBadge(')]
        methods = methods.replace('private func', 'func')
        driver = r'''
import Foundation
final class Child {
    let processIdentifier: Int32 = 123
    var isRunning = true
    var terminations = 0
    func terminate() { terminations += 1 }
}
final class Page {
    var loaded: URL?
    func loadDashboard(_ url: URL) { loaded = url }
    func retryIfNeeded(_ url: URL) {}
    func setControlCapability(_ capability: String?, origin: URL) {}
}
struct Task { let action: () -> Void; func resume() { action() } }
final class Session {
    var status = 200
    var paths: [String] = []
    func dataTask(with request: URLRequest, completionHandler: @escaping (Data?, URLResponse?, Error?) -> Void) -> Task {
        paths.append(request.url!.path)
        let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)
        return Task { completionHandler(Data("{}".utf8), response, nil) }
    }
}
final class Harness {
    var origin: URL?
    var serviceError: String?
    var terminating = false
    var startupBytes = Data()
    var observer: Child? = Child()
    var startupTimer: Timer?
    var stableTimer: Timer?
    var pollingTimer: Timer?
    var restartDelay: Double = 1
    var popoverDashboard: Page! = Page()
    var windowDashboard: Page? = Page()
    var controlCapability: String? = nil
    var requestInFlight = false
    var observerGeneration: UInt64 = 0
    var observerHealthy = false
    var pollFailures = 0
    var session = Session()
    func failService(_ reason: String) { serviceError = reason }
    func showUnknownBadge(_ reason: String) {}
    func updateBadge(_ snapshot: [String: Any]) {}
'''
        tail = r'''
}
let harness = Harness()
harness.session.status = Int(CommandLine.arguments[1])!
harness.consumeStartup(Data("{\"port\":8765,\"pid\":123}\n".utf8))
harness.pollingTimer?.invalidate()
for _ in 0..<6 {
    RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.01))
    harness.pollSnapshot()
}
RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.01))
print(String(data: try JSONSerialization.data(withJSONObject: [
    "paths": harness.session.paths, "terminations": harness.observer!.terminations,
    "page": harness.popoverDashboard.loaded!.path, "healthy": harness.observerHealthy,
    "failures": harness.pollFailures]), encoding: .utf8)!)
'''
        swift = stage / 'main.swift'
        swift.write_text(driver + methods + tail)
        cls.executable = stage / 'poll-test'
        environment = dict(os.environ, TMPDIR=str(stage), CLANG_MODULE_CACHE_PATH=str(stage / 'clang-cache'),
                           SWIFT_MODULECACHE_PATH=str(stage / 'swift-cache'))
        result = subprocess.run(['xcrun', 'swiftc', '-module-cache-path', str(stage / 'swift-cache'),
                                 str(swift), '-o', str(cls.executable)], env=environment,
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise AssertionError(result.stderr)

    def run_poll(self, status):
        result = subprocess.run([str(self.executable), str(status)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_startup_keeps_monitor_page_and_polls_served_snapshot(self):
        from test_review_regressions import handler
        from test_server import snapshot
        from server import SnapshotStore
        result = self.run_poll(200)
        store = SnapshotStore()
        store.publish(snapshot())
        self.assertEqual(result['page'], '/monitor')
        self.assertTrue(all(handler(path, store).do_GET()[0] == 200 for path in result['paths']))
        self.assertEqual(result['terminations'], 0)
        self.assertTrue(result['healthy'])

    def test_repeated_404_does_not_kill_observer(self):
        result = self.run_poll(404)
        self.assertEqual(result['terminations'], 0)
        self.assertEqual(result['failures'], 0)
        self.assertFalse(result['healthy'])

    def test_repeated_server_failures_still_trigger_recovery(self):
        self.assertEqual(self.run_poll(503)['terminations'], 1)
