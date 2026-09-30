"""Compile production coordination code with synthetic app inventory; never launch Monitor."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform == 'darwin', 'native Monitor coordination requires macOS and Cocoa')
class MonitorInstanceTests(unittest.TestCase):
    def test_native_policy_and_lifecycle(self):
        source = Path(__file__).with_name('Monitor.swift').read_text()
        begin = source.index('// BEGIN MONITOR INSTANCE POLICY')
        end_marker = '// END MONITOR INSTANCE COORDINATOR'
        end = source.index(end_marker) + len(end_marker)
        harness = r'''
let full = "local.codemode.inference-monitor"
let mas = "com.louiscalata.agiw.inference-monitor.mas"
func app(_ id: String, _ pid: Int32, _ date: Double?, _ terminated: Bool = false,
         executableName: String? = nil) -> MonitorInstanceCandidate {
    MonitorInstanceCandidate(bundleIdentifier: id,
                             executableName: executableName ?? (id == full ? "InferenceMonitor" : "AgiwInferenceMonitor"),
                             processIdentifier: pid,
                             launchDate: date.map { Date(timeIntervalSince1970: $0) }, isTerminated: terminated)
}
var checks = 0
func check(_ value: Bool, _ name: String) {
    precondition(value, name)
    checks += 1
}
let current = app(full, 20, 20)
let older = app(mas, 10, 10)
let newer = app(mas, 30, 30)
func exits(_ others: [MonitorInstanceCandidate], _ phase: MonitorInstancePhase = .running) -> Bool {
    MonitorInstancePolicy.shouldExit(current: current, others: others, phase: phase)
}
let jevHelper = app(full, 12750, nil, executableName: "JevKeychain")
let observedMain = app(full, 96375, nil)
let missingExecutable = MonitorInstanceCandidate(bundleIdentifier: full, executableName: nil,
                                                processIdentifier: 1, launchDate: nil, isTerminated: false)
let unknownMASExecutable = MonitorInstanceCandidate(bundleIdentifier: mas, executableName: nil,
                                                   processIdentifier: 2, launchDate: nil, isTerminated: false)
for phase in [MonitorInstancePhase.startup, .running] {
    check(!MonitorInstancePolicy.shouldExit(current: observedMain, others: [observedMain, jevHelper], phase: phase), "observed lower-PID Jev helper cannot evict real main")
    for peer in [jevHelper, app(full, 1, nil, executableName: "Other"), app(mas, 1, nil, executableName: "InferenceMonitor"), app(full, 1, nil, executableName: "AgiwInferenceMonitor"), app(mas, 1, nil, executableName: "NisiNode"), missingExecutable, unknownMASExecutable] {
        check(!exits([peer], phase), "same-bundle helper/wrong executable/missing executable cannot win election")
    }
    for invalidCurrent in [jevHelper, missingExecutable, unknownMASExecutable, app(full, 1, nil, executableName: ""), app(mas, 1, nil, executableName: "Wrong")] {
        check(MonitorInstancePolicy.shouldExit(current: invalidCurrent, others: [], phase: phase), "recognized current without qualified main executable refuses admission")
    }
    check(exits([older], phase), "real MAS main executable retains lower-PID preference")
    check(MonitorInstancePolicy.shouldExit(current: older, others: [app(full, 1, nil)], phase: phase), "real full main executable retains lower-PID preference")
}
check(!exits([newer], .startup), "startup lower PID current remains")
check(exits([older], .startup), "startup lower PID peer wins")
for snapshot in [[current, older], [older, current]] {
    let currentExits = MonitorInstancePolicy.shouldExit(current: current, others: snapshot, phase: .startup)
    let olderExits = MonitorInstancePolicy.shouldExit(current: older, others: snapshot, phase: .startup)
    check(currentExits != olderExits && !olderExits, "mirrored simultaneous startup admits exactly one")
}
let unknownA = app(full, 100, nil)
let unknownB = app(mas, 101, nil)
for snapshot in [[unknownA, unknownB], [unknownB, unknownA]] {
    let aExits = MonitorInstancePolicy.shouldExit(current: unknownA, others: snapshot, phase: .startup)
    let bExits = MonitorInstancePolicy.shouldExit(current: unknownB, others: snapshot, phase: .startup)
    check(aExits != bExits && !aExits, "unknown-date mirrored startup admits lower PID")
}
check(exits([older]), "lower PID peer wins")
check(!exits([newer]), "lower PID current remains")
check(exits([newer, older]) && exits([older, newer]), "inventory ordering irrelevant")
check(exits([app(mas, 19, 20)]), "equal date lower PID wins")
check(!exits([app(mas, 21, 20)]), "equal date higher PID loses")
check(!exits([current, app("worker", 1, 1), app(mas, 2, 1, true)], .startup), "self unrelated terminated ignored")
check(exits([app(mas, 1, nil)]), "any unknown date selects lower PID across snapshot")
check(MonitorInstancePolicy.shouldExit(current: app(full, 20, nil), others: [app(mas, 10, nil)], phase: .running), "all unknown dates choose lower PID")
let launchAgent = app(full, 50, nil)
let datedMAS = app(mas, 60, 1)
check(!MonitorInstancePolicy.shouldExit(current: launchAgent, others: [datedMAS], phase: .startup), "direct full LaunchAgent lower PID survives new dated MAS")
check(MonitorInstancePolicy.shouldExit(current: datedMAS, others: [launchAgent], phase: .startup), "dated MAS yields to lower PID direct full")
let mixed = [app(full, 10, 30), app(mas, 20, 10), app(full, 30, nil)]
for snapshot in [mixed, Array(mixed.reversed())] {
    for phase in [MonitorInstancePhase.startup, .running] {
        let survivors = snapshot.filter { !MonitorInstancePolicy.shouldExit(current: $0, others: snapshot, phase: phase) }
        check(survivors.count == 1 && survivors[0].processIdentifier == 10, "three mixed candidates elect one lower PID without pairwise cycle")
    }
}
let knownThree = [app(full, 10, 30), app(mas, 20, 10), app(full, 30, 20)]
check(knownThree.filter { !MonitorInstancePolicy.shouldExit(current: $0, others: knownThree, phase: .running) }.map { $0.processIdentifier } == [10], "all known dates choose lowest PID without chronology")
let temporalA = app(full, 200, 1)
let temporalB = app(mas, 100, 2)
let temporalC = app(full, 300, nil)
for snapshot in [[temporalA, temporalB, temporalC], [temporalA, temporalB]] {
    for phase in [MonitorInstancePhase.startup, .running] {
        let survivors = snapshot.filter { !MonitorInstancePolicy.shouldExit(current: $0, others: snapshot, phase: phase) }
        check(survivors.count == 1 && survivors[0].processIdentifier == 100, "unknown departure cannot change winner while losing A lingers")
    }
}
check(!MonitorInstancePolicy.shouldExit(current: app("worker", 20, 20), others: [older], phase: .startup), "non-Monitor current excluded")
check(!MonitorInstancePolicy.shouldExit(current: app(full, 20, 20, true), others: [older], phase: .startup), "terminated current excluded")
var inventory: [MonitorInstanceCandidate] = [older]
var callback: (() -> Void)?
var subscriptions = 0
var cancellations = 0
var selfExits = 0
func coordinator(_ subscribePeer: Bool = false) -> MonitorInstanceCoordinator {
    MonitorInstanceCoordinator(current: { current }, snapshot: { inventory }, subscribe: { changed in
        subscriptions += 1
        callback = changed
        if subscribePeer { inventory = [older] }
        return { cancellations += 1 }
    }, requestSelfTermination: { selfExits += 1 })
}
let blocked = coordinator()
check(!blocked.start() && subscriptions == 0 && selfExits == 0, "startup refuses before subscription/work")
inventory = []
let gap = coordinator(true)
check(!gap.start() && cancellations == 1, "peer appearing during subscription blocks startup and cancels")
inventory = []
let live = coordinator()
check(live.start(), "empty inventory admits startup")
inventory = [jevHelper, missingExecutable]
callback?()
check(selfExits == 0 && cancellations == 1, "live helper arrival keeps main and its observation active")
inventory = [newer]
callback?()
check(selfExits == 0, "running winner keeps itself")
inventory = [older]
callback?()
callback?()
check(selfExits == 1 && cancellations == 2, "loser exits self once and cancels observation")
live.stop()
check(cancellations == 2, "stop idempotent")
inventory = []
let stopped = coordinator()
check(stopped.start(), "fresh coordinator admits")
let queued = callback
stopped.stop()
inventory = [older]
queued?()
check(selfExits == 1 && cancellations == 3, "queued callback after teardown cannot exit")
print("PASS \(checks) production policy/lifecycle assertions")
'''
        environment = dict(os.environ, DEVELOPER_DIR='/Applications/Xcode.app/Contents/Developer')
        with tempfile.TemporaryDirectory(prefix='agiw-monitor-instance-') as directory:
            stage = Path(directory)
            swift = stage / 'main.swift'
            swift.write_text('import Cocoa\n' + source[begin:end] + '\n' + harness)
            binary = stage / 'policy-tests'
            subprocess.run(['xcrun', 'swiftc', '-framework', 'Cocoa', str(swift), '-o', str(binary)],
                           env=environment, check=True)
            subprocess.run([str(binary)], env=environment, check=True)


if __name__ == '__main__':
    unittest.main()
