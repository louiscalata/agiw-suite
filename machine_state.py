"""Pure machine observations: a missing read is unknown, never evidence of downtime."""
from __future__ import annotations

import datetime as dt
import math


FRESH_SECONDS = {"mac": 3.0, "windows": 30.0, "linux": 30.0}
TRUST_LEVELS = {"local", "authenticated", "share-reported", "unverified"}
UNKNOWN_REASONS = frozenset({
    "not_observed", "unavailable", "timeout", "refused", "non_json", "oversize",
    "schema_invalid", "stale", "clock_skew", "host_key_mismatch", "share_unmounted",
    "asleep", "untrusted", "unverified", "not_connected", "identity_mismatch", "read_error",
})


def finite_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def platform_name(value):
    return "mac" if value in ("mac", "macos") else value if isinstance(value, str) and value in FRESH_SECONDS else None


def observation_age(report, now):
    """Use the original timestamp when supplied; relays cannot refresh an observation."""
    stamp = report.get("observedAt")
    if stamp is not None:
        try:
            observed = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if observed.tzinfo is None:
                return None
            age = now - observed.timestamp()
        except (AttributeError, TypeError, ValueError, OverflowError):
            return None
    else:
        age = report.get("ageSeconds")
    return age if finite_number(age) else None


def machine_summary(host, platform, report=None, *, now, trust="unverified", reason=None):
    """A validated feed's state and counts. Only positive, attributed evidence permits down."""
    platform = platform_name(platform)
    trust = trust if isinstance(trust, str) and trust in TRUST_LEVELS else "unverified"
    row = {"id": host, "host": host, "platform": platform, "state": "unknown",
           "reasonCode": reason if reason in UNKNOWN_REASONS else "not_observed",
           "trust": trust, "observedAt": None, "ageSeconds": None,
           "freshForSeconds": FRESH_SECONDS.get(platform), "modelsComplete": False,
           "loadedCount": None, "busyCount": None, "promptTokensPerSecond": None,
           "generationTokensPerSecond": None, "handsOff": None}
    if not isinstance(report, dict):
        return row
    age = observation_age(report, now)
    row.update(observedAt=report.get("observedAt"), ageSeconds=max(0.0, age) if age is not None else None)
    if reason is not None:
        row["reasonCode"] = reason if reason in UNKNOWN_REASONS else "unavailable"
    elif platform is None or age is None or not finite_number(now):
        row["reasonCode"] = "schema_invalid"
    elif age < -2:
        row["reasonCode"] = "clock_skew"
    elif age > FRESH_SECONDS[platform]:
        row["reasonCode"] = "stale"
    elif trust == "unverified" or (platform != "windows" and trust == "share-reported"):
        row["reasonCode"] = "untrusted"
    elif report.get("health") not in ("ok", "degraded", "down"):
        row["reasonCode"] = "schema_invalid"
    else:
        health = report["health"]
        row.update(state="down" if health == "down" else "up",
                   reasonCode="reported_down" if health == "down" else
                   "reported_degraded" if health == "degraded" else "observed")
        if row["state"] == "up":
            for key in ("promptTokensPerSecond", "generationTokensPerSecond"):
                value = report.get(key)
                if finite_number(value) and value >= 0:
                    row[key] = value
        if type(report.get("handsOff")) is bool:
            row["handsOff"] = report["handsOff"]
        models = report.get("models")
        complete = (report.get("modelsComplete") is True and isinstance(models, list)
                    and all(isinstance(model, dict) and model.get("loadedState") in ("loaded", "inactive")
                            for model in models))
        if complete and row["state"] == "up":
            row["modelsComplete"] = True
            loaded = [model for model in models if model["loadedState"] == "loaded"]
            row["loadedCount"] = len(loaded)
            if all(model.get("activity") in ("idle", "busy", "generating") for model in loaded):
                row["busyCount"] = sum(model["activity"] in ("busy", "generating") for model in loaded)
    return row


def _local_model_counts(models, cli_live):
    # The API keeps an inventory row while the CLI adds rows for its instance aliases.
    # Count that model once, and require coverage of every instance before claiming idle.
    groups = {}
    for index, model in enumerate(models):
        key = model.get("modelKey") or model.get("id") or index
        groups.setdefault(key, []).append(model)
    result = []
    for rows in groups.values():
        loaded = [row for row in rows if row.get("loaded") is True]
        state = "loaded" if loaded else "inactive" if all(row.get("loaded") is False for row in rows) else "unknown"
        cli = [row for row in loaded if cli_live and row.get("source") == "lms-ps"]
        activity = "unknown"
        if any(row.get("state") in ("busy", "generating") for row in cli):
            activity = "busy"
        elif cli and all(row.get("state") == "idle" for row in cli):
            instances = {row.get("instanceId") or row.get("id") for row in cli}
            covered = all((row.get("source") == "lms-ps" and row.get("loadedInstanceIds") is None) or
                          (isinstance(row.get("loadedInstanceIds"), list)
                           and bool(row["loadedInstanceIds"])
                           and set(row["loadedInstanceIds"]) <= instances) for row in loaded)
            if covered:
                activity = "idle"
        result.append({"loadedState": state, "activity": activity})
    return result


def local_summary(snapshot):
    now = snapshot["sampledAt"]
    sources = {source.get("id"): source for source in snapshot.get("sources", [])}
    live = [sources[key] for key in ("lmstudio-api", "lms-ps")
            if sources.get(key, {}).get("state") == "live"]
    if not live:
        reason = sources.get("lmstudio-api", {}).get("reasonCode", "unavailable")
        return machine_summary("mac", "mac", now=now, trust="local", reason=reason)
    models = [model for model in snapshot.get("models", []) if model.get("host") == "mac"]
    models.extend(model for model in snapshot.get("localInstanceObservations", [])
                  if model.get("host") == "mac")
    ages = [source.get("ageSeconds", 0.0) for source in live]
    ages.extend(model.get("ageSeconds") for model in models)
    age = max(ages) if all(finite_number(age) and age >= 0 for age in ages) else None
    complete = all(source.get("modelsComplete", True) is True for key, source in sources.items()
                   if key in ("lmstudio-api", "lms-ps"))
    for model in models:
        metadata = model.get("metadata")
        # A capped or invalid instance list cannot correlate every CLI alias.
        # Metadata survives a same-ID CLI merge even when the API row is replaced.
        if (isinstance(metadata, dict) and isinstance(metadata.get("loadedInstances"), list)
                and metadata.get("loadedInstanceCount") != len(metadata["loadedInstances"])):
            complete = False
    report = {"health": "ok", "ageSeconds": age, "modelsComplete": complete,
              "models": _local_model_counts(models, sources.get("lms-ps", {}).get("state") == "live")}
    if finite_number(age):
        report["observedAt"] = dt.datetime.fromtimestamp(now - age, dt.timezone.utc).isoformat()
    return machine_summary("mac", "mac", report, now=now, trust="local")


# The Mac row always comes from the local summary. The Windows PC is the other
# expected machine. Linux is retired: keep a row only when a feed actually names
# one, and never invent an unknown Linux machine that withholds the status.
EXPECTED_PEER_PLATFORMS = ("windows",)


def machines_summary(snapshot, peers):
    """Keep each host distinct and retain an unknown row for each missing expected peer."""
    rows = [local_summary(snapshot)]
    seen = {("mac", "mac")}
    for entry in peers:
        feed, source = entry.get("feed"), entry.get("source", {})
        platform = platform_name(source.get("platform"))
        host = source.get("host")
        identity = source.get("node") or host
        if not platform or not isinstance(host, str) or not host or (platform, identity) in seen:
            continue
        seen.add((platform, identity))
        reason = source.get("reasonCode") if source.get("state") not in ("live", "recorded") else None
        report = feed if feed is not None else {"ageSeconds": source.get("ageSeconds"),
                                               "observedAt": source.get("observedAt")}
        row = machine_summary(host, platform, report, now=snapshot["sampledAt"],
                              trust=source.get("trust", "unverified"), reason=reason)
        row["id"] = identity
        for key in ("label", "reportedHost", "diagnostics", "diagnosticsTruncated"):
            if key in source:
                row[key] = source[key]
        rows.append(row)
    for platform in EXPECTED_PEER_PLATFORMS:
        if not any(row["platform"] == platform for row in rows):
            rows.append(machine_summary(platform, platform, now=snapshot["sampledAt"]))
    return rows


def expire_machines(rows, now):
    """Expire aged overlays without inventing a fresh observation or replacing its named failure."""
    for row in rows:
        age = observation_age(row, now)
        limit = FRESH_SECONDS.get(platform_name(row.get("platform")))
        reason = ("schema_invalid" if age is None or limit is None else "clock_skew" if age < -2
                  else "stale" if age > limit else None)
        if row.get("state") != "unknown" and reason:
            row.update(state="unknown", reasonCode=reason, modelsComplete=False, loadedCount=None,
                       busyCount=None, promptTokensPerSecond=None, generationTokensPerSecond=None,
                       handsOff=None)
        row["ageSeconds"] = max(0.0, age) if age is not None else None
    return rows
