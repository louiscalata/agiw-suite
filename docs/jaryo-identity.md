# AGIW marks and theme

Jaryo replaces the monitor’s visual language. It is not a second chrome beside the old one.

Shared files live in `jaryo/`. They are framework-neutral. OPOS and Nisi can use that folder later. This checkout does not modify those repositories and does not publish a package.

## Binding decisions

- Strict black and white. No graphite, copper, or color accent in AGIW.
- System light/dark is the default. Light or Dark is an explicit override and follows later system changes only while Theme is System.
- Factory structure, quiet surfaces, balanced density. Evidence rows expand. The acceptance line stays visible.
- Persistent sidebar on a wide desktop. Overview, map, components, and identity stay reachable on a small screen.
- Motion is the schematic rule and transitions. It is not false telemetry. Reduced motion removes it.
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

Review them at `/identity`. The gallery does not change the running monitor.

Dark treatment is white on black. Light treatment is black on white. Narrow treatment is the square mark alone. The favicon is `jaryo/marks/favicon.svg`.
