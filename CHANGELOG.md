# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- The fleet job worker can use a local `jev-task` CLI as the repo-patch analyzer: pass `--jev-task PATH|auto` or set `AGIW_JEV_TASK`. It stays off by default, runs with `--driver never` and a stripped environment, and passes the projected text through a 0600 temp file.
- Repo-patch records bounded, advisory Jev pre/post analysis by default. Set `jev_analysis: false` to skip it; without an injected analyzer, both phases report unavailable when reached.

Merged on 14 November 2026:

- #160 (`0315afb`) re-running `agiw service install` on Windows ends a Running or Queued peer-link task with `schtasks /End` before `/Run`, so the new code starts. A task that does not stop, or a State that is Unknown or cannot be read, starts nothing and exits 1 with the `/End` command to run.
- #161 (`553db72`) stops two telemetry tests racing first-run script latency, and the `lms` reader no longer blocks on a pipe that a child process keeps open after the command exits.
- #162 (`24ad90b`) keeps Overview map captions at full contrast when a node is selected or faded; only the halo, ring and core dim.
- #163 (`04f9cb8`) adds the screening check-job engine (`eci_jobs.py`, `docs/eci-check-jobs.md`). Its gate ships closed and it starts no model, server or lane.
- #164 (`34f1b89`) opens the Overview at `/` in the Mac dashboard window; the compact popover keeps `/monitor`. The shell keeps theme, text size and the window frame in its own preferences and returns a failed page to its landing page. The app was not built or opened: native rendering is NOT_RUN.
- #165 (`23edba4`) publishes the router state contract (`docs/router-state-contract.md`) and runs the router dual-read cases against test writers in `test_support/router_contract/`. A fixture pass is not router acceptance.
- #166 (`caa4d9e`) makes Text size reachable beside Theme on every page that has the Theme control, keeps both choices in the documented `agiw-theme` and `agiw-text-size` keys, and documents the preference contract (`docs/monitor-preferences.md`).
- #167 (`95995df`) adds the lane-context pre-check (`context_exceeded`) and three read-only check-job GET routes. `MonitorServer` serves them only when handed a route object; the default is none, and no caller passes one.
- #168 (`cc4c5c7`) lists what is not live, and why, in the Diagnostics panel.

- Add model fast-check planning, statistics and admission gates, an opt-in HTTP harness adapter, and read-only dataset integration for both observers. `AGIW_ECI_TUNING_DATASET_DIR` selects a normalized local `dataset.json`; unset configuration uses the bundled catalog, and invalid configured data reports unknown. Screening results remain separate from full ECI scores and cannot update routing policy. Real model checks, native rendering and independent full ECI runs remain NOT_RUN.

- #138 (`607b302`) makes Diagnostics read-only: opening it reads cached observations and starts no repair. Repair requests choose one target (`local`, `route` or `nisi`) with `reviewed: true`; bundled `all` and `both` scopes are rejected.

- #119 (`8230e60`) replaces Fix inference with the AGIW x ECI Model Tuning Tool after #117. The old name, control IDs and `/api/inference/fix` route are deprecated compatibility aliases; `/api/eci-tuning/repair` uses the same guarded repair. Opening the tool never starts repair; Run check confirms it. This is not a tagged release.

- #117 (`eedb253`) adds the AGIW x ECI Model Tuning Tool: offline ingest, protocol-separated rankings, explicit policy apply, and a read-only model tuning page with provisional and pending scores.

- #114 (`66a660a`) reconciles the agent guide with current source and CI ownership.
- #122 (`b80607b`) routes Linux CI jobs to hosted Ubuntu and uses Ubuntu 22.04 for the sandboxed kit smoke.
- #123 (`3a5b4da`) integrates the Plex observer web variant and its font distribution checks.

These entries record local merge history; CI outcomes and current-source native acceptance remain unverified here. See the [source verification addendum](docs/source-verification.md#local-merge-reconciliation--12-november-2026).

Previously merged after the v1.1.0 tag:

- #69 Android monitor encoding, insets, and Gradle tests.
- #71 Flip6 inner and cover readings stay when the window turns.
- #72 Map key and activity dots use Jaryo ink.
- #73 Jaryo tokens on the Android monitor screens.
- #74 Docs name this checkout 1.1.0.
- #75 Overview panels stay readable in the light theme.
- #76 The control-route check is described as part of the v1.1.0 source.
- #77 A Jev archive file added in the same timestamp tick is reported.
- #78 Mac route tests run on Linux.
- #79 Flip hinge posture, font scale, and TalkBack labels.
- #80 A missing or unreachable observer stays on the monitor screen.
- #81 Android monitor tests, including an emulator smoke, run in CI.
- #82 Forced-colors emulation on the monitor pages in Ubuntu CI.
- #83 Acceptance block uses plain words.
- #84 The footer About control has a 44px target.
- #85 Development notes say the Windows review is not a pass.
- #86 The README says fold posture is read, and that a physical Flip 6 is not.
- #88 `GET /api/snapshot` is documented as the read-only export.
- #89 The changelog uses Keep a Changelog form.

## [1.1.0] - 2026-11-09

Tagged `v1.1.0` and published on 9 November 2026 as a source release. No build assets are attached. The signed Mac download remains **1.0.0 (7)**.

Version identifiers for the Mac app, the `agiw` command, the Windows observer, and the Android monitor are **1.1.0**. The Mac build number is **14**. The unreleased source label 1.2.0 (13) was never shipped. Bundled public Nisi stays pinned at **0.2.0**.

Details are in [docs/release-notes/v1.1.0.md](docs/release-notes/v1.1.0.md).

Merged through the 1.1.0 preparation commit: #2, #3, #4, #6, #7, #8, #9, #10, #11, #13, #14, #15, #16, #17, #18, #19, #20, #21, #22, #23, #24, #25, #26, #27, #28, #29, #30, #31, #32, #33, #34, #35, #36, #37, #38, #39, #40, #41, #42, #43, #44, #45, #46, #47, #48, #49, #50, #51, #52, #53, #54, #55, #56, #57, #58, #59, #60, #61, #62, #63, #64, #65.

On the v1.1.0 tag after that list: #66 (version identifiers), #67 (Jaryo shell), #68 (installable Jaryo kit).

Not merged: #1 and #5 were closed without merge. #12 was closed without merge on 9 November 2026. The same Linux-node outcome had already landed in #10 and #11. #12 is not part of 1.1.0.
