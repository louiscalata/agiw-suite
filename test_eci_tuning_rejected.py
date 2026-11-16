"""Rejected fast checks reach the /api/eci-tuning status view. Offline; no model or server calls."""

import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import eci_tuning as e


def entry(i):
    return {"record_id": f"{i:064x}", "reason": "summary disagrees with observations"}


class RejectedFastCheckViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=e.ROOT)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"AGIW_ECI_TUNING_CONFIG": str(self.directory / "policy.json")})
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("AGIW_ECI_TUNING_DATASET_DIR", None)

    def dataset(self, rejected):
        return dict(e.ingest([e.CATALOG]), rejected_fast_checks=rejected)

    def settled(self):
        for _ in range(100):
            result = e.status()
            if "in progress" not in (result.get("dataset", {}).get("reason") or ""):
                return result
            time.sleep(0.01)
        self.fail("fixture validation did not settle")

    def test_bundled_view_lists_no_rejections_with_a_note(self):
        result = e.status()
        self.assertEqual(result["rejected_fast_checks"], [])
        self.assertEqual(result["rejected_fast_checks_note"], "No fast-check record failed validation.")

    def test_supplied_rejection_is_listed_with_its_count(self):
        result = e.status(self.dataset([entry(1)]))
        self.assertEqual(result["rejected_fast_checks"], [entry(1)])
        self.assertEqual(
            result["rejected_fast_checks_note"], "1 fast-check record failed validation and is not ranked."
        )
        json.dumps(result, allow_nan=False)

    def test_view_is_bounded_and_the_note_keeps_the_full_count(self):
        rejected = [entry(i) for i in range(e.MAX_REJECTED_FAST_CHECKS_VIEW + 5)]
        result = e.status(self.dataset(rejected))
        self.assertEqual(result["rejected_fast_checks"], rejected[: e.MAX_REJECTED_FAST_CHECKS_VIEW])
        self.assertEqual(
            result["rejected_fast_checks_note"],
            f"{len(rejected)} fast-check records failed validation and are not ranked. "
            f"Showing the first {e.MAX_REJECTED_FAST_CHECKS_VIEW}.",
        )

    def test_view_does_not_change_ranking_or_the_dataset(self):
        data = self.dataset([entry(1), entry(2)])
        before = copy.deepcopy(data)
        result = e.status(data)
        self.assertEqual(data, before)
        self.assertEqual(result["rankings"], e.status(self.dataset([]))["rankings"])
        result["rejected_fast_checks"][0]["reason"] = "changed"
        self.assertEqual(data, before)

    def test_malformed_supplied_entry_is_refused_not_echoed(self):
        for bad in (
            {"record_id": "private text", "reason": "r"},
            {"record_id": None, "reason": "x" * 201},
            {"record_id": None, "reason": "r", "extra": "private"},
        ):
            with self.subTest(entry=bad), self.assertRaises(e.ValidationError):
                e.status(self.dataset([bad]))

    def test_configured_dataset_rejection_reaches_the_status_view(self):
        (self.directory / "dataset.json").write_text(
            json.dumps(
                dict(schema_version=1, runs=[], items=[], fast_checks=[{"record_id": "private text, not a digest"}])
            ),
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"AGIW_ECI_TUNING_DATASET_DIR": str(self.directory)}):
            result = self.settled()
        self.assertEqual(result["dataset"]["status"], "configured")
        self.assertEqual(len(result["rejected_fast_checks"]), 1)
        self.assertIsNone(result["rejected_fast_checks"][0]["record_id"])
        self.assertNotIn("private", json.dumps(result["rejected_fast_checks"]))
        self.assertEqual(
            result["rejected_fast_checks_note"], "1 fast-check record failed validation and is not ranked."
        )


if __name__ == "__main__":
    unittest.main()
