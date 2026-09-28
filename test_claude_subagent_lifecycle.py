"""Bounded, content-free Claude hook lifecycle integration fixtures."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

import client_models


NOW = dt.datetime(2026, 9, 27, 20, tzinfo=dt.timezone.utc)


class ClaudeSubagentLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "events.jsonl"

    def write(self, rows):
        self.path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        self.path.chmod(0o600)

    def event(self, kind, agent="agent-1", owner="session-1", seconds=0):
        raw = {"hook_event_name": kind, "session_id": owner, "agent_id": agent,
               "agent_type": "Explore", "transcript_path": "/private/session",
               "last_assistant_message": "private answer"}
        return client_models.normalize_claude_subagent_hook(
            raw, NOW + dt.timedelta(seconds=seconds))

    def summary(self, *, owner="session-1", complete=True, seconds=2):
        return client_models.collect_claude_subagents(
            (NOW + dt.timedelta(seconds=seconds)).timestamp(), feed_path=self.path,
            owner_session_id=owner, source_complete=complete)

    def test_normalized_hook_drops_content_and_rejects_unsupported(self):
        row = self.event("SubagentStart")
        self.assertEqual((row["ownerSessionId"], row["agentId"], row["event"]),
                         ("session-1", "agent-1", "start"))
        self.assertNotIn("private", repr(row))
        with self.assertRaises(ValueError):
            self.event("Stop")

    def test_default_client_row_is_unknown_and_not_based_on_transcript(self):
        root = Path(self.temp.name)
        roots = {"claude": root / "claude", "codex": root / "codex",
                 "opencode": root / "opencode.db", "cursor": root / "cursor",
                 "grok": root / "grok"}
        roots["claude"].mkdir()
        project = roots["claude"] / "project"
        project.mkdir()
        (project / "session.jsonl").write_text(json.dumps({
            "type": "assistant", "timestamp": NOW.isoformat(),
            "message": {"model": "claude-fable-5-1", "content": "active?"}}) + "\n")
        rows, _ = client_models.collect_clients(NOW.timestamp(), roots=roots)
        claude = next(row for row in rows if row["id"] == "claude")
        self.assertEqual(claude["subagents"]["reason"], "unconfigured")
        self.assertIsNone(claude["subagents"]["active"])

    def test_exact_owner_complete_start_terminal_establishes_zero(self):
        self.write([self.event("SubagentStart"),
                    self.event("SubagentStop", seconds=1),
                    self.event("SubagentStart", owner="other-session", seconds=1)])
        result = self.summary()
        self.assertEqual((result["state"], result["active"]), ("known", 0))
        self.assertEqual(result["ownerSessionId"], "session-1")
        self.assertEqual(result["observedOpenCount"], 0)

    def test_unmatched_start_does_not_claim_live(self):
        self.write([self.event("SubagentStart")])
        result = self.summary()
        self.assertEqual(result["state"], "unknown")
        self.assertIsNone(result["active"])
        self.assertEqual(result["observedOpenCount"], 1)
        self.assertEqual(result["reason"], "unmatched-start-no-child-process-witness")

    def test_explicit_feed_reaches_client_row_without_changing_model_activity(self):
        self.write([self.event("SubagentStart"), self.event("SubagentStop", seconds=1)])
        root = Path(self.temp.name)
        roots = {"claude": root / "claude", "codex": root / "codex",
                 "opencode": root / "opencode.db", "cursor": root / "cursor",
                 "grok": root / "grok"}
        roots["claude"].mkdir()
        rows, _ = client_models.collect_clients(
            (NOW + dt.timedelta(seconds=2)).timestamp(), roots=roots,
            subagent_feed_path=self.path, subagent_owner_session_id="session-1",
            subagent_source_complete=True)
        claude = next(row for row in rows if row["id"] == "claude")
        self.assertEqual(claude["subagents"]["active"], 0)
        self.assertEqual(claude["activity"], "unknown")

    def test_partial_missing_stale_and_future_are_unknown(self):
        self.write([self.event("SubagentStop")])
        self.assertEqual(self.summary(complete=False)["reason"], "partial-source")
        self.assertEqual(self.summary(seconds=31)["reason"], "stale-source")
        self.assertEqual(self.summary(seconds=-1)["reason"], "clock-skew")
        self.assertEqual(self.summary(owner="another")["reason"], "missing-events")

    def test_untrusted_and_malformed_feed_fail_closed(self):
        self.write([self.event("SubagentStop")])
        self.path.chmod(0o644)
        self.assertEqual(self.summary()["reason"], "unavailable-or-invalid-feed")
        self.path.chmod(0o600)
        self.path.write_text('{"schemaVersion":1,"schemaVersion":1}\n')
        self.assertEqual(self.summary()["reason"], "unavailable-or-invalid-feed")
        self.write([{"schemaVersion": 1, "client": "claude-code", "event": "start",
                     "ownerSessionId": "session-1", "agentId": "agent-1",
                     "observedAt": "not-a-time"}])
        self.assertEqual(self.summary()["reason"], "malformed-event")

    def test_content_fields_naive_time_and_orphan_terminal_fail_closed(self):
        row = self.event("SubagentStart")
        self.write([{**row, "last_assistant_message": "private"}])
        self.assertEqual(self.summary()["reason"], "malformed-event")
        self.write([{**row, "observedAt": "2026-09-27T20:00:00"}])
        self.assertEqual(self.summary()["reason"], "malformed-event")
        self.write([self.event("SubagentStop")])
        self.assertEqual(self.summary()["reason"], "inconsistent-events")


if __name__ == "__main__":
    unittest.main()
