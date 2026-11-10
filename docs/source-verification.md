# Source review and verification · 2026-11-03

This page is the 3 November 2026 review. It describes the unreleased **1.2.0 (13)** label at base `c0a057dbebecfa4d95469d1b158ce040a541d8f0`. This checkout is **1.1.0 (14)**. The review does not update the **1.0.0 (7)** download or establish native, package or release acceptance. The canonical [roadmap](../roadmap.md) retains release gates.

## Source dispositions

- Native polling resolves `/api/snapshot` from the origin root; 404 does not trigger observer recovery. Compiled production-method coverage is in `test_monitor_poll.py`.
- Map observations expire, refreshes serialize, failed requests clear measurements and missing selections close. Coverage: `test_review_regressions.py` and `test_monitor_lifecycle.mjs`.
- Native control POSTs require a per-launch capability sent over child stdin and attached only to same-origin POST requests. Coverage: `test_review_regressions.py`, `test_control_capability.mjs` and `test_monitor_routes.py`. The released download remains affected by the [known issue](../SECURITY.md#known-issue-in-v100-build-7-local-control-authentication).
- Model operations reconcile only on fresh positive desired-state evidence. Unchanged inventory does not prove cancellation and must not clear an uncertain operation. Coverage: `test_review_regressions.py` and `test_model_control_ownership.py`.
- The Mac map reuses telemetry's verified LM Studio endpoint and complete inventory without another request. Standalone helper defaults are separate. Optional probes have total deadlines and follow the memory-watchdog tick. Coverage: `test_review_regressions.py` and `test_inference_collect.py`.
- PC tunnel environment overrides take precedence over valid file values; displayed endpoints match fetched forwards. Duplicate Windows lane ports/aliases remain unknown. Coverage: `test_pc_tunnel.py`, `test_review_regressions.py` and `windows/tests/test_lane_config_uniqueness.py`.
- Source/release scope, platform boundaries, LAN exposure and recovery prerequisites are documented. Native rendering, clean-Mac installation, the full credential lifecycle, native Windows acceptance and reproducible release packaging remain open. Source tests cannot close these gates.

## Documentation follow-up

- Corrected architecture's obsolete LM Studio port-limit claim to describe telemetry reuse and distinguish the standalone helper.
- Replaced diagram/text artwork and removed the superseded SVG assets with current two-machine Mermaid dependencies and Markdown tables. Arrows mean dependencies; Linux and PAIR remain retired. The supplied feedback image was removed.

## Verification provenance

Test runs for each change are recorded in its pull request, not in this file. GitHub rendering of the Mermaid diagrams is checked by viewing the published README.
