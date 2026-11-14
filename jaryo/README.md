# Jaryo kit

Shared tokens, theme, controls, and a pipeline page. The kit and the Suite's full observer web now apply Louis's 17 lab choices, with the Copper accent selected on 2026-11-12. This checkout does not publish the kit to a registry.

The dark palette uses the composed spec's graphite surfaces, light text, and cool status tints. The light palette uses the existing black-and-white fallback. Both use Copper `#C47A3A` for accent fills and focus, with black labels on Copper fills. Copper text on white does not meet normal-text contrast and is not used. Forced colors use the operating system's colors. System light/dark remains the default; Light and Dark remain explicit overrides.

## Decision and provenance

`choices.json` is a byte-for-byte copy of the owner's `jaryo-lab-choices.json`, saved at `2026-11-06T17:52:56.877Z`. Its SHA-256 is `6490e5b15b624b82703d2ce897054c2e9a40f6c8dcc5d389de45e86d7004be2d`. `tokens.json.appliedChoiceFile` records that original name and hash. `chosenFamilyIds` contains all 17 choices, including F17.01.

F17.01 is **Copper Workshop**, the color family. The owner selected Copper on 2026-11-12, superseding the 2026-11-11 booklet-red override. `ownerOverrides.F17` records the Copper accent and focus, black accent ink, and unchanged system forced colors. The remaining dark F17 tokens come from the composed interface spec v0.5c. `decision.mjs` pins the accepted record independently of the editable tokens.

| Family | Choice | Applied pattern |
| --- | --- | --- |
| F01 | F01.01 | Main Bus Spine: vertical step list with right-angle taps |
| F02 | F02.04 | Human label with exact technical ID below in mono |
| F03 | F03.03 | Dependency pager; forks present every branch |
| F04 | F04.01 | Inverted selection and declared neighbour outlines |
| F05 | F05.02 | Port sockets opening the inspector's binding list |
| F06 | F06.02 | Ported boxes; inline IN/OUT labels on phones |
| F07 | F07.01 | Mono ledger, row rules, sticky first column and right-aligned values |
| F08 | F08.10 | Unknown nodes keep a dashed frame, hatch and words |
| F09 | F09.01 | Word and glyph states; color never supplies the only signal |
| F10 | F10.01 | Empty states give a reason and a next step |
| F11 | F11.01 | Reserved inspector column; native modal sheet on phones |
| F12 | F12.10 | Dashed gaps; blocked, unknown, stale and review groups cannot be hidden |
| F13 | F13.02 | Event Tick; no decorative loops; count resets per run |
| F14 | F14.02 | 56px phone thumb bar with safe-area padding |
| F15 | F15.01 | Install line with honest copy feedback and GUI setup path |
| F16 | F16.01 | Visible Commands button, Cmd/Ctrl-K and capability-gated combobox |
| F17 | F17.01 | Copper accent and focus with the remaining spec palette |

The chrome word remains AGIW. D4 Split is rejected; `marks/` contains studies, not a newly approved wordmark. The web uses bundled IBM Plex Sans and Mono. The standalone kit names the same fonts and uses Sans/Mono fallbacks when they are not installed; it does not bundle font binaries. Android retains its original Inter, black-and-white foundation and dimensional scale. The top-level provisional font and `surface` fields describe that retained foundation, not the applied web palette.

## Scope and data limits

The Suite's monitor boards and the standalone pipeline use the same generated component CSS and JavaScript. Existing Suite navigation, controls and server contracts remain intact. Windows already serves and installs these same root `web/jaryo.css`, `web/jaryo.mjs`, `web/monitor-board.mjs`, `web/setup.html`, `jaryo/primitives.css` and `jaryo/tokens.json` assets; there is no Windows fork. The App Store wrapper is not changed.

Some selected interactions require data the observer does not supply. These are explicitly unavailable, not inferred:

- F03/F04: absent `inputs` means dependencies are not declared. The pager does not substitute array order, and neighbours are not guessed. The fictional kit fixture declares its linear edges.
- F05: the kit supports binding rows and a supplied `onBind` callback. The observer declares no binding editor; it shows the reason instead of a working Change action.
- F12: the kit supports explicitly grouped optional/collapsed steps. Current observer rows declare no such groups, so none are hidden automatically.
- F13: `receiveEvent` advances only when a caller declares an event stream and supplies a run ID plus a unique event ID. Snapshot polling is not an event stream. Current Suite boards and the fixture show no tick.
- F15: the Suite is an installed observer, not a marketing site. The install hero is on Setup; its command is the documented source-checkout CLI install, and its GUI path opens the existing connection guide. No new installer or connection capability is invented.

## Install

From this repository:

```bash
node jaryo/cli.mjs install --dest /tmp/jaryo-kit
node /tmp/jaryo-kit/cli.mjs demo --port 8731
```

Open http://127.0.0.1:8731/ . The server redirects to the pipeline page. The sample lane renders four fictional stations from `demo/pipeline.json`. `install` refuses the kit source as a destination. A copied kit can install and serve itself.

## Generate and check

There was no token/export generator in this kit. `generate.mjs` now exports `primitives.css` and the marked sections of `web/jaryo.css` and `web/jaryo.mjs`. Edit `tokens.json`, `patterns.css`, or `interactions.mjs`, then regenerate; do not hand-edit those generated sections. Files outside those sections remain ordinary UI source.

```bash
node jaryo/generate.mjs
node jaryo/generate.mjs --check
node jaryo/cli.mjs check
node jaryo/contrast.mjs
node jaryo/cli.mjs tokens
node jaryo/cli.mjs mcp
```

`check` requires the exact choice record and override, verifies the choice file hash, compares generated exports, computes contrast, and retains the artwork, state, motion and pipeline checks. `tokens` prints the token record. The stdio tools remain `jaryo_tokens`, `jaryo_install`, and `jaryo_check`.

The Node-only contrast audit covers all 44 semantic text and UI pairings used by these tokens in light/dark, selected, blocked, primary, focus and border states. Text must reach 4.5:1; UI boundaries must reach 3:1. The limiting accepted pair is the dark border on surface at 3.007:1. Copper on white is only 3.391:1, so Copper is not used as normal text on white. Black labels on Copper reach 6.192:1, and primary controls have a contrasting page-ink border.

## Use the components

```html
<link rel="stylesheet" href="jaryo/primitives.css">
<link rel="stylesheet" href="jaryo/components.css">
<link rel="stylesheet" href="jaryo/layout.css">
<link rel="stylesheet" href="jaryo/motion.css">
<script src="jaryo/boot.js"></script>
<script type="module" src="jaryo/behavior.mjs"></script>
```

`components.css` imports `patterns.css`. Each station needs `id`, `name`, `word`, and `detail`. `inputs` is an explicit list of source IDs; `[]` declares a source node. Optional presentation data includes `bindings`, `durationMs`, `group`, and `optional`. Unknown durations display “Unknown.” A standalone consumer can import `mountChoicePipeline` and `mountCommandPalette` from `interactions.mjs`.

At 701px and wider the inspector column is reserved; on phones it is a native modal dialog. The pager becomes the thumb bar at 700px and below, with the full destination name and ID in its accessible name. The native dialog supplies modal focus containment, Escape and inertness. A fork opens a separate branch chooser. Selection never erases blocked or unknown marks. Focus is a 3px dashed outline offset 3px outside. Reduced motion removes animation; Event Tick state changes remain available.

## Verification boundaries

```bash
PYTHONDONTWRITEBYTECODE=1 node --test --test-skip-pattern='a fresh install serves the pipeline page' test_*.mjs
```

The excluded fresh-install HTTP smoke test needs network permission, including loopback. The Node DOM tests exercise event handlers without a browser; they do not establish native rendering or assistive-technology acceptance.

CI must run `node jaryo/demo/smoke.mjs --out /tmp/jaryo-shots` (the `jaryo kit smoke` job), `node checks/forced-colors-emulation.mjs`, and `node checks/overview-ink.mjs`. Browser checks are NOT_RUN on this Mac. The real phone sheet, 260px overflow, standalone safe areas, screen-reader announcements and Windows High Contrast remain native verification gates. Android's Python source contract accepts the scoped approval while preserving its colors and fonts; its Java/Gradle/device stages are separate.
