"""Both API stores project identical facts without opening a network socket."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from fleet_state import build_fleet_state
from server import SnapshotStore as MacStore

sys.path.append(str(Path(__file__).resolve().parent / "windows"))
from agiw_win.server import SnapshotStore as WindowsStore


class FleetStoreTests(unittest.TestCase):
    def test_same_fixture_on_both_editions_and_both_api_payloads(self):
        fixture = json.loads(Path(__file__).with_name("test_support").joinpath("fleet-state.json").read_text())
        for store_class in (MacStore, WindowsStore):
            for delay in (0, 4, 40):
                with self.subTest(edition=store_class.__module__, delay=delay):
                    store = store_class()
                    store.snapshot = copy.deepcopy(fixture["snapshot"])
                    expected = build_fleet_state(fixture["snapshot"], now=fixture["now"] + delay)
                    with patch("time.time", return_value=fixture["now"] + delay):
                        data = store.read()
                    self.assertEqual(data["fleetState"], expected)
                    self.assertEqual(data["inference"]["fleetState"], expected)
                    self.assertNotIn("fleetState", store.snapshot)

    def test_both_servers_ship_the_selector_module(self):
        from server import ASSETS as mac_assets
        from agiw_win.server import ASSETS as windows_assets
        for assets in (mac_assets, windows_assets):
            self.assertEqual(assets["/fleet-state.mjs"], ("fleet-state.mjs", "text/javascript; charset=utf-8"))


if __name__ == "__main__":
    unittest.main()
