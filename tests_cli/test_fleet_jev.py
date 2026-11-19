import io
import json
import unittest
from unittest.mock import Mock, patch

from agiw import fleet_jev as jev
from agiw import fleet_router_client as rc
from agiw.windows_worker import JEV_LIMIT

PROGRAM = "tiny-arithmetic.mjs"
WORKER = "m5-macbook-pro"
KEY = "fake-test-credential"


def response():
    return {"model": jev.JEV_MODEL, "answers": {"plan": {"choice": "direct"}, "underspecified": {"noul": 0.49}}}


def opener(raw, status=200):
    stream = io.BytesIO(raw)
    stream.status = status
    return Mock(open=Mock(return_value=stream))


class JevTests(unittest.TestCase):
    def test_exact_post_and_optional_confidence(self):
        transport = opener(json.dumps(response()).encode())
        judge = jev.JevJudge(KEY, opener=transport)
        evidence = jev.preanalyze(PROGRAM, judge)
        transport.open.assert_called_once()
        req = transport.open.call_args.args[0]
        self.assertEqual((req.method, req.full_url), ("POST", jev.JEV_ENDPOINT))
        self.assertEqual(req.get_header("Authorization"), "Bearer " + KEY)
        self.assertEqual(transport.open.call_args.kwargs, {"timeout": 60})
        self.assertEqual(json.loads(req.data), dict(evidence["request"], model=jev.JEV_MODEL))
        self.assertEqual(evidence["request"]["state"], jev.task_state(PROGRAM))
        self.assertEqual(evidence["request"]["questions"], jev.plan_questions())
        self.assertIsNone(evidence["confidence"])
        self.assertEqual(evidence["response"], response())
        self.assertNotIn(KEY, repr(judge) + repr(evidence))

    def test_transport_failures_and_secret_echo(self):
        for transport, code in (
            (Mock(open=Mock(side_effect=OSError(KEY))), "JEV_UNAVAILABLE"),
            (opener(b"", 302), "JEV_UNAVAILABLE"),
            (opener(b"x" * (jev.RESPONSE_LIMIT + 1)), "JEV_RESPONSE_INVALID"),
            (opener(b"not json"), "JEV_RESPONSE_INVALID"),
            (opener(b'{"model":"a","model":"b","answers":{}}'), "JEV_RESPONSE_INVALID"),
            (opener(json.dumps(dict(response(), model=KEY)).encode()), "JEV_RESPONSE_INVALID"),
            (opener(json.dumps(dict(response(), model="jev-other")).encode()), "JEV_MODEL_MISMATCH"),
        ):
            with self.subTest(code=code), self.assertRaisesRegex(jev.JevError, "^" + code + "$") as caught:
                jev.JevJudge(KEY, opener=transport)({}, {})
            self.assertNotIn(KEY, repr(caught.exception))
            transport.open.assert_called_once()
        transport = opener(json.dumps(response()).encode())
        self.assertEqual(jev.JevJudge(KEY, "jev-latest", opener=transport)({}, {}), response())
        with self.assertRaisesRegex(jev.JevError, "^JEV_API_KEY_REQUIRED$"):
            jev.JevJudge(None, opener=transport)({}, {})
        transport.open.assert_called_once()

    def test_answers_fail_closed(self):
        answers = response()["answers"]
        for invalid in (
            None,
            {"plan": answers["plan"]},
            dict(answers, extra={}),
            dict(answers, plan=[]),
            dict(answers, plan={"choice": "unknown"}),
            *(
                dict(answers, plan={"choice": "reason", "confidence": v})
                for v in (True, None, -1, 1.01, "1", float("inf"))
            ),
            dict(answers, underspecified={"type": "noul"}),
            dict(answers, underspecified={"noul": 0.1, "confidence": None}),
            *(dict(answers, underspecified={"noul": v}) for v in (False, "0", -1, 2, float("nan"))),
        ):
            with self.subTest(answers=invalid), self.assertRaisesRegex(jev.JevError, "^JEV_RESPONSE_INVALID$"):
                jev.preanalyze(PROGRAM, Mock(return_value=dict(response(), answers=invalid)))
        for value in (0.5, 1):
            judge = Mock(return_value=dict(response(), answers=dict(answers, underspecified={"noul": value})))
            with self.assertRaisesRegex(jev.JevError, "^JEV_UNDERSPECIFIED$"):
                jev.preanalyze(PROGRAM, judge)


class RouterGateTests(unittest.TestCase):
    def client(self, judge, **kwargs):
        client = rc.FleetRouterClient({WORKER: {}}, judge=judge, **kwargs)
        client.probe = Mock(return_value=({WORKER: rc.routing.WorkerEvidence(1, True, True, True, False)}, 1))
        client.request = Mock(return_value=(409, {}))
        return client

    def test_cli_missing_env_key_blocks_before_post(self):
        workers = {WORKER: {}}
        ready = {"worker_id": WORKER, "busy": False, "model_ready": True}
        with (
            patch.dict(rc.os.environ, {}, clear=True),
            patch.object(rc, "load_workers", return_value=(workers, {})),
            patch.object(rc.FleetRouterClient, "request", return_value=(200, ready)) as request,
            patch.object(rc.pilot, "emit") as emit,
        ):
            self.assertEqual(rc.main(["--config", "fixture", "--job-id", "job"]), 2)
        request.assert_called_once_with(WORKER, "GET", "/v1/ready")
        result = json.loads(emit.call_args.args[0])
        self.assertEqual(result["reason"], "JEV_PREANALYSIS_FAILED:JEV_API_KEY_REQUIRED")

    def test_each_failure_blocks_before_dispatch(self):
        for code in jev.ERROR_CODES:
            with self.subTest(code=code):
                client = self.client(Mock(side_effect=jev.JevError(code)))
                self.assertEqual(
                    client.run("job", PROGRAM),
                    {
                        "outcome": "BLOCKED",
                        "reason": "JEV_PREANALYSIS_FAILED:" + code,
                        "worker": WORKER,
                        "job_id": "job",
                    },
                )
                client.request.assert_not_called()

    def test_real_answer_shape_dispatches_compact_evidence(self):
        answers = {
            "plan": {
                "type": "choice",
                "choice": "direct",
                "confidence": 0.42,
                "probabilities": {"direct": 0.71, "reason": 0.29},
            },
            "underspecified": {"type": "noul", "noul": 0.1},
        }
        transport = opener(json.dumps({"model": jev.JEV_MODEL, "answers": answers}).encode())
        client = self.client(jev.JevJudge(KEY, opener=transport))
        client.run("job", PROGRAM)
        client.request.assert_called_once()
        name, method, path, body = client.request.call_args.args
        self.assertEqual((name, method, path), (WORKER, "POST", "/v1/jobs"))
        route = body["route"]
        self.assertEqual((route["worker"], route["preanalysis"], route["reason"]), (WORKER, "jev", "jev-preanalyzed"))
        self.assertTrue(jev.valid_evidence(route["jev"], PROGRAM))
        self.assertEqual(
            route["jev"]["response"],
            {
                "model": jev.JEV_MODEL,
                "answers": {"plan": {"choice": "direct", "confidence": 0.42}, "underspecified": {"noul": 0.1}},
            },
        )
        self.assertLessEqual(len(json.dumps(route["jev"], ensure_ascii=True, allow_nan=False)), JEV_LIMIT)

    def test_skip_paths(self):
        cases = ((PROGRAM, "off", "strict-local"), ("repo-patch", "required", "jev-not-applicable"))
        for program, mode, reason in cases:
            judge = Mock()
            client = self.client(judge, jev_preanalysis=mode)
            client.run("job", program)
            route = client.request.call_args.args[-1]["route"]
            self.assertEqual(route, {"worker": WORKER, "preanalysis": "local", "reason": reason})
            judge.assert_not_called()
        with self.assertRaisesRegex(rc.Refused, "^JEV_PREANALYSIS_INVALID$"):
            self.client(None, jev_preanalysis=None)
