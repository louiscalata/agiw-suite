"""Isolated fixtures for passive client model identity sampling."""
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import client_models


class ClientModelsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.roots = {"codex": root / "codex", "claude": root / "claude",
                      "opencode": root / "opencode" / "opencode.db",
                      "cursor": root / "Cursor/User/globalStorage/state.vscdb",
                      "grok": root / "grok"}
        for name in ("codex", "claude", "opencode"):
            (root / name).mkdir()
        self.now = dt.datetime.now(dt.timezone.utc).timestamp()

    def _write_jsonl(self, client, records):
        root = self.roots[client]
        if client == "codex":
            root = root / dt.date.today().strftime("%Y/%m/%d")
            root.mkdir(parents=True)
        else:
            root = root / "project"
            root.mkdir()
        path = root / "session.jsonl"
        path.write_text("\n".join(json.dumps(item) for item in records) + "\n")
        return path

    def test_records_are_observed_but_activity_remains_unknown(self):
        instant = dt.datetime.fromtimestamp(self.now - 10, dt.timezone.utc).isoformat()
        self._write_jsonl("codex", [{"type": "turn_context", "timestamp": instant,
                                      "payload": {"model": "gpt-6-luna", "summary": "secret"}}])
        self._write_jsonl("claude", [{"type": "assistant", "timestamp": instant,
                                       "message": {"model": "claude-opus-5-5", "content": "secret"}}])
        clients, sources = client_models.collect_clients(self.now, roots=self.roots)
        rows = {row["id"]: row for row in clients}
        self.assertEqual(rows["codex"]["model"], "gpt-6-luna")
        self.assertEqual(rows["claude"]["model"], "claude-opus-5-5")
        self.assertEqual(rows["codex"]["modelState"], "observed")
        self.assertEqual(rows["claude"]["activity"], "unknown")
        self.assertEqual(rows["opencode"]["modelState"], "unknown")
        self.assertEqual(len(sources), 5)
        self.assertEqual({item["state"] for item in sources if item["id"] in
                          ("codex-metadata", "claude-metadata")}, {"recorded"})
        self.assertNotIn("secret", repr((clients, sources)))

    def test_codex_identity_behind_a_long_tool_tail_is_found_once(self):
        instant = dt.datetime.fromtimestamp(self.now - 10, dt.timezone.utc).isoformat()
        filler = {"type": "response_item", "timestamp": instant, "payload": {"text": "x" * 4000}}
        path = self._write_jsonl("codex", [{"type": "turn_context", "timestamp": instant,
                                             "payload": {"model": "gpt-6-sol"}}] + [filler] * 200)
        self.assertGreater(path.stat().st_size, client_models._MAX_TAIL)
        client_models._CODEX_DEEP.clear()
        clients, _ = client_models.collect_clients(self.now, roots=self.roots)
        self.assertEqual(next(r for r in clients if r["id"] == "codex")["model"], "gpt-6-sol")
        self.assertEqual(len(client_models._CODEX_DEEP), 1)
        with mock.patch.object(client_models, "_codex_turn_contexts",
                               side_effect=AssertionError("deep scan must not repeat")):
            clients, _ = client_models.collect_clients(self.now, roots=self.roots)
        self.assertEqual(next(r for r in clients if r["id"] == "codex")["model"], "gpt-6-sol")

    def test_future_record_is_not_fresh_identity(self):
        future = dt.datetime.fromtimestamp(self.now + 30, dt.timezone.utc).isoformat()
        self._write_jsonl("codex", [{"type": "turn_context", "timestamp": future,
                                      "payload": {"model": "future-model"}}])
        clients, sources = client_models.collect_clients(self.now, roots=self.roots)
        self.assertIsNone(next(row for row in clients if row["id"] == "codex")["model"])
        self.assertEqual(next(row for row in sources if row["id"] == "codex-metadata")["state"],
                         "unavailable")

    def test_opencode_observed_wins_over_session_choice(self):
        path = self.roots["opencode"]
        db = sqlite3.connect(path)
        db.executescript("""
            CREATE TABLE session(id TEXT PRIMARY KEY, model TEXT, time_updated INTEGER);
            CREATE TABLE message(session_id TEXT, data TEXT, time_created INTEGER);
            CREATE INDEX message_session_time_created_id_idx ON message(session_id,time_created);
        """)
        ms = int((self.now - 6) * 1000)
        db.execute("INSERT INTO session VALUES(?,?,?)", ("private-session",
                   json.dumps({"id": "configured-model"}), ms))
        db.execute("INSERT INTO message VALUES(?,?,?)", ("private-session", json.dumps({
            "role": "assistant", "modelID": "actual-model", "providerID": "opencode",
            "time": {"created": ms}, "content": "secret"}), ms))
        db.commit()
        db.close()
        clients, _ = client_models.collect_clients(self.now, roots=self.roots)
        row = next(row for row in clients if row["id"] == "opencode")
        self.assertEqual((row["model"], row["modelState"]), ("actual-model", "observed"))
        self.assertEqual(row["activity"], "unknown")
        self.assertNotIn("secret", repr(row))
        self.assertNotIn("private-session", repr(row))

    def test_configured_fallback_and_invalid_model_rejection(self):
        path = self.roots["opencode"]
        db = sqlite3.connect(path)
        db.executescript("CREATE TABLE session(id TEXT, model TEXT, time_updated INTEGER);"
                         "CREATE TABLE message(session_id TEXT, data TEXT, time_created INTEGER);")
        db.execute("INSERT INTO session VALUES(?,?,?)", ("s", json.dumps({"id": "chosen-model"}),
                                                int(self.now * 1000)))
        db.commit()
        db.close()
        instant = dt.datetime.fromtimestamp(self.now - 1, dt.timezone.utc).isoformat()
        self._write_jsonl("claude", [{"type": "assistant", "timestamp": instant,
                                       "message": {"model": "bad model<script>"}}])
        clients, _ = client_models.collect_clients(self.now, roots=self.roots)
        rows = {row["id"]: row for row in clients}
        self.assertEqual(rows["opencode"]["modelState"], "configured")
        self.assertEqual(rows["claude"]["modelState"], "unknown")

    def test_future_observed_identity_does_not_hide_valid_configured_fallback(self):
        observations = [
            ("future-observed", self.now + 30, "opencode-assistant-record", "observed"),
            ("valid-choice", self.now - 5, "opencode-session-choice", "configured"),
        ]
        row, source = client_models._project("opencode", observations, self.now)
        self.assertEqual((row["model"], row["modelState"]),
                         ("valid-choice", "configured"))
        self.assertEqual(source["state"], "recorded")

    def test_safe_rejects_windows_reparse_point_in_path(self):
        root = self.roots["claude"]
        project = root / "project"
        project.mkdir()
        record = project / "session.jsonl"
        record.write_text("{}\n")
        with mock.patch.object(client_models, "_is_windows_reparse_point",
                               side_effect=lambda value: value == project):
            self.assertFalse(client_models._safe(record, root))

    def test_opencode_detects_path_swap_after_sqlite_open(self):
        path = self.roots["opencode"]
        original = sqlite3.connect(path)
        original.executescript("CREATE TABLE session(id TEXT, model TEXT, time_updated INTEGER);"
                               "CREATE TABLE message(session_id TEXT, data TEXT, time_created INTEGER);")
        original.close()
        replacement = path.with_name("replacement.db")
        other = sqlite3.connect(replacement)
        other.executescript("CREATE TABLE session(id TEXT, model TEXT, time_updated INTEGER);"
                            "CREATE TABLE message(session_id TEXT, data TEXT, time_created INTEGER);")
        other.close()
        real_connect = sqlite3.connect

        def swap_then_connect(*args, **kwargs):
            path.unlink()
            path.symlink_to(replacement)
            return real_connect(*args, **kwargs)

        with mock.patch.object(client_models.sqlite3, "connect", side_effect=swap_then_connect):
            with self.assertRaises(OSError):
                client_models._opencode_observations(path, __import__("time").monotonic() + .35)

    def test_opencode_read_only_observer_reads_live_wal(self):
        path = self.roots["opencode"]
        writer = sqlite3.connect(path)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.executescript("CREATE TABLE session(id TEXT, model TEXT, time_updated INTEGER);"
                             "CREATE TABLE message(session_id TEXT, data TEXT, time_created INTEGER);")
        ms = int((self.now - 2) * 1000)
        writer.execute("INSERT INTO session VALUES(?,?,?)",
                       ("live", json.dumps({"id": "wal-model"}), ms))
        writer.commit()
        clients, _ = client_models.collect_clients(self.now, roots=self.roots)
        row = next(item for item in clients if item["id"] == "opencode")
        self.assertEqual((row["model"], row["modelState"]), ("wal-model", "configured"))

    def test_resumed_older_opencode_session_is_seen_by_recent_message(self):
        path = self.roots["opencode"]
        db = sqlite3.connect(path)
        db.executescript("CREATE TABLE session(id TEXT PRIMARY KEY, model TEXT, time_updated INTEGER);"
                         "CREATE TABLE message(session_id TEXT, data TEXT, time_created INTEGER);")
        old = int((self.now - 3600) * 1000)
        db.execute("INSERT INTO session VALUES(?,?,?)", ("resumed-old-session", None, old))
        for index in range(40):
            db.execute("INSERT INTO session VALUES(?,?,?)", (f"newer-{index}", None,
                                                          old + index))
        recent = int((self.now - 5) * 1000)
        db.execute("INSERT INTO message VALUES(?,?,?)", ("resumed-old-session", json.dumps({
            "role": "assistant", "modelID": "resumed-model", "time": "malformed",
            "content": "secret"}), recent))
        db.commit()
        db.close()
        clients, _ = client_models.collect_clients(self.now, roots=self.roots)
        row = next(item for item in clients if item["id"] == "opencode")
        self.assertEqual((row["model"], row["modelState"]), ("resumed-model", "observed"))
        self.assertNotIn("secret", repr(row))

    def test_missing_current_codex_day_does_not_hide_prior_day(self):
        yesterday = dt.date.today() - dt.timedelta(days=1)
        folder = self.roots["codex"] / yesterday.strftime("%Y/%m/%d")
        folder.mkdir(parents=True)
        instant = dt.datetime.fromtimestamp(self.now - 10, dt.timezone.utc).isoformat()
        (folder / "session.jsonl").write_text(json.dumps({"type": "turn_context",
            "timestamp": instant, "payload": {"model": "gpt-6-luna"}}) + "\n")
        clients, _ = client_models.collect_clients(self.now, roots=self.roots)
        row = next(item for item in clients if item["id"] == "codex")
        self.assertEqual(row["model"], "gpt-6-luna")

    def test_symlinked_session_is_ignored(self):
        instant = dt.datetime.fromtimestamp(self.now - 1, dt.timezone.utc).isoformat()
        real = Path(self.temp.name) / "external.jsonl"
        real.write_text(json.dumps({"type": "assistant", "timestamp": instant,
                                    "message": {"model": "claude-secret"}}) + "\n")
        link_root = self.roots["claude"] / "other"
        link_root.mkdir()
        (link_root / "session.jsonl").symlink_to(real)
        clients, _ = client_models.collect_clients(self.now, roots=self.roots)
        self.assertIsNone(next(row for row in clients if row["id"] == "claude")["model"])

    def test_cursor_and_grok_are_explicitly_unknown_without_supported_identity_records(self):
        cursor = self.roots["cursor"]
        cursor.parent.mkdir(parents=True)
        # A local editor database can exist without exposing a supported,
        # identity-only model record. Presence alone must not imply a model.
        cursor.write_bytes(b"SQLite format 3\x00")
        clients, sources = client_models.collect_clients(self.now, roots=self.roots)
        rows = {row["id"]: row for row in clients}
        source_rows = {row["id"]: row for row in sources}
        for client in ("cursor", "grok"):
            self.assertIsNone(rows[client]["model"])
            self.assertEqual(rows[client]["modelState"], "unknown")
            self.assertEqual(rows[client]["activity"], "unknown")
            self.assertEqual(rows[client]["models"], [])
            self.assertEqual(source_rows[f"{client}-metadata"]["state"], "unavailable")
        self.assertIn("database found; no supported model identity field", rows["cursor"]["detail"])
        self.assertIn("No supported bounded local Grok model metadata source", rows["grok"]["detail"])

    def test_cursor_symlink_is_not_reported_as_a_present_local_database(self):
        cursor = self.roots["cursor"]
        cursor.parent.mkdir(parents=True)
        target = Path(self.temp.name) / "external-cursor-db"
        target.write_bytes(b"SQLite format 3\x00")
        cursor.symlink_to(target)
        clients, _ = client_models.collect_clients(self.now, roots=self.roots)
        row = next(item for item in clients if item["id"] == "cursor")
        self.assertEqual(row["modelState"], "unknown")
        self.assertIn("No supported bounded Cursor model metadata", row["detail"])


if __name__ == "__main__":
    unittest.main()
