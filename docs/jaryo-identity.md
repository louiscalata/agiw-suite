# AGIW marks and theme

Jaryo replaces the monitor’s visual language. It is not a second chrome beside the old one.

Shared files live in `jaryo/`. They are framework-neutral. OPOS and Nisi can use that folder later. This checkout does not modify those repositories and does not publish a package.

## Binding decisions

- Strict black and white. No graphite, copper, or color accent in AGIW.
- System light/dark is the default. Light or Dark is an explicit override and follows later system changes only while Theme is System.
- Factory structure, quiet surfaces, balanced density. Evidence rows expand. The acceptance line stays visible.
- Persistent sidebar on a wide desktop. Overview, map, components, and identity stay reachable on a small screen.
- Motion is the schematic rule and transitions. It is not false telemetry. Reduced motion removes it.
- Transitions zoom into the new page. Moving between Components, Identity, Machines and Setup grows the page body from 96% to full size in 240 ms while the nav stays put, and the Overview/Simple switch does the same to the whole face. Only transform moves, so no frame is gray. Reduced motion, or a browser without View Transitions, gets an instant change. If a window gets no frames the switch still lands after 400 ms.
- The app opens on Overview.
- The chrome name is the word AGIW in Inter (SIL Open Font License 1.1), weight 600, with the font’s own kerning plus 0.02em tracking. This is provisional. Louis did not choose the font. Fallbacks are Liberation Sans and Noto Sans.
- D4 Split is rejected. The geometric files remain in `jaryo/marks/` as a superseded study. They are not on the chrome or the favicon.
- No choice file is applied. Option ids are not inferred.
- `c92dab2` is historical. Receipts in `docs/data/` stay. They are not a pass on the current head.

## Directions

| Id | File | Mechanism |
| --- | --- | --- |
| D1 Stamp | `jaryo/marks/d1-stamp.svg` | Historical comparison. Square index. |
| D2 Ledger | `jaryo/marks/d2-ledger.svg` | Three rules of different length. |
| D3 Schematic | `jaryo/marks/d3-schematic.svg` | Corner ticks and a center mark. |
| D4 Split | `jaryo/marks/d4-split.svg` | Rejected. Superseded by the Inter wordmark. |
| D5 Signal | `jaryo/marks/d5-signal.svg` | Reticle in a dashed frame. The frame is the mark, not unknown data. |

The favicon is the letters AG in Inter, white on black, because the full word does not fit there. There is no geometric app mark.

## Icons

Louis, Nov 7 2026: the logo and every icon use Inter 600 (SemiBold). The letters are outlines taken from Inter 4.1 (SIL Open Font License 1.1) with the font's own kerning plus 0.02em tracking, so no icon needs the font installed. Every icon is black and white. `brand/build-icons.py` builds all of them from `brand/glyphs.json`; run it with `--font Inter-SemiBold.ttf` to re-read the outlines.

| Where | File | Mark |
| --- | --- | --- |
| Logo | `brand/agiw-wordmark-on-light.svg`, `brand/agiw-wordmark-on-dark.svg` | AGIW |
| Square mark | `brand/agiw-mark.svg` | AG, white on black |
| Favicon | `jaryo/marks/favicon.svg` | AG, white on black |
| Mac app (full monitor) | `brand/macos/AppIcon.icns` | AG on a black rounded square |
| Mac menu bar (full monitor) | `brand/macos/MenuBarMark.png`, `@2x` | AG template image; the count beside it carries state |
| Mac App Store edition | `wrapper-patch/app-icon/AppIcon.appiconset` | Same as the Mac app; see its README to apply |
| Windows tray | `brand/windows/tray-*.ico`, embedded in `windows/agiw-monitor.ps1` | AG; state is a shape: plain = idle, inverted = working, framed = needs attention, dashed = starting or stale |
| Windows app | `brand/windows/agiw.ico` | AG, white on black |
| Android launcher | `android/app/src/main/res/mipmap-*` | AG; adaptive icon on black with a monochrome layer |

Controls inside the pages stay text only. The marks are product icons, not UI glyphs.

Review them at `/identity`. The gallery does not change the running monitor.

Dark treatment is white on black. Light treatment is black on white. Narrow treatment is the square mark alone. The favicon is `jaryo/marks/favicon.svg`.
