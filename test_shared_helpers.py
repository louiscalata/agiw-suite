"""Differential contracts for the two UTC formats and caller-relative Jaryo lookup.

The old helper bodies below are frozen from origin/main 4905bcb. Every replaced
function, and peer_link's deliberately retained local copy, is compared against its
own format on the same edge-input table; values, return types, exception classes and
exception arguments must remain identical.
"""

from __future__ import annotations

import ast
import datetime as dt
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import inference_collect
import machine_state
import peer_feed
import peer_link
import server
from windows.agiw_win import server as windows_server


class _IntSubclass(int):
    pass


class _FloatSubclass(float):
    pass


# Cases cover None, bool, integral/fractional/negative/huge numbers, NaN/infinity,
# strings/bytes (including empty and odd strings), datetime with/without tz, and
# built-in numeric subclasses. Both old UTC bodies receive every one of them.
TIMESTAMP_CASES = (
    ("none", None),
    ("false", False),
    ("true", True),
    ("zero", 0),
    ("positive integer", 1),
    ("negative integer", -1),
    ("negative fraction", -1.5),
    ("negative submillisecond", -0.000001),
    ("zero float", 0.0),
    ("submicrosecond", 0.0000001),
    ("microseconds", 0.123456),
    ("millisecond boundary", 1.999999),
    ("current-scale epoch", 1700000000.123456),
    ("huge integer", 10**400),
    ("huge negative integer", -(10**400)),
    ("huge float", 1e300),
    ("nan", float("nan")),
    ("positive infinity", float("inf")),
    ("negative infinity", float("-inf")),
    ("numeric string", "0"),
    ("empty string", ""),
    ("odd string", "2026-11-15T17:00:00Z\x00?"),
    ("numeric bytes", b"0"),
    ("empty bytes", b""),
    ("naive datetime", dt.datetime(2026, 11, 15, 17)),
    ("aware datetime", dt.datetime(2026, 11, 15, 17, tzinfo=dt.timezone.utc)),
    ("offset datetime", dt.datetime(2026, 11, 15, 17, tzinfo=dt.timezone(dt.timedelta(hours=-8)))),
    ("integer subclass", _IntSubclass(1)),
    ("float subclass", _FloatSubclass(0.123456)),
)


# Frozen inference_collect._iso body (when) and machine_state.utc_iso body
# (seconds) differ only in their parameter's spelling.
def _old_datetime_iso(when):
    return dt.datetime.fromtimestamp(when, dt.timezone.utc).isoformat().replace("+00:00", "Z")


# Frozen peer_link._iso and peer_feed._iso bodies are AST-identical. peer_link keeps
# its own copy so the module stays stdlib-only; peer_feed uses the shared one.
def _old_peer_iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + f".{int(ts % 1 * 1000):03d}Z"


# Frozen root and Windows jaryo_root bodies are AST-identical; source_file
# supplies the __file__ binding each original module read from its own globals.
def _old_jaryo_root(source_file):
    here = Path(source_file).resolve().parent
    for candidate in (here / "jaryo", here.parent / "jaryo", here.parent.parent / "jaryo"):
        if (candidate / "tokens.json").is_file():
            return candidate
    return here / "jaryo"


def _outcome(function, value):
    try:
        result = function(value)
    except (TypeError, ValueError, OverflowError, OSError) as error:
        return "error", type(error), error.args
    return "value", type(result), result


class TimestampDifferentialTests(unittest.TestCase):
    def test_datetime_format_matches_old_body_for_every_edge_input(self):
        for name, value in TIMESTAMP_CASES:
            for replacement in (machine_state.utc_iso, inference_collect._iso):
                with self.subTest(case=name, replacement=replacement.__module__):
                    self.assertEqual(_outcome(replacement, value), _outcome(_old_datetime_iso, value))

    def test_peer_format_matches_old_body_for_every_edge_input(self):
        for name, value in TIMESTAMP_CASES:
            for replacement in (machine_state.utc_iso_milliseconds, peer_link._iso, peer_feed._iso):
                with self.subTest(case=name, replacement=replacement.__module__):
                    self.assertEqual(_outcome(replacement, value), _outcome(_old_peer_iso, value))

    def test_formats_keep_their_existing_differences(self):
        # Integer epochs omit the fraction in datetime format; peer format always
        # has three digits. Submillisecond input truncates in the peer format;
        # datetime preserves microseconds and rounds at its own precision.
        differences = (
            (0, "1970-01-01T00:00:00Z", "1970-01-01T00:00:00.000Z"),
            (0.123456, "1970-01-01T00:00:00.123456Z", "1970-01-01T00:00:00.123Z"),
            (-0.000001, "1969-12-31T23:59:59.999999Z", "1969-12-31T23:59:59.999Z"),
            (1.999999, "1970-01-01T00:00:01.999999Z", "1970-01-01T00:00:01.999Z"),
        )
        for value, datetime_format, peer_format in differences:
            with self.subTest(value=value):
                self.assertEqual(machine_state.utc_iso(value), datetime_format)
                self.assertEqual(machine_state.utc_iso_milliseconds(value), peer_format)
                self.assertNotEqual(datetime_format, peer_format)

    def test_peer_local_copy_and_shared_formatter_agree_for_every_edge_input(self):
        for name, value in TIMESTAMP_CASES:
            with self.subTest(case=name):
                shared = _outcome(machine_state.utc_iso_milliseconds, value)
                self.assertEqual(_outcome(peer_link._iso, value), shared)
                self.assertEqual(_outcome(peer_feed._iso, value), shared)

    def test_peer_feed_uses_the_shared_formatter(self):
        self.assertIs(peer_feed._iso, machine_state.utc_iso_milliseconds)

    def test_peer_link_keeps_its_local_formatter_and_stdlib_only_imports(self):
        # The native composition harness copies peer_link.py alone into an
        # isolated child, so peer_link must not import a sibling module.
        self.assertIsNot(peer_link._iso, machine_state.utc_iso_milliseconds)
        self.assertEqual(peer_link._iso.__module__, "peer_link")
        source = Path(peer_link.__file__)
        siblings = {path.stem for path in source.parent.glob("*.py")}
        imported = set()
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported & siblings, set())

    def test_inference_wrapper_uses_shared_datetime_formatter(self):
        with mock.patch.object(inference_collect, "utc_iso", return_value="shared result") as formatter:
            self.assertEqual(inference_collect._iso(when=0.123456), "shared result")
            formatter.assert_called_once_with(0.123456)

    def test_private_keyword_arguments_remain_accepted(self):
        self.assertEqual(inference_collect._iso(when=0.123456), _old_datetime_iso(0.123456))
        self.assertEqual(peer_link._iso(ts=0.123456), _old_peer_iso(0.123456))
        self.assertEqual(peer_feed._iso(ts=0.123456), _old_peer_iso(0.123456))


class JaryoRootDifferentialTests(unittest.TestCase):
    def assert_lookup_matches_old(self, source_file):
        expected = _outcome(_old_jaryo_root, source_file)
        self.assertEqual(_outcome(machine_state.find_jaryo_root, source_file), expected)
        for observer in (server, windows_server):
            with self.subTest(observer=observer.__name__), mock.patch.object(observer, "__file__", source_file):
                actual = _outcome(lambda _value, module=observer: module.jaryo_root(), source_file)
                self.assertEqual(actual, expected)

    def test_lookup_layout_table_and_both_caller_anchors(self):
        # Depths index the old search order: sibling, parent, grandparent.
        layouts = (
            ("fallback", (), (), 0),
            ("sibling", (0,), (), 0),
            ("parent", (1,), (), 1),
            ("grandparent", (2,), (), 2),
            ("all prefer sibling", (0, 1, 2), (), 0),
            ("parent before grandparent", (1, 2), (), 1),
            ("tokens directory is not a file", (1,), (0,), 1),
            ("all tokens directories use fallback", (), (0, 1, 2), 0),
        )
        with tempfile.TemporaryDirectory(prefix="agiw-helper-layouts-") as directory:
            for name, file_depths, directory_depths, chosen in layouts:
                with self.subTest(layout=name):
                    here = Path(directory).resolve() / name / "grandparent" / "parent" / "here"
                    here.mkdir(parents=True)
                    source_file = here / "server.py"
                    candidates = (here / "jaryo", here.parent / "jaryo", here.parent.parent / "jaryo")
                    for depth in file_depths:
                        candidates[depth].mkdir()
                        (candidates[depth] / "tokens.json").write_text("{}", encoding="utf-8")
                    for depth in directory_depths:
                        (candidates[depth] / "tokens.json").mkdir(parents=True)
                    self.assertEqual(_old_jaryo_root(source_file), candidates[chosen])
                    self.assert_lookup_matches_old(source_file)
                    self.assert_lookup_matches_old(str(source_file))

    def test_source_symlink_uses_resolved_caller_directory(self):
        with tempfile.TemporaryDirectory(prefix="agiw-helper-symlink-") as directory:
            root = Path(directory).resolve()
            target = root / "resolved" / "server.py"
            target.parent.mkdir()
            target.touch()
            assets = target.parent / "jaryo"
            assets.mkdir()
            (assets / "tokens.json").write_text("{}", encoding="utf-8")
            source_file = root / "server-link.py"
            source_file.symlink_to(target)
            self.assert_lookup_matches_old(source_file)
            self.assertEqual(machine_state.find_jaryo_root(source_file), assets)

    def test_symlinked_jaryo_directory_is_returned_unresolved(self):
        with tempfile.TemporaryDirectory(prefix="agiw-helper-jaryo-link-") as directory:
            root = Path(directory).resolve()
            here = root / "here"
            here.mkdir()
            source_file = here / "server.py"
            source_file.touch()
            target = root / "real-jaryo"
            target.mkdir()
            (target / "tokens.json").write_text("{}", encoding="utf-8")
            (here / "jaryo").symlink_to(target, target_is_directory=True)
            self.assert_lookup_matches_old(source_file)
            self.assertEqual(machine_state.find_jaryo_root(source_file), here / "jaryo")
            self.assertNotEqual(machine_state.find_jaryo_root(source_file), target)

    def test_relative_empty_odd_and_invalid_source_values(self):
        values = (Path("relative/server.py"), "", "odd/../server.py", *[value for _name, value in TIMESTAMP_CASES])
        for value in values:
            with self.subTest(value=repr(value)):
                self.assert_lookup_matches_old(value)

    def test_each_wrapper_passes_its_own_source_file(self):
        sentinel = Path("shared-helper-result")
        for observer in (server, windows_server):
            with (
                self.subTest(observer=observer.__name__),
                mock.patch.object(observer, "__file__", "different-caller/server.py"),
                mock.patch.object(observer, "find_jaryo_root", return_value=sentinel) as lookup,
            ):
                self.assertIs(observer.jaryo_root(), sentinel)
                lookup.assert_called_once_with("different-caller/server.py")


if __name__ == "__main__":
    unittest.main()
