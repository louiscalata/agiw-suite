"""Dry-run policy for the proposed AGIW private fleet. Never dispatches a job."""

from dataclasses import dataclass
from math import isfinite
from typing import Optional

ROUTER_TARGET = "nisi-0.3"
POOL_MODEL = "gpt-5.4-mini"
WORKERS = {
    "m5-macbook-pro": ("llm", "opencode", "grok", "codex", "claude"),
    "dual-gpu-pc": ("llm", "grok", "codex", "claude"),
    "monterey-mac-pro": ("llm", "grok", "codex"),
}
MAX_AGE_SECONDS = 30


@dataclass(frozen=True)
class WorkerEvidence:
    observed_at: float
    local_model_ready: bool = False
    authenticated: bool = False
    lease_available: bool = False
    busy: bool = True

    def eligible(self, now: float) -> bool:
        age = now - self.observed_at
        return (
            isfinite(age)
            and 0 <= age <= MAX_AGE_SECONDS
            and self.local_model_ready is True
            and self.authenticated is True
            and self.lease_available is True
            and self.busy is False
        )


@dataclass(frozen=True)
class HostedEvidence:
    online_opt_in: bool = False
    jev_ready: Optional[bool] = None
    pool_key_verified: bool = False
    budget_approved: bool = False
    summary_redacted: bool = False


@dataclass(frozen=True)
class RoutePlan:
    worker: Optional[str]
    preanalysis: str
    reason: str


def plan(
    mode: str,
    workload: str,
    workers: dict[str, WorkerEvidence],
    hosted: HostedEvidence,
    *,
    now: float,
    source: str = "agent",
) -> RoutePlan:
    """Return a candidate route, not an authorization, receipt, lease or cost guarantee."""
    if source == "notion":
        return RoutePlan(None, "none", "notion-documentation-only")
    if source != "agent":
        raise ValueError("Unknown request source")
    if mode not in {"local", "online"} or workload not in {"routine", "gpu-heavy"}:
        raise ValueError("Unknown mode or workload")

    order = tuple(WORKERS) if workload == "routine" else ("dual-gpu-pc", "m5-macbook-pro", "monterey-mac-pro")
    selected = next((worker for worker in order if worker in workers and workers[worker].eligible(now)), None)
    if selected is None:
        return RoutePlan(None, "none", "no-verified-local-worker")
    if mode == "local":
        return RoutePlan(selected, "local", "strict-local")
    if hosted.online_opt_in is not True or hosted.summary_redacted is not True:
        return RoutePlan(selected, "local", "hosted-not-authorized")
    if hosted.jev_ready is True:
        return RoutePlan(selected, "jev", "jev-first")
    if hosted.jev_ready is False and hosted.pool_key_verified is True and hosted.budget_approved is True:
        return RoutePlan(selected, f"responses/{POOL_MODEL}", "jev-unavailable-pool-candidate")
    return RoutePlan(selected, "local", "hosted-unavailable")
