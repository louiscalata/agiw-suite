# Source review and verification · 2026-11-03

This page is the 3 November 2026 review. The working copy was labeled **1.2.0 (13)** at base `c0a057dbebecfa4d95469d1b158ce040a541d8f0`. That label was never published. The checkout is now **1.1.0 (14)**, tagged as a source release. The review does not update the **1.0.0 (7)** download or establish native, package or release acceptance. The canonical [roadmap](../roadmap.md) retains release gates.

## Local merge reconciliation — 12 November 2026

Local history at `ef5193b` contains these merges. This establishes source integration only; CI receipt review is **NOT_RUN** here and CI outcomes remain unverified.

| Pull request | Merge commit | Integrated concern |
| --- | --- | --- |
| #114 | `66a660a` | Agent guide and CI ownership |
| #117 | `eedb253` | Model Tuning Tool adapters and read-only page |
| #119 | `8230e60` | Model Tuning Tool replaces Fix inference after #117 |
| #122 | `b80607b` | Hosted Linux CI fallback and Ubuntu 22.04 kit smoke |
| #123 | `3a5b4da` | Plex observer web variant and font distribution checks |

`Monitor.swift`'s `consumeStartup` schedules a one-second timer; `pollSnapshot` resolves `/api/snapshot` from the origin root and guards against overlapping requests. Current-source native GUI, clean-Mac installation and the full Jev credential lifecycle remain **NOT_RUN** in this source check. The historical Build 6/7 observations in the roadmap do not establish acceptance of this checkout.

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
