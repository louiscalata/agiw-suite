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


def completed(stdout, code=0, stderr=""):
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr=stderr)


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

    def test_unified_memory_ceiling_comes_only_from_the_app_shell(self):
        ceiling = str(51539607552)  # 48 GiB, the shape Metal's recommendedMaxWorkingSetSize takes
        gpu, source = gpu_probe.mac_gpu(lambda *a, **k: completed(IOREG), {gpu_probe.CEILING_ENV: ceiling})
        self.assertEqual((gpu["unifiedMemoryCeilingBytes"], gpu["unifiedMemoryCeilingSource"]),
                         (51539607552, gpu_probe.CEILING_SOURCE))
        self.assertIn("from Metal", source["detail"])

    def test_ioreg_failure_preserves_the_independent_metal_ceiling(self):
        for runner in (lambda *a, **k: completed("", 1), lambda *a, **k: completed("no stats here"),
                       lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("ioreg", 1.5))):
            with self.subTest(runner=runner):
                gpu, source = gpu_probe.mac_gpu(runner, {gpu_probe.CEILING_ENV: "51539607552"})
                self.assertIsNotNone(gpu)
                self.assertEqual((gpu["unifiedMemoryCeilingBytes"], gpu["unifiedMemoryCeilingSource"]),
                                 (51539607552, gpu_probe.CEILING_SOURCE))
                for field in ("model", "cores", "utilizationPercent", "rendererPercent", "tilerPercent",
                              "allocatedBytes", "inUseBytes", "ageSeconds"):
                    self.assertIsNone(gpu[field], field)
                self.assertEqual(source["state"], "unavailable")
                self.assertIsNone(source["ageSeconds"])
                self.assertIn("ioreg", source["detail"])
                self.assertIn("from Metal", source["detail"])

    def test_missing_or_invalid_ceiling_is_unknown_never_zero(self):
        for environ, why in (({}, "not passed in"), ({gpu_probe.CEILING_ENV: "0"}, "invalid"),
                             ({gpu_probe.CEILING_ENV: "1024"}, "invalid"), ({gpu_probe.CEILING_ENV: "-5"}, "invalid"),
                             ({gpu_probe.CEILING_ENV: "4e10"}, "invalid"), ({gpu_probe.CEILING_ENV: " 51539607552"}, "invalid"),
                             ({gpu_probe.CEILING_ENV: "１２３"}, "invalid"), ({gpu_probe.CEILING_ENV: str(1 << 51)}, "invalid")):
            with self.subTest(environ=environ):
                gpu, source = gpu_probe.mac_gpu(lambda *a, **k: completed(IOREG), environ)
                self.assertIsNone(gpu["unifiedMemoryCeilingBytes"])
                self.assertIn(why, gpu["unifiedMemoryCeilingSource"])
                self.assertTrue(gpu["unifiedMemoryCeilingSource"].startswith("unknown"))
                self.assertEqual(source["state"], "live")  # the ioreg values are still live

    def test_ceiling_defaults_to_the_process_environment(self):
        self.assertEqual(gpu_probe.working_set_ceiling({gpu_probe.CEILING_ENV: "17179869184"}),
                         (17179869184, gpu_probe.CEILING_SOURCE))
        from unittest import mock
        with mock.patch.dict(gpu_probe.os.environ, {gpu_probe.CEILING_ENV: "17179869184"}):
            self.assertEqual(gpu_probe.working_set_ceiling()[0], 17179869184)
        with mock.patch.dict(gpu_probe.os.environ, {}, clear=True):
            self.assertIsNone(gpu_probe.working_set_ceiling()[0])

    def test_app_shell_passes_metal_working_set_to_the_observer(self):
        from pathlib import Path
        source = (Path(__file__).resolve().parent / "Monitor.swift").read_text()
        self.assertIn("import Metal", source)
        launch = source[source.index('let script = resources.appendingPathComponent("server.py")'):]
        launch = launch[:launch.index("process.environment = environment")]
        self.assertIn("MTLCreateSystemDefaultDevice()?.recommendedMaxWorkingSetSize", launch)
        self.assertIn(f'environment["{gpu_probe.CEILING_ENV}"]', launch)
        # A stale value inherited from the shell's own environment must not survive a failed read.
        self.assertIn(f'environment.removeValue(forKey: "{gpu_probe.CEILING_ENV}")', launch)


class LocalCallerTests(unittest.TestCase):
    listener = (982, "/Applications/LM Studio.app/Contents/MacOS/LM Studio")
    busy = [{"host": "mac", "state": "busy"}]

    def feed(self, runner, **kwargs):
        kwargs.setdefault("executable", lambda pid: self.listener[1])
        return local_callers.LocalCallers(runner, **kwargs)

    def sample(self, feed, models=None, **kwargs):
        kwargs.setdefault("port", 1235)
        kwargs.setdefault("listener", self.listener)
        return feed.sample(self.busy if models is None else models, **kwargs)

    @staticmethod
    def connection(pid=9, name="opencode", port=1235, client_port=50000, address="127.0.0.1"):
        return (f"p982\ncLM Studio\nn{address}:{port}->{address}:{client_port}\n"
                f"p{pid}\nc{name}\nn{address}:{client_port}->{address}:{port}\n")

    def runner(self, args, **kwargs):
        return completed("p982\ncLM Studio\nn127.0.0.1:1235\n" if "-sTCP:LISTEN" in args
                         else self.connection())

    def test_parse_groups_connections_by_process_and_drops_the_server(self):
        out = (self.connection(4242, "node") + self.connection(4242, "node", client_port=50001)
               + self.connection(77, "python3.14", client_port=50002))
        self.assertEqual(local_callers.parse(out, {982}, 1235), [
            {"pid": 4242, "name": "node", "connections": 2}, {"pid": 77, "name": "python3.14", "connections": 1}])

    def test_printable_names_include_parentheses_and_unicode(self):
        for name in ("Codex (Service)", "Code Helper (Plugin)", "Café agent", "$(rm -rf)", "x" * 64):
            with self.subTest(name=name):
                self.assertEqual(local_callers.parse(self.connection(name=name), {982}, 1235)[0]["name"], name)

    def test_control_blank_and_oversize_process_names_are_unknown(self):
        for name in ("", " ", "Code\tHelper", "Code\rHelper", "Code\x00Helper", "x" * 65):
            with self.subTest(name=name):
                self.assertEqual(local_callers.parse(self.connection(name=name), {982}, 1235)[0]["name"], "unknown")

    def test_only_paired_clients_of_the_verified_endpoint_are_counted(self):
        sockets = (self.connection() + "p10\ncBackup Agent\nn192.0.2.1:50003->203.0.113.8:1235\n"
                   "p11\ncOther server\nn127.0.0.1:1235->127.0.0.1:50004\n"
                   "p12\ncOther client\nn127.0.0.1:50005->127.0.0.1:1234\n"
                   "p15\ncInvalid source\nn127.0.0.1:0->127.0.0.1:1235\n"
                   "n127.0.0.1:65536->127.0.0.1:1235\nn127.0.0.256:50009->127.0.0.1:1235\n")
        self.assertIsNone(local_callers.parse(sockets, {982}, 1235), 'malformed socket ends make the inventory incomplete')
        callers = local_callers.parse(sockets.split('p15')[0], {982}, 1235)
        self.assertEqual(callers, [{"pid": 9, "name": "opencode", "connections": 1}])

    def test_remote_port_match_does_not_become_a_model_caller(self):
        def runner(args, **kw):
            return completed("p982\ncLM Studio\nn127.0.0.1:1235\n" if "-sTCP:LISTEN" in args
                             else "p10\ncBackup Agent\nn192.0.2.1:50000->203.0.113.8:1235\n")

        callers, source = self.sample(self.feed(runner))
        self.assertEqual(callers, [])
        self.assertEqual(source["state"], "live")

    def test_missing_or_nonmatching_listener_keeps_callers_unknown(self):
        for output in ("", " \n", "p982\ncLM Studio\n", "p982\ncLM Studio\nn203.0.113.8:1235\n",
                       "p982\ncLM Studio\nn127.0.0.1:1234\n", "p982\ncLM Studio\nn*:1234\n",
                       "p0\ncLM Studio\nn127.0.0.1:1235\n", "pbad\ncLM Studio\nn127.0.0.1:1235\n",
                       "n127.0.0.1:1235\n"):
            with self.subTest(output=output):
                calls = []

                def runner(args, **kw):
                    calls.append(args)
                    return completed(output, 1 if not output.strip() else 0)

                callers, source = self.sample(self.feed(runner))
                self.assertIsNone(callers)
                self.assertEqual(source["state"], "unavailable")
                self.assertIsNone(source["ageSeconds"])
                self.assertIn("listener", source["detail"].lower())
                self.assertEqual(len(calls), 1, "no caller probe without a matching listener")

    def test_ambiguous_listener_in_either_family_keeps_callers_unknown(self):
        for address in ("*", "[::1]", "[::]"):
            output = f"p982\ncLM Studio\nn127.0.0.1:1235\np983\ncOther server\nn{address}:1235\n"
            callers, source = self.sample(self.feed(lambda *a, **kw: completed(output)))
            self.assertIsNone(callers)
            self.assertEqual(source["state"], "unavailable")

    def test_replaced_listener_pid_or_executable_is_unknown(self):
        for pid, executable in ((983, self.listener[1]), (982, "/Applications/Other.app/Contents/MacOS/Other"),
                                (982, None)):
            with self.subTest(pid=pid, executable=executable):
                calls = []

                def runner(args, **kwargs):
                    calls.append(args)
                    return completed(f"p{pid}\ncOther server\nn127.0.0.1:1235\n")

                callers, source = self.sample(self.feed(runner, executable=lambda pid: executable))
                self.assertIsNone(callers)
                self.assertEqual(source["state"], "unavailable")
                self.assertEqual(len(calls), 1)

    def test_executable_is_rechecked_after_established_read(self):
        from unittest.mock import Mock
        path = Mock(side_effect=[self.listener[1], "/Applications/Other"])
        callers, source = self.sample(self.feed(self.runner, executable=path))
        self.assertIsNone(callers)
        self.assertEqual(source["state"], "unavailable")
        self.assertEqual(path.call_count, 2)

    def test_unseen_client_or_server_end_is_unknown(self):
        for sockets in ("p982\ncLM Studio\nn127.0.0.1:1235->127.0.0.1:50000\n",
                        "p9\ncOther uid\nn127.0.0.1:50000->127.0.0.1:1235\n",
                        self.connection() + "p982\ncLM Studio\nn127.0.0.1:1235->127.0.0.1:50001\n"):
            with self.subTest(sockets=sockets):
                def runner(args, **kwargs):
                    return completed("p982\ncLM Studio\nn127.0.0.1:1235\n" if "-sTCP:LISTEN" in args else sockets)

                callers, source = self.sample(self.feed(runner))
                self.assertIsNone(callers)
                self.assertEqual(source["state"], "unavailable")
                self.assertIn("not visible", source["detail"])

    def test_lan_and_ipv6_pairs_are_counted(self):
        for address, listener in (("192.168.1.5", "*"), ("[::1]", "[::1]"), ("[::1]", "[::]")):
            with self.subTest(address=address, listener=listener):
                def runner(args, **kwargs):
                    self.assertIn("-iTCP:1235", args)
                    return completed(f"p982\ncLM Studio\nn{listener}:1235\n" if "-sTCP:LISTEN" in args
                                     else self.connection(address=address))

                callers, source = self.sample(self.feed(runner))
                self.assertEqual(callers, [{"pid": 9, "name": "opencode", "connections": 1}])
                self.assertEqual(source["state"], "live")

    def test_missing_ipv6_client_does_not_become_zero(self):
        def runner(args, **kwargs):
            return completed("p982\ncLM Studio\nn127.0.0.1:1235\nn[::1]:1235\n" if "-sTCP:LISTEN" in args
                             else "p982\ncLM Studio\nn[::1]:1235->[::1]:50000\n")

        callers, source = self.sample(self.feed(runner))
        self.assertIsNone(callers)
        self.assertEqual(source["state"], "unavailable")

    def test_disappeared_listener_replaces_cached_callers_with_unknown(self):
        now = [100.0]

        def runner(args, **kw):
            return completed("", 1) if now[0] > 100.0 else self.runner(args, **kw)

        feed = self.feed(runner, clock=lambda: now[0])
        self.assertEqual(self.sample(feed)[1]["state"], "live")
        now[0] += 2.0
        callers, source = self.sample(feed)
        self.assertIsNone(callers)
        self.assertEqual(source["state"], "unavailable")

    def test_runs_only_while_busy_and_at_most_every_two_seconds(self):
        calls, now = [], [100.0]

        def runner(args, **kw):
            calls.append(args)
            self.assertIn("-a", args)
            return self.runner(args, **kw)

        feed = self.feed(runner, clock=lambda: now[0])
        idle = [{"host": "mac", "state": "idle", "queued": 0, "loaded": True}]
        self.assertEqual(self.sample(feed, idle, activity_known=True)[1]["state"], "not-checked")
        self.assertEqual(calls, [])
        callers, source = self.sample(feed)
        self.assertEqual((callers, source["state"]), ([{"pid": 9, "name": "opencode", "connections": 1}], "live"))
        self.assertEqual(len(calls), 2)
        now[0] += 1.0
        self.sample(feed)
        self.assertEqual(len(calls), 2, "second sample within 2 s must reuse the last answer")
        now[0] += 1.5
        self.sample(feed, [{"host": "mac", "state": "idle", "queued": 3}])
        self.assertEqual(len(calls), 4, "queued requests count as busy")

    def test_cached_source_reports_actual_age_without_mutating_first_sample(self):
        now = [100.0]
        feed = self.feed(self.runner, clock=lambda: now[0])
        first = self.sample(feed)
        now[0] = 101.9
        cached = self.sample(feed)
        self.assertEqual(first[1]["ageSeconds"], 0.0)
        self.assertAlmostEqual(cached[1]["ageSeconds"], 1.9)
        self.assertEqual(cached[0], first[0])

    def test_listener_identity_change_bypasses_cache(self):
        for listener in ((983, self.listener[1]), (982, "/Applications/Other")):
            with self.subTest(listener=listener):
                calls = []

                def runner(args, **kwargs):
                    calls.append(args)
                    return self.runner(args, **kwargs)

                feed = self.feed(runner, clock=lambda: 100.0)
                self.assertEqual(self.sample(feed)[1]["state"], "live")
                callers, source = self.sample(feed, listener=listener)
                self.assertIsNone(callers)
                self.assertEqual(source["state"], "unavailable")
                self.assertEqual(len(calls), 3)

    def test_lsof_failure_reports_unavailable(self):
        feed = self.feed(lambda *a, **k: completed("", 2), clock=lambda: 0.0)
        callers, source = self.sample(feed)
        self.assertIsNone(callers)
        self.assertEqual(source["state"], "unavailable")

    def test_missing_activity_evidence_is_unknown_and_clears_cached_callers(self):
        calls = []

        def runner(args, **kw):
            calls.append(args)
            return self.runner(args, **kw)

        for rows in ([], [{"host": "mac", "state": "loaded", "loaded": True}],
                     [{"host": "mac", "state": "idle", "loaded": True}]):
            with self.subTest(rows=rows):
                feed = self.feed(runner, clock=lambda: 100.0)
                self.sample(feed)
                calls.clear()
                callers, source = self.sample(feed, rows)
                self.assertIsNone(callers)
                self.assertEqual(source["state"], "unavailable")
                self.assertIn("unknown", source["detail"])
                self.assertIsNone(source["ageSeconds"])
                self.assertEqual(calls, [])
                self.sample(feed)
                self.assertEqual(len(calls), 2, "unknown activity must invalidate cached callers")

    def test_lsof_diagnostics_and_partial_results_are_unavailable(self):
        for phase in ("LISTEN", "ESTABLISHED"):
            for result in (completed("", 1, "lsof: permission denied"), completed("lsof: permission denied", 1),
                           completed("p9\ncodex\nnx\n", 1), completed("p9\ncodex\nnx\n", 0, "lsof: permission denied")):
                with self.subTest(phase=phase, result=result):
                    def runner(args, **kw):
                        return result if f"-sTCP:{phase}" in args else self.runner(args, **kw)

                    callers, source = self.sample(self.feed(runner))
                    self.assertIsNone(callers)
                    self.assertEqual(source["state"], "unavailable")
                    self.assertIsNone(source["ageSeconds"])
                    self.assertNotIn("permission denied", source["detail"])

    def test_idle_requires_explicit_true_activity_evidence(self):
        def runner(*args, **kw):
            self.fail("no busy row should trigger lsof")

        feed = self.feed(runner)
        for known in (False, None, 1, "true", True):
            with self.subTest(activity_known=known):
                callers, source = self.sample(feed, [], activity_known=known)
                self.assertIsNone(callers)
                self.assertEqual(source["state"], "not-checked" if known is True else "unavailable")

    def test_unknown_residency_cannot_report_idle_even_with_activity_flag(self):
        rows = [{"id": "example-model", "host": "mac", "loaded": None, "state": "unknown"}]
        callers, source = self.sample(self.feed(self.runner), rows, activity_known=True)
        self.assertIsNone(callers)
        self.assertEqual(source["state"], "unavailable")
        self.assertNotIn("idle", source["detail"])

    def test_lsof_empty_no_match_is_a_known_zero_with_a_matching_listener(self):
        for listener in ("127.0.0.1:1235", "*:1235", "[::1]:1235"):
            for output in ("", " \n"):
                with self.subTest(listener=listener, output=output):
                    def runner(args, **kw):
                        return (completed(f"p982\ncLM Studio\nn{listener}\n") if "-sTCP:LISTEN" in args
                                else completed(output, 1))

                    callers, source = self.sample(self.feed(runner))
                    self.assertEqual(callers, [])
                    self.assertEqual(source["state"], "live")
                    self.assertIn("0 local process(es)", source["detail"])

    def test_unverified_or_invalid_identity_fails_closed_and_clears_cache(self):
        calls = []

        def runner(args, **kw):
            calls.append(args)
            return self.runner(args, **kw)

        feed = self.feed(runner, clock=lambda: 100.0)
        self.assertEqual(self.sample(feed)[1]["state"], "live")
        for invalid in (None, "1235", True, 0, 65536):
            callers, source = self.sample(feed, port=invalid)
            self.assertIsNone(callers)
            self.assertEqual(source["state"], "unavailable")
            self.assertIn("verified", source["detail"])
        for invalid in (None, (), (982,), (True, self.listener[1]), (0, self.listener[1]), (982, "")):
            callers, source = self.sample(feed, listener=invalid)
            self.assertIsNone(callers)
            self.assertEqual(source["state"], "unavailable")
        self.assertEqual(len(calls), 2)
        self.sample(feed)
        self.assertEqual(len(calls), 4)

    def test_port_change_bypasses_throttle_and_cache(self):
        calls = []

        def runner(args, **kw):
            port = int(next(arg for arg in args if arg.startswith("-iTCP:")).split(":")[1])
            calls.append(port)
            return completed(f"p982\ncLM Studio\nn127.0.0.1:{port}\n" if "-sTCP:LISTEN" in args
                             else self.connection(port=port))

        feed = self.feed(runner, clock=lambda: 100.0)
        self.assertEqual(self.sample(feed, port=1234)[1]["state"], "live")
        calls.clear()
        self.sample(feed)
        self.assertEqual(calls, [1235, 1235])

    def test_nonblocking_lsof_flags_and_combined_deadline(self):
        now, calls = [100.0], []

        def runner(args, **kwargs):
            calls.append(kwargs["timeout"])
            for flag in ("-b", "-w", "-iTCP:1235"):
                self.assertIn(flag, args)
            self.assertTrue(kwargs["start_new_session"])
            now[0] += 0.3 if "-sTCP:LISTEN" in args else 0.1
            return self.runner(args, **kwargs)

        callers, source = self.sample(self.feed(runner, clock=lambda: now[0]))
        self.assertEqual(source["state"], "live")
        self.assertAlmostEqual(calls[0], 0.5)
        self.assertAlmostEqual(calls[1], 0.2)

    def test_first_probe_consuming_budget_prevents_second_probe(self):
        now, calls = [100.0], []

        def runner(args, **kwargs):
            calls.append(args)
            now[0] += 0.5
            return self.runner(args, **kwargs)

        callers, source = self.sample(self.feed(runner, clock=lambda: now[0]))
        self.assertIsNone(callers)
        self.assertEqual(source["state"], "unavailable")
        self.assertEqual(len(calls), 1)

    def test_production_diagnostics_are_not_a_known_zero(self):
        from unittest.mock import patch
        import telemetry
        for output in ("lsof: permission denied\n", self.connection() + "lsof: permission denied\n"):
            with self.subTest(output=output), patch.object(telemetry, "_bounded_command", side_effect=[
                    "p982\ncLM Studio\nn127.0.0.1:1235\n", output]) as bounded:
                callers, source = self.sample(self.feed(None))
                self.assertIsNone(callers)
                self.assertEqual(source["state"], "unavailable")
                self.assertTrue(bounded.call_args.kwargs["merge_stderr"])

    def test_production_uses_process_group_safe_bounded_runner(self):
        from unittest.mock import patch
        import telemetry
        with patch.object(telemetry, "_bounded_command", side_effect=[
                "p982\ncLM Studio\nn127.0.0.1:1235\n", self.connection()]) as bounded:
            callers, source = self.sample(self.feed(None))
        self.assertEqual(source["state"], "live")
        self.assertEqual(bounded.call_count, 2)
        for call in bounded.call_args_list:
            self.assertLessEqual(call.kwargs["timeout"], 0.5)
            self.assertEqual(call.kwargs["ok_codes"], (0, 1))
            self.assertTrue(call.kwargs["merge_stderr"])
            self.assertIn("-b", call.args[0])
            self.assertIn("-w", call.args[0])


if __name__ == "__main__":
    unittest.main()
