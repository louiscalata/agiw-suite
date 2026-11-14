# Monitor display preferences

Every page of the full web Monitor loads `web/jaryo.mjs`. The App Store edition has its own copy of the same contract; see below. That module owns two display preferences, Theme and Text size, and the contract that a native shell can use to keep them between launches.

## The preferences

| Preference | Key | Values | Default |
|---|---|---|---|
| Theme | `agiw-theme` | `system`, `light`, `dark` | `system` |
| Text size | `agiw-text-size` | `100`, `125`, `150`, `200` (percent) | `100` |

- Each page that has the Theme control (`#jaryoTheme` inside `label.j-theme`) gets a Text size control beside it. The script adds the control; no page needs its own markup for it.
- The size is applied while `jaryo.mjs` evaluates, before it mounts any control, as `data-text-size` on the root element. A page without the Theme control still gets the saved size. The 125, 150 and 200 percent zoom rules are in `web/jaryo.css`, which every page loads. Only the overview has its own layout for 150 and 200 percent (`web/simple-view.css`); the other pages are zoomed as they are.
- Values outside these lists are ignored, never applied.

## Where a choice comes from

When a page opens, each preference is read in this order:

1. This browser's `localStorage` under the key above, when storage is available and the value is valid.
2. The native seed: `window.agiwPreferenceSeed`, an object with the same keys. Only its own, valid string entries count.
3. The default.

A saved value wins over the seed, so a change made during a session carries to the next page in that session. Restoring a value at page load does not save it: only a person's change writes `localStorage`. Until a person changes a preference, each page reads the seed again, so a seed that a shell updates between pages still applies. The seed covers a store that starts empty, such as the native app's non-persistent web data store (`Monitor.swift` uses `.nonPersistent()`), which discards `localStorage` when the app quits.

## The change message

After a person changes Theme or Text size, the page saves the value to `localStorage` when it can and dispatches one event on `window`:

- Type: `agiw:preferences`
- `detail`: a frozen object with both keys, for example `{"agiw-theme": "dark", "agiw-text-size": "150"}`

Restoring a saved or seeded value at page load sends no message and writes nothing. A blocked storage still applies the change and still sends the message.

## What a native shell does

The detail has the same shape as the seed, so a shell can store the detail it observes and inject it back unchanged:

1. At document start, set `window.agiwPreferenceSeed` to the stored object. To apply the size before the first paint, the same script can also set `document.documentElement.dataset.textSize` and `dataset.theme`.
2. Forward `agiw:preferences` events to the shell through its own message handler, and store the latest detail.

The macOS shell does not do either yet; that is separate native work. This page describes the web side, which is in place.

## App Store edition

The App Store web edition (`wrapper-patch/web-edition`) does not ship `web/jaryo.mjs` or `web/jaryo.css`. It keeps the same contract through its own copy: `PREFERENCE_RUNTIME` in `jaryo-adapt.mjs` is appended to the edition's `app.js` and handles Theme and Text size with the same keys, read order, saving rule and change message. `TEXT_ZOOM` adds the three zoom rules to the edition's `style.css`. The edition's `simple-view.css` keeps the overview layouts for 150 and 200 percent. The edition applies the size when `app.js` evaluates, which is after the document is parsed.

## Verification

`node --test test_text_size.mjs test_simple_view_theme.mjs` runs the shipped module source in a DOM double. `node --test wrapper-patch/web-edition/test_edition_preferences.mjs` does the same for the edition runtime and checks that the edition rules carry it and the zoom rules once. NOT_RUN: browser rendering, the visual effect of each size, horizontal overflow at 200 percent on the pages without an enlarged-text layout of their own (monitor, machines, components, identity, setup, eci-tuning), first-paint timing in a real browser, native WebKit behaviour, and a full edition build (the current source tree stops on drifted statement pins before and after this change).
