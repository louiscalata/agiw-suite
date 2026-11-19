"""Check-job engine for screening checks; Python 3.9, stdlib only.

One reviewed screening check at a time, tracked in an append-only journal.
The engine starts nothing by itself: no model, no server, no route. It calls an
injected runner against a lane that is already serving, and ingests a result
only after the record validates. Every unreviewed, unknown or undecided state
refuses. The submission gate ships closed. See ``docs/eci-check-jobs.md``.
"""

import copy
import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from urllib.parse import urlsplit

import eci_fast_check as fast
import eci_fast_check_adapter as adapter
from remote_machines import PROTECTED_PORTS

JOURNAL_VERSION = 1
STATES = ("queued", "running", "finished", "failed", "cancelled", "interrupted")
RESUMABLE = ("failed", "cancelled", "interrupted")
MAX_QUEUED = 8
MAX_JOBS = 256
MAX_ATTEMPTS = 16
MAX_JOURNAL_BYTES = 8 * 1024 * 1024
JOURNAL_RESERVE_BYTES = 1024 * 1024
MAX_COUNT = 1000
MAX_MAX_TOKENS = 65536
MAX_REQUEST_TIMEOUT_S = 600
MAX_BUDGET_CHARS = 32
DEFAULT_COUNT = 200
DEFAULT_REQUEST_TIMEOUT_S = 180
REQUEST_FIELDS = frozenset(
    {
        "submission_id",
        "target",
        "model_id",
        "sample_set",
        "count",
        "max_tokens",
        "wall_budget_s",
        "request_timeout_s",
        "disable_thinking",
        "facts",
    }
)
TEXT_FACTS = ("model_label", "machine_label", "cpu", "gpu", "runtime_build_id")
INTEGER_FACTS = ("file_size_bytes", "available_memory_bytes", "memory_bytes")
FACT_FIELDS = frozenset(("model_sha256", "quantization") + TEXT_FACTS + INTEGER_FACTS)
_SUBMISSION_ID = re.compile(r"[A-Za-z0-9_-]{8,64}\Z")
_SAMPLE_SET = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_TARGET = re.compile(r"registry:[a-z][a-z0-9_-]{0,15}:[a-z][a-z0-9_-]{0,15}\Z")
_JOB_ID = re.compile(r"[0-9a-f]{32}\Z")
_REASON = re.compile(r"[A-Za-z0-9 .,:;()_/'-]{1,200}\Z")


class Refused(Exception):
    """A request the engine did not act on. ``code`` is stable; the text is fixed."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Stopped(Exception):
    """A runner raises this when it stopped because ``should_stop`` asked it to."""


class _Corrupt(Exception):
    pass


def _closed():
    return False


def _budget(value):
    """An exact positive decimal of bounded length as a Fraction, else ``None``.

    The length is checked before any conversion: an unbounded digit string
    would bloat the journal, and newer interpreters raise a bare ``ValueError``
    when converting a very long one.
    """
    if not isinstance(value, str) or len(value) > MAX_BUDGET_CHARS:
        return None
    try:
        return fast.decimal(value, high=None, positive=True)
    except ValueError:
        return None


class Policy:
    """Owner decisions the engine cannot make. The default refuses everything.

    ``gate`` must return exactly ``True`` for a submission, a resume or a start.
    ``allowed_targets`` lists the registry lane ids that may be targets.
    ``max_wall_budget_s`` caps a job's wall budget; without it every budget is
    refused. ``default_wall_budget_s`` applies when a request names no budget.
    Both are exact positive decimal strings of at most 32 characters.
    """

    def __init__(self, gate=_closed, allowed_targets=(), max_wall_budget_s=None, default_wall_budget_s=None):
        if not callable(gate):
            raise ValueError("gate must be callable")
        targets = frozenset(allowed_targets)
        if any(not isinstance(item, str) or not _TARGET.fullmatch(item) for item in targets):
            raise ValueError("allowed targets must be registry lane ids")
        for value in (max_wall_budget_s, default_wall_budget_s):
            if value is not None and _budget(value) is None:
                raise ValueError("a wall budget must be a short exact positive decimal string")
        if default_wall_budget_s is not None and (
            max_wall_budget_s is None or _budget(default_wall_budget_s) > _budget(max_wall_budget_s)
        ):
            raise ValueError("default wall budget must not exceed the maximum")
        self.gate = gate
        self.allowed_targets = targets
        self.max_wall_budget_s = max_wall_budget_s
        self.default_wall_budget_s = default_wall_budget_s

    def gate_open(self):
        try:
            return self.gate() is True
        except Exception:  # fail closed: a gate that cannot answer is a closed gate
            return False


CLOSED = Policy()


def _invalid(message):
    return Refused("invalid_request", message)


def normalize_request(request, policy=CLOSED):
    """Return the exact request a review binds to, with every default explicit."""
    if not isinstance(request, dict) or set(request) - REQUEST_FIELDS:
        raise _invalid("Request has missing or unexpected fields.")
    for key in ("submission_id", "target", "model_id", "sample_set"):
        if not isinstance(request.get(key), str):
            raise _invalid("Request needs submission_id, target, model_id and sample_set as text.")
    if not _SUBMISSION_ID.fullmatch(request["submission_id"]):
        raise _invalid("submission_id must be 8 to 64 letters, digits, underscores or hyphens.")
    if not _TARGET.fullmatch(request["target"]):
        raise _invalid("target must be a registry lane id.")
    if not _SAMPLE_SET.fullmatch(request["sample_set"]):
        raise _invalid("sample_set must be a configured sample set id.")
    try:
        adapter.model_name(request["model_id"])
        count = request.get("count", DEFAULT_COUNT)
        fast.numeric(count, 1, MAX_COUNT, integer=True)
        max_tokens = request.get("max_tokens", adapter.DEFAULT_MAX_TOKENS)
        fast.numeric(max_tokens, adapter.MIN_MAX_TOKENS, MAX_MAX_TOKENS, integer=True)
        timeout = request.get("request_timeout_s", DEFAULT_REQUEST_TIMEOUT_S)
        fast.numeric(timeout, 1, MAX_REQUEST_TIMEOUT_S, integer=True)
    except fast.ValidationError:
        raise _invalid("model_id, count, max_tokens or request_timeout_s is outside the allowed range.") from None
    thinking = request.get("disable_thinking", False)
    if type(thinking) is not bool:
        raise _invalid("disable_thinking must be true or false.")
    budget = request.get("wall_budget_s", policy.default_wall_budget_s)
    if budget is None:
        raise Refused("budget_required", "A wall budget is required and no default is decided.")
    wanted = _budget(budget)
    if wanted is None:
        raise _invalid("wall_budget_s must be an exact positive decimal string of at most 32 characters.")
    # The policy is re-read on every call; a cap that is no longer a valid budget is undecided.
    cap = _budget(policy.max_wall_budget_s)
    if cap is None or wanted > cap:
        raise Refused("budget_refused", "The wall budget exceeds the decided maximum, or no maximum is decided.")
    supplied = request.get("facts", {})
    if not isinstance(supplied, dict) or set(supplied) - FACT_FIELDS:
        raise _invalid("facts has unexpected fields.")
    facts = {}
    try:
        for key, value in sorted(supplied.items()):
            if value is None:
                continue
            if key == "model_sha256":
                fast.sha(value)
            elif key == "quantization":
                if adapter.quantization_name(value) != value:
                    raise fast.ValidationError("quantization")
            elif key in INTEGER_FACTS:
                fast.numeric(value, 1, integer=True)
            elif key == "model_label":
                # The adapter writes this label as the record's model name.
                adapter.model_name(value)
            elif adapter.safe_text(value) != value:
                raise fast.ValidationError("fact")
            facts[key] = value
    except fast.ValidationError:
        raise _invalid(
            "facts must be short public descriptors, a public model label, sizes and a lowercase SHA-256."
        ) from None
    return {
        "submission_id": request["submission_id"],
        "target": request["target"],
        "model_id": request["model_id"],
        "sample_set": request["sample_set"],
        "count": count,
        "max_tokens": max_tokens,
        "wall_budget_s": budget,
        "request_timeout_s": timeout,
        "disable_thinking": thinking,
        "facts": facts,
    }


def job_id_for(request):
    """Identity of one submission: the normalized request, which names its own id."""
    return fast.digest({"kind": "agiw_check_job", "request": request})[:32]


def review_digest(job_id, attempt, request):
    """What a reviewer approves: this exact request, for this attempt only."""
    return fast.digest({"kind": "agiw_check_job_review", "job_id": job_id, "attempt": attempt, "request": request})


def default_plan_size(files, count):
    """Planned item count from the named local sample files. No network."""
    datasets = {}
    for path in files:
        data = fast.load(path)
        if not isinstance(data, dict) or datasets.keys() & data.keys():
            raise fast.ValidationError("sample files must have distinct benchmark descriptors")
        datasets.update(data)
    return sum(len(spec["item_ids"]) for spec in fast.make_plan(datasets, count=count)["benchmarks"])


def adapter_runner(spec, should_stop, progress):
    """Bind a job to the adapter's public ``run()``. It never starts a server.

    The adapter exposes no stop or progress hook, so with this runner a cancel
    is honoured before the first request and when ``run()`` returns; in between
    the adapter's own wall budget bounds the job. A runner with a per-item hook
    honours ``should_stop`` between items.
    """
    if should_stop():
        raise Stopped()
    facts = spec["facts"]
    return adapter.run(
        spec["base_url"],
        spec["model_id"],
        [Path(p) for p in spec["sample_files"]],
        Path(spec["output_dir"]),
        count=spec["count"],
        max_tokens=spec["max_tokens"],
        timeout=spec["request_timeout_s"],
        wall_budget_s=spec["wall_budget_s"],
        disable_thinking=spec["disable_thinking"],
        **{key: facts.get(key) for key in sorted(FACT_FIELDS)},
    )


def _bound(record, request):
    """True only when a validated record is for exactly the reviewed request.

    The model name must be the reviewed label, or the reviewed model id when no
    label was given, and ``max_tokens`` must be the reviewed value, with or
    without a declared model hash. A lane whose context is smaller than the
    reviewed ``max_tokens`` makes the adapter lower it; that record is refused,
    because the reviewer approved a different setting.
    """
    facts = request["facts"]
    declared = facts.get("model_sha256")
    settings = record["runtime_settings"]
    tokens = settings.get("max_tokens") if isinstance(settings, dict) else None
    return (
        record["plan"]["items_per_benchmark"] == request["count"]
        and record["plan"]["wall_budget_s"] == request["wall_budget_s"]
        and record["model"]["name"] == facts.get("model_label", request["model_id"])
        and type(tokens) is int
        and tokens == request["max_tokens"]
        and (declared is None or record["model"]["file_sha256"] == declared)
    )


def _utc_now():
    return datetime.now(timezone.utc)


def _stamp(now):
    value = now()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("clock must return an aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _reason(text):
    return text if isinstance(text, str) and _REASON.fullmatch(text) else "unspecified"


def _lock_file(stream):
    try:
        if os.name == "nt":
            import msvcrt

            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


# ---------------------------------------------------------------- journal


def _entry_line(body):
    entry = dict(body)
    entry["sha256"] = fast.digest(body)
    return (fast.canonical(entry) + "\n").encode("utf-8"), entry


def _parse_entry(line):
    """One self-consistent entry: valid JSON whose own SHA-256 matches. Not yet in sequence."""
    try:
        entry = json.loads(line.decode("utf-8"), object_pairs_hook=fast.unique_object)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise _Corrupt() from None
    if not isinstance(entry, dict) or not isinstance(entry.get("sha256"), str):
        raise _Corrupt()
    body = {key: value for key, value in entry.items() if key != "sha256"}
    try:
        if fast.digest(body) != entry["sha256"]:
            raise _Corrupt()
        fast.timestamp(entry.get("at"))
    except (ValueError, TypeError, RecursionError):
        raise _Corrupt() from None
    if entry.get("kind") == "repair":
        if set(body) != {"v", "seq", "prev", "at", "kind", "skipped_sha256"}:
            raise _Corrupt()
    elif entry.get("kind") == "job":
        if set(body) != {"v", "seq", "prev", "at", "kind", "job_id", "attempt", "state", "detail"}:
            raise _Corrupt()
    else:
        raise _Corrupt()
    return entry


def _in_sequence(entry, seq, prev):
    return (
        type(entry["v"]) is int
        and entry["v"] == JOURNAL_VERSION
        and type(entry["seq"]) is int
        and entry["seq"] == seq
        and entry["prev"] == prev
    )


def _result_ok(value, nullable):
    if value is None:
        return nullable
    return (
        isinstance(value, dict)
        and set(value) == {"record_id", "post_decision", "benchmarks"}
        and isinstance(value["benchmarks"], list)
    )


def _apply(jobs, entry):
    """Fold one job entry into ``jobs``; any unknown shape or move is corruption."""
    job_id, attempt, state, detail = (entry[key] for key in ("job_id", "attempt", "state", "detail"))
    if (
        not isinstance(job_id, str)
        or not _JOB_ID.fullmatch(job_id)
        or type(attempt) is not int
        or state not in STATES
        or not isinstance(detail, dict)
    ):
        raise _Corrupt()
    job = jobs.get(job_id)
    at = entry["at"]
    if state == "queued":
        if job is None:
            if attempt != 1 or set(detail) != {"request", "review_sha256"}:
                raise _Corrupt()
            request = detail["request"]
            if not isinstance(request, dict) or job_id_for(request) != job_id:
                raise _Corrupt()
            job = jobs[job_id] = {
                "job_id": job_id,
                "request": request,
                "submitted_at": at,
                "entries": 0,
            }
        elif job["state"] not in RESUMABLE or attempt != job["attempt"] + 1 or set(detail) != {"review_sha256"}:
            raise _Corrupt()
        if detail["review_sha256"] != review_digest(job_id, attempt, job["request"]):
            raise _Corrupt()
        job.update(
            attempt=attempt,
            review_sha256=detail["review_sha256"],
            started_at=None,
            ended_at=None,
            cancel_requested=False,
            reason=None,
            result=None,
        )
    else:
        if job is None or attempt != job["attempt"]:
            raise _Corrupt()
        before = job["state"]
        if state == "running":
            if set(detail) != {"cancel_requested"} or type(detail["cancel_requested"]) is not bool:
                raise _Corrupt()
            if before == "queued" and not detail["cancel_requested"]:
                job["started_at"] = at
            elif not (before == "running" and detail["cancel_requested"] and not job["cancel_requested"]):
                raise _Corrupt()
            job["cancel_requested"] = detail["cancel_requested"]
        else:
            # A queued job fails only when it is refused at start; it never ran.
            allowed = {
                "finished": ("running",),
                "failed": ("queued", "running"),
                "cancelled": ("queued", "running"),
                "interrupted": ("queued", "running"),
            }
            fields = {
                "finished": {"result"},
                "failed": {"reason", "result"},
                "cancelled": {"reason"},
                "interrupted": {"reason"},
            }
            if before not in allowed[state] or set(detail) != fields[state]:
                raise _Corrupt()
            if "reason" in detail and _reason(detail["reason"]) != detail["reason"]:
                raise _Corrupt()
            if "result" in detail and not _result_ok(detail["result"], state == "failed"):
                raise _Corrupt()
            job.update(ended_at=at, reason=detail.get("reason"), result=detail.get("result"))
    job["state"] = state
    job["updated_at"] = at
    job["entries"] += 1


def replay(raw):
    """Fold journal bytes. Returns a dict; ``problem`` set means nothing is trusted.

    A line that does not validate is accepted only when the next valid entry is
    a repair entry naming the SHA-256 of exactly the skipped bytes. An
    unterminated or invalid tail is reported in ``tail`` and never applied.
    A self-consistent entry that is out of sequence is damage, never a tail:
    a crash leaves a cut line, not a whole entry in the wrong place.
    """
    damaged = {
        "jobs": OrderedDict(),
        "seq": 0,
        "prev": None,
        "tail": b"",
        "problem": "The job journal is damaged; no job state is trusted.",
    }
    jobs, seq, prev, bad, offset = OrderedDict(), 0, None, b"", 0
    while offset < len(raw):
        end = raw.find(b"\n", offset)
        if end < 0:
            bad += raw[offset:]
            break
        line, chunk = raw[offset:end], raw[offset : end + 1]
        offset = end + 1
        try:
            entry = _parse_entry(line)
        except _Corrupt:
            bad += chunk
            continue
        if not _in_sequence(entry, seq + 1, prev):
            return damaged
        try:
            if entry["kind"] == "repair":
                if not bad or entry["skipped_sha256"] != hashlib.sha256(bad).hexdigest():
                    raise _Corrupt()
            elif bad:
                raise _Corrupt()
            else:
                _apply(jobs, entry)
        except _Corrupt:
            return damaged
        bad, seq, prev = b"", entry["seq"], entry["sha256"]
    return {"jobs": jobs, "seq": seq, "prev": prev, "tail": bad, "problem": None}


def _read_journal(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return b"", None
    except OSError:
        return b"", "The job journal cannot be read."
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        return b"", "The job journal must be a regular file, not a link."
    if info.st_size > MAX_JOURNAL_BYTES:
        return b"", "The job journal exceeds its size limit."
    try:
        with open(path, "rb") as stream:
            raw = stream.read(MAX_JOURNAL_BYTES + 1)
    except OSError:
        return b"", "The job journal cannot be read."
    if len(raw) > MAX_JOURNAL_BYTES:
        return b"", "The job journal exceeds its size limit."
    return raw, None


def _view(job, progress=None):
    view = copy.deepcopy(
        {
            key: job[key]
            for key in (
                "job_id",
                "state",
                "attempt",
                "request",
                "submitted_at",
                "updated_at",
                "started_at",
                "ended_at",
                "cancel_requested",
                "reason",
                "result",
            )
        }
    )
    view["progress"] = dict(progress) if progress and job["state"] == "running" else None
    view["resumable"] = job["state"] in RESUMABLE and job["attempt"] < MAX_ATTEMPTS
    return view


def read_status(state_dir):
    """Read-only job list from the journal. Takes no lock, writes and starts nothing."""
    raw, problem = _read_journal(Path(state_dir) / "jobs.jsonl")
    state = replay(raw) if problem is None else {"jobs": {}, "problem": problem, "tail": b""}
    if state["problem"]:
        return {"status": "unknown", "reason": state["problem"], "jobs": [], "active": None}
    jobs = [_view(job) for job in state["jobs"].values()]
    return {
        "status": "ok",
        "reason": None,
        "jobs": jobs,
        "active": next((job["job_id"] for job in jobs if job["state"] == "running"), None),
    }


# ----------------------------------------------------------------- engine


class Engine:
    """Owns one job state directory. A second engine on the same directory is refused.

    ``targets`` returns the registry lane targets (``remote_machines.lane_targets``).
    ``sample_sets`` maps a set id to absolute local sample file paths.
    ``runner(spec, should_stop, progress)`` returns a fast-check record.
    ``sink(record)`` ingests a validated, complete record and raises on failure.
    ``now`` returns an aware datetime; ``clock`` is monotonic seconds.
    """

    def __init__(
        self,
        state_dir,
        *,
        policy=CLOSED,
        targets=None,
        sample_sets=None,
        runner=None,
        sink=None,
        now=_utc_now,
        clock=time.monotonic,
        plan_size=default_plan_size,
    ):
        if not isinstance(policy, Policy):
            raise ValueError("policy must be a Policy")
        self._dir = Path(state_dir)
        if not self._dir.is_absolute() or ".." in self._dir.parts:
            raise ValueError("state directory must be an absolute path without parent segments")
        self._sets = {}
        for name, files in (sample_sets or {}).items():
            paths = [Path(item) for item in files]
            if (
                not isinstance(name, str)
                or not _SAMPLE_SET.fullmatch(name)
                or not paths
                or any(not path.is_absolute() or ".." in path.parts for path in paths)
            ):
                raise ValueError("sample sets map an id to absolute file paths")
            self._sets[name] = [str(path) for path in paths]
        self._policy, self._targets = policy, targets
        self._runner, self._sink = runner, sink
        self._now, self._clock, self._plan_size = now, clock, plan_size
        self._lock = threading.RLock()
        self._run_lock = threading.Lock()
        self._jobs, self._seq, self._prev = OrderedDict(), 0, None
        self._problem, self._owner = None, False
        self._progress, self._size = {}, 0
        self._lock_stream = self._journal = None
        self._open()

    # -- opening and recovery

    def _open(self):
        path = self._dir / "jobs.jsonl"
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._lock_stream = (self._dir / "engine.lock").open("a+b")
        except OSError:
            self._problem = "The job state directory cannot be opened."
            return
        if not _lock_file(self._lock_stream):
            self._lock_stream.close()
            self._lock_stream = None
            self._problem = "Another engine owns this job state directory."
            return
        self._owner = True
        raw, problem = _read_journal(path)
        if problem:
            self._problem = problem
            return
        try:
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            self._journal = os.open(path, flags, 0o600)
        except OSError:
            self._problem = "The job journal cannot be opened for appending."
            return
        self._size = len(raw)
        try:
            if raw and not raw.endswith(b"\n"):
                # Terminate a line cut short by a crash so later entries stay line-framed.
                self._write(b"\n")
                raw += b"\n"
            state = replay(raw)
            if state["problem"]:
                self._problem = state["problem"]
                return
            self._jobs, self._seq, self._prev = state["jobs"], state["seq"], state["prev"]
            if state["tail"]:
                self._repair(state["tail"])
            # The lock proves no engine is running these jobs. Nothing resumes by itself.
            for job in list(self._jobs.values()):
                if job["state"] == "running":
                    self._append(job, "interrupted", {"reason": "The engine stopped while the job was running."})
                elif job["state"] == "queued":
                    self._append(job, "interrupted", {"reason": "The engine stopped before the job started."})
        except Refused:
            pass

    def _repair(self, tail):
        """Supersede an invalid tail left by a crash; the bytes stay in the file."""
        body = {
            "v": JOURNAL_VERSION,
            "seq": self._seq + 1,
            "prev": self._prev,
            "at": _stamp(self._now),
            "kind": "repair",
            "skipped_sha256": hashlib.sha256(tail).hexdigest(),
        }
        line, entry = _entry_line(body)
        self._write(line)
        self._seq, self._prev = entry["seq"], entry["sha256"]

    def _write(self, payload):
        try:
            if os.write(self._journal, payload) != len(payload):
                raise OSError("short write")
            os.fsync(self._journal)
        except OSError:
            self._problem = "The job journal could not be written; job state is unknown."
            raise Refused("journal_unavailable", self._problem) from None
        self._size += len(payload)

    def _append(self, job, state, detail, request=None, attempt=None):
        """Durably append one superseding entry, then fold it into memory."""
        job_id = job["job_id"] if job is not None else job_id_for(request)
        if attempt is None:
            attempt = job["attempt"] if job is not None else 1
        body = {
            "v": JOURNAL_VERSION,
            "seq": self._seq + 1,
            "prev": self._prev,
            "at": _stamp(self._now),
            "kind": "job",
            "job_id": job_id,
            "attempt": attempt,
            "state": state,
            "detail": detail,
        }
        line, entry = _entry_line(body)
        # Fold into a copy first: an entry replay would reject is never written.
        scratch = {} if job is None else {job_id: copy.deepcopy(job)}
        try:
            _apply(scratch, copy.deepcopy(entry))
        except _Corrupt:
            raise RuntimeError("illegal job transition") from None
        self._write(line)
        self._seq, self._prev = entry["seq"], entry["sha256"]
        self._jobs[job_id] = scratch[job_id]
        return scratch[job_id]

    def close(self):
        """Release the journal and the directory lock. A running job is not stopped."""
        with self._lock:
            if self._journal is not None:
                os.close(self._journal)
                self._journal = None
            if self._lock_stream is not None:
                self._lock_stream.close()
                self._lock_stream = None
            self._owner = False
            self._problem = self._problem or "The engine is closed."

    # -- reading; nothing here writes or starts anything

    def status(self):
        with self._lock:
            if self._problem:
                return {
                    "status": "unknown",
                    "reason": self._problem,
                    "owner": self._owner,
                    "gate": "closed",
                    "jobs": [],
                    "active": None,
                }
            jobs = [_view(job, self._progress.get(job["job_id"])) for job in self._jobs.values()]
            return {
                "status": "ok",
                "reason": None,
                "owner": True,
                "gate": "open" if self._policy.gate_open() else "closed",
                "jobs": jobs,
                "active": next((j["job_id"] for j in jobs if j["state"] == "running"), None),
            }

    def job(self, job_id):
        with self._lock:
            self._usable()
            return _view(self._known(job_id), self._progress.get(job_id))

    def preview(self, request):
        """What a reviewer must see before ``submit``. Writes and starts nothing."""
        with self._lock:
            normalized = normalize_request(request, self._policy)
            return self._preview(job_id_for(normalized), 1, normalized)

    def preview_resume(self, job_id):
        with self._lock:
            self._usable()
            job = self._known(job_id)
            if job["state"] not in RESUMABLE or job["attempt"] >= MAX_ATTEMPTS:
                raise Refused("not_resumable", "Only a failed, cancelled or interrupted job can be resumed.")
            return self._preview(job_id, job["attempt"] + 1, job["request"])

    def _preview(self, job_id, attempt, request):
        refusals = []
        for check in (
            self._usable,
            self._require_gate,
            lambda: normalize_request(request, self._policy),
            lambda: self._target(request),
            lambda: self._context(request),
            lambda: self._samples(request),
            self._require_parts,
        ):
            try:
                check()
            except Refused as refusal:
                refusals.append({"code": refusal.code, "message": str(refusal)})
        planned = None
        if request["sample_set"] in self._sets:
            try:
                planned = self._plan_size(list(self._sets[request["sample_set"]]), request["count"])
                fast.numeric(planned, 1, integer=True)
            except Exception:
                planned = None
                refusals.append({"code": "samples_unreadable", "message": "The sample set cannot be read or planned."})
        return {
            "job_id": job_id,
            "attempt": attempt,
            "request": copy.deepcopy(request),
            "review_sha256": review_digest(job_id, attempt, request),
            "refusals": refusals,
            "effects": {
                "target": request["target"],
                "sample_set": request["sample_set"],
                "items_per_benchmark": request["count"],
                "planned_requests": planned,
                # The adapter retries one transient failure per item.
                "max_requests": None if planned is None else planned * 2,
                "wall_budget_s": request["wall_budget_s"],
                "starts_model": False,
                # What the registry says, not a measurement; ``None`` is unknown.
                "lane_context_tokens": self._advertised_context(request),
                "writes": ["job journal", "job output directory", "dataset, only for a valid complete record"],
            },
        }

    # -- refusals shared by submit, resume and start

    def _usable(self):
        if self._problem:
            raise Refused("journal_unavailable" if self._owner else "not_owner", self._problem)

    def _known(self, job_id):
        job = self._jobs.get(job_id) if isinstance(job_id, str) else None
        if job is None:
            raise Refused("unknown_job", "No job has this id.")
        return job

    def _require_gate(self):
        if not self._policy.gate_open():
            raise Refused("gate_closed", "Check submission is closed.")

    def _require_parts(self):
        if not callable(self._runner) or not callable(self._sink):
            raise Refused("not_configured", "No runner or no dataset sink is configured.")

    def _target(self, request):
        """Base URL of an allowed registry lane, re-read from the registry each time."""
        lane = self._lane(request)
        if lane is None or not isinstance(lane.get("base"), str):
            raise Refused("unknown_target", "The target lane is not in the registry.")
        base = lane["base"]
        try:
            port = urlsplit(base).port
        except ValueError:
            port = None
        if port in PROTECTED_PORTS:
            raise Refused("protected_port", "The target lane uses a protected port.")
        try:
            adapter.validate_base_url(base)
        except fast.ValidationError:
            raise Refused("invalid_target", "The target lane is not a loopback lane address.") from None
        if request["target"] not in self._policy.allowed_targets:
            raise Refused("target_not_allowed", "The target lane is not an allowed check target.")
        return base

    def _lane(self, request):
        """The one registry entry for the target, or ``None``. Reads the registry only."""
        try:
            listed = list(self._targets()) if callable(self._targets) else []
        except Exception:
            listed = []
        matches = [item for item in listed if isinstance(item, dict) and item.get("id") == request["target"]]
        return matches[0] if len(matches) == 1 else None

    def _advertised_context(self, request):
        """The lane's ``contextTokens`` when it is a nonnegative integer, else ``None``."""
        lane = self._lane(request)
        value = lane.get("contextTokens") if lane is not None else None
        return value if type(value) is int and value >= 0 else None

    def _context(self, request):
        """Refuse a ``max_tokens`` above the context the registry advertises for the lane.

        The adapter lowers ``max_tokens`` to the lane's context, and the record
        then fails the exact binding after the whole check has run. This check
        uses the registry entry only and contacts nothing. A lane that
        advertises no context is unknown and passes here; the exact binding at
        ingestion still applies. A lane that is not in the registry is refused
        by ``_target``.
        """
        lane = self._lane(request)
        if lane is None or "contextTokens" not in lane:
            return
        advertised = lane["contextTokens"]
        # A negative value is below every allowed max_tokens, so it refuses as well.
        if type(advertised) is not int or request["max_tokens"] > advertised:
            raise Refused(
                "context_exceeded",
                "The reviewed max_tokens exceeds the lane's advertised context, or that context is not a valid number.",
            )

    def _samples(self, request):
        if request["sample_set"] not in self._sets:
            raise Refused("unknown_sample_set", "The sample set is not configured.")
        return list(self._sets[request["sample_set"]])

    def _reviewed(self, review, job_id, attempt, request):
        if (
            not isinstance(review, dict)
            or set(review) != {"reviewed", "review_sha256"}
            or review["reviewed"] is not True
        ):
            raise Refused("not_reviewed", "A reviewed submission is required.")
        if review["review_sha256"] != review_digest(job_id, attempt, request):
            raise Refused("review_mismatch", "The review does not match this exact request and attempt.")

    def _admit(self, request):
        self._require_gate()
        self._target(request)
        self._context(request)
        self._samples(request)
        self._require_parts()
        if sum(job["state"] == "queued" for job in self._jobs.values()) >= MAX_QUEUED:
            raise Refused("queue_full", "The check queue is full.")
        if self._size > MAX_JOURNAL_BYTES - JOURNAL_RESERVE_BYTES:
            raise Refused("journal_full", "The job journal is full.")

    # -- reviewed actions

    def submit(self, request, review):
        """Queue one reviewed check. Repeating the same submission changes nothing."""
        with self._lock:
            self._usable()
            normalized = normalize_request(request, self._policy)
            job_id = job_id_for(normalized)
            self._reviewed(review, job_id, 1, normalized)
            if job_id in self._jobs:
                return _view(self._jobs[job_id], self._progress.get(job_id))
            self._admit(normalized)
            if len(self._jobs) >= MAX_JOBS:
                raise Refused("journal_full", "The job journal holds its maximum number of jobs.")
            detail = {"request": normalized, "review_sha256": review["review_sha256"]}
            return _view(self._append(None, "queued", detail, request=normalized))

    def resume(self, job_id, review):
        """Queue the next attempt of a stopped job; it reuses the job's output directory."""
        with self._lock:
            self._usable()
            job = self._known(job_id)
            if job["state"] in RESUMABLE and job["attempt"] < MAX_ATTEMPTS:
                self._reviewed(review, job_id, job["attempt"] + 1, job["request"])
                normalize_request(job["request"], self._policy)
                self._admit(job["request"])
                return _view(
                    self._append(job, "queued", {"review_sha256": review["review_sha256"]}, attempt=job["attempt"] + 1)
                )
            if job["attempt"] > 1:
                # The same reviewed resume again: already applied, nothing to write.
                self._reviewed(review, job_id, job["attempt"], job["request"])
                return _view(job, self._progress.get(job_id))
            raise Refused("not_resumable", "Only a failed, cancelled or interrupted job can be resumed.")

    def cancel(self, job_id):
        """Cancel a queued job now; ask a running job to stop between items."""
        with self._lock:
            self._usable()
            job = self._known(job_id)
            if job["state"] == "queued":
                job = self._append(job, "cancelled", {"reason": "Cancelled before the job started."})
            elif job["state"] == "running":
                if not job["cancel_requested"]:
                    job = self._append(job, "running", {"cancel_requested": True})
            elif job["state"] != "cancelled":
                raise Refused("not_cancellable", "Only a queued or running job can be cancelled.")
            return _view(job, self._progress.get(job_id))

    # -- running one job

    def run_next(self):
        """Run the oldest queued job to its end in this thread. One job at a time.

        Returns the job view, or ``None`` when nothing is queued. The caller
        decides when to call this; the engine has no timer and no thread.
        """
        if not self._run_lock.acquire(blocking=False):
            raise Refused("busy", "A check is already running.")
        try:
            with self._lock:
                self._usable()
                job = next((j for j in self._jobs.values() if j["state"] == "queued"), None)
                if job is None:
                    return None
                self._require_gate()
                self._require_parts()
                try:
                    normalize_request(job["request"], self._policy)
                    base = self._target(job["request"])
                    self._context(job["request"])
                    files = self._samples(job["request"])
                except Refused as refusal:
                    return _view(
                        self._append(job, "failed", {"reason": "Refused at start: " + refusal.code, "result": None})
                    )
                job_id, request = job["job_id"], copy.deepcopy(job["request"])
                output = self._dir / "runs" / job_id
                job = self._append(job, "running", {"cancel_requested": False})
                started = Fraction(str(self._clock()))
                self._progress[job_id] = {"completed": 0, "planned": None}

            def should_stop():
                with self._lock:
                    if self._jobs[job_id]["cancel_requested"]:
                        return "cancel"
                if Fraction(str(self._clock())) - started >= Fraction(request["wall_budget_s"]):
                    return "budget"
                return None

            def progress(completed, planned):
                if type(completed) is int and type(planned) is int and 0 <= completed <= planned:
                    with self._lock:
                        self._progress[job_id] = {"completed": completed, "planned": planned}

            spec = dict(
                request,
                base_url=base,
                sample_files=files,
                output_dir=str(output),
                job_id=job_id,
                attempt=job["attempt"],
            )
            record = failure = None
            stopped = False
            try:
                output.mkdir(parents=True, exist_ok=True)
                record = self._runner(spec, should_stop, progress)
            except Stopped:
                stopped = True
            except Exception as error:
                message = str(error) if isinstance(error, fast.ValidationError) else ""
                failure = "Runner failed: " + (message if _reason(message) == message else type(error).__name__)
            except BaseException:
                with self._lock:
                    self._progress.pop(job_id, None)
                    self._append(self._jobs[job_id], "interrupted", {"reason": "The runner was interrupted."})
                raise
            with self._lock:
                self._progress.pop(job_id, None)
                job = self._jobs[job_id]
                return _view(self._finish(job, record, failure, stopped, should_stop()))
        finally:
            self._run_lock.release()

    def _finish(self, job, record, failure, stopped, stop):
        if job["cancel_requested"]:
            # A cancelled job is never ingested, whatever the runner returned.
            return self._append(job, "cancelled", {"reason": "Cancelled while running."})
        if failure:
            return self._append(job, "failed", {"reason": _reason(failure), "result": None})
        if stopped:
            reason = "The wall budget ran out." if stop == "budget" else "The runner stopped without a request."
            return self._append(job, "failed", {"reason": reason, "result": None})
        try:
            record = fast.validate_record(copy.deepcopy(record))
        except Exception:
            return self._append(job, "failed", {"reason": "The runner returned an invalid record.", "result": None})
        request = job["request"]
        result = {
            "record_id": record["record_id"],
            "post_decision": record["post"]["decision"],
            "benchmarks": [
                {
                    "benchmark": b["benchmark"],
                    "planned": b["planned"],
                    "attempted": b["attempted"],
                    "stop_reason": b["stop_reason"],
                }
                for b in record["benchmarks"]
            ],
        }
        if not _bound(record, request):
            return self._append(
                job, "failed", {"reason": "The record does not match the reviewed request.", "result": result}
            )
        if any(b["attempted"] < b["planned"] for b in record["benchmarks"]):
            return self._append(
                job, "failed", {"reason": "The check is incomplete; the checkpoint is kept.", "result": result}
            )
        try:
            self._sink(copy.deepcopy(record))
        except Exception:
            return self._append(
                job,
                "failed",
                {"reason": "The record could not be ingested; the dataset is unchanged.", "result": result},
            )
        return self._append(job, "finished", {"result": result})


# ------------------------------------------------------------ dataset sink


class DatasetSink:
    """Add one validated fast-check record to ``dataset.json`` in a chosen folder.

    The whole candidate dataset is validated by ``eci_tuning.ingest`` before an
    atomic replace. Any failure leaves the existing file untouched. A record
    that is already present is not written again. No folder is chosen here.
    """

    def __init__(self, directory, now=_utc_now):
        self._directory = Path(directory)
        if not self._directory.is_absolute() or ".." in self._directory.parts:
            raise ValueError("dataset directory must be an absolute path without parent segments")
        self._now = now
        self._lock = threading.Lock()

    def __call__(self, record):
        import eci_tuning as tuning

        record = fast.validate_record(copy.deepcopy(record))
        with self._lock:
            directory = self._directory.resolve(strict=True)
            path = directory / "dataset.json"
            before = None
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                document = {"schema_version": 1, "runs": [], "items": [], "fast_checks": []}
            else:
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > tuning.MAX_DATASET_BYTES:
                    raise tuning.ValidationError("dataset.json must be a bounded regular file")
                before = (info.st_size, info.st_mtime_ns)
                document = json.loads(path.read_bytes().decode("utf-8"), object_pairs_hook=fast.unique_object)
                if (
                    not isinstance(document, dict)
                    or type(document.get("schema_version")) is not int
                    or document["schema_version"] != 1
                    or not all(isinstance(document.get(key), list) for key in ("runs", "items"))
                    or not isinstance(document.get("fast_checks", []), list)
                ):
                    raise tuning.ValidationError("expected normalized dataset version 1")
            checks = list(document.get("fast_checks", []))
            for row in checks:
                if isinstance(row, dict) and row.get("record_id") == record["record_id"]:
                    if fast.canonical(row) == fast.canonical(record):
                        return "present"
                    raise tuning.ValidationError("a different record already uses this record id")
            checks.append(record)
            computed = sum(isinstance(row, dict) and row.get("status") == "COMPUTED" for row in document["runs"])
            pairs = computed * (computed - 1) // 2 + len(checks) * (len(checks) - 1) // 2
            if (
                len(document["runs"]) + len(checks) > tuning.MAX_DATASET_RUNS
                or pairs > tuning.MAX_DATASET_COMPARISON_PAIRS
            ):
                raise tuning.ValidationError("the dataset would exceed the comparison limit")
            document = dict(document, fast_checks=checks, ingested_at=_stamp(self._now))
            payload = (json.dumps(document, indent=2, allow_nan=False) + "\n").encode("utf-8")
            if len(payload) > tuning.MAX_DATASET_BYTES:
                raise tuning.ValidationError("the dataset would exceed the size limit")
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=directory, prefix=".dataset-", suffix=".json", delete=False
                ) as stream:
                    temporary = Path(stream.name)
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                data = tuning.ingest([temporary], require_normalized=True)
                if record["record_id"] not in {row["record_id"] for row in data["fast_checks"]}:
                    raise tuning.ValidationError("the record was rejected by dataset validation")
                try:
                    info = os.lstat(path)
                    after = (info.st_size, info.st_mtime_ns)
                except FileNotFoundError:
                    after = None
                if after != before:
                    raise tuning.ValidationError("dataset.json changed during ingestion; retry")
                os.replace(temporary, path)
                temporary = None
            finally:
                if temporary is not None and temporary.exists():
                    temporary.unlink()
            return "added"
