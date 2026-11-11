# Jaryo kit

Shared tokens, theme, controls, and a pipeline page. The folder is the product. This checkout does not publish it to a registry. The AGIW monitor pages have their own shell in `web/jaryo.css` and `web/jaryo.mjs`. This kit does not draw those pages.

AGIW decisions recorded here:

- Strict black and white. No graphite, copper, or color accent in the default surface.
- System light/dark is the default. Light or Dark in the theme control is an explicit override.
- Factory structure, quiet surfaces, balanced density, expandable evidence detail.
- Persistent desktop sidebar. The same sections stay reachable on a phone and on a foldable cover (about 260–300px wide).
- Motion is the belt, the arm, and a short rule. It is not a measurement. Reduced motion holds them still.
- The chrome word is AGIW. The kit names Inter as a provisional family and does not ship an `@font-face`. Body text on this checkout uses `--j-font` (ui-monospace and the named monospace fallbacks). Wordmark outlines are Inter paths. Overview headlines are Newsreader and overview body text is Jost, matching the website. The kit contract still requires provisional Inter and an empty `chosenFamilyIds`, so these pages are not retargeted here. D4 Split is rejected. No option id is inferred.
- `appliedChoiceFile` is null. `chosenFamilyIds` is empty. Do not infer option ids.
- Direction studies in `marks/` stay as files. They are not the wordmark and they are not loaded by the page.

## Install

From this repository:

```bash
node jaryo/cli.mjs install --dest /tmp/jaryo-kit
node /tmp/jaryo-kit/cli.mjs demo --port 8731
```

Open http://127.0.0.1:8731/ . The server redirects to the pipeline page. The sample lane renders four stations from `demo/pipeline.json`.

`install` refuses to copy into the kit source. A copied kit can install and serve itself, because its `cli.mjs` uses its own folder.

Other commands:

```bash
node jaryo/cli.mjs check
node jaryo/cli.mjs tokens
node jaryo/cli.mjs mcp
```

`check` exits 0 when the token, state, motion, and pipeline files agree. `tokens` prints `tokens.json`. `mcp` speaks newline-framed Content-Length JSON on stdio. Its tools are `jaryo_tokens`, `jaryo_install`, and `jaryo_check`.

## Use the files

Link the sheets and the two scripts. Open the page over HTTP so the module can read `pipeline.json`. `boot.js` sets the theme before paint. `behavior.mjs` renders the stations, moves the theme control, and wires arrow keys on the station toolbar and the tabs.

```html
<link rel="stylesheet" href="jaryo/primitives.css">
<link rel="stylesheet" href="jaryo/components.css">
<link rel="stylesheet" href="jaryo/layout.css">
<link rel="stylesheet" href="jaryo/motion.css">
<script src="jaryo/boot.js"></script>
<script type="module" src="jaryo/behavior.mjs"></script>
```

Replace `demo/pipeline.json` with another lane. Each station needs `id`, `name`, `word`, and `detail`. The page says when the file does not load.

Theme is `system`, `light`, or `dark`. `?theme=dark` on the page URL overrides the saved choice for that load. The saved choice is `jaryo-kit-theme` in local storage.

## Layout

Below 980px the sections are a wrapping row and the page scrolls. At 980px and wider the sidebar stays put while the page scrolls. At 300px and below the theme control takes the next line, the title steps down, and the stations stack. Between those widths the stations sit two-up, then four-up from 720px.

## States

Controls use the page ink. Selected, pressed, and primary invert the fill. Hover adds a short rule. Focus is a 3px dashed ring. Disabled keeps full contrast and uses a dashed border. Loading keeps its name and draws the belt. Error is a double rule plus the word. Empty is a dashed frame plus the word. Windows High Contrast uses the system highlight for the selected control.

## Tests

`node --test test_jaryo_design.mjs` installs a fresh copy, serves it, and checks the contract.

`node jaryo/demo/smoke.mjs --out /tmp/jaryo-shots` opens the served page in headless Chrome, checks overflow, the station keys, reduced motion, and axe, and writes screenshots. Set `CHROME_BIN` when `google-chrome` is not on `PATH`. The Ubuntu source-check job `jaryo kit smoke` runs it. It does not run on the Mac.
