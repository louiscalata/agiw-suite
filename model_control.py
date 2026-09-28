"""Explicit, bounded LM Studio model actions for the loopback monitor.

The sampler remains passive. Only an explicit same-origin HTTP POST can
reach request(); an accepted action is checked against fresh observed inventory
again in its worker before invoking the local CLI.
"""
from __future__ import annotations

import copy
from contextlib import nullcontext
import math
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import threading
import time
import uuid
from typing import Callable

import mem_guard
from durable_model_journal import DurableModelJournal, JournalError


MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,159}\Z")
MAX_OUTPUT = 64 * 1024
LOAD_TIMEOUT = 120.0
UNLOAD_TIMEOUT = 30.0
VERIFY_TIMEOUT = 12.0
MAX_SNAPSHOT_AGE = 3.0


class ControlError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


class _Cancelled(ControlError):
    def __init__(self):
        super().__init__(409, "Model operation was cancelled.")


class _Operation:
    """One retained operation; cancellation also covers Popen/registration races."""
    def __init__(self, identifier: str):
        self.identifier = identifier
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.process = None
        self.attempted = False
        self.cleanup_failed = False
        self.group_signal_sent = False
        self.worker = None

    def check(self):
        if self.cancelled.is_set():
            raise _Cancelled()

    def register(self, process):
        with self.lock:
            self.process = process
            self.attempted = True

    def cancel(self):
        # Only the worker may signal or reap. A different thread checking poll
        # then killpg can race a reap and signal a subsequently reused PGID.
        self.cancelled.set()

    def release(self, process):
        with self.lock:
            if self.process is process and process.returncode is not None:
                self.process = None


def _source_live(snapshot: dict, source_id: str) -> bool:
    return any(source.get("id") == source_id and source.get("state") == "live"
               for source in snapshot.get("sources", []) if isinstance(source, dict))


def _target(snapshot: dict | None, action: str, model_id: str) -> dict:
    """Resolve the exact selected ID from a recent trusted local observation."""
    if not isinstance(action, str) or action not in {"load", "unload"}:
        raise ControlError(400, "Action must be load or unload.")
    if not isinstance(model_id, str) or not MODEL_ID.fullmatch(model_id):
        raise ControlError(400, "A valid exact model ID is required.")
    if not isinstance(snapshot, dict):
        raise ControlError(503, "Model inventory is not available yet.")
    sampled = snapshot.get("sampledAt")
    if type(sampled) not in (int, float) or not math.isfinite(sampled):
        raise ControlError(503, "Model inventory has no valid sample time.")
    age = time.time() - sampled
    if age < -0.5 or age > MAX_SNAPSHOT_AGE:
        raise ControlError(503, "Model inventory is stale. Wait for a fresh sample.")
    rows = snapshot.get("models")
    if not isinstance(rows, list):
        raise ControlError(503, "Model inventory is unavailable.")
    matches = [row for row in rows if isinstance(row, dict)
               and row.get("host") == "mac" and row.get("id") == model_id]
    if len(matches) != 1:
        raise ControlError(404, "Selected model is not in the current Mac inventory.")
    row = matches[0]
    row_age = row.get("ageSeconds")
    if type(row_age) not in (int, float) or not math.isfinite(row_age) or row_age < 0 or row_age > MAX_SNAPSHOT_AGE:
        raise ControlError(503, "Selected model observation is stale.")

    if action == "load":
        if not _source_live(snapshot, "lmstudio-api") or row.get("source") != "lmstudio-api":
            raise ControlError(503, "A fresh LM Studio model inventory is required to load.")
        if row.get("modelKey") != model_id:
            raise ControlError(503, "The selected row has no exact LM Studio model key.")
        if row.get("loaded") is not False or row.get("state") != "unloaded":
            raise ControlError(409, "Selected model is already loaded or its state is unknown.")
    else:
        if not _source_live(snapshot, "lms-ps") or row.get("source") != "lms-ps":
            raise ControlError(503, "Fresh loaded-instance activity is required to unload.")
        if not _source_live(snapshot, "lmstudio-api"):
            raise ControlError(503, "Fresh LM Studio instance inventory is required to unload.")
        if row.get("loaded") is not True:
            raise ControlError(409, "Selected model is not loaded.")
        if row.get("instanceId") != model_id:
            raise ControlError(503, "The selected row has no exact loaded-instance identifier.")
        if not isinstance(row.get("modelKey"), str) or not MODEL_ID.fullmatch(row["modelKey"]):
            raise ControlError(503, "The selected instance has no exact model key.")
        matching_lists = [candidate["loadedInstanceIds"] for candidate in rows
                          if isinstance(candidate, dict) and candidate.get("host") == "mac"
                          and candidate.get("modelKey") == row["modelKey"]
                          and isinstance(candidate.get("loadedInstanceIds"), list)]
        if len(matching_lists) != 1 or model_id not in matching_lists[0]:
            raise ControlError(503, "The selected instance is not confirmed by LM Studio inventory.")
        if row.get("state") != "idle":
            raise ControlError(409, "Selected model is busy or its activity is unknown.")
        if type(row.get("queued")) is not int or row["queued"] != 0:
            raise ControlError(409, "Selected model has queued work or queue state is unknown.")
    return row


LOAD_FALLBACK_NEED = 8 * mem_guard.GiB


def load_need_bytes(row: dict) -> int:
    """Memory a load needs: the inventory's model size x 1.15 + 1 GiB, or 8 GiB without a size."""
    size = row.get("sizeBytes") if isinstance(row, dict) else None
    if type(size) is int and 0 < size <= 1 << 50:
        return int(size * 1.15) + mem_guard.GiB
    return LOAD_FALLBACK_NEED


def _memory_config() -> dict:
    try:
        return mem_guard.load_config()
    except Exception:
        return mem_guard.default_config()


def _memory_gate(snapshot: dict, row: dict, config: Callable[[], dict]) -> None:
    """Refuse (409) a load the sampled memory cannot hold safely. The monitor's own memory block
    is the state; a snapshot without one is unknown memory, so the heavy load is refused."""
    memory = snapshot.get("memory") if isinstance(snapshot, dict) else None
    memory = memory if isinstance(memory, dict) else {}
    try:
        cfg = config()
    except Exception:
        cfg = mem_guard.default_config()
    decision = mem_guard.admit(load_need_bytes(row), "heavy", memory, cfg)
    if decision.allowed:
        return
    tips = memory.get("suggestions")
    tip = next((t for t in tips if isinstance(t, str) and t), None) if isinstance(tips, list) else None
    name = row.get("name") if isinstance(row.get("name"), str) and row.get("name") else row.get("id")
    message = f"Not enough free memory to load {name} safely: {decision.reason}."
    raise ControlError(409, f"{message} {tip}." if tip else message)


def _find_lms() -> str:
    fixed = Path.home() / ".lmstudio/bin/lms"
    try:
        for directory in (fixed.parent.parent, fixed.parent):
            info = directory.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o022):
                raise ValueError("unsafe CLI directory")
        info = fixed.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022 or info.st_nlink != 1
                or not os.access(fixed, os.X_OK)):
            raise ValueError("unsafe CLI executable")
    except (OSError, ValueError):
        raise ControlError(503, "The installed LM Studio CLI is unavailable or unsafe.")
    return str(fixed)


def capability() -> dict:
    try:
        _find_lms()
    except ControlError:
        return {"supported": False,
                "reason": "The installed LM Studio CLI is unavailable or unsafe on this Mac."}
    return {"supported": True, "reason": "Explicit local LM Studio model controls are available."}


def _clean_output(data: bytes) -> str:
    # CLI text is diagnostic data, never HTML. Keep status messages short.
    value = data.decode("utf-8", errors="replace")
    value = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", value)
    value = " ".join(value.split())
    return value[:500]


def _run_cli(action: str, model_id: str, *, operation: _Operation | None = None) -> None:
    if operation is not None:
        operation.check()
    executable = _find_lms()
    command = ([executable, "load", model_id, "--yes"] if action == "load"
               else [executable, "unload", model_id])
    timeout = LOAD_TIMEOUT if action == "load" else UNLOAD_TIMEOUT
    if operation is not None:
        operation.check()
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               close_fds=True, start_new_session=True)
    if operation is not None:
        operation.register(process)
    output = bytearray()
    selector = None
    deadline = time.monotonic() + timeout
    try:
        selector = selectors.DefaultSelector()
        if process.stdout is None:
            raise ControlError(502, "LM Studio command output pipe is unavailable.")
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            if operation is not None:
                operation.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ControlError(504, f"LM Studio {action} timed out after {int(timeout)} seconds.")
            ready = selector.select(min(remaining, 0.1))
            if ready:
                part = os.read(process.stdout.fileno(), min(4096, MAX_OUTPUT + 1 - len(output)))
                if not part:
                    break
                output.extend(part)
                if len(output) > MAX_OUTPUT:
                    raise ControlError(502, "LM Studio command output exceeded the safety limit.")
            # Do not poll/reap while reading. If a descendant holds the pipe
            # after its leader exits, that unreaped leader still reserves the
            # PGID, allowing this worker to signal the group on cancellation.
        if operation is not None:
            operation.check()
        while True:
            if operation is not None:
                operation.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ControlError(504, f"LM Studio {action} timed out after {int(timeout)} seconds.")
            try:
                exit_code = process.wait(timeout=min(0.1, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        if operation is not None:
            operation.check()
        if exit_code != 0:
            detail = _clean_output(output)
            suffix = f" {detail}" if detail else ""
            raise ControlError(502, f"LM Studio {action} failed (exit {exit_code}).{suffix}")
    except subprocess.TimeoutExpired:
        raise ControlError(504, f"LM Studio {action} timed out after {int(timeout)} seconds.")
    finally:
        try:
            # This worker is the sole reaper. Never signal after wait() reaped
            # the leader: its PGID may now belong to an unrelated process.
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                    if operation is not None:
                        operation.group_signal_sent = True
                except ProcessLookupError:
                    pass
                reap_deadline = time.monotonic() + 2
                while True:
                    remaining = reap_deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(command, 2)
                    try:
                        process.wait(timeout=min(0.1, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
        except (OSError, subprocess.SubprocessError):
            if operation is not None:
                operation.cleanup_failed = True
            raise
        finally:
            if selector is not None:
                selector.close()
            if process.stdout is not None:
                process.stdout.close()
            if operation is not None:
                operation.release(process)


def _observed(snapshot: dict | None, action: str, model_id: str,
              model_key: str, after: float) -> bool:
    if not isinstance(snapshot, dict):
        return False
    sampled = snapshot.get("sampledAt")
    if type(sampled) not in (int, float) or sampled <= after:
        return False
    rows = snapshot.get("models", [])
    if not isinstance(rows, list):
        return False
    if action == "load":
        if not (_source_live(snapshot, "lmstudio-api") or _source_live(snapshot, "lms-ps")):
            return False
        return any(isinstance(row, dict) and row.get("host") == "mac"
                   and row.get("id") == model_id and row.get("loaded") is True
                   for row in rows)
    if not (_source_live(snapshot, "lms-ps") and _source_live(snapshot, "lmstudio-api")):
        return False
    if any(isinstance(row, dict) and row.get("host") == "mac"
           and row.get("id") == model_id and row.get("source") == "lms-ps"
           and row.get("loaded") is True for row in rows):
        return False
    # API instance IDs are projected only when every ID was safely captured.
    # A missing CLI row alone could be a transient omission, not an unload.
    instance_lists = [row["loadedInstanceIds"] for row in rows
                      if isinstance(row, dict) and row.get("host") == "mac"
                      and row.get("modelKey") == model_key
                      and isinstance(row.get("loadedInstanceIds"), list)]
    return len(instance_lists) == 1 and model_id not in instance_lists[0]


def _exact_loaded_instance(rows: list, model_id: str, model_key: str) -> bool:
    """Match the API's sole loaded ID to one live CLI instance for this key.

    LM Studio may assign an instance ID different from the requested model key.
    The API inventory supplies that ID; the CLI row must corroborate it.
    """
    key_rows = [row for row in rows if isinstance(row, dict)
                and row.get("host") == "mac" and row.get("modelKey") == model_key
                and row.get("id") == model_id and row.get("loaded") is True]
    if len(key_rows) != 1:
        return False
    instances = key_rows[0].get("loadedInstanceIds")
    if (not isinstance(instances, list) or len(instances) != 1
            or not isinstance(instances[0], str) or not MODEL_ID.fullmatch(instances[0])):
        return False
    instance_id = instances[0]
    cli_rows = [row for row in rows if isinstance(row, dict)
                and row.get("host") == "mac" and row.get("modelKey") == model_key
                and row.get("source") == "lms-ps"]
    return (len(cli_rows) == 1 and cli_rows[0].get("id") == instance_id
            and cli_rows[0].get("instanceId") == instance_id
            and cli_rows[0].get("loaded") is True)


def _positive_settlement(snapshot: dict | None, action: str, model_id: str,
                         model_key: str, after: float) -> bool:
    """Fresh exact inventory evidence suitable for durable confirmation."""
    if not isinstance(snapshot, dict):
        return False
    sampled = snapshot.get("sampledAt")
    if (type(sampled) not in (int, float) or not math.isfinite(sampled)
            or sampled <= after or not -0.5 <= time.time() - sampled <= MAX_SNAPSHOT_AGE):
        return False
    if not (_source_live(snapshot, "lmstudio-api") and _source_live(snapshot, "lms-ps")):
        return False
    rows = snapshot.get("models")
    if not isinstance(rows, list):
        return False
    relevant = [row for row in rows if isinstance(row, dict)
                and row.get("host") == "mac" and row.get("modelKey") == model_key]
    if not relevant or not all(type(row.get("ageSeconds")) in (int, float)
                               and math.isfinite(row["ageSeconds"])
                               and 0 <= row["ageSeconds"] <= MAX_SNAPSHOT_AGE for row in relevant):
        return False
    if action == "load":
        if not _exact_loaded_instance(rows, model_id, model_key):
            return False
    elif action == "unload":
        lists = [row["loadedInstanceIds"] for row in relevant
                 if isinstance(row.get("loadedInstanceIds"), list)]
        if len(lists) != 1 or model_id in lists[0]:
            return False
    else:
        return False
    return _observed(snapshot, action, model_id, model_key, after)


class ModelControl:
    """Shared, retained model operation ownership within one Monitor process.

    Unconfirmed dispatches block admission until explicit positive reconciliation.
    When journal_path is provided, a private write-ahead record also holds this
    latch across Monitor restarts. Missing or invalid journals never self-heal.
    """
    def __init__(self, store, *, runner: Callable[[str, str], None] = _run_cli,
                 memory_config: Callable[[], dict] = _memory_config,
                 journal_path: str | Path | None = None):
        self.store = store
        self.runner = runner
        self.memory_config = memory_config
        self.lock = threading.Lock()
        self._operation = None
        self.journal = DurableModelJournal(journal_path) if journal_path is not None else None
        self._durable_fault = None
        self._journal_generation = None
        self._recovery_started_at = time.time()
        self.status = {"status": "idle", "operationId": None, "action": None,
                       "modelId": None, "message": "No model operation has been requested.",
                       "startedAt": None, "finishedAt": None,
                       "settlement": "not-started", "cancellationRequested": False}
        if self.journal is not None:
            try:
                record = self.journal.read()
                self._journal_generation = record["generation"]
                if record["phase"] == "pending":
                    saved = record["operation"]
                    operation = _Operation(saved["operationId"])
                    operation.model_key = saved["modelKey"]
                    operation.attempted = True  # possible dispatch; never infer not-started after a crash
                    operation.cleanup_failed = not saved["cleanupConfirmed"]
                    self._operation = operation
                    self.status = {"status": "failed", "operationId": saved["operationId"],
                                   "action": saved["action"], "modelId": saved["modelId"],
                                   "message": "Recovered an unconfirmed model operation; no command was repeated.",
                                   "startedAt": saved["startedAt"],
                                   "finishedAt": saved["finishedAt"] or saved["startedAt"],
                                   "settlement": "unconfirmed", "cancellationRequested": False}
            except JournalError as error:
                self._durable_fault = str(error)
                self.status.update(status="failed", settlement="unconfirmed",
                                   message="Model operation journal is unavailable: " + str(error))

    def read(self) -> dict:
        with self.lock:
            result = copy.deepcopy(self.status)
            result["workerActive"] = self._worker_active()
            result["cleanupConfirmed"] = (self._durable_fault is None and
                                          (self._operation is None or
                                           (self._operation.process is None and not self._operation.cleanup_failed)))
            # Compatibility field above concerns only the CLI leader. A sent
            # group signal is evidence of the signal, not descendant exit.
            result["cleanupScope"] = "cli-leader"
            result["groupSignalSent"] = bool(self._operation and self._operation.group_signal_sent)
            if self.journal is not None:
                result["durableJournal"] = {"enabled": True,
                                            "blocked": self._durable_fault is not None,
                                            "reason": self._durable_fault,
                                            "generation": self._journal_generation}
            return result

    def _worker_active(self):
        return bool(self._operation is not None and self._operation.worker is not None
                    and self._operation.worker.is_alive())

    def _check_available(self):
        if self._durable_fault is not None:
            raise ControlError(409, "Model operation journal is unavailable; no action can start.")
        if self.status["status"] == "running" or self._worker_active():
            raise ControlError(409, "A model operation is already running.")
        if self.status["settlement"] == "unconfirmed":
            raise ControlError(409, "The previous model operation is unconfirmed; reconcile fresh inventory before another operation.")

    def _selected_operation(self, operation_id):
        if operation_id is not None and operation_id != self.status["operationId"]:
            raise ControlError(409, "The selected model operation is no longer current.")
        return self._operation

    def cancel(self, operation_id: str | None = None) -> dict:
        """Request cancellation without claiming that LM Studio rolled back the command.

        A submitted command stays unconfirmed until fresh positive inventory can
        reconcile it. Call join() separately; cancellation never blocks on a CLI.
        """
        with self.lock:
            operation = self._selected_operation(operation_id)
            if operation is not None and (self.status["status"] == "running" or operation.cleanup_failed):
                operation.cancel()
                self.status["cancellationRequested"] = True
        return self.read()

    def join(self, timeout: float, *, operation_id: str | None = None) -> bool:
        """Wait at most timeout seconds for the selected worker; never joins itself."""
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError("join timeout must be finite and nonnegative")
        with self.lock:
            operation = self._selected_operation(operation_id)
            worker = operation.worker if operation is not None else None
        if worker is None:
            return True
        if worker is threading.current_thread():
            return False
        worker.join(timeout)
        return not worker.is_alive()

    def reconcile(self, operation_id: str) -> dict:
        """Clear an uncertainty latch only on fresh positive desired-state evidence.

        Opposite-state evidence cannot prove an interrupted server load has
        stopped. An absent model row is also insufficient for unload: the API
        must supply its exact instance list. Omitted inventory stays blocked;
        restarting is not proof of settlement. This method never repeats work.
        """
        if not isinstance(operation_id, str) or not operation_id:
            raise ControlError(400, "An exact model operation ID is required to reconcile.")
        with self.lock:
            if self._durable_fault is not None:
                raise ControlError(409, "Model operation journal is unavailable; manual recovery is required.")
            operation = self._selected_operation(operation_id)
            if self.status["status"] == "running" or self._worker_active():
                raise ControlError(409, "The model operation is still running.")
            if self.status["settlement"] != "unconfirmed":
                return copy.deepcopy(self.status)
            if operation is None or operation.cleanup_failed:
                raise ControlError(409, "Model command cleanup was not confirmed; inspect its owner before retrying.")
            before = copy.deepcopy(self.status)
        # The sampler can take its store lock before calling model-control.
        # Never hold this controller lock while acquiring the store lock.
        snapshot = self.store.read()
        with self.lock:
            self._selected_operation(operation_id)
            if self._operation is not operation or self.status != before or self._worker_active():
                raise ControlError(409, "The model operation changed during reconciliation; retry its status read.")
            sampled = snapshot.get("sampledAt") if isinstance(snapshot, dict) else None
            rows = snapshot.get("models") if isinstance(snapshot, dict) else None
            relevant = ([row for row in rows if isinstance(row, dict) and row.get("host") == "mac"
                         and row.get("modelKey") == operation.model_key] if isinstance(rows, list) else [])
            fresh_rows = relevant and all(type(row.get("ageSeconds")) in (int, float)
                                          and math.isfinite(row["ageSeconds"])
                                          and 0 <= row["ageSeconds"] <= MAX_SNAPSHOT_AGE for row in relevant)
            if self.status["action"] == "load":
                exact_instance = _exact_loaded_instance(rows, self.status["modelId"],
                                                        operation.model_key)
            else:
                exact_instance = True  # _observed checks the exact API instance list for an unload.
            if (type(sampled) not in (int, float) or not math.isfinite(sampled)
                    or not -0.5 <= time.time() - sampled <= MAX_SNAPSHOT_AGE
                    or not fresh_rows or not exact_instance
                    or not (_source_live(snapshot, "lmstudio-api") and _source_live(snapshot, "lms-ps"))
                    or not _observed(snapshot, self.status["action"], self.status["modelId"],
                                     operation.model_key, self.status["finishedAt"])):
                raise ControlError(409, "Fresh inventory has not confirmed the selected model operation.")
            if self.journal is not None:
                if not _positive_settlement(snapshot, self.status["action"],
                                            self.status["modelId"], operation.model_key,
                                            self.status["finishedAt"]):
                    raise ControlError(409, "Exact fresh inventory has not confirmed the selected model operation.")
                try:
                    saved = self.journal.read()
                    if (saved["phase"] != "pending"
                            or saved["generation"] != self._journal_generation
                            or saved["operation"]["operationId"] != operation_id
                            or saved["operation"]["action"] != self.status["action"]
                            or saved["operation"]["modelId"] != self.status["modelId"]
                            or saved["operation"]["modelKey"] != operation.model_key):
                        raise JournalError("Exact journal operation identity changed.")
                    if sampled <= max(saved["operation"]["updatedAt"], self._recovery_started_at):
                        raise ControlError(409, "A fresh post-restart model observation is required.")
                    self.journal.confirm_recovered(
                        operation_id=operation_id, action=self.status["action"],
                        model_id=self.status["modelId"], model_key=operation.model_key,
                        generation=self._journal_generation, observed_at=sampled,
                        recovery_started_at=self._recovery_started_at)
                except ControlError:
                    raise
                except JournalError as error:
                    self._durable_fault = str(error)
                    raise ControlError(409, "Durable reconciliation failed; no new action can start.") from error
            self.status.update(settlement="confirmed", message=(
                "The interrupted model operation is now confirmed by fresh inventory; no command was repeated."))
        return self.read()

    def request(self, action: str, model_id: str, *, action_guard=None) -> dict:
        """Accept a model action; an optional internal guard spans the worker's action and
        confirmation. The guard is entered in the worker before its fresh target check."""
        with self.lock:
            self._check_available()
        snapshot = self.store.read()
        selected = _target(snapshot, action, model_id)
        if action == "load":  # unload frees memory and is never gated
            _memory_gate(snapshot, selected, self.memory_config)
        model_key = selected["modelKey"]
        with self.lock:
            self._check_available()
            started = time.time()
            operation = _Operation(uuid.uuid4().hex)
            operation.model_key = model_key
            if self.journal is not None:
                try:
                    saved = self.journal.begin(operation_id=operation.identifier,
                                               action=action, model_id=model_id,
                                               model_key=model_key, started_at=started)
                    self._journal_generation = saved["generation"]
                except JournalError as error:
                    self._durable_fault = str(error)
                    self.status.update(status="failed", settlement="unconfirmed",
                                       message="Durable model admission was refused: " + str(error))
                    raise ControlError(409, "Durable model operation admission failed; no command was started.") from error
            self._operation = operation
            self.status = {"status": "running", "operationId": operation.identifier,
                           "action": action, "modelId": model_id,
                           "message": f"LM Studio {action} requested; checking current state.",
                           "startedAt": started, "finishedAt": None,
                           "settlement": "pending", "cancellationRequested": False}
            accepted = copy.deepcopy(self.status)
            worker = threading.Thread(target=self._work,
                                      args=(action, model_id, model_key, started, action_guard, operation),
                                      daemon=True)
            operation.worker = worker
            # Start while holding admission so cancel/join never sees an
            # unstarted worker. The worker takes this lock only for status.
            try:
                worker.start()
            except Exception:
                operation.worker = None
                self.status.update(status="failed",
                                   settlement="unconfirmed" if self.journal else "not-started",
                                   finishedAt=time.time(),
                                   message="Model operation worker could not start; durable admission remains held.")
                raise
        return accepted

    def _work(self, action: str, model_id: str, model_key: str, started: float,
              action_guard=None, operation=None) -> None:
        assert operation is not None
        confirmed = False
        observed_at = None
        try:
            operation.check()
            with action_guard() if action_guard is not None else nullcontext():
                operation.check()
                # The sampler could have advanced while the request thread started.
                snapshot = self.store.read()
                current = _target(snapshot, action, model_id)
                if current["modelKey"] != model_key:
                    raise ControlError(409, "Selected model identity changed before the action started.")
                if action == "load":  # memory may have changed since the request; check it again
                    _memory_gate(snapshot, current, self.memory_config)
                operation.check()
                if self.runner is _run_cli:
                    self.runner(action, model_id, operation=operation)
                else:
                    # Injectable legacy runners retain their two-argument API.
                    # Their internal activity is opaque, so assume dispatch.
                    operation.attempted = True
                    self.runner(action, model_id)
                operation.check()
                command_finished = time.time()
                with self.lock:
                    self.status["message"] = (f"LM Studio {action} command completed; "
                                              "waiting for fresh inventory confirmation.")
                deadline = time.monotonic() + VERIFY_TIMEOUT
                while time.monotonic() < deadline:
                    operation.check()
                    observed = self.store.read()
                    positive = (_positive_settlement(observed, action, model_id, model_key,
                                                     command_finished) if self.journal is not None else
                                _observed(observed, action, model_id, model_key, command_finished))
                    if positive:
                        operation.check()
                        confirmed = True
                        observed_at = observed["sampledAt"]
                        result = ("succeeded", f"Model {action} confirmed by fresh LM Studio inventory.")
                        break
                    operation.cancelled.wait(0.25)
                else:
                    result = ("failed", f"LM Studio {action} command completed, but fresh inventory did not confirm the change.")
        except ControlError as error:
            confirmed = False
            result = ("failed", error.message)
        except (OSError, subprocess.SubprocessError) as error:
            confirmed = False
            result = ("failed", f"LM Studio {action} could not run: {type(error).__name__}.")
        except Exception:
            confirmed = False
            result = ("failed", f"LM Studio {action} failed unexpectedly.")
        with self.lock:
            # Cancellation can race the final observation or guard exit. It
            # must never be silently replaced with a successful completion.
            if operation.cancelled.is_set():
                confirmed = False
                result = ("failed", "Model operation was cancelled.")
            owner_cleanup_confirmed = (operation.attempted and not operation.cleanup_failed
                                       and operation.process is None
                                       and self.runner is _run_cli)
            if confirmed and self.journal is not None and not owner_cleanup_confirmed:
                confirmed = False
                result = ("failed", "Model result was observed but command owner cleanup was not confirmed.")
            if self.journal is not None:
                try:
                    self.journal.finish(
                        operation_id=operation.identifier, action=action, model_id=model_id,
                        model_key=model_key, generation=self._journal_generation,
                        finished_at=time.time(), cleanup_confirmed=owner_cleanup_confirmed,
                        observed_at=observed_at if confirmed and owner_cleanup_confirmed else None)
                except JournalError as error:
                    self._durable_fault = str(error)
                    confirmed = False
                    result = ("failed", "Durable model journal write failed; settlement remains unconfirmed.")
            settlement = ("confirmed" if confirmed else
                          "unconfirmed" if self.journal is not None or operation.attempted else "not-started")
            message = result[1]
            if settlement == "unconfirmed":
                message += " LM Studio settlement is unconfirmed; no rollback or retry is claimed."
            elif settlement == "not-started" and operation.cancelled.is_set():
                message += " No LM Studio command was started."
            if operation.cleanup_failed:
                message += " The CLI process could not be confirmed terminated."
            self.status.update(status=result[0], message=message, finishedAt=time.time(),
                               settlement=settlement,
                               cancellationRequested=operation.cancelled.is_set())
