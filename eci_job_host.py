"""Build a check-job engine from one explicit config; Python 3.9, stdlib only.

The observer calls ``open_host(path)`` only when it is started with
``--check-jobs-config PATH``. The config names the state directory, the dataset
folder, the sample sets, the allowed registry lanes and the wall budget. The
gate is not in the config: it is ``lambda: False`` here, so submit, resume and
start refuse with ``gate_closed``. The observer gets the three read-only routes
and nothing else. This module opens no socket, starts no thread and contacts no
lane. See ``docs/eci-check-jobs.md``.
"""

import json
import os
from pathlib import Path
import re
import stat

import eci_jobs
import eci_job_routes
import remote_machines

MAX_CONFIG_BYTES = 65536
MAX_SAMPLE_SETS = 8
MAX_SAMPLE_FILES = 16
MAX_TARGETS = 64
MAX_PATH_CHARS = 1024
FIELDS = frozenset({"state_dir", "dataset_dir", "sample_sets", "allowed_targets",
                    "max_wall_budget_s", "default_wall_budget_s"})
_SAMPLE_SET = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
UNREADABLE = "The check-job configuration cannot be read safely."
INVALID = "The check-job configuration is not valid."
ENGINE_FAILED = "The check-job engine cannot be opened."


class ConfigError(Exception):
    """The config was not used. The message is one of the fixed reasons above."""


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _reject_constant(name):
    raise ValueError("non-finite number")


def _read_bytes(path):
    """Read an owned regular file that no group or other user can write. Links refuse."""
    path = Path(path) if isinstance(path, (str, Path)) else None
    if path is None or not path.is_absolute() or ".." in path.parts:
        raise ConfigError(UNREADABLE)
    if not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_NONBLOCK", "getuid")):
        raise ConfigError(UNREADABLE)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise ConfigError(UNREADABLE) from None
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or info.st_size > MAX_CONFIG_BYTES):
            raise ConfigError(UNREADABLE)
        raw = bytearray()
        while len(raw) <= MAX_CONFIG_BYTES:
            chunk = os.read(descriptor, MAX_CONFIG_BYTES + 1 - len(raw))
            if not chunk:
                break
            raw.extend(chunk)
    except OSError:
        raise ConfigError(UNREADABLE) from None
    finally:
        os.close(descriptor)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ConfigError(UNREADABLE)
    return bytes(raw)


def _directory(value):
    if not isinstance(value, str) or not value or len(value) > MAX_PATH_CHARS:
        raise ConfigError(INVALID)
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ConfigError(INVALID)
    return value


def read_config(path):
    """The validated config at ``path``, exactly as written. Raises ``ConfigError``."""
    raw = _read_bytes(path)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique,
                           parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ConfigError(INVALID) from None
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ConfigError(INVALID)
    state, dataset = _directory(value["state_dir"]), _directory(value["dataset_dir"])
    if Path(state) == Path(dataset):
        raise ConfigError(INVALID)
    sets = value["sample_sets"]
    if not isinstance(sets, dict) or not sets or len(sets) > MAX_SAMPLE_SETS:
        raise ConfigError(INVALID)
    for name, files in sets.items():
        if (not _SAMPLE_SET.fullmatch(name) or not isinstance(files, list) or not files
                or len(files) > MAX_SAMPLE_FILES or len(set(map(str, files))) != len(files)):
            raise ConfigError(INVALID)
        for item in files:
            _directory(item)
    targets = value["allowed_targets"]
    if (not isinstance(targets, list) or len(targets) > MAX_TARGETS
            or len(set(map(str, targets))) != len(targets)):
        raise ConfigError(INVALID)
    if not isinstance(value["max_wall_budget_s"], str) or not isinstance(value["default_wall_budget_s"], str):
        raise ConfigError(INVALID)
    try:
        # Policy checks the lane ids and both budgets with the engine's own rules.
        eci_jobs.Policy(allowed_targets=targets, max_wall_budget_s=value["max_wall_budget_s"],
                        default_wall_budget_s=value["default_wall_budget_s"])
    except (ValueError, TypeError):
        raise ConfigError(INVALID) from None
    return value


def _registry_targets(registry):
    """Registry lanes read again on every call, so previews follow the live registry."""
    return lambda: remote_machines.lane_targets(registry())


class Unavailable:
    """Routes for a config or engine that could not be used. Reads answer unknown."""

    def __init__(self, reason):
        self.reason = reason

    def __call__(self, path, query):
        if path == eci_job_routes.STATUS_PATH:
            return 200, {"status": "unknown", "reason": self.reason, "owner": False,
                         "gate": "closed", "jobs": [], "active": None}
        return 503, {"status": "unknown", "reason": self.reason}


class Host:
    """The engine the observer reads, and its routes. ``engine`` is ``None`` when unusable."""

    def __init__(self, routes, engine=None):
        self.routes, self.engine = routes, engine

    def close(self):
        if self.engine is not None:
            self.engine.close()


def open_host(path, *, registry=None):
    """Build the engine named by the config at ``path``. Never raises; the gate stays closed.

    ``registry`` returns the lane registry document; the default is the
    monitor's own ``remote_machines.load_config()``.
    """
    try:
        config = read_config(path)
    except ConfigError as error:
        return Host(Unavailable(str(error)))
    loader = registry if registry is not None else (lambda: remote_machines.load_config())
    try:
        engine = eci_jobs.Engine(
            config["state_dir"],
            # The submission gate stays closed until owner decision D1 says what opens it.
            policy=eci_jobs.Policy(gate=lambda: False, allowed_targets=config["allowed_targets"],
                                   max_wall_budget_s=config["max_wall_budget_s"],
                                   default_wall_budget_s=config["default_wall_budget_s"]),
            targets=_registry_targets(loader),
            sample_sets=config["sample_sets"],
            runner=eci_jobs.adapter_runner,
            sink=eci_jobs.DatasetSink(config["dataset_dir"]))
    except Exception:
        return Host(Unavailable(ENGINE_FAILED))
    return Host(eci_job_routes.ReadOnlyRoutes(engine), engine)
