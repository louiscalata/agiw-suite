"""Change-driven fast path for the monitor's live feed.

The full sample (collect_snapshot) takes about 165 ms, almost all of it the
`lms ps` subprocess, so it stays at one per second. The evidence files that
move when work happens (the PC job journal, the router's active record and
archive, the PC headless switch, the Nisi marker) cost only a stat() to watch.
LiveFeed checks their fingerprints every tick and re-reads just the groups that
changed, so a PC job, a route stage or a switch flip reaches the page in a
fraction of a second instead of up to two.
"""
from __future__ import annotations

import copy
import datetime as _dt
import os
import time
from typing import Any, Callable

import telemetry
from activity import collect_activity


def _paths() -> dict[str, list[os.PathLike]]:
    # Read the telemetry constants at call time so tests can point them elsewhere.
    router = telemetry._ROUTER_ROOT
    return {
        "jobs": [telemetry._WINDOWS_JOBS_PATH],
        "headless": [telemetry._WINDOWS_HEADLESS_PATH],
        # Both journal layouts (router concurrency, spec 6.12): a per-run record, note or lock
        # appears or is replaced inside active/, notes/ or locks/, which moves that directory.
        "router": [telemetry._ACTIVE_PATH, router / "archive", router / "checkpoints",
                   telemetry._PENDING_PATH, router / "active", router / "notes", router / "locks",
                   router / "policy.json", router / "install-fence.json"],
    }


def fingerprint(path: os.PathLike) -> tuple[int, int, int] | None:
    """Identity of a file or directory's current content; None when absent or unreadable."""
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def shift_ages(value: Any, delta: float) -> Any:
    """Age every relative time field by delta seconds, so moving sampledAt keeps absolute freshness."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "ageSeconds" and type(item) in (int, float):
                value[key] = item + delta
            elif key == "expiresInSeconds" and type(item) in (int, float):
                shifted = item - delta
                value[key] = int(shifted) if type(item) is int else shifted
            else:
                shift_ages(item, delta)
    elif isinstance(value, list):
        for item in value:
            shift_ages(item, delta)
    return value


def _replace_sources(sources: list[dict[str, Any]], fresh: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids = {source.get("id") for source in fresh}
    return [source for source in sources if source.get("id") not in ids] + fresh


class LiveFeed:
    """Fingerprint baseline plus a bounded overlay of the groups that changed."""

    def __init__(self, paths: Callable[[], dict[str, list[os.PathLike]]] = _paths):
        self._paths = paths
        self._baseline: dict[str, tuple] = {}

    def fingerprints(self) -> dict[str, tuple]:
        return {group: tuple(fingerprint(path) for path in paths) for group, paths in self._paths().items()}

    def set_baseline(self, prints: dict[str, tuple]) -> None:
        self._baseline = dict(prints)

    def changed(self) -> tuple[list[str], dict[str, tuple]]:
        """Groups whose evidence changed since the baseline, and the prints that were compared."""
        prints = self.fingerprints()
        return [group for group, value in prints.items() if self._baseline.get(group) != value], prints

    def overlay(self, snapshot: dict[str, Any], groups: list[str], now: float | None = None) -> dict[str, Any]:
        """A copy of the last full snapshot with the changed groups re-read at `now`."""
        now = time.time() if now is None else now
        data = copy.deepcopy(snapshot)
        delta = now - data["sampledAt"]
        if delta < 0:
            raise ValueError("snapshot is from the future")
        shift_ages(data, delta)
        data["sampledAt"] = now
        data["observedAt"] = _dt.datetime.fromtimestamp(now, _dt.timezone.utc).isoformat().replace("+00:00", "Z")
        sources = data.get("sources", [])
        if "jobs" in groups:
            jobs, jobs_source = telemetry._windows_jobs(now)
            data["windowsJobs"] = jobs
            sources = _replace_sources(sources, [jobs_source])
        if "headless" in groups and isinstance(data.get("windowsWorker"), dict):
            data["windowsWorker"]["headless"] = telemetry._windows_headless(now)
        if "router" in groups:
            observed = data["observedAt"]
            # One observation of both layouts for the pipeline, the mode and the activity rows.
            router = telemetry.observe_router(now)
            pipeline, router_source = telemetry._pipeline(now, router)
            mode = telemetry._online_code_mode(now, observed, router)
            telemetry._mark_live_route(pipeline, router_source, mode)
            data["pipeline"], data["onlineCodeMode"] = pipeline, mode
            activity = collect_activity()
            data["activity"] = {"runs": activity["runs"]}
            sources = _replace_sources(sources, [router_source, *activity["sources"]])
        data["sources"] = sources
        data["liveGroups"] = sorted(groups)
        return data
