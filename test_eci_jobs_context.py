"""Lane-context pre-check: registry facts only, fake runner, no socket, no model call."""

from pathlib import Path
import unittest

from test_eci_jobs import LANE, OTHER, EngineCase, request, targets


def lanes(context, lane=LANE):
    value = targets()
    for row in value:
        if row["id"] == lane:
            row["contextTokens"] = context
    return value


class ContextTests(EngineCase):
    def codes(self, value=None, engine=None):
        return [row["code"] for row in (engine or self.engine).preview(value or request())["refusals"]]

    def test_a_request_above_the_advertised_context_refuses_before_anything_is_written(self):
        self.lanes = lanes(8192)
        value = request(max_tokens=16384)
        preview = self.engine.preview(value)
        self.assertEqual(preview["effects"]["lane_context_tokens"], 8192)
        self.assertEqual([row for row in preview["refusals"] if row["code"] == "context_exceeded"], [{
            "code": "context_exceeded",
            "message": "The reviewed max_tokens exceeds the lane's advertised context, "
                       "or that context is not a valid number."}])
        review = {"reviewed": True, "review_sha256": preview["review_sha256"]}
        self.refused("context_exceeded", self.engine.submit, value, review)
        self.assertEqual(self.journal(), b"")
        self.assertIsNone(self.engine.run_next())
        self.assertEqual(self.runner.calls, [])

    def test_the_default_max_tokens_is_checked_too(self):
        self.lanes = lanes(16383)
        self.assertIn("context_exceeded", self.codes(request()))
        self.lanes = lanes(16384)
        self.assertEqual(self.codes(request()), [])

    def test_a_request_at_or_below_the_advertised_context_is_admitted(self):
        for context, tokens in ((8192, 8192), (16384, 16384), (32768, 8192)):
            with self.subTest(context=context, tokens=tokens):
                self.lanes = lanes(context)
                value = request(max_tokens=tokens, submission_id="submission-%d-%d" % (context, tokens))
                preview = self.engine.preview(value)
                self.assertEqual(preview["refusals"], [])
                self.assertEqual(preview["effects"]["lane_context_tokens"], context)
                self.assertEqual(self.submit(value)["state"], "queued")

    def test_a_lane_that_advertises_no_context_is_unknown_and_not_refused_here(self):
        preview = self.engine.preview(request())
        self.assertIsNone(preview["effects"]["lane_context_tokens"])
        self.assertEqual(preview["refusals"], [])
        self.assertEqual(self.submit()["state"], "queued")

    def test_an_advertised_context_that_is_not_a_nonnegative_integer_refuses(self):
        for bad in (True, False, "8192", 1.5, 65536.0, -1, None, [65536], {"tokens": 65536}):
            with self.subTest(context=bad):
                self.lanes = lanes(bad)
                preview = self.engine.preview(request())
                self.assertIn("context_exceeded", [row["code"] for row in preview["refusals"]])
                self.assertIsNone(preview["effects"]["lane_context_tokens"])
                self.refused("context_exceeded", self.engine.submit, request(), self.reviewed())
        self.assertEqual(self.journal(), b"")

    def test_an_advertised_context_of_zero_refuses_every_request(self):
        self.lanes = lanes(0)
        self.assertEqual(self.engine.preview(request(max_tokens=8192))["effects"]["lane_context_tokens"], 0)
        self.refused("context_exceeded", self.engine.submit, request(max_tokens=8192),
                     self.reviewed(request(max_tokens=8192)))

    def test_the_context_is_read_again_at_start_and_the_runner_is_not_called(self):
        self.lanes = lanes(32768)
        job = self.submit(request(max_tokens=16384))
        self.lanes = lanes(8192)
        done = self.engine.run_next()
        self.assertEqual((done["state"], done["reason"]), ("failed", "Refused at start: context_exceeded"))
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.sink.records, [])
        # The same reviewed request cannot be resumed while the lane advertises too little.
        again = self.engine.preview_resume(job["job_id"])
        self.assertIn("context_exceeded", [row["code"] for row in again["refusals"]])
        review = {"reviewed": True, "review_sha256": again["review_sha256"]}
        self.refused("context_exceeded", self.engine.resume, job["job_id"], review)
        self.lanes = lanes(16384)
        self.assertEqual(self.engine.resume(job["job_id"], review)["state"], "queued")
        self.assertEqual(self.engine.run_next()["state"], "finished")
        self.assertEqual(len(self.runner.calls), 1)

    def test_a_preview_lists_the_context_refusal_beside_the_target_refusal(self):
        self.lanes = lanes(8192, lane=OTHER)
        self.assertEqual(self.codes(request(target=OTHER)), ["target_not_allowed", "context_exceeded"])
        # A lane that is not in the registry has no advertised context to compare.
        self.assertEqual(self.codes(request(target="registry:bench:absent")), ["unknown_target"])
        self.lanes = lanes(8192) + lanes(8192)
        self.assertEqual(self.codes(), ["unknown_target"])

    def test_the_review_digest_does_not_depend_on_the_advertised_context(self):
        before = self.engine.preview(request())["review_sha256"]
        self.lanes = lanes(8192)
        self.assertEqual(self.engine.preview(request())["review_sha256"], before)

    def test_the_pre_check_reads_the_registry_and_nothing_else(self):
        seen = []

        def listed():
            seen.append(1)
            return lanes(8192)

        engine = self.open(state=self.root / "counted", targets=listed, runner=None, sink=None)
        self.assertIn("context_exceeded", self.codes(engine=engine))
        self.assertTrue(seen)
        self.assertEqual(self.journal(self.root / "counted"), b"")

    def test_the_refusal_is_a_stable_documented_code(self):
        text = (Path(__file__).resolve().parent / "docs" / "eci-check-jobs.md").read_text(encoding="utf-8")
        self.assertTrue("`context_exceeded`" in text, "the refusal code is not in docs/eci-check-jobs.md")

if __name__ == "__main__":
    unittest.main()
