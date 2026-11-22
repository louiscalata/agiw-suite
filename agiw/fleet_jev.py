"""One bounded System One judgment about a fixed preview-program task."""

import json
import math
import re
import urllib.request
from pathlib import Path
from urllib.error import HTTPError

JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-1.14.0"
KEY_ENV = "TYPESAFE_API_KEY"
UNDERSPECIFIED_THRESHOLD = 0.5
RESPONSE_LIMIT = 1024 * 1024
MODEL_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")
TASKS = Path(__file__).resolve().parent.parent / "preview-pilot" / "program-tasks.json"
ERROR_CODES = (
    "JEV_TASK_INVALID",
    "JEV_API_KEY_REQUIRED",
    "JEV_UNAVAILABLE",
    "JEV_RESPONSE_INVALID",
    "JEV_MODEL_MISMATCH",
    "JEV_UNDERSPECIFIED",
)


class JevError(RuntimeError):
    def __init__(self, code):
        self.code = code if code in ERROR_CODES else "JEV_UNAVAILABLE"
        super().__init__(self.code)


def plan_questions():
    return {
        "plan": {
            "type": "choice",
            "instructions": "How much working-through does a careful model need to answer `task` correctly?",
            "criteria": {
                "direct": "A single lookup, extraction, classification or formatting step; "
                "the answer follows directly from the input.",
                "reason": "Several dependent steps, exact arithmetic, calendar or ordering rules, "
                "or edge cases that are easy to get wrong without working through them.",
            },
        },
        "underspecified": {
            "type": "noul",
            "instructions": "Is `task` missing information needed to produce exactly one correct answer?",
        },
    }


def _unique(pairs):
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("DUPLICATE_KEY")
    return obj


def task_state(program):
    try:
        manifest = json.loads(TASKS.read_bytes(), object_pairs_hook=_unique)
        task = manifest["tasks"][program]
        if (
            type(manifest["schemaVersion"]) is int
            and manifest["schemaVersion"] == 1
            and isinstance(task, dict)
            and set(task) == {"taskId", "mode", "language", "allowedFiles", "acceptanceCriteria"}
            and all(isinstance(task[k], str) and task[k].strip() for k in ("taskId", "mode", "language"))
            and all(
                isinstance(task[k], list) and task[k] and all(isinstance(v, str) and v.strip() for v in task[k])
                for k in ("allowedFiles", "acceptanceCriteria")
            )
        ):
            return {"task": task}
    except (KeyError, TypeError, ValueError, OSError, RecursionError):
        pass
    raise JevError("JEV_TASK_INVALID")


def _probability(value):
    return type(value) in (int, float) and 0 <= value <= 1


def _summary(response):
    # TypeSafe may add metadata such as `usage`; only model and answers are required.
    if not isinstance(response, dict) or "model" not in response or "answers" not in response:
        raise JevError("JEV_RESPONSE_INVALID")
    model, answers = response["model"], response["answers"]
    if not isinstance(model, str) or not MODEL_ID.fullmatch(model):
        raise JevError("JEV_RESPONSE_INVALID")
    if not isinstance(answers, dict) or set(answers) != {"plan", "underspecified"}:
        raise JevError("JEV_RESPONSE_INVALID")
    plan, under = answers["plan"], answers["underspecified"]
    if (
        not isinstance(plan, dict)
        or plan.get("choice") not in ("direct", "reason")
        or not isinstance(under, dict)
        or not _probability(under.get("noul"))
        or any("confidence" in answer and not _probability(answer["confidence"]) for answer in (plan, under))
    ):
        raise JevError("JEV_RESPONSE_INVALID")
    return {
        "jevModel": model,
        "plan": plan["choice"],
        "confidence": plan.get("confidence"),
        "underspecified": under["noul"],
    }


def _evidence_response(summary):
    plan = {"choice": summary["plan"]}
    if summary["confidence"] is not None:
        plan["confidence"] = summary["confidence"]
    return {
        "model": summary["jevModel"],
        "answers": {"plan": plan, "underspecified": {"noul": summary["underspecified"]}},
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class JevJudge:
    def __init__(self, api_key, model=JEV_MODEL, timeout_s=60, opener=None):
        self._key = api_key
        self.model = model
        self.timeout_s = timeout_s
        self.opener = opener

    def __call__(self, state, questions):
        if not isinstance(self._key, str) or not re.fullmatch(r"[!-~]+", self._key):
            raise JevError("JEV_API_KEY_REQUIRED")
        if not isinstance(self.model, str) or not MODEL_ID.fullmatch(self.model):
            raise JevError("JEV_MODEL_MISMATCH")
        try:
            if type(self.timeout_s) not in (int, float) or not math.isfinite(self.timeout_s) or self.timeout_s <= 0:
                raise ValueError
            opener = self.opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
            body = {"model": self.model, "state": state, "questions": questions}
            req = urllib.request.Request(
                JEV_ENDPOINT,
                data=json.dumps(body, allow_nan=False).encode(),
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + self._key},
                method="POST",
            )
            try:
                response = opener.open(req, timeout=self.timeout_s)
            except HTTPError as error:
                error.close()
                raise
            with response:
                if response.status != 200:
                    raise ValueError
                raw = response.read(RESPONSE_LIMIT + 1)
        except Exception:  # noqa: BLE001 - transport errors can contain credentials
            pass
        else:
            return self._decode(raw)
        raise JevError("JEV_UNAVAILABLE")

    def _decode(self, raw):
        try:
            if not isinstance(raw, bytes) or len(raw) > RESPONSE_LIMIT:
                raise ValueError
            result = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
            _summary(result)
            if self._key in json.dumps(result):
                raise ValueError
        except (TypeError, ValueError, RecursionError):
            pass
        else:
            if self.model != "jev-latest" and result["model"] != self.model:
                raise JevError("JEV_MODEL_MISMATCH")
            return result
        raise JevError("JEV_RESPONSE_INVALID")


def preanalyze(program, judge):
    state, questions = task_state(program), plan_questions()
    if judge is None:
        raise JevError("JEV_API_KEY_REQUIRED")
    try:
        response = judge(state, questions)
    except JevError:
        raise
    except Exception:  # noqa: BLE001 - never expose injected transport errors
        raise JevError("JEV_UNAVAILABLE") from None
    summary = _summary(response)
    if summary["underspecified"] >= UNDERSPECIFIED_THRESHOLD:
        raise JevError("JEV_UNDERSPECIFIED")
    return {
        "status": "ok",
        **summary,
        "request": {"state": state, "questions": questions},
        "response": _evidence_response(summary),
    }


def valid_evidence(jev, program):
    fields = {"jevModel", "plan", "confidence", "underspecified", "request", "response"}
    if not isinstance(jev, dict) or set(jev) != fields:
        return False
    try:
        summary = _summary(jev["response"])
        return (
            summary["underspecified"] < UNDERSPECIFIED_THRESHOLD
            and jev["response"] == _evidence_response(summary)
            and _probability(jev["underspecified"])
            and (jev["confidence"] is None or _probability(jev["confidence"]))
            and all(jev[k] == value for k, value in summary.items())
            and jev["request"] == {"state": task_state(program), "questions": plan_questions()}
        )
    except JevError:
        return False
