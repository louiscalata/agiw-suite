"""Record full fleet projections from pinned, unmodified production bytes."""

import argparse
import hashlib
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

BASE = "5694d16e8d77e8a0dddd5cf597ac1a91f2db1784"
NOW = 1000.0
STAMP = "1970-01-01T00:16:40Z"
ENV = {key: value for key, value in os.environ.items() if not key.startswith("GIT_CONFIG_")}


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], env=ENV, check=True, capture_output=True, timeout=30).stdout


def machine(identity="mac", **values):
    return {
        "id": identity,
        "host": identity,
        "state": "up",
        "modelsComplete": True,
        "loadedCount": 1,
        "busyCount": 0,
        "observedAt": STAMP,
        **values,
    }


def lane(identity="lane", **values):
    return {
        "id": identity,
        "machine": "mac",
        "role": "llama",
        "health": "ok",
        "modelId": "fixture/model",
        "observedAt": STAMP,
        **values,
    }


def root(identity="remote", **values):
    return {"id": identity, "role": "machine", **values}


def _boundaries(cases, add, nodes, local):
    # Boundaries precede ordinary composites. Expected values are never constructed here.
    for label, value in (
        ("false", False),
        ("true", True),
        ("string", "1000"),
        ("list", []),
        ("dict", {}),
        ("nan", {"$float": "nan"}),
        ("positive_infinity", {"$float": "inf"}),
        ("negative_infinity", {"$float": "-inf"}),
        ("huge_integer", 10**1000),
    ):
        add("now_invalid_" + label, now=value)
    for label, value in (("negative", -1.0), ("zero", 0), ("integer", 1000), ("fraction", 1000.125)):
        add("now_valid_" + label, now=value)
    add("clock_used_for_none", now=None, clock=1001.25)
    add("clock_invalid", now=None, clock={"$float": "nan"})
    add("now_finite_beyond_timestamp_range", now=1e20)
    add("now_finite_negative_beyond_timestamp_range", now=-1e20)

    for parent in ("mac", "pc", "registry:pc", None):
        for age in (-2.001, -2.0, -1.999, 0.0, 2.999, 3.0, 3.001, 29.999, 30.0, 30.001):
            nodes(
                "lane_default_"
                + str(parent).replace(":", "_")
                + "_age_"
                + str(age).replace("-", "minus").replace(".", "_"),
                [lane(machine=parent)],
                now=NOW + age,
            )
    for window in (-1, 0, 1, 2, 3, 30, False, True, None, "3", {"$float": "nan"}, {"$float": "inf"}):
        label = (
            (type(window).__name__ + "_" + str(window))
            .replace(" ", "")
            .replace("{", "")
            .replace("}", "")
            .replace("'", "")
            .replace(":", "_")
            .replace("$", "")
        )
        nodes("fresh_window_" + label.replace("-", "minus"), [lane(freshForSeconds=window)], now=1003.0)
    for interval in (-1, 0, 1, 1.5, 2, False, True, None, "3", {"$float": "nan"}):
        label = (
            (type(interval).__name__ + "_" + str(interval))
            .replace(" ", "")
            .replace("{", "")
            .replace("}", "")
            .replace("'", "")
            .replace(":", "_")
            .replace("$", "")
        )
        nodes("probe_interval_" + label.replace("-", "minus"), [lane(probeIntervalSeconds=interval)], now=1003.0)
    for label, expiry in (
        ("before", "1970-01-01T00:16:39.999Z"),
        ("equal", STAMP),
        ("after", "1970-01-01T00:16:40.001Z"),
        ("invalid", "not-a-date"),
        ("none", None),
        ("naive", "1970-01-01T00:16:40"),
        ("odd_type", []),
    ):
        nodes("explicit_expiry_" + label, [lane(expiresAt=expiry)])
    for label, observed in (
        ("none", None),
        ("empty", ""),
        ("invalid", "broken"),
        ("naive", "1970-01-01T00:16:40"),
        ("date_only", "1970-01-01"),
        ("odd_type", []),
        ("numeric", 1000),
        ("offset", "1970-01-01T01:16:40+01:00"),
        ("min_year", "0001-01-01T00:00:00Z"),
        ("max_year", "9999-12-31T23:59:59+00:00"),
    ):
        nodes("observed_" + label, [lane(observedAt=observed)])


def _count_boundaries(cases, add, nodes, local):
    for count in (-1, 0, 1, False, True, None, "1", 0.0, 1.0, {"$float": "nan"}):
        label = (
            (type(count).__name__ + "_" + str(count))
            .replace(" ", "")
            .replace("{", "")
            .replace("}", "")
            .replace("'", "")
            .replace(":", "_")
            .replace("$", "")
        )
        for field in ("loadedCount", "busyCount"):
            local(field + "_" + label.replace("-", "minus"), machine(**{field: count}))
    for count in (-1, 0, 1, False, True, None, "1", 0.0, 1.0):
        local(
            "empty_models_loaded_" + (type(count).__name__ + "_" + str(count)).replace("-", "minus").replace(".", "_"),
            machine(loadedCount=count),
            models=[],
        )


def _normalization(cases, add, nodes, local):
    # Normalization, iterable oddities, and current escaping exception surfaces.
    for label, value in (("none", None), ("list", []), ("number", 7), ("string", "host"), ("false", False)):
        cases.append({"name": "snapshot_" + label, "snapshot": value, "inference": None, "now": NOW})
    for key in ("machines", "sources"):
        for label, value in (("none", None), ("empty", []), ("string", "abc"), ("mapping", {"x": {}}), ("integer", 7)):
            add(key + "_container_" + label, {key: value})
    for label, value in (("none", None), ("empty", []), ("string", "abc"), ("mapping", {"x": {}}), ("integer", 7)):
        add("nodes_container_" + label, inference={"nodes": value})
    nodes("filters_non_mapping_rows", [None, 7, False, "x", [], lane()], {"machines": [None, 8, "x", {}, machine()]})
    add("embedded_inference", {"inference": {"nodes": [lane()]}})
    add(
        "explicit_inference_overrides_embedded",
        {"inference": {"nodes": [lane("embedded")]}},
        {"nodes": [lane("explicit")]},
    )
    add("empty_explicit_inference_overrides_embedded", {"inference": {"nodes": [lane()]}}, {})
    add("non_mapping_inference_uses_embedded", {"inference": {"nodes": [lane()]}}, [])
    add("non_mapping_embedded_inference", {"inference": []})


def _identities(cases, add, nodes, local):
    # Root creation, local-ID fallbacks, report order, attribution and legacy joins.
    for host in ("pc", "windows", "mac", "remote", None):
        add("default_host_" + str(host), {"host": host})
    add("default_platform_windows", {"platform": "windows"})
    for marker in ({"trust": "local"}, {"role": "this-machine"}, {"source": "local"}):
        local("local_marker_" + next(iter(marker)), machine("custom", **marker))
    local("local_id_host_fallback", machine(None, host="other", trust="local"))
    local("local_id_default_fallback", machine("", host="", trust="local"))
    local("local_id_integer_stringified", machine(123, trust="local"))
    add(
        "multiple_local_rows_first_wins",
        {"machines": [machine("first", trust="local"), machine("last", trust="local")]},
    )
    nodes("duplicate_root_last_value_first_position", [root("z", label="first"), root("a"), root("z", label="last")])
    nodes("child_synthesizes_parent", [lane(machine="synthetic")])
    nodes("client_does_not_synthesize_parent", [lane(role="client", machine="absent")])
    nodes("non_string_parent_not_root", [lane(machine=123)])
    nodes("client_non_string_id_and_missing_id", [lane(role="client"), lane(id=None), lane(id=12), {"role": "llama"}])
    nodes("optional_not_configured_skipped", [lane(reason="not_configured")])
    nodes("duplicate_lane_first_wins", [lane(label="first"), lane(label="last", health="down", modelId=None)])
    nodes("empty_string_ids", [root(""), lane("", machine="")], {"machines": [machine("")]})
    add("report_invalid_ids_skipped", {"machines": [machine(None), machine(""), machine(123), machine(False)]})
    add("report_duplicate_first_wins", {"machines": [machine(label="first"), machine(label="last", loadedCount=7)]})
    add("report_host_fallback", {"machines": [machine(None, host="peer")]})
    add("report_label_host_fallback", {"machines": [machine("peer", label="", host="hostname")]})
    add("report_label_identity_fallback", {"machines": [machine("peer", label="", host="")]})


def _legacy(cases, add, nodes, local):
    for label, row in (
        ("report", machine("legacy", platform="windows")),
        ("placeholder", {"id": "pc", "platform": "windows"}),
        ("placeholder_windows", {"id": "windows", "platform": "windows"}),
        ("placeholder_observed", machine("pc", platform="windows")),
        ("placeholder_trusted", {"id": "pc", "platform": "windows", "trust": "trusted"}),
        ("placeholder_reason", {"id": "pc", "platform": "windows", "reasonCode": "timeout"}),
        ("placeholder_local", {"id": "pc", "platform": "windows", "trust": "local"}),
        ("registry", machine("legacy", platform="windows", observationSource="registry", label="configured")),
        ("nonwindows", machine("legacy", platform="mac")),
        ("exact_root", machine("remote", platform="windows")),
    ):
        nodes("legacy_" + label, [root(replaces="legacy-pc")], {"machines": [row]})
    nodes(
        "legacy_two_replacements",
        [root("a", replaces="legacy-pc"), root("b", replaces="legacy-pc")],
        {"machines": [machine("legacy", platform="windows")]},
    )
    nodes(
        "legacy_two_candidates",
        [root(replaces="legacy-pc")],
        {"machines": [machine("one", platform="windows"), machine("two", platform="windows")]},
    )
    nodes(
        "legacy_duplicate_candidate_identity",
        [root(replaces="legacy-pc")],
        {
            "machines": [
                machine("legacy", platform="windows", label="first"),
                machine("legacy", platform="windows", label="last"),
            ]
        },
    )
    nodes(
        "legacy_marked_candidate_preferred",
        [root(replaces="legacy-pc")],
        {"machines": [machine("one", platform="windows"), machine("two", platform="windows", legacy=True)]},
    )
    for marker in ({"legacy": True}, {"id": "legacy-pc"}, {"sourceId": "legacy-pc"}, {"source": "legacy-pc"}):
        nodes(
            "legacy_marker_" + next(iter(marker)),
            [root(replaces="legacy-pc")],
            {"machines": [machine("one", platform="windows"), machine("two", platform="windows", **marker)]},
        )


def _attribution(cases, add, nodes, local):
    for field in ("attributedMachineId", "registryMachineId", "machineId"):
        for target in ("remote", "unconfigured", ""):
            nodes(
                "attribution_" + field + "_" + (target or "empty"),
                [root(replaces="legacy-pc")],
                {"machines": [machine("legacy", platform="windows", **{field: target})]},
            )
    nodes(
        "attribution_conflict",
        [root(replaces="legacy-pc"), root("other")],
        {"machines": [machine("legacy", platform="windows", machineId="remote", registryMachineId="other")]},
    )
    nodes("nested_attribution", [root()], {"machines": [machine("peer", source={"machineId": "remote"})]})
    nodes(
        "two_attributed_peer_reports_order",
        [root()],
        {"machines": [machine("z", machineId="remote", label="first"), machine("a", machineId="remote", label="last")]},
    )
    for match in ({"node": "peer"}, {"host": "peer"}, {"id": "producer"}):
        nodes(
            "source_attribution_" + next(iter(match)),
            [root()],
            {"machines": [machine("peer", sourceId="producer")], "sources": [{**match, "machineId": "remote"}]},
        )
    nodes(
        "explicit_attribution_over_source",
        [root(), root("other")],
        {"machines": [machine("peer", machineId="remote")], "sources": [{"node": "peer", "machineId": "other"}]},
    )
    nodes(
        "source_conflicting_attribution_blocks_legacy",
        [root(replaces="legacy-pc"), root("other")],
        {
            "machines": [machine("peer", platform="windows")],
            "sources": [{"node": "peer", "machineId": "remote"}, {"node": "peer", "machineId": "other"}],
        },
    )
    nodes(
        "registry_label_precedes_report",
        [root(observationSource="registry", label="Root")],
        {"machines": [machine("remote", label="Report")]},
    )
    nodes("report_label_precedes_root", [root(label="Root")], {"machines": [machine("remote", label="Report")]})


def _lane_states(cases, add, nodes, local):
    # Lane and host state precedence, summary order, rounding, long and hostile text.
    for reason in (
        "refused",
        "timeout",
        "unavailable",
        "upstream_error",
        "tunnel_down",
        "stale",
        "load_state_unreported",
        "identity_unknown",
        "multiple_loaded",
        "model_unreported",
        "model_id_too_long",
        "models_unreadable",
        "model_not_loaded",
        "no_models",
        "novel",
        "",
        None,
    ):
        nodes("lane_reason_" + str(reason), [lane(reason=reason, modelId=None, health="unknown")])
    for health in ("ok", "degraded", "down", None, True):
        nodes("lane_health_" + str(health), [lane(health=health)])
    for label, model in (("empty", ""), ("none", None), ("number", 7), ("bool", True), ("list", [])):
        nodes("lane_model_" + label, [lane(modelId=model)])
    nodes("answer_precedes_failure_reason", [lane(reason="timeout")])
    nodes("freshness_precedes_failure_reason", [lane(reason="timeout")], now=1004.0)
    nodes("registry_lane_source", [lane(observationSource="registry")])
    for age in (3.499, 3.5, 3.501):
        nodes("stale_summary_round_" + str(age).replace(".", "_"), [lane()], now=NOW + age)
    nodes("stale_explicit_reason_with_zero_age", [lane(reason="stale", modelId=None)])
    nodes(
        "all_summary_states_in_order",
        [
            lane("unknown-z", health="unknown", modelId=None, reason="no_models"),
            lane("stale-b", observedAt="1970-01-01T00:16:35Z"),
            lane("unavailable-z", modelId=None, reason="timeout"),
            lane("unknown-a", health="unknown", modelId=None),
            lane("unavailable-a", modelId=None, reason="refused"),
        ],
    )
    nodes(
        "long_hostile_values_preserved",
        [
            lane(
                "id/" + "x" * 129,
                label="<script>\nzero\u0000 Ω " + "L" * 129,
                modelId="M" * 513,
                health="unknown",
                reason="identity_unknown",
                extra={"nested": [1, {"ok": False}]},
            ),
        ],
    )


def _host_states(cases, add, nodes, local):
    for state in ("up", "down", "unknown", None):
        local("machine_state_" + str(state), machine(state=state))
    for complete in (False, True, None, 0, 1, "true"):
        local("machine_complete_" + str(complete), machine(modelsComplete=complete))
    for reason in ("stale", "reported", "", None):
        local("machine_reason_" + str(reason), machine(reasonCode=reason))
    local("machine_inventory_identity_only", machine(inventoryScope="configured-lanes"))
    local("machine_registry_identity_only", machine(observationSource="registry"))
    local("machine_stale", machine(), sampledAt=NOW + 100)
    add("peer_machine_default_expiry_equal", {"machines": [machine("peer")]}, now=1030.0)
    add("peer_machine_default_expiry_after", {"machines": [machine("peer")]}, now=1030.001)
    add("local_machine_default_expiry_after", {"machines": [machine()]}, now=1003.001)
    local("machine_no_timestamp", machine(observedAt=None))
    local("machine_future_timestamp", machine(observedAt="1970-01-01T00:16:43Z"))
    local("machine_invalid_expiry", machine(expiresAt="1970-01-01T00:16:39Z"))


def _inventory(cases, add, nodes, local):
    for value in (False, None, True, 0):
        local("inventory_complete_" + str(value), inventoryComplete=value)
        local("activity_known_" + str(value), activityKnown=value)
    for label, models in (
        ("missing_local", [{"host": "elsewhere", "loaded": None}]),
        ("none", None),
        ("mapping", {}),
        ("tuple", {"$tuple": []}),
        ("empty", []),
        ("nonmapping", [None, 7, "x"]),
        ("true", [{"host": "mac", "loaded": True}]),
        ("false", [{"host": "mac", "loaded": False}]),
        ("unknown", [{"host": "mac", "loaded": None}]),
        ("integer", [{"host": "mac", "loaded": 1}]),
        ("incomplete", [{"host": "mac", "loaded": True, "inventoryComplete": False}]),
        ("integer_inventory", [{"host": "mac", "loaded": True, "inventoryComplete": 0}]),
        ("aliases", [{"host": "mac", "loaded": True}, {"host": "mac", "loaded": True}]),
    ):
        local("local_models_" + label, models=models)
    for platform, alias in (
        ("mac", "mac"),
        ("macos", "mac"),
        ("windows", "pc"),
        ("windows", "windows"),
        ("other", "alien"),
    ):
        local(
            "local_alias_" + platform + "_" + alias,
            machine("custom", platform=platform, trust="local"),
            models=[{"host": alias, "loaded": True}],
        )


def _registry(cases, add, nodes, local):
    # Registry precedence and complete/partial aggregate combinations.
    for status in ("configured", "invalid", "unknown", None):
        for label, age in (("fresh", 0.0), ("expiry_equal", 3.0), ("stale", 3.001), ("skew", -2.001)):
            add(
                "registry_" + str(status) + "_" + label,
                inference={"nodes": [lane()], "remoteMachines": {"state": status}, "observedAt": STAMP},
                now=NOW + age,
            )
    for label, value in (("missing", None), ("list", []), ("false", False), ("string", "configured"), ("empty", {})):
        add("registry_container_" + label, inference={"remoteMachines": value})
    add("registry_configured_missing_timestamp", inference={"remoteMachines": {"state": "configured"}})
    add("registry_invalid_no_lanes", inference={"remoteMachines": {"state": "invalid"}, "observedAt": STAMP})
    add(
        "registry_partial_no_answers",
        inference={
            "remoteMachines": {"state": "invalid"},
            "observedAt": STAMP,
            "nodes": [lane(health="unknown", modelId=None, reason="timeout")],
        },
    )
    add(
        "complete_two_machine_totals",
        {"machines": [machine(loadedCount=2, busyCount=1), machine("peer", loadedCount=3, busyCount=2)]},
        {"nodes": [lane(), lane("peer-lane", machine="peer")]},
    )
    add(
        "complete_totals_registry_partial",
        {"machines": [machine()]},
        {"remoteMachines": {"state": "invalid"}, "observedAt": STAMP, "nodes": [lane()]},
    )
    add(
        "complete_totals_registry_complete",
        {"machines": [machine()]},
        {"remoteMachines": {"state": "configured"}, "observedAt": STAMP, "nodes": [lane()]},
    )


def _hostile(cases, add, nodes, local):
    # Odd/unhashable values: preserve current failures rather than silently sanitize.
    nodes("unhashable_parent_lane", [lane(machine=[])])
    nodes("unhashable_reason", [lane(reason=["timeout"], modelId=None)])
    add("unhashable_report_id_metadata", {"machines": [machine(["peer"])]})
    nodes("unhashable_windows_id", [root(replaces="legacy-pc")], {"machines": [machine(["peer"], platform="windows")]})
    add("unhashable_local_host", {"host": [], "machines": [machine(trust="local")]})
    add("unhashable_model_host", {"machines": [machine()], "models": [{"host": [], "loaded": True}]})
    nodes("unhashable_source_identity", [root()], {"machines": [machine("peer", source={"machineId": []})]})


def _survivor_boundaries(cases, add, nodes, local):
    nodes(
        "rooted_windows_report_not_eligible_with_other_peer",
        [root(replaces="legacy-pc")],
        {
            "machines": [
                machine("remote", host="elsewhere", platform="windows"),
                machine("eligible", platform="windows"),
            ]
        },
    )
    for alias in ("mac", "pc", "windows"):
        local(
            "foreign_platform_rejects_alias_" + alias,
            machine("custom", platform="other", trust="local"),
            models=[{"host": alias, "loaded": True}],
        )
    nodes(
        "windows_id_uses_host",
        [root(replaces="legacy-pc")],
        {"machines": [machine(None, host="host-only", platform="windows")]},
    )
    nodes("machine_role_parent_does_not_synthesize_root", [root("declared", machine="unused-parent")])
    nodes(
        "conflicting_attribution_not_eligible_with_other_peer",
        [root(replaces="legacy-pc"), root("other")],
        {
            "machines": [
                machine("ambiguous", platform="windows", machineId="remote", registryMachineId="other"),
                machine("eligible", platform="windows"),
            ]
        },
    )
    add(
        "multiple_local_host_fallback_first_wins",
        {
            "machines": [
                machine(None, host="first-host", trust="local"),
                machine(None, host="second-host", trust="local"),
            ]
        },
    )
    nodes("missing_timestamp_preserves_reason", [lane(observedAt=None, reason="producer_unknown")])
    nodes(
        "sources_non_mapping_entries_filtered",
        [root()],
        {"machines": [machine("peer")], "sources": [None, 3, "x", [], {"node": "peer", "machineId": "remote"}]},
    )
    for field in ("loadedCount", "busyCount"):
        local("missing_" + field, {key: value for key, value in machine().items() if key != field})
        local("huge_" + field, machine(**{field: 10**1000}))
    add("mixed_valid_and_invalid_counts", {"machines": [machine(), machine("peer", busyCount=-1)]})
    nodes(
        "legacy_local_placeholder_windows_host",
        [root(replaces="legacy-pc")],
        {"host": "windows", "machines": [{"id": "pc", "platform": "windows", "trust": "local"}]},
    )
    nodes("tuples_of_nodes_and_machines", {"$tuple": [lane()]}, {"machines": {"$tuple": [machine()]}})


def _mutation_boundaries(cases, add, nodes, local):
    """Concrete boundary inputs covering attribution, clocks and host evidence."""
    inputs = {
        "lane_expiry_without_observed": {
            "snapshot": {},
            "inference": {"nodes": [{"id": "l", "expiresAt": "1970-01-01T00:20:00Z"}]},
            "now": 1000.0,
        },
        "source_node_matches_id_not_host": {
            "snapshot": {
                "machines": [{"id": "mac", "trust": "local"}, {"id": "x", "host": "h"}],
                "sources": [{"node": "x", "machineId": "remote"}],
            },
            "inference": {"nodes": [{"id": "remote", "role": "machine"}]},
            "now": 1000.0,
        },
        "source_node_matches_host_no_id": {
            "snapshot": {
                "machines": [{"id": "mac", "trust": "local"}, {"host": "h"}],
                "sources": [{"node": "h", "machineId": "remote"}],
            },
            "inference": {"nodes": [{"id": "remote", "role": "machine"}]},
            "now": 1000.0,
        },
        "answering_and_unknown_lane": {
            "snapshot": {},
            "inference": {
                "nodes": [
                    {"id": "a", "health": "ok", "modelId": "m", "observedAt": "1970-01-01T00:16:39Z"},
                    {"id": "b", "observedAt": "1970-01-01T00:16:39Z"},
                ]
            },
            "now": 1000.0,
        },
        "pc_placeholder_without_replacement": {
            "snapshot": {"machines": [{"id": "mac", "trust": "local"}, {"id": "pc", "platform": "windows"}]},
            "inference": None,
            "now": 1000.0,
        },
        "pc_placeholder_not_windows": {
            "snapshot": {"machines": [{"id": "mac", "trust": "local"}, {"id": "pc", "platform": "linux"}]},
            "inference": {"nodes": [{"id": "remote", "role": "machine", "replaces": "legacy-pc"}]},
            "now": 1000.0,
        },
        "pc_placeholder_is_local": {
            "snapshot": {"machines": [{"id": "pc", "role": "this-machine", "platform": "windows"}]},
            "inference": {"nodes": [{"id": "remote", "role": "machine", "replaces": "legacy-pc"}]},
            "now": 1000.0,
        },
        "unobserved_windows_other_id": {
            "snapshot": {"machines": [{"id": "mac", "trust": "local"}, {"id": "winbox", "platform": "windows"}]},
            "inference": {"nodes": [{"id": "remote", "role": "machine", "replaces": "legacy-pc"}]},
            "now": 1000.0,
        },
        "peer_counts_with_incomplete_inventory": {
            "snapshot": {
                "machines": [
                    {"id": "mac", "trust": "local"},
                    {
                        "id": "remote",
                        "state": "up",
                        "observedAt": "1970-01-01T00:16:39Z",
                        "loadedCount": 2,
                        "busyCount": 1,
                        "modelsComplete": True,
                    },
                ],
                "inventoryComplete": False,
                "activityKnown": False,
            },
            "inference": None,
            "now": 1000.0,
        },
        "machine_node_integer_id": {
            "snapshot": {},
            "inference": {"nodes": [{"id": 5, "role": "machine"}]},
            "now": 1000.0,
        },
        "same_id_unattributed_then_unknown_attribution": {
            "snapshot": {
                "machines": [
                    {"id": "mac", "trust": "local"},
                    {"id": "w", "platform": "windows"},
                    {"id": "w", "machineId": "nope", "label": "second"},
                ]
            },
            "inference": {"nodes": [{"id": "remote", "role": "machine", "replaces": "legacy-pc"}]},
            "now": 1000.0,
        },
        "same_id_unknown_attribution_then_unattributed": {
            "snapshot": {
                "machines": [
                    {"id": "mac", "trust": "local"},
                    {"id": "w", "machineId": "nope"},
                    {"id": "w", "platform": "windows", "label": "second"},
                ]
            },
            "inference": {"nodes": [{"id": "remote", "role": "machine", "replaces": "legacy-pc"}]},
            "now": 1000.0,
        },
        "source_host_differs_from_row_host": {
            "snapshot": {
                "machines": [{"id": "mac", "trust": "local"}, {"id": "w", "host": "h1"}],
                "sources": [{"host": "h2", "machineId": "remote"}],
            },
            "inference": {"nodes": [{"id": "remote", "role": "machine"}]},
            "now": 1000.0,
        },
        "source_and_row_without_host_or_id": {
            "snapshot": {
                "machines": [{"id": "mac", "trust": "local"}, {"id": "w"}],
                "sources": [{"machineId": "remote"}],
            },
            "inference": {"nodes": [{"id": "remote", "role": "machine"}]},
            "now": 1000.0,
        },
        "source_id_differs_from_row_source_id": {
            "snapshot": {
                "machines": [{"id": "mac", "trust": "local"}, {"id": "w", "sourceId": "s1"}],
                "sources": [{"id": "s2", "machineId": "remote"}],
            },
            "inference": {"nodes": [{"id": "remote", "role": "machine"}]},
            "now": 1000.0,
        },
        "peer_label_and_host": {
            "snapshot": {"machines": [{"id": "mac", "trust": "local"}, {"id": "x", "label": "Label", "host": "h"}]},
            "inference": None,
            "now": 1000.0,
        },
        "configured_host_only_windows_row_plus_candidate": {
            "snapshot": {
                "machines": [
                    {"id": "mac", "trust": "local"},
                    {"host": "h", "platform": "windows"},
                    {"id": "w", "platform": "windows"},
                ]
            },
            "inference": {
                "nodes": [{"id": "h", "role": "machine"}, {"id": "remote", "role": "machine", "replaces": "legacy-pc"}]
            },
            "now": 1000.0,
        },
        "local_row_host_alias_models": {
            "snapshot": {
                "machines": [
                    {
                        "id": "mac",
                        "host": "studio",
                        "trust": "local",
                        "state": "up",
                        "observedAt": "1970-01-01T00:16:39Z",
                        "loadedCount": 0,
                    }
                ],
                "models": [{"host": "studio", "loaded": "yes"}],
            },
            "inference": None,
            "now": 1000.0,
        },
        "unavailable_lane_with_input_endpoint_answered": {
            "snapshot": {},
            "inference": {
                "nodes": [
                    {"id": "l", "reason": "refused", "observedAt": "1970-01-01T00:16:39Z", "endpointAnswered": True}
                ]
            },
            "now": 1000.0,
        },
        "reported_host_with_reason_code": {
            "snapshot": {
                "machines": [
                    {
                        "id": "mac",
                        "trust": "local",
                        "state": "up",
                        "observedAt": "1970-01-01T00:16:39Z",
                        "reasonCode": "degraded",
                    }
                ]
            },
            "inference": None,
            "now": 1000.0,
        },
        "unmeasured_host_with_reason_code": {
            "snapshot": {
                "machines": [
                    {
                        "id": "mac",
                        "trust": "local",
                        "state": "odd",
                        "observedAt": "1970-01-01T00:16:39Z",
                        "reasonCode": "probe_failed",
                    }
                ]
            },
            "inference": None,
            "now": 1000.0,
        },
    }
    for name, values in inputs.items():
        add(name, **values)


def inputs():
    cases = []

    def add(name, snapshot=None, inference=None, now=NOW, **values):
        cases.append(
            {
                "name": name,
                "snapshot": {} if snapshot is None else snapshot,
                "inference": inference,
                "now": now,
                **values,
            }
        )

    def nodes(name, items, snapshot=None, **values):
        add(name, snapshot, {"nodes": items}, **values)

    def local(name, row=None, **values):
        add(name, {"machines": [machine() if row is None else row], **values})

    _boundaries(cases, add, nodes, local)
    _count_boundaries(cases, add, nodes, local)
    _normalization(cases, add, nodes, local)
    _identities(cases, add, nodes, local)
    _legacy(cases, add, nodes, local)
    _attribution(cases, add, nodes, local)
    _lane_states(cases, add, nodes, local)
    _host_states(cases, add, nodes, local)
    _inventory(cases, add, nodes, local)
    _registry(cases, add, nodes, local)
    _hostile(cases, add, nodes, local)
    _survivor_boundaries(cases, add, nodes, local)
    _mutation_boundaries(cases, add, nodes, local)
    names = [case["name"] for case in cases]
    if len(names) != len(set(names)):
        raise ValueError(
            "Duplicate input case names: " + str(sorted({name for name in names if names.count(name) > 1}))
        )
    return cases


def baseline(repo, folder):
    hashes = {}
    for name in ("fleet_state.py", "machine_state.py"):
        raw = git(repo, "show", BASE + ":" + name)
        hashes[name] = hashlib.sha256(raw).hexdigest()
        (folder / name).write_bytes(raw)
    sys.path.insert(0, str(folder))
    dependency = importlib.import_module("machine_state")
    module = types.ModuleType("golden_baseline")
    module.__file__ = str(folder / "fleet_state.py")
    exec(compile((folder / "fleet_state.py").read_bytes(), module.__file__, "exec"), module.__dict__)
    if Path(dependency.__file__).parent != folder:
        raise ValueError("Baseline dependency imported from wrong directory")
    return module, hashes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="fleet-golden-baseline-") as temp:
        module, hashes = baseline(args.repo, Path(temp))
        sys.path.insert(0, str(args.repo))
        adapter = importlib.import_module("test_fleet_state_golden")
        cases = inputs()
        for case in cases:
            case["expected"] = adapter._run_case(module, case)
    payload = {
        "schemaVersion": 1,
        "sourceRevision": BASE,
        "sourceSha256": hashes,
        "description": "Recorded unmodified fleet projection, escaping errors, helper call order and input ownership.",
        "cases": cases,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=True, allow_nan=False) + "\n", encoding="utf-8")
    sys.stdout.write(json.dumps({"cases": len(cases), "sourceRevision": BASE, "sourceSha256": hashes}) + "\n")


if __name__ == "__main__":
    main()
