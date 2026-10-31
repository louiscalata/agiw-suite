import copy
import datetime as dt
import unittest

from machine_state import expire_machines, finite_number, machine_summary, machines_summary


NOW = 1800000000.0


def report(**extra):
    return {"health": "ok", "ageSeconds": 0, "modelsComplete": True,
            "models": [{"id": "m", "loadedState": "loaded", "activity": "busy"}], **extra}


def local(**extra):
    return {"sampledAt": NOW, "models": [], "sources": [
        {"id": "lms-ps", "state": "live", "ageSeconds": 0}], **extra}


class MachineStateTests(unittest.TestCase):
    def test_c2_fault_table_never_claims_down_or_zero(self):
        for reason in ("timeout", "refused", "non_json", "oversize", "schema_invalid", "stale",
                       "host_key_mismatch", "share_unmounted", "asleep", "clock_skew"):
            for platform in ("mac", "windows", "linux"):
                with self.subTest(reason=reason, platform=platform):
                    row = machine_summary(platform, platform, report(health="down"), now=NOW,
                                          trust="authenticated", reason=reason)
                    self.assertEqual((row["state"], row["reasonCode"]), ("unknown", reason))
                    for key in ("busyCount", "loadedCount", "generationTokensPerSecond", "promptTokensPerSecond"):
                        self.assertIsNone(row[key])

    def test_up_report_keeps_measured_rates_and_drops_unusable_ones(self):
        row = machine_summary("mac", "mac", report(promptTokensPerSecond=1.5, generationTokensPerSecond=2),
                              now=NOW, trust="local")
        self.assertEqual((row["promptTokensPerSecond"], row["generationTokensPerSecond"]), (1.5, 2.0))
        for bad in (float("nan"), -1, True, "2"):
            cleaned = machine_summary("mac", "mac", report(generationTokensPerSecond=bad), now=NOW, trust="local")
            self.assertIsNone(cleaned["generationTokensPerSecond"])
        down = machine_summary("mac", "mac", report(health="down", generationTokensPerSecond=9),
                               now=NOW, trust="local")
        self.assertIsNone(down["generationTokensPerSecond"])

    def test_fresh_reports_keep_trust_and_positive_down_evidence(self):
        for trust in ("local", "authenticated", "share-reported"):
            for health, state in (("ok", "up"), ("degraded", "up"), ("down", "down")):
                with self.subTest(trust=trust, health=health):
                    row = machine_summary("pc-one", "windows", report(health=health), now=NOW, trust=trust)
                    self.assertEqual((row["state"], row["trust"]), (state, trust))

    def test_unverified_report_cannot_claim_up_or_down(self):
        for health in ("ok", "down"):
            row = machine_summary("pc", "windows", report(health=health), now=NOW)
            self.assertEqual((row["state"], row["reasonCode"]), ("unknown", "untrusted"))

    def test_linux_share_cannot_supply_health_even_if_marked_reported(self):
        for health in ("ok", "down"):
            row = machine_summary("linux-one", "linux", report(health=health), now=NOW, trust="share-reported")
            self.assertEqual((row["state"], row["reasonCode"]), ("unknown", "untrusted"))
            self.assertIsNone(row["loadedCount"])

    def test_mac_share_cannot_supply_health_even_if_marked_reported(self):
        for platform in ("mac", "macos"):
            for health in ("ok", "degraded", "down"):
                with self.subTest(platform=platform, health=health):
                    row = machine_summary("other-mac", platform, report(health=health),
                                          now=NOW, trust="share-reported")
                    self.assertEqual((row["state"], row["reasonCode"]), ("unknown", "untrusted"))
                    self.assertIsNone(row["loadedCount"])
                    self.assertIsNone(row["busyCount"])
                    self.assertFalse(row["modelsComplete"])

    def test_paired_machine_identity_is_stable_across_share_fallback(self):
        node = "11111111-1111-4111-8111-111111111111"
        for state, trust in (("live", "authenticated"), ("recorded", "share-reported")):
            peer = {"feed": report(), "source": {"host": "pc-one", "node": node,
                    "platform": "windows", "state": state, "trust": trust}}
            row = machines_summary(local(), [peer])[1]
            self.assertEqual((row["id"], row["host"], row["state"]), (node, "pc-one", "up"))

    def test_paired_labels_and_share_diagnostics_never_replace_machine_identity(self):
        nodes = ["11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"]
        diagnostic = {"host": "pc-b", "correlation": "unverified", "reportedHealth": "down"}
        peers = [{"feed": None, "source": {"host": node, "node": node, "platform": "windows",
                  "state": "unavailable", "reasonCode": "not_connected", "trust": "unverified",
                  "label": "pc-b (reported)", "reportedHost": "pc-b", "diagnostics": [diagnostic],
                  "diagnosticsTruncated": True}} for node in nodes]
        pcs = [row for row in machines_summary(local(), peers) if row["platform"] == "windows"]
        self.assertEqual([row["id"] for row in pcs], nodes)
        self.assertEqual([row["host"] for row in pcs], nodes)
        for row in pcs:
            self.assertEqual(row["label"], "pc-b (reported)")
            self.assertEqual(row["reportedHost"], "pc-b")
            self.assertEqual(row["diagnostics"], [diagnostic])
            self.assertIs(row["diagnosticsTruncated"], True)
            self.assertEqual(row["state"], "unknown")
            self.assertIsNone(row["busyCount"])

    def test_freshness_boundaries_are_per_platform_and_unrounded(self):
        for platform, limit in (("mac", 3), ("windows", 30), ("linux", 30)):
            for age, state in ((limit, "up"), (limit + .001, "unknown"), (-2, "up"), (-2.001, "unknown")):
                with self.subTest(platform=platform, age=age):
                    row = machine_summary(platform, platform, report(ageSeconds=age), now=NOW, trust="authenticated")
                    self.assertEqual(row["state"], state)

    def test_original_timestamp_wins_over_relay_age(self):
        observed = dt.datetime.fromtimestamp(NOW - 31, dt.timezone.utc).isoformat()
        row = machine_summary("linux", "linux", report(observedAt=observed), now=NOW, trust="authenticated")
        self.assertEqual((row["state"], row["reasonCode"], row["ageSeconds"]), ("unknown", "stale", 31))

    def test_bad_ages_and_naive_timestamps_are_unknown(self):
        for value in (True, float('nan'), float('inf'), "0", None, 10 ** 1000):
            row = machine_summary("linux", "linux", report(ageSeconds=value), now=NOW, trust="authenticated")
            self.assertEqual(row["reasonCode"], "schema_invalid")
        row = machine_summary("linux", "linux", report(observedAt="2026-10-17T01:00:00"), now=NOW, trust="authenticated")
        self.assertEqual(row["reasonCode"], "schema_invalid")
        self.assertFalse(finite_number(True))

    def test_only_complete_known_inventory_gets_counts(self):
        for data in (report(modelsComplete=False), report(models=[{"loadedState": "unknown"}]), report(models=None)):
            row = machine_summary("pc", "windows", data, now=NOW, trust="share-reported")
            self.assertEqual(row["state"], "up")
            self.assertIsNone(row["loadedCount"])
            self.assertIsNone(row["busyCount"])
        row = machine_summary("pc", "windows", report(models=[]), now=NOW, trust="share-reported")
        self.assertEqual((row["loadedCount"], row["busyCount"]), (0, 0))

    def test_unknown_activity_does_not_erase_known_loaded_count(self):
        row = machine_summary("pc", "windows", report(models=[{"loadedState": "loaded", "activity": "unknown"}]),
                              now=NOW, trust="share-reported")
        self.assertEqual(row["loadedCount"], 1)
        self.assertIsNone(row["busyCount"])

    def test_missing_windows_gets_an_unknown_row_and_retired_linux_does_not(self):
        rows = machines_summary(local(), [])
        self.assertEqual([row["platform"] for row in rows], ["mac", "windows"])
        self.assertEqual([row["state"] for row in rows], ["up", "unknown"])
        self.assertEqual(rows[0]["busyCount"], 0)
        self.assertIsNone(rows[1]["busyCount"])
        self.assertNotIn("linux", [row["platform"] for row in rows])

    def test_a_named_linux_peer_stays_optional(self):
        peers = [{"feed": None, "source": {"host": "linux-one", "platform": "linux", "state": "unavailable",
                                            "reasonCode": "stale", "ageSeconds": 60}}]
        rows = machines_summary(local(), peers)
        self.assertEqual([row["host"] for row in rows], ["mac", "linux-one", "windows"])
        linux = next(row for row in rows if row["host"] == "linux-one")
        self.assertEqual(linux["reasonCode"], "stale")
        self.assertEqual(next(row for row in rows if row["platform"] == "windows")["reasonCode"], "not_observed")

    def test_multiple_hosts_keep_attribution_and_failed_age(self):
        peers = [{"feed": report(), "source": {"host": host, "platform": "windows", "state": "recorded",
                                               "trust": "share-reported"}} for host in ("pc-one", "pc-two")]
        peers.append({"feed": None, "source": {"host": "linux-one", "platform": "linux", "state": "unavailable",
                                                "reasonCode": "stale", "ageSeconds": 60}})
        rows = machines_summary(local(), peers)
        self.assertEqual([row["host"] for row in rows], ["mac", "pc-one", "pc-two", "linux-one"])
        self.assertEqual(rows[-1]["ageSeconds"], 60)
        self.assertEqual(rows[-1]["reasonCode"], "stale")

    def test_local_missing_and_incomplete_sources_do_not_imply_zero(self):
        row = machines_summary(local(sources=[]), [])[0]
        self.assertEqual(row["state"], "unknown")
        self.assertIsNone(row["busyCount"])
        row = machines_summary(local(sources=[{"id": "lms-ps", "state": "live", "modelsComplete": False}]), [])[0]
        self.assertIsNone(row["loadedCount"])

    def test_local_inventory_without_activity_is_unknown_activity(self):
        row = machines_summary(local(models=[{"host": "mac", "loaded": True, "state": "idle",
                                               "source": "lmstudio-api", "ageSeconds": 0}],
                                     sources=[{"id": "lmstudio-api", "state": "live"}]), [])[0]
        self.assertEqual(row["loadedCount"], 1)
        self.assertIsNone(row["busyCount"])

    def test_local_api_model_and_cli_alias_count_once(self):
        from telemetry import _merge_cli_rows, _parse_api, _parse_lms
        api = _parse_api({"models": [{"key": "provider/model", "loaded_instances": [
            {"identifier": "alias-1"}]}]}, NOW)[0]
        cli = _parse_lms({"models": [{"identifier": "alias-1", "modelKey": "provider/model",
                                      "status": "idle"}]}, NOW)
        models = {api["id"]: api}
        _merge_cli_rows(models, cli)
        row = machines_summary(local(models=list(models.values()), sources=[
            {"id": "lmstudio-api", "state": "live"}, {"id": "lms-ps", "state": "live"}]), [])[0]
        self.assertEqual((row["loadedCount"], row["busyCount"]), (1, 0))

    def test_local_multiple_instances_keep_one_model_and_unknown_unseen_activity(self):
        from telemetry import _merge_cli_rows, _parse_api, _parse_lms
        for statuses, expected in ((("idle", "generating"), 1), (("idle", "idle"), 0), (("idle",), None)):
            with self.subTest(statuses=statuses):
                api = _parse_api({"models": [{"key": "provider/model", "loaded_instances": [
                    {"identifier": "alias-1"}, {"identifier": "alias-2"}]}]}, NOW)[0]
                cli = _parse_lms({"models": [{"identifier": f"alias-{i + 1}",
                    "modelKey": "provider/model", "status": state} for i, state in enumerate(statuses)]}, NOW)
                models = {api["id"]: api}
                _merge_cli_rows(models, cli)
                row = machines_summary(local(models=list(models.values()), sources=[
                    {"id": "lmstudio-api", "state": "live"}, {"id": "lms-ps", "state": "live"}]), [])[0]
                self.assertEqual(row["loadedCount"], 1)
                self.assertEqual(row["busyCount"], expected)

    def test_identifier_only_cli_aliases_count_once_with_complete_or_partial_activity(self):
        from telemetry import _merge_cli_rows, _parse_api, _parse_lms
        for api_key in ("key", "id"):
            for identifiers, statuses, expected in (
                    (("alias-1",), ("idle",), 0),
                    (("alias-1",), ("generating",), 1),
                    (("alias-1", "alias-2"), ("idle",), None),
                    (("alias-1", "alias-2"), ("idle", "idle"), 0),
                    (("alias-1", "alias-2"), ("idle", "generating"), 1)):
                with self.subTest(api_key=api_key, identifiers=identifiers, statuses=statuses):
                    api = _parse_api({"models": [{api_key: "provider/model", "loaded_instances": [
                        {"identifier": identifier} for identifier in identifiers]}]}, NOW)[0]
                    cli = _parse_lms({"models": [{"identifier": identifier, "status": state}
                        for identifier, state in zip(identifiers, statuses)]}, NOW)
                    models = {api["id"]: api}
                    _merge_cli_rows(models, cli)
                    row = machines_summary(local(models=list(models.values()), sources=[
                        {"id": "lmstudio-api", "state": "live"}, {"id": "lms-ps", "state": "live"}]), [])[0]
                    self.assertEqual((row["loadedCount"], row["busyCount"]), (1, expected))

    def test_incomplete_api_instance_correlation_keeps_machine_counts_unknown(self):
        from telemetry import _merge_cli_rows, _parse_api, _parse_lms
        for count in (16, 17, 256):
            for same_id in (False, True):
                with self.subTest(count=count, same_id=same_id):
                    identifiers = [f"alias-{index}" for index in range(count)]
                    if same_id:
                        identifiers[0] = "provider/model"
                    api = _parse_api({"models": [{"key": "provider/model", "loaded_instances": [
                        {"identifier": identifier} for identifier in identifiers]}]}, NOW)[0]
                    cli = _parse_lms({"models": [{"identifier": identifier, "status": "idle"}
                        for identifier in identifiers]}, NOW)
                    models = {api["id"]: api}
                    _merge_cli_rows(models, cli)
                    row = machines_summary(local(models=list(models.values()), sources=[
                        {"id": "lmstudio-api", "state": "live"}, {"id": "lms-ps", "state": "live"}]), [])[0]
                    self.assertEqual(row["state"], "up")
                    self.assertEqual(row["modelsComplete"], count <= 16)
                    self.assertEqual((row["loadedCount"], row["busyCount"]),
                                     (1, 0) if count <= 16 else (None, None))

    def test_invalid_api_instance_correlation_keeps_machine_counts_unknown(self):
        from telemetry import _merge_cli_rows, _parse_api, _parse_lms
        api = _parse_api({"models": [{"key": "provider/model", "loaded_instances": [
            {"identifier": "alias-1"}, {"identifier": "invalid id"}]}]}, NOW)[0]
        cli = _parse_lms({"models": [{"identifier": "alias-1", "status": "idle"}]}, NOW)
        models = {api["id"]: api}
        _merge_cli_rows(models, cli)
        row = machines_summary(local(models=list(models.values()), sources=[
            {"id": "lmstudio-api", "state": "live"}, {"id": "lms-ps", "state": "live"}]), [])[0]
        self.assertEqual(row["state"], "up")
        self.assertFalse(row["modelsComplete"])
        self.assertIsNone(row["loadedCount"])
        self.assertIsNone(row["busyCount"])

    def test_local_same_id_merge_does_not_hide_unseen_instance_activity(self):
        from telemetry import _merge_cli_rows, _parse_api, _parse_lms
        api = _parse_api({"models": [{"key": "provider/model", "loaded_instances": [
            {"identifier": "provider/model"}, {"identifier": "alias-2"}]}]}, NOW)[0]
        cli = _parse_lms({"models": [{"identifier": "provider/model", "modelKey": "provider/model",
                                     "status": "idle"}]}, NOW)
        models = {api["id"]: api}
        _merge_cli_rows(models, cli)
        row = machines_summary(local(models=list(models.values())), [])[0]
        self.assertEqual(row["loadedCount"], 1)
        self.assertIsNone(row["busyCount"])

    def test_cli_instance_collision_preserves_inventory_and_model_counts(self):
        from telemetry import _merge_cli_rows, _parse_api, _parse_lms
        for statuses, expected in ((("idle",), None), (("generating",), 1),
                                   (("idle", "idle"), 0), (("idle", "generating"), 1)):
            with self.subTest(statuses=statuses):
                api = _parse_api({"models": [
                    {"key": "provider/model", "loaded_instances": [{"id": "alias-1"}, {"id": "alias-2"}]},
                    {"key": "alias-1", "display_name": "Other model", "loaded_instances": []},
                    {"key": "alias-2", "loaded_instances": []}]}, NOW)
                inventory = copy.deepcopy(api)
                cli = _parse_lms({"models": [{"identifier": f"alias-{index + 1}", "status": state}
                    for index, state in enumerate(statuses)]}, NOW)
                models = {row["id"]: row for row in api}
                instances = _merge_cli_rows(models, cli)
                self.assertEqual(list(models.values()), inventory)
                self.assertEqual(len({row["id"] for row in models.values()}), len(models))
                row = machines_summary(local(models=list(models.values()), localInstanceObservations=instances,
                    sources=[{"id": "lmstudio-api", "state": "live"}, {"id": "lms-ps", "state": "live"}]), [])[0]
                self.assertEqual((row["loadedCount"], row["busyCount"]), (1, expected))

    def test_cli_instance_collision_activity_expires_with_its_source(self):
        from telemetry import _merge_cli_rows, _parse_api, _parse_lms
        api = _parse_api({"models": [
            {"key": "provider/model", "loaded_instances": [{"id": "alias-1"}]},
            {"key": "alias-1", "loaded_instances": []}]}, NOW)
        cli = _parse_lms({"models": [{"identifier": "alias-1", "status": "idle"}]}, NOW)
        models = {row["id"]: row for row in api}
        instances = _merge_cli_rows(models, cli)
        for cli_state, age, expected in (("live", 0, 0), ("error", 0, None), ("live", 4, None)):
            with self.subTest(cli_state=cli_state, age=age):
                row = machines_summary(local(models=list(models.values()),
                    localInstanceObservations=[{**instance, "ageSeconds": age} for instance in instances],
                    sources=[{"id": "lmstudio-api", "state": "live"}, {"id": "lms-ps", "state": cli_state}]), [])[0]
                self.assertEqual(row["busyCount"], expected)

    def test_overlay_expires_without_refreshing_observed_time(self):
        row = machines_summary(local(), [])[0]
        observed = row["observedAt"]
        expired = expire_machines([copy.deepcopy(row)], NOW + 4)[0]
        self.assertEqual((expired["state"], expired["reasonCode"]), ("unknown", "stale"))
        self.assertIsNone(expired["busyCount"])
        self.assertEqual(expired["observedAt"], observed)

    def test_hands_off_requires_explicit_boolean_not_gaming(self):
        for data, expected in ((report(handsOff=True), True), (report(handsOff=False), False),
                               (report(handsOff=1), None), (report(gaming=True), None)):
            row = machine_summary("pc", "windows", data, now=NOW, trust="share-reported")
            self.assertIs(row["handsOff"], expected)
