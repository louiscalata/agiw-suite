"""PDH ownership and structural checks, runnable without a Windows GPU."""
import ast
import ctypes
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

WINDOWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WINDOWS))
from agiw_win import probes, winsys  # noqa: E402


def after_return(tree):
    """Find statements following an unconditional return in the same suite."""
    found = []
    for node in ast.walk(tree):
        for _, value in ast.iter_fields(node):
            if not isinstance(value, list):
                continue
            returned = False
            for statement in value:
                if not isinstance(statement, ast.stmt):
                    continue
                if returned:
                    found.append(statement.lineno)
                returned = returned or isinstance(statement, ast.Return)
    return found


class GpuLifecycle(unittest.TestCase):
    def pdh(self, add_result=0):
        pdh = mock.Mock()

        def opened(_source, _user, query):
            query._obj.value = 42
            return 0

        pdh.PdhOpenQueryW.side_effect = opened
        pdh.PdhAddEnglishCounterW.return_value = add_result
        pdh.PdhCollectQueryData.return_value = 0
        return pdh

    def test_close_is_a_class_method_on_every_platform(self):
        self.assertTrue(hasattr(winsys.GpuCounters, "close"))
        self.assertEqual(winsys.GpuCounters.close.__qualname__, "GpuCounters.close")

    def test_query_is_closed_once_and_cleared(self):
        pdh = self.pdh()
        with mock.patch.object(winsys, "IS_WINDOWS", True), \
                mock.patch.object(ctypes, "windll", SimpleNamespace(pdh=pdh), create=True), \
                mock.patch.object(winsys, "dxgi_adapters", return_value=[]):
            counters = winsys.GpuCounters()
        self.assertEqual(counters.query.value, 42)
        counters.close()
        counters.close()
        pdh.PdhCloseQuery.assert_called_once()
        self.assertEqual(pdh.PdhCloseQuery.call_args.args[0].value, 42)
        self.assertFalse(counters.query)

    def test_failed_initialization_also_releases_the_query(self):
        pdh = self.pdh(add_result=1)
        with mock.patch.object(winsys, "IS_WINDOWS", True), \
                mock.patch.object(ctypes, "windll", SimpleNamespace(pdh=pdh), create=True):
            with self.assertRaisesRegex(OSError, "counter unavailable"):
                winsys.GpuCounters()
        pdh.PdhCloseQuery.assert_called_once()

    @unittest.skipIf(winsys.IS_WINDOWS, "non-Windows refusal only")
    def test_non_windows_construction_remains_unavailable(self):
        with self.assertRaisesRegex(OSError, "Windows-only"):
            winsys.GpuCounters()


class DeadCode(unittest.TestCase):
    def test_guard_catches_nested_method_after_return(self):
        tree = ast.parse("def aggregate():\n    return []\n    def close(self):\n        pass\n")
        self.assertEqual(after_return(tree), [3])
        self.assertEqual(after_return(ast.parse("def live(ok):\n    if ok:\n        return 1\n    return 2\n")), [])

    def test_windows_modules_have_no_statements_after_return(self):
        for path in sorted((WINDOWS / "agiw_win").glob("*.py")):
            with self.subTest(module=path.name):
                self.assertEqual(after_return(ast.parse(path.read_text(encoding="utf-8"))), [])


class AttributionCompleteness(unittest.TestCase):
    ADAPTER = {"name": "GPU", "vendor": "nvidia", "luid": "000000000000D1A4", "memoryTotalMiB": 12288}

    def engine(self, pid, value, index=0):
        return f"pid_{pid}_luid_0x00000000_0x0000D1A4_phys_0_eng_{index}_engtype_Compute", value

    def reading(self, engines):
        return winsys.aggregate_gpu_counters(engines, [], [self.ADAPTER])[0]

    def test_only_a_complete_single_engine_split_is_attributed(self):
        row = self.reading([self.engine(100, 90.0), self.engine(200, 0.0, index=1)])
        self.assertTrue(row["engineSharesComplete"])
        row = self.reading([self.engine(100, 90.0), self.engine(200, 80.0, index=1)])
        self.assertFalse(row["engineSharesComplete"])
        self.assertEqual(row["utilizationPercent"], 90.0)

    def test_truncated_shares_never_claim_complete_attribution(self):
        row = self.reading([self.engine(pid, 3.0) for pid in range(1, winsys.ENGINE_SHARES_MAX + 2)])
        self.assertEqual(len(row["engineShares"]), winsys.ENGINE_SHARES_MAX)
        self.assertFalse(row["engineSharesComplete"])
        self.assertEqual(row["utilizationPercent"], (winsys.ENGINE_SHARES_MAX + 1) * 3.0)

    def test_missing_or_invalid_process_attribution_is_incomplete(self):
        for extra in (self.engine("unknown", 10.0), self.engine(0, 10.0), self.engine(200, float("nan")),
                      ("unidentified engine", 10.0)):
            with self.subTest(extra=extra):
                self.assertFalse(self.reading([self.engine(100, 90.0), extra])["engineSharesComplete"])
        self.assertFalse(self.reading([])["engineSharesComplete"])

    def test_listener_owner_is_known_only_after_valid_process_inspection(self):
        table = [{"state": winsys.MIB_TCP_STATE_LISTEN, "local": "127.0.0.1", "localPort": 1235,
                  "remote": "0.0.0.0", "remotePort": 0, "pid": 100}]
        base = {"pid": 100, "created": 1, "cpuTime": 0, "workingSetBytes": 1024}
        for info, known in (({**base, "name": "llama-server.exe"}, True),
                            ({**base, "name": "unknown"}, False), ({**base, "name": ""}, False), (None, False)):
            with self.subTest(info=info):
                reader = winsys.LaneProcesses(table_fn=lambda: table, info_fn=lambda _pid: info)
                servers, _, _ = reader.sample({1235: "fast"})
                self.assertIs(servers["fast"]["ownerKnown"], known)


class LoadCompleteness(unittest.TestCase):
    ADAPTERS = [AttributionCompleteness.ADAPTER,
                {**AttributionCompleteness.ADAPTER, "name": "Other GPU", "luid": "000000000000E2B5"}]

    def engine(self, value, luid="D1A4", index=0):
        return f"pid_100_luid_0x00000000_0x0000{luid}_phys_0_eng_{index}_engtype_Compute", value

    def assert_unknown(self, row):
        self.assertIsNone(row["utilizationPercent"])
        self.assertFalse(row["engineSharesComplete"])
        result = probes.hands_off([row], gpu_live=True, lane_pids=frozenset({100}), notification_state=5)
        self.assertIsNone(result["active"])
        self.assertIn("gpu-load-unknown", result["unknown"])

    def test_invalid_engine_sample_makes_its_adapter_unknown_even_with_valid_low_load(self):
        invalid = [self.engine(value, index=1) for value in (None, -1.0, float("nan"), float("inf"), True, "bad")]
        invalid.append(("pid_100_luid_0x00000000_0x0000D1A4_phys_0_malformed_engine", 3.0))
        for sample in invalid:
            with self.subTest(sample=sample):
                rows = winsys.aggregate_gpu_counters([self.engine(3.0), sample, self.engine(2.0, luid="E2B5")],
                                                     [], self.ADAPTERS)
                self.assert_unknown(rows[0])
                self.assertEqual(rows[1]["utilizationPercent"], 2.0)

    def test_unidentified_engine_sample_makes_every_adapter_unknown(self):
        for name in (None, "unknown instance"):
            with self.subTest(name=name):
                rows = winsys.aggregate_gpu_counters([self.engine(3.0), self.engine(2.0, luid="E2B5"), (name, None)],
                                                     [], self.ADAPTERS)
                for row in rows:
                    self.assert_unknown(row)

    def test_failed_memory_read_does_not_turn_valid_engine_load_unknown(self):
        rows = winsys.aggregate_gpu_counters([self.engine(3.0)],
                                             [("luid_0x00000000_0x0000D1A4_phys_0", None)], self.ADAPTERS[:1])
        self.assertEqual(rows[0]["utilizationPercent"], 3.0)
        self.assertIsNone(rows[0]["memoryUsedMiB"])

    def test_invalid_pdh_item_status_is_preserved_and_prevents_clear(self):
        entries = (winsys._PdhItem * 2)()
        entries[0].szName, entries[0].FmtValue.doubleValue, entries[0].FmtValue.CStatus = (*self.engine(3.0), 0)
        entries[1].szName, entries[1].FmtValue.doubleValue, entries[1].FmtValue.CStatus = (*self.engine(90.0, index=1), 0xC0000BBA)

        def read_array(_counter, _format, size, count, buffer):
            size._obj.value, count._obj.value = ctypes.sizeof(entries), len(entries)
            if buffer is None:
                return winsys.PDH_MORE_DATA
            ctypes.memmove(buffer, entries, ctypes.sizeof(entries))
            return 0

        counters = winsys.GpuCounters.__new__(winsys.GpuCounters)
        counters.pdh = mock.Mock(PdhGetFormattedCounterArrayW=mock.Mock(side_effect=read_array))
        sampled = counters._array(ctypes.c_void_p(1))
        self.assertEqual(sampled, [self.engine(3.0), self.engine(None, index=1)])
        self.assert_unknown(winsys.aggregate_gpu_counters(sampled, [], self.ADAPTERS[:1])[0])

    def test_array_query_and_read_failures_raise_instead_of_dropping_load(self):
        for fail_query in (True, False):
            with self.subTest(fail_query=fail_query):
                def read_array(_counter, _format, size, count, buffer):
                    size._obj.value, count._obj.value = ctypes.sizeof(winsys._PdhItem), 1
                    return 0xC0000BBA if fail_query or buffer is not None else winsys.PDH_MORE_DATA

                counters = winsys.GpuCounters.__new__(winsys.GpuCounters)
                counters.pdh = mock.Mock(PdhGetFormattedCounterArrayW=mock.Mock(side_effect=read_array))
                with self.assertRaisesRegex(OSError, "PdhGetFormattedCounterArray"):
                    counters._array(ctypes.c_void_p(1))


if __name__ == "__main__":
    unittest.main()
