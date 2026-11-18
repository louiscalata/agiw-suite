"""Offline owner-status admission assessment. No transport, reservation or dispatch I/O."""

from dataclasses import dataclass
from math import isfinite
from typing import Optional

from .fleet_routing_plan import MAX_AGE_SECONDS, WorkerEvidence


@dataclass(frozen=True)
class BoundEvidence:
    # Caller-verified evidence, never inferred from an inventory or hostname.
    transport_bound: bool = False
    share_present: bool = False
    observed_at: float = float("nan")
    served_model: Optional[str] = None
    model_verified: bool = False
    reservation_valid: bool = False
    reservation_job: Optional[str] = None
    reservation_model: Optional[str] = None
    reservation_expires_at: float = float("nan")


@dataclass(frozen=True)
class Assessment:
    reason: str
    candidate: Optional[WorkerEvidence] = None


def _number(x):
    return type(x) in (int, float) and isfinite(x)


def _evidence_problem(evidence, now):
    if not isinstance(evidence, BoundEvidence):
        return "evidence-unknown"
    if evidence.share_present is not True:
        return "share-missing-or-unknown"
    if evidence.transport_bound is not True:
        return "transport-identity-unverified"
    if not (_number(now) and _number(evidence.observed_at) and 0 <= now - evidence.observed_at <= MAX_AGE_SECONDS):
        return "observation-stale-or-unknown"
    return None


def assess(status, evidence, *, requested_model, job_id, lane, now):
    """Consume pc-llm.status-shaped data plus independently bound proofs.

    A candidate is only a preview policy input. It does not acquire/consume a
    lease, authenticate SMB, verify a model, authorize dispatch or prevent races.
    Revalidation and atomic lease consumption remain required at dispatch.
    """
    problem = _evidence_problem(evidence, now)
    if problem:
        return Assessment(problem)
    if not isinstance(status, dict) or type(status.get("schemaVersion")) is not int or status["schemaVersion"] != 1:
        return Assessment("unsupported-status")
    switch, worker, lanes = (status.get(k) for k in ("switch", "worker", "lanes"))
    if not all(isinstance(v, dict) for v in (switch, worker, lanes)):
        return Assessment("malformed-status")
    if switch.get("state") != "on" or status.get("usable") is not True or worker.get("ok") is not True:
        return Assessment("owner-unavailable")
    age = worker.get("heartbeatAgeSeconds")
    if not (_number(age) and age >= 0 and 0 <= age + now - evidence.observed_at <= MAX_AGE_SECONDS):
        return Assessment("heartbeat-stale-or-unknown")
    row = lanes.get(lane) if isinstance(lane, str) else None
    if not isinstance(row, dict) or row.get("up") is not True:
        return Assessment("lane-unavailable")
    if not isinstance(requested_model, str) or not requested_model or row.get("model") != requested_model:
        return Assessment("advertised-model-mismatch")
    # pc-llm lane.model is configured inventory, not proof of the served model.
    if evidence.model_verified is not True or evidence.served_model != requested_model:
        return Assessment("served-model-unverified-or-mismatch")
    for field in ("outstandingJobs", "macSlotsInUse"):
        if type(row.get(field)) is not int or row[field] != 0:
            return Assessment("pending-or-busy-or-unknown")
    if type(row.get("macSlotsUnsafe")) is not int or row["macSlotsUnsafe"] != 0:
        return Assessment("unsafe-or-unknown-local-slots")
    cap = row.get("slotsPerLane")
    slots = row.get("pcSlots")
    if (
        type(cap) is not int
        or cap < 1
        or not isinstance(slots, dict)
        or type(slots.get("busy")) is not int
        or slots["busy"] != 0
        or type(slots.get("total")) is not int
        or not 1 <= slots["total"] <= 64
    ):
        return Assessment("remote-idle-or-capacity-unverified")
    if (
        not isinstance(job_id, str)
        or not job_id
        or evidence.reservation_valid is not True
        or evidence.reservation_job != job_id
        or evidence.reservation_model != requested_model
        or not _number(evidence.reservation_expires_at)
        or evidence.reservation_expires_at <= now
    ):
        return Assessment("reservation-missing-invalid-or-expired")
    # Oldest supporting timestamp bounds the planner's freshness check too.
    return Assessment(
        "offline-candidate-only",
        WorkerEvidence(
            observed_at=evidence.observed_at - age,
            local_model_ready=True,
            authenticated=True,
            lease_available=True,
            busy=False,
        ),
    )
