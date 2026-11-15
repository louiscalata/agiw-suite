"""Read-only HTTP projection of a check-job engine; Python 3.9, stdlib only.

Three reads and nothing else: status, one job, and the preview of a request.
None of them writes to the journal, calls the runner or contacts a lane. There
is no submit, resume, cancel or start here, and no thread. The observer serves
these only when it is handed a ``ReadOnlyRoutes``; it ships with none. See
``docs/eci-check-jobs.md``.
"""

import re
from urllib.parse import parse_qsl

import eci_jobs

STATUS_PATH = "/api/eci-jobs"
PREVIEW_PATH = "/api/eci-jobs/preview"
MAX_QUERY_BYTES = 4096
TEXT_FIELDS = ("submission_id", "target", "model_id", "sample_set", "wall_budget_s")
INTEGER_FIELDS = ("count", "max_tokens", "request_timeout_s")
FACT_PREFIX = "facts."
_JOB_PATH = re.compile(r"/api/eci-jobs/jobs/([0-9a-f]{32})\Z")
_INTEGER = re.compile(r"(0|[1-9][0-9]{0,9})\Z")
# A refusal the caller can correct is 400 or 404; one about engine state is 503.
# Every other refusal is a decision or a limit that stands against the request.
HTTP_STATUS = {"invalid_request": 400, "unknown_job": 404, "not_found": 404,
               "not_owner": 503, "journal_unavailable": 503}


def _bad_query():
    return eci_jobs.Refused("invalid_request", "The preview query is not a valid check request.")


def _integer(text):
    if not _INTEGER.fullmatch(text):
        raise _bad_query()
    return int(text)


def request_from_query(query):
    """The request a preview query names. Every field at most once, every name known.

    Text fields are passed as written. Integers are plain decimal digits and
    ``disable_thinking`` is ``true`` or ``false``. A fact is ``facts.<name>``.
    The engine normalizes and bounds the values; this only shapes them.
    """
    if not isinstance(query, str) or not query or len(query) > MAX_QUERY_BYTES:
        raise _bad_query()
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True,
                          encoding="utf-8", errors="strict")
    except ValueError:
        raise _bad_query() from None
    request, facts, seen = {}, {}, set()
    for name, text in pairs:
        if name in seen:
            raise _bad_query()
        seen.add(name)
        if name in TEXT_FIELDS:
            request[name] = text
        elif name in INTEGER_FIELDS:
            request[name] = _integer(text)
        elif name == "disable_thinking":
            if text not in ("true", "false"):
                raise _bad_query()
            request[name] = text == "true"
        elif name.startswith(FACT_PREFIX) and name[len(FACT_PREFIX):] in eci_jobs.FACT_FIELDS:
            fact = name[len(FACT_PREFIX):]
            facts[fact] = _integer(text) if fact in eci_jobs.INTEGER_FACTS else text
        else:
            raise _bad_query()
    if facts:
        request["facts"] = facts
    return request


class ReadOnlyRoutes:
    """``routes(path, query)`` returns ``(http_status, json_object)`` for a GET.

    It calls ``status()``, ``job()`` and ``preview()`` on the engine it was
    given, and nothing else.
    """

    def __init__(self, engine):
        if not isinstance(engine, eci_jobs.Engine):
            raise ValueError("routes need a check-job engine")
        self._engine = engine

    def __call__(self, path, query):
        try:
            if path == STATUS_PATH:
                return 200, self._engine.status()
            if path == PREVIEW_PATH:
                return 200, self._engine.preview(request_from_query(query))
            match = _JOB_PATH.fullmatch(path) if isinstance(path, str) else None
            if match is None:
                raise eci_jobs.Refused("not_found", "No such check-job route.")
            return 200, self._engine.job(match.group(1))
        except eci_jobs.Refused as refusal:
            return HTTP_STATUS.get(refusal.code, 409), {
                "status": "refused", "code": refusal.code, "message": str(refusal)}
