# Remote machine registry fix verification

The FIX FIRST review was read in full and its findings verified against the draft before fixes. This record describes source checks and local test-server behavior. It does not establish installed application behavior or release acceptance. No commit, push, browser, model completion, remote service call, model control, or transport activation was performed.

| Finding | Change | Regression pin |
| --- | --- | --- |
| Protected services | `remote_machines.validate_config` accepts only numeric ports 1024–65535, excludes all eight documented protected ports with an error naming the port, and constructs only the fixed loopback URL. | `RegistryTests.test_protected_ports_rejected_by_name_before_any_probe`; `test_only_bounded_numeric_ports_and_no_connection_fields`. |
| Windows package startup | The actual Windows installer inventory ships the new module. | `WindowsPackageRuntimeTests.test_observer_imports_from_clean_staged_installer_inventory`: isolated Python imports the real observer from only staged installer resources. Failed with the missing module before the fix; passes afterward. |
| HTTP identity truth | Strict registry transport checks complete length/chunk framing, byte limit and UTF-8. Parsing rejects duplicate keys, nonstandard constants, ambiguous/empty model lists and nonprintable or overlong IDs. Failure replaces the previous identity. | Eleven `RegistryTransportTests` use actual ephemeral loopback HTTP servers. Permissive transport mutations make the truncated-body and invalid-UTF-8 pins fail with false `ok`. |
| Existing PC visibility | Namespaced registry IDs coexist with legacy rows; only a valid explicit `replaces: legacy-pc` suppresses legacy collection and rows. Invalid configuration preserves legacy visibility. | `RegistryTests.test_namespace_keeps_legacy_rows_unless_explicit_replacement`; `test_invalid_config_is_off_and_preserves_legacy_rows`; `SamplerTests.test_registry_does_not_bypass_legacy_tunnel_unless_replacement`. |
| Config file custody | No-follow/nonblocking open; descriptor type, owner, permissions and size checked before bounded read; failures disable the feature with a deduplicated reason. | `ConfigSafetyTests`: symlink, FIFO, flags, unsafe modes, wrong owner, size before read, invalid JSON logging and huge numeric timeout. |
| Timing and concurrency | Eight background workers, per-lane timeout default 1.5 seconds and maximum 6 seconds, completion timestamps, fair due-lane admission and nonblocking snapshot reads. | `BackgroundTests`: a real 4.2-second endpoint succeeds with timeout 6 while snapshot returns within 0.25 seconds; nine real endpoints check bounded concurrency and eventual queued-lane service. Synchronous and forced-short-timeout mutations both fail the slow-endpoint pin. |
| Registry freshness | Registry results expire after twice the three-second interval, independent of the legacy PC expiry; snapshot reads and DOM ticks advance age and clear stale identity. | `RegistryFreshnessTests.test_registry_snapshot_boundary_uses_its_interval_even_for_pc_and_rerolls_root` exercises actual `SnapshotStore.read`; actual DOM stalled-refresh pin. Both reject a legacy-expiry mutation. |
| Private audit | Original audit copied verbatim outside the repository to the separately requested audit destination. Repository guide and example are general and use placeholders. | `FeatureDocumentationTests` guard the guide/example and every changed or new file. Original audit: 28,024 bytes; SHA-256 `940fd1598eb2ad93bafce0b614082d5d51ee41abd9aaa90c21cd6605063a09a8`. |
| Actual UI rendering | Actual page tick/draw/click/detail DOM path tested for OK, UNKNOWN and STALE; labels and server identities remain literal text. | Five DOM regressions in `test_remote_machine_ui.mjs`; an `innerHTML` mutation fails the escaping pin. |
| Future metadata | Optional nonnegative integer `contextTokens` and exact `thinking` enum validated and passed untouched through targets, lane records and public export. No runtime consumer added. | `RegistryTests.test_timeout_and_capability_fields_validated_and_passed_untouched`; `RegistryFreshnessTests.test_public_map_keeps_registry_freshness_and_optional_capability_metadata`. |
| Additional small findings | Config removal invalidates old results and stops subsequent scheduling; late generations cannot republish; huge numeric timeout cannot escape feature-off handling. | `BackgroundTests.test_removed_registry_stops_probes_and_readmission_starts_unknown`; `test_config_change_does_not_reuse_identity_or_late_result`; `ConfigSafetyTests.test_huge_numeric_timeout_disables_registry_without_raising`. |

The initial expanded registry regression run against the original draft produced 14 failures and 7 errors across 17 tests. The Windows staging test failed before shipping the module. UI pins initially produced five failures; backend freshness pins produced two failures and two errors. Transport defects were also reproduced directly before the strict implementation. All mutation work used separate processes, temporary copies or in-memory replacements and left no production mutation in the draft.

Final source checks used Python 3.15.2 and Node v24.22.2. Temporary files were directed inside the checkout for the final run.

```sh
TMPDIR="$PWD/.task-tmp" python3 -B -m unittest test_remote_machines test_inference_collect test_inference_stats test_pc_tunnel test_package_resources < /dev/null
# 71 tests passed; zero failures, errors or skips; exit 0.

TMPDIR="$PWD/.task-tmp" python3 -B -m unittest discover -s windows/tests -p test_package_runtime.py -v < /dev/null
# 1 test passed; zero skips; exit 0.

TMPDIR="$PWD/.task-tmp" python3 -B -m unittest discover -s windows/tests -p test_inference_expiry.py -v < /dev/null
# 3 tests passed; zero skips; exit 0.

TMPDIR="$PWD/.task-tmp" node --test test_inference_map.mjs test_remote_machine_ui.mjs test_monitor_tabs.mjs < /dev/null
# 39 tests passed; zero failures, cancellations or skips; exit 0.

node --check web/monitor.mjs
bash -n build.sh package-release.sh
git diff --check
# Each exited 0.
```

**Open privacy finding:** the whole-checkout scan still finds pre-existing identity references in 53 unchanged files, including public attribution, links, bundle/keychain identifiers, legacy fixtures and vendored metadata. Every changed or new file passes the owner-string scan. A checkout-wide identity migration would affect unrelated runtime identities, attribution and vendor integrity, so it remains open pending the scope clarification. The zero-match result applies to the outgoing files, not the whole checkout.

Native Windows installation, native application rendering, live endpoint identity, hardware behavior, deployment and CI are NOT_RUN. The test servers prove the specified HTTP and timing behavior only. No real model endpoint was probed in this round.
