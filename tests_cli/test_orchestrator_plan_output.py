"""Planner-output contracts through the real plan path, without a provider call."""

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agiw import orchestrator_mcp as om


class PlanOutputTestCase(unittest.TestCase):
    def brief(self, **changes):
        return {
            "title": "  Keep exact title  ",
            "goal": "Preserve the valid plan",
            "steps": ["One step"],
            "acceptance": ["One check"],
            "risks": [],
            "lane_hint": "human",
            **changes,
        }

    def invoke(self, output, *, payer="openai", returncode=0, project="agiw-suite"):
        # The real resolver, temporary prompt, validation, receipt and cleanup run.
        # Only the provider subprocess is replaced; no fake guard is executed.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "free_pool_call.py"
            script.write_text("raise AssertionError('must never execute')\n", encoding="utf-8")
            orchestrator = om.Orchestrator(om.Config(root=root / "queue", free_pool_call=str(script)))
            envelope = {
                "output_text": output,
                "model": "gpt-5.6-terra",
                "response_id": "resp_fixture",
                "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
                "billing": {"payer": payer} if payer else {},
            }
            completed = subprocess.CompletedProcess([], returncode, json.dumps(envelope), "")
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(om.subprocess, "run", return_value=completed) as run,
            ):
                result = orchestrator.call_tool("plan", {"request": "Draft a small task.", "project": project})
            receipts = orchestrator._receipt_rows()
            if project in om.POOL_PROJECTS:
                run.assert_called_once()
                command = run.call_args.args[0]
                self.assertEqual(command[command.index("--project") + 1], project)
                self.assertEqual(command[command.index("--lane") + 1], om.LANE)
                self.assertIn("--no-reroute", command)
                self.assertEqual(command[command.index("--max-output") + 1], "600")
                self.assertFalse(Path(run.call_args.kwargs["cwd"]).exists())
                self.assertEqual(len(receipts), 1)
                if os.name != "nt":
                    self.assertEqual(stat.S_IMODE(orchestrator.receipt_path.stat().st_mode), 0o600)
            else:
                run.assert_not_called()
                self.assertEqual(receipts, [])
            self.assertFalse(orchestrator.inbox.exists(), "Planning must never enqueue work")
            receipt_text = orchestrator.receipt_path.read_text() if receipts else ""
            self.assertNotIn("output_text", receipt_text)
            self.assertNotIn("Draft a small task.", receipt_text)
            return result, receipts, orchestrator.paused_path.exists()

    def invalid(self, output):
        result, receipts, paused = self.invoke(output)
        self.assertTrue(result["isError"], "Invalid planner output was accepted")
        self.assertEqual(result["structuredContent"], {"code": "OUTPUT_INVALID"})
        self.assertEqual(receipts[0]["outcome"], "BLOCKED")
        self.assertEqual(receipts[0]["code"], "OUTPUT_INVALID")
        self.assertEqual(receipts[0]["payer"], "openai")
        self.assertEqual(receipts[0]["usage"]["total_tokens"], 5)
        self.assertFalse(paused, "Schema refusal must not pretend the payer failed")

    def test_rejects_malformed_nonobject_and_surrounding_prose(self):
        valid = json.dumps(self.brief())
        for output in (
            "plain text",
            "{",
            "[]",
            "null",
            '"text"',
            "true",
            "3",
            "Here is the plan:\n" + valid,
            valid + "\nDone",
            valid + valid,
        ):
            with self.subTest(output=output):
                self.invalid(output)

    def test_existing_blank_or_wrong_type_envelope_refusal_is_preserved(self):
        for output in ("", " \t\n", None, False, 1, [], {}):
            with self.subTest(output=output):
                result, receipts, paused = self.invoke(output)
                self.assertEqual(result["structuredContent"], {"code": "RESPONSE_INVALID"})
                self.assertTrue(result["isError"])
                self.assertEqual(receipts[0]["outcome"], "BLOCKED")
                self.assertEqual(receipts[0]["code"], "RESPONSE_INVALID")
                self.assertFalse(paused)

    def test_rejects_duplicate_keys_instead_of_falling_back_to_text(self):
        valid = json.dumps(self.brief())
        self.invalid('{"title":"other",' + valid[1:])
        self.invalid('{"lane_hint":"codex",' + valid[1:])

    def test_rejects_missing_or_unknown_keys(self):
        for key in self.brief():
            value = self.brief()
            del value[key]
            with self.subTest(missing=key):
                self.invalid(json.dumps(value))
        self.invalid(json.dumps(self.brief(execute=True)))
        self.invalid("{}")

    def test_rejects_nonstring_or_blank_title_and_goal(self):
        for key in ("title", "goal"):
            for value in (None, True, 1, [], {}, "", " \t\n"):
                with self.subTest(key=key, value=value):
                    self.invalid(json.dumps(self.brief(**{key: value})))

    def test_rejects_wrong_array_types_entries_and_excess_lengths(self):
        for key, maximum in (("steps", 8), ("acceptance", 5), ("risks", 3)):
            for value in (
                None,
                False,
                0,
                "step",
                {},
                [None],
                [True],
                [1],
                [[]],
                [{}],
                [""],
                [" \t\n"],
                ["entry"] * (maximum + 1),
            ):
                with self.subTest(key=key, value=value):
                    self.invalid(json.dumps(self.brief(**{key: value})))

    def test_rejects_unknown_blank_or_wrong_type_lane(self):
        for value in ("", " ", "other", "CODEX", None, False, 1, [], {}):
            with self.subTest(lane=value):
                self.invalid(json.dumps(self.brief(lane_hint=value)))

    def test_valid_boundaries_preserve_exact_brief_and_metadata(self):
        for lane in ("codex", "opencode", "pc-worker", "monterey-worker", "human"):
            for full in (False, True):
                expected = self.brief(
                    lane_hint=lane,
                    steps=[" step "] * (8 if full else 0),
                    acceptance=["check"] * (5 if full else 0),
                    risks=["risk"] * (3 if full else 0),
                )
                with self.subTest(lane=lane, full=full):
                    result, receipts, paused = self.invoke(" \n" + json.dumps(expected) + "\t")
                    self.assertFalse(result["isError"])
                    self.assertEqual(
                        result["structuredContent"],
                        {
                            "brief": expected,
                            "model_served": "gpt-5.6-terra",
                            "response_id": "resp_fixture",
                            "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
                            "payer": "openai",
                        },
                    )
                    self.assertEqual(receipts[0]["outcome"], "COMPLETED")
                    self.assertEqual(receipts[0]["code"], "COMPLETED")
                    self.assertFalse(paused)

    def test_project_payer_budget_and_credit_guards_remain_prior_to_output(self):
        for project in om.POOL_PROJECTS:
            result, _, _ = self.invoke(json.dumps(self.brief()), project=project)
            self.assertFalse(result["isError"])
        result, _, _ = self.invoke("invalid", project="other")
        self.assertEqual(result["structuredContent"]["code"], "PROJECT_INVALID")
        for payer, returncode, code, paused in (
            ("developer", 0, "PAYER_REFUSED", True),
            ("", 0, "PAYER_REFUSED", True),
            ("openai", 7, "PAYER_REFUSED", True),
            ("openai", 10, "BUDGET_REFUSED", False),
            ("openai", 9, "CREDIT_EXHAUSTED", False),
        ):
            with self.subTest(payer=payer, returncode=returncode):
                result, receipts, is_paused = self.invoke("invalid", payer=payer, returncode=returncode)
                self.assertEqual(result["structuredContent"]["code"], code)
                self.assertEqual(receipts[0]["outcome"], "BLOCKED")
                self.assertEqual(receipts[0]["code"], code)
                self.assertEqual(is_paused, paused)
