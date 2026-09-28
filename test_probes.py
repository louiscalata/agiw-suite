import subprocess
import unittest

import gpu_probe
import local_callers

IOREG = '''+-o AGXAcceleratorG17X  <class AGXAcceleratorG17X>
    {
      "model" = "Apple M5 Pro"
      "gpu-core-count" = 20
      "PerformanceStatistics" = {"In use system memory (driver)"=0,"Alloc system memory"=40451211264,"Tiler Utilization %"=22,"Renderer Utilization %"=45,"Device Utilization %"=46,"In use system memory"=1758035968}
    }
'''


def completed(stdout, code=0):
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr="")


class GpuProbeTests(unittest.TestCase):
    def test_parses_utilisation_memory_model_and_cores(self):
        gpu = gpu_probe.parse(IOREG)
        self.assertEqual((gpu["model"], gpu["cores"], gpu["utilizationPercent"], gpu["rendererPercent"], gpu["tilerPercent"]),
                         ("Apple M5 Pro", 20, 46, 45, 22))
        self.assertEqual((gpu["allocatedBytes"], gpu["inUseBytes"]), (40451211264, 1758035968))

    def test_out_of_range_values_become_unknown(self):
        gpu = gpu_probe.parse(IOREG.replace('"Device Utilization %"=46', '"Device Utilization %"=146'))
        self.assertIsNone(gpu["utilizationPercent"])

    def test_failure_or_missing_statistics_report_unavailable(self):
        for runner in (lambda *a, **k: completed("", 1), lambda *a, **k: completed("no stats here"),
                       lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("ioreg", 1.5))):
            gpu, source = gpu_probe.mac_gpu(runner)
            self.assertIsNone(gpu)
            self.assertEqual(source["state"], "unavailable")
        gpu, source = gpu_probe.mac_gpu(lambda *a, **k: completed(IOREG))
        self.assertEqual((gpu["utilizationPercent"], source["state"]), (46, "live"))


class LocalCallerTests(unittest.TestCase):
    def test_parse_groups_connections_by_process_and_drops_the_server(self):
        out = "p982\ncLM Studio\nn127.0.0.1:1234->127.0.0.1:50000\np4242\ncnode\nn127.0.0.1:50000->127.0.0.1:1234\nn127.0.0.1:50001->127.0.0.1:1234\np77\ncpython3.14\nn127.0.0.1:50002->127.0.0.1:1234\n"
        self.assertEqual(local_callers.parse(out, {982}), [
            {"pid": 4242, "name": "node", "connections": 2}, {"pid": 77, "name": "python3.14", "connections": 1}])

    def test_odd_process_names_are_masked(self):
        self.assertEqual(local_callers.parse("p5\nc$(rm -rf)\nnx\n", set())[0]["name"], "unknown")

    def test_runs_only_while_busy_and_at_most_every_two_seconds(self):
        calls, now = [], [100.0]
        def runner(args, **kw):
            calls.append(args[3])
            return completed("p982\ncLM Studio\n" if "LISTEN" in args[3] else "p9\ncopencode\nnx\n")
        feed = local_callers.LocalCallers(runner, clock=lambda: now[0])
        idle = [{"host": "mac", "state": "idle", "queued": 0}]
        busy = [{"host": "mac", "state": "generating", "queued": 0}]
        self.assertEqual(feed.sample(idle)[1]["state"], "idle")
        self.assertEqual(calls, [])
        callers, source = feed.sample(busy)
        self.assertEqual((callers, source["state"]), ([{"pid": 9, "name": "opencode", "connections": 1}], "live"))
        now[0] += 1.0
        feed.sample(busy)
        self.assertEqual(len(calls), 2, "second sample within 2 s must reuse the last answer")
        now[0] += 1.5
        feed.sample([{"host": "mac", "state": "idle", "queued": 3}])
        self.assertEqual(len(calls), 4, "queued requests count as busy")

    def test_lsof_failure_reports_unavailable(self):
        feed = local_callers.LocalCallers(lambda *a, **k: completed("", 2), clock=lambda: 0.0)
        callers, source = feed.sample([{"host": "mac", "state": "busy"}])
        self.assertIsNone(callers)
        self.assertEqual(source["state"], "unavailable")


if __name__ == "__main__":
    unittest.main()
