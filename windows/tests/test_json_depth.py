"""JSON depth bounds do not depend on the interpreter's parser recursion limit."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agiw_win import probes


class JsonDepth(unittest.TestCase):
    def test_over_limit_arrays_objects_and_mixed_containers_are_refused(self):
        for opening, closing in (("[", "]"), ('{"x":', "}"), ('[{"x":', "}]")):
            with self.subTest(opening=opening):
                with self.assertRaisesRegex(ValueError, "JSON nested too deeply"):
                    probes.loads_strict(opening * 65 + "0" + closing * 65)

    def test_depth_is_checked_before_calling_the_json_parser(self):
        with mock.patch.object(probes.json, "loads", return_value=[]) as parser:
            with self.assertRaisesRegex(ValueError, "JSON nested too deeply"):
                probes.loads_strict("[" * 65 + "0" + "]" * 65)
        parser.assert_not_called()

    def test_boundary_accepts_arrays_objects_and_mixed_containers(self):
        for opening, closing, count in (("[", "]", 64), ('{"x":', "}", 64),
                                        ('[{"x":', "}]", 32)):
            with self.subTest(opening=opening):
                text = opening * count + "0" + closing * count
                self.assertEqual(probes.loads_strict(text), json.loads(text))

    def test_quoted_brackets_and_escaped_quotes_do_not_change_depth(self):
        for value in ("[{}]" * 100, '\\"' + "[" * 100, "\\", '"', "\\\\"):
            with self.subTest(value=value):
                text = "[" * 64 + json.dumps(value) + "]" * 64
                parsed = probes.loads_strict(text)
                for _ in range(64):
                    parsed = parsed[0]
                self.assertEqual(parsed, value)

    def test_sibling_containers_do_not_accumulate_depth(self):
        branch = "[" * 63 + "0" + "]" * 63
        text = "[" + ",".join([branch] * 100) + "]"
        self.assertEqual(len(probes.loads_strict(text)), 100)

    def test_below_byte_cap_nested_manifest_is_malformed_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "20261017-000000-fixture"
            run.mkdir()
            for depth in (64, 50000):
                with self.subTest(depth=depth):
                    text = '{"metrics":[],"ignored":' + "[" * depth + "]" * depth + "}"
                    self.assertLess(len(text.encode("utf-8")), probes.CODEMODE_MANIFEST_MAX)
                    (run / "manifest.json").write_text(text, encoding="utf-8")
                    jobs, source = probes.codemode_jobs(root, [], 1000)
                    self.assertEqual(jobs["availability"]["state"], "unknown")
                    self.assertFalse(jobs["liveAvailable"])
                    self.assertEqual(source["state"], "unavailable")
                    self.assertIn("1 malformed manifest", source["detail"])
