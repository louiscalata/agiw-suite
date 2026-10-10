"""Run only the tray's actual status helpers, with no tray or observer startup."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "agiw-monitor.ps1"
POWERSHELL = (Path(os.environ.get("SystemRoot", r"C:\Windows"))
              / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
NOW = 1700000000.0
SPECIAL_NUMBERS = [{"__double": name} for name in ("nan", "infinity", "negative-infinity")]


def snapshot():
    lane = {"status": "idle", "ageSeconds": 0, "slotsBusy": 0, "slotsTotal": 2}
    return {"schemaVersion": 1, "host": "windows", "sampledAt": NOW,
            "memory": {"level": "unknown", "availablePercent": None},
            "windowsWorker": {"lanes": {"fast": copy.deepcopy(lane), "deep": copy.deepcopy(lane)}},
            "pcGpus": [], "macPeer": {"state": "unknown", "ageSeconds": None}}


# Parsing function ASTs keeps top-level WinForms, mutex, observer and model code
# out of the test process. The bodies exercised here always come from the tray.
RUNNER = r"""
param([string]$TraySource, [string]$CasesPath)
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($TraySource, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$names = @('Test-FiniteNumber', 'Get-SnapshotSummary', 'Format-TrayText', 'Get-StatusSummary')
$functions = $ast.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -in $names
}, $true)
foreach ($definition in $functions) { Invoke-Expression $definition.Extent.Text }

function Expand-TestValue($Value) {
    if ($Value -is [System.Management.Automation.PSCustomObject]) {
        if ($Value.PSObject.Properties.Name -contains '__double') {
            switch ($Value.__double) {
                'nan' { return [double]::NaN }
                'infinity' { return [double]::PositiveInfinity }
                'negative-infinity' { return [double]::NegativeInfinity }
                default { throw 'unknown numeric fixture' }
            }
        }
        foreach ($property in $Value.PSObject.Properties) { $property.Value = Expand-TestValue $property.Value }
    } elseif ($Value -is [array]) {
        return ,@($Value | ForEach-Object { Expand-TestValue $_ })
    }
    return ,$Value
}

# The network wrapper is real; only its input boundary is replaced. No network
# request or dashboard launch occurs in this process.
function Get-DashboardUrl { return $script:FakeUrl }
function Invoke-RestMethod {
    $script:Fetches++
    if ($script:FetchThrows) { throw 'fixture network unavailable' }
    return $script:FetchedSnapshot
}
$inputCases = Get-Content -LiteralPath $CasesPath -Raw | ConvertFrom-Json
$results = @()
foreach ($case in $inputCases.cases) {
    $script:Fetches = 0
    try {
        $value = switch ($case.kind) {
            'summary' { Get-SnapshotSummary -Snapshot (Expand-TestValue $case.snapshot) -NowUnix $case.now }
            'finite' { Test-FiniteNumber (Expand-TestValue $case.value) }
            'format' { Format-TrayText $case.message }
            'status' {
                $script:FakeUrl = $case.url
                $script:LastError = $case.lastError
                $script:FetchThrows = [bool]$case.fail
                $script:FetchedSnapshot = Expand-TestValue $case.snapshot
                Get-StatusSummary
            }
            default { throw 'unknown case kind' }
        }
        $results += @{ name = $case.name; value = $value; fetches = $script:Fetches }
    } catch {
        $results += @{ name = $case.name; error = $_.Exception.Message; fetches = $script:Fetches }
    }
}
@{ powershell = $PSVersionTable.PSVersion.ToString(); results = $results } | ConvertTo-Json -Depth 25 -Compress
"""


@unittest.skipUnless(os.name == "nt", "Windows PowerShell tray only")
class TraySummaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = []

        def add(name, data, now=NOW):
            cls.cases.append({"name": name, "kind": "summary", "snapshot": data, "now": now})

        add("idle", snapshot())
        busy = snapshot()
        busy["windowsWorker"]["lanes"]["deep"].update(status="busy", slotsBusy=1, cpuPercent=42)
        add("busy", busy)
        for index, counters in enumerate([
                {"slotsBusy": None}, {"slotsTotal": None}, {"slotsBusy": True}, {"slotsBusy": 0.5},
                {"slotsBusy": -1}, {"slotsTotal": 0}, {"slotsBusy": 3, "slotsTotal": 2},
                {"slotsBusy": "0"}, {"slotsTotal": "2"}, {"slotsTotal": False}, {"slotsTotal": 33}]):
            data = snapshot()
            for lane in data["windowsWorker"]["lanes"].values():
                lane.update(counters)
            add(f"counters-{index}", data)
        for status, count in [("busy", 0), ("idle", 1), ("slots_unknown", 0)]:
            data = snapshot()
            for lane in data["windowsWorker"]["lanes"].values():
                lane.update(status=status, slotsBusy=count)
            add(f"contradiction-{status}", data)
        for index, status in enumerate([["idle", "busy"], ["idle"], {"state": "busy"}, True, 1]):
            data = snapshot()
            for lane in data["windowsWorker"]["lanes"].values():
                lane["status"] = status
            add(f"lane-status-{index}", data)
        for index, sampled in enumerate([NOW - 6, NOW + 1, None, True, False, "1700000000", "bad", [],
                                         {}, *SPECIAL_NUMBERS]):
            data = snapshot()
            data["sampledAt"] = sampled
            add(f"sample-time-{index}", data)
        for index, age in enumerate([None, -1, "0", "bad", True, False, 4, *SPECIAL_NUMBERS]):
            data = snapshot()
            for lane in data["windowsWorker"]["lanes"].values():
                lane["ageSeconds"] = age
            add(f"lane-age-{index}", data)
        missing_age = snapshot()
        for lane in missing_age["windowsWorker"]["lanes"].values():
            del lane["ageSeconds"]
        add("lane-age-missing", missing_age)
        at_boundary = snapshot()
        at_boundary["sampledAt"] -= 1
        for lane in at_boundary["windowsWorker"]["lanes"].values():
            lane["ageSeconds"] = 2
        add("lane-age-boundary", at_boundary)
        after_boundary = copy.deepcopy(at_boundary)
        after_boundary["sampledAt"] -= 0.01
        add("lane-age-after-boundary", after_boundary)
        for index, value in enumerate([None, True, False, "50", "bad", -1, 101, {}, [], *SPECIAL_NUMBERS]):
            data = copy.deepcopy(busy)
            data["windowsWorker"]["lanes"]["deep"]["cpuPercent"] = value
            add(f"cpu-invalid-{index}", data)
            data = snapshot()
            data["memory"] = {"level": "tight", "availablePercent": value}
            add(f"memory-invalid-{index}", data)
            data = copy.deepcopy(busy)
            data["pcGpus"] = [{"utilizationPercent": value}, {"utilizationPercent": 20}]
            add(f"gpu-invalid-{index}", data)
        for index, memory in enumerate([None, [], "bad", {}, {"level": "unknown", "availablePercent": 50},
                                         {"level": "unreported", "availablePercent": 50}]):
            data = snapshot()
            data["memory"] = memory
            add(f"memory-unknown-{index}", data)
        valid_memory = snapshot()
        valid_memory["memory"] = {"level": "ok", "availablePercent": 75}
        add("memory-valid", valid_memory)
        for index, age in enumerate([None, -1, "0", "bad", True, False, {}, [], *SPECIAL_NUMBERS]):
            data = snapshot()
            data["macPeer"] = {"state": "reachable", "ageSeconds": age}
            add(f"mac-age-{index}", data)
        missing_mac_age = snapshot()
        missing_mac_age["macPeer"] = {"state": "reachable"}
        add("mac-age-missing", missing_mac_age)
        for state, age, label in [("linked", 29, "reported"), ("reachable", 44, "answers")]:
            data = snapshot()
            data["sampledAt"] -= 1
            data["macPeer"] = {"state": state, "ageSeconds": age}
            add(f"mac-boundary-{label}", data)
            after = copy.deepcopy(data)
            after["sampledAt"] -= 0.01
            add(f"mac-expired-{label}", after)
        unanswered = snapshot()
        unanswered["macPeer"] = {"state": "unreachable", "ageSeconds": 0}
        add("mac-unanswered", unanswered)
        for index, data in enumerate([None, [], "bad", {}, {"sampledAt": NOW},
                                      {**snapshot(), "host": "mac"}, {**snapshot(), "schemaVersion": 2},
                                      {**snapshot(), "schemaVersion": True},
                                      {**snapshot(), "windowsWorker": "bad"},
                                      {**snapshot(), "windowsWorker": {"lanes": []}}]):
            add(f"malformed-{index}", data)
        partial = snapshot()
        del partial["windowsWorker"]["lanes"]["deep"]
        add("partial", partial)
        worker_array = snapshot()
        worker_array["windowsWorker"] = [worker_array["windowsWorker"]]
        add("shape-worker-array", worker_array)
        lanes_array = snapshot()
        lanes_array["windowsWorker"]["lanes"] = [lanes_array["windowsWorker"]["lanes"]]
        add("shape-lanes-array", lanes_array)
        lane_array = snapshot()
        lane_array["windowsWorker"]["lanes"]["deep"] = [lane_array["windowsWorker"]["lanes"]["deep"]]
        add("shape-one-lane-array", lane_array)
        memory_array = snapshot()
        memory_array["memory"] = [{"level": "tight", "availablePercent": 10}]
        add("shape-memory-array", memory_array)
        peer_array = snapshot()
        peer_array["macPeer"] = [{"state": "reachable", "ageSeconds": 0}]
        add("shape-peer-array", peer_array)
        gpu_object = snapshot()
        gpu_object["pcGpus"] = {"utilizationPercent": 99}
        add("shape-gpu-object", gpu_object)
        gpu_nested = copy.deepcopy(busy)
        gpu_nested["pcGpus"] = [[{"utilizationPercent": 99}], {"utilizationPercent": 20}]
        add("shape-gpu-nested-array", gpu_nested)
        for index, value in enumerate([0, 1, -1, 0.5, None, True, False, "0", "3.5", [], {}, *SPECIAL_NUMBERS]):
            cls.cases.append({"name": f"finite-{index}", "kind": "finite", "value": value})
        for size in [0, 1, 55, 56, 57, 1000]:
            cls.cases.append({"name": f"format-{size}", "kind": "format", "message": "x" * size})
        cls.cases.extend([
            {"name": "network-failure", "kind": "status", "url": "http://127.0.0.1:8767/", "fail": True},
            {"name": "not-started", "kind": "status", "url": None, "fail": True, "lastError": "Fixture startup error"},
        ])
        with tempfile.TemporaryDirectory() as tmp:
            runner = Path(tmp) / "tray-helper-tests.ps1"
            cases = Path(tmp) / "cases.json"
            runner.write_text(RUNNER, encoding="ascii")
            cases.write_text(json.dumps({"cases": cls.cases}), encoding="ascii")
            result = subprocess.run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                     "-File", str(runner), "-TraySource", str(SCRIPT), "-CasesPath", str(cases)],
                                    capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise AssertionError(f"headless PowerShell runner failed: {result.stderr}")
        response = json.loads(result.stdout)
        if not response["powershell"].startswith("5.1."):
            raise AssertionError(f"tests require Windows PowerShell 5.1: {response['powershell']}")
        cls.results = {row["name"]: row for row in response["results"]}

    def result(self, name):
        row = self.results[name]
        self.assertNotIn("error", row, f"{name}: {row.get('error')}")
        return row["value"]

    def group(self, prefix):
        return [case["name"] for case in self.cases if case["name"].startswith(prefix)]

    def test_fresh_idle_and_confirmed_busy(self):
        idle = self.result("idle")
        self.assertEqual(idle["tone"], "ok")
        self.assertIn("fast idle", idle["text"])
        self.assertIn("deep idle", idle["text"])
        busy = self.result("busy")
        self.assertEqual(busy["tone"], "live")
        self.assertIn("deep 1/2", busy["text"])

    def test_partial_or_invalid_counters_never_claim_idle_or_busy(self):
        for name in self.group("counters-") + self.group("contradiction-") + self.group("lane-status-"):
            with self.subTest(case=name):
                summary = self.result(name)
                self.assertEqual(summary["tone"], "muted")
                self.assertNotIn(" idle", summary["text"])

    def test_stale_future_and_malformed_sample_times_are_neutral(self):
        for name in self.group("sample-time-"):
            with self.subTest(case=name):
                self.assertEqual(self.result(name)["tone"], "muted")

    def test_lane_age_is_finite_nonnegative_and_includes_snapshot_age(self):
        for name in self.group("lane-age-"):
            if name == "lane-age-boundary":
                continue
            with self.subTest(case=name):
                self.assertEqual(self.result(name)["tone"], "muted")
        self.assertEqual(self.result("lane-age-boundary")["tone"], "ok")
        self.assertEqual(self.result("idle")["tone"], "ok", "zero is a real, current age")

    def test_bad_optional_cpu_does_not_lose_confirmed_activity(self):
        for name in self.group("cpu-invalid-"):
            with self.subTest(case=name):
                summary = self.result(name)
                self.assertEqual(summary["tone"], "live")
                self.assertIn("deep 1/2", summary["text"])
                self.assertNotIn(" cpu ", summary["text"])

    def test_bad_or_unknown_optional_memory_is_omitted(self):
        for name in self.group("memory-invalid-") + self.group("memory-unknown-"):
            with self.subTest(case=name):
                summary = self.result(name)
                self.assertEqual(summary["tone"], "ok")
                self.assertIn("fast idle", summary["text"])
                self.assertNotIn("RAM", summary["text"])
        self.assertIn("RAM 75% free", self.result("memory-valid")["text"])

    def test_bad_optional_gpu_reading_preserves_confirmed_activity_and_other_adapter(self):
        for name in self.group("gpu-invalid-"):
            with self.subTest(case=name):
                summary = self.result(name)
                self.assertEqual(summary["tone"], "live")
                self.assertIn("deep 1/2", summary["text"])
                self.assertIn("GPU 20%", summary["text"])

    def test_mac_peer_age_is_finite_and_its_freshness_includes_snapshot_age(self):
        for name in self.group("mac-age-") + self.group("mac-expired-"):
            with self.subTest(case=name):
                summary = self.result(name)
                self.assertEqual(summary["tone"], "ok", "bad peer data preserves known local lane evidence")
                self.assertNotIn("Mac ", summary["text"])
        self.assertIn("Mac reported", self.result("mac-boundary-reported")["text"])
        self.assertIn("Mac answers", self.result("mac-boundary-answers")["text"])

    def test_an_unanswered_mac_is_not_claimed_offline(self):
        summary = self.result("mac-unanswered")
        self.assertIn("Mac no answer", summary["text"])
        self.assertNotIn("offline", summary["text"])

    def test_required_malformed_data_and_partial_lane_are_neutral_without_throwing(self):
        for name in self.group("malformed-") + ["partial"]:
            with self.subTest(case=name):
                self.assertEqual(self.result(name)["tone"], "muted")
        self.assertIn("fast idle", self.result("partial")["text"], "a missing lane preserves known sibling evidence")

    def test_array_worker_or_lanes_cannot_use_powershell_member_enumeration_to_claim_idle(self):
        for name in ["shape-worker-array", "shape-lanes-array"]:
            with self.subTest(case=name):
                summary = self.result(name)
                self.assertEqual(summary["tone"], "muted")
                self.assertNotIn(" idle", summary["text"])

    def test_an_array_lane_remains_unknown_and_preserves_its_valid_sibling(self):
        summary = self.result("shape-one-lane-array")
        self.assertEqual(summary["tone"], "muted")
        self.assertIn("fast idle", summary["text"])
        self.assertNotIn("deep idle", summary["text"])

    def test_optional_container_shapes_cannot_create_false_readings(self):
        for name, omitted in [("shape-memory-array", "RAM"), ("shape-peer-array", "Mac "),
                              ("shape-gpu-object", "GPU")]:
            with self.subTest(case=name):
                summary = self.result(name)
                self.assertEqual(summary["tone"], "ok")
                self.assertIn("fast idle", summary["text"])
                self.assertNotIn(omitted, summary["text"])
        nested = self.result("shape-gpu-nested-array")
        self.assertEqual(nested["tone"], "live")
        self.assertIn("GPU 20%", nested["text"])
        self.assertNotIn("99", nested["text"])

    def test_finite_number_rejects_boolean_text_and_nonfinite_values(self):
        for index, name in enumerate(self.group("finite-")):
            with self.subTest(case=name):
                self.assertIs(self.result(name), index < 4)

    def test_tooltip_limit_includes_the_full_prefix(self):
        for size in [0, 1, 55, 56, 57, 1000]:
            with self.subTest(size=size):
                text = self.result(f"format-{size}")
                self.assertTrue(text.startswith("AGIW | "))
                self.assertLessEqual(len(text), 63)
                if size <= 56:
                    self.assertEqual(text, "AGIW | " + "x" * size)
                else:
                    self.assertEqual(len(text), 63)
                    self.assertTrue(text.endswith("..."))

    def test_network_failure_and_not_started_are_neutral(self):
        summary = self.result("network-failure")
        self.assertEqual(summary["tone"], "muted")
        self.assertEqual(self.results["network-failure"]["fetches"], 1)
        self.assertIn("Waiting", summary["text"])
        startup = self.result("not-started")
        self.assertEqual(startup["tone"], "muted")
        self.assertEqual(self.results["not-started"]["fetches"], 0)
        self.assertIn("Fixture startup error", startup["text"])


if __name__ == "__main__":
    unittest.main()
