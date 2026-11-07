# Isolated wrapper patch

Replace `scripts/sync-web.sh` and the whole `scripts/web-edition/` directory on a copy of the complete sanitized wrapper. Do not apply this to a dirty checkout, and do not add a git remote.

The web-edition already inside that archive still emits only `LOCAL MODELS` and does not drop a late control error. Copying the Jaryo source bundle does not replace those scripts. That is why `4089e55` and `cb25a7f` stage logs still fail the two DemoGolden checks.

## Preconditions

- The copy is a fresh extract of the source-only archive whose SHA-256 is `acf07b5d0aadfc5930f3a1db6200d1a012cb43109b19903adf61e3982aedc301`.
- That archive includes `Packages/AgiwCore/Tests/AgiwCoreTests/Fixtures/DemoGolden/demo-idle.json` and `RUNTIME-MANIFEST.sha256`. It does not include the `VendorRuntime/NisiNode` binary.
- `scripts/appstore-distribution.sh` still pins Xcode `27A266a`. This patch does not change that file and does not add a skip.
- After the copy, `scripts/web-edition/feature-denylist.txt` must still be SHA-256 `03378746a130f9217b679a475a14ac14236c53e90c404c3fd3a5c426d0b761f9`.
- After the copy, `scripts/web-edition/engine.mjs` must still be SHA-256 `159bb12d14edae6fca2d37643300dcac7c5e341d7d97c6e4dd2928c925ed576f`.
- `jaryo-adapt.mjs` in this patch is SHA-256 `259d8e3c9b5c6f3a78a9b6ed7b3579bbe15767f15adbb5709a3e7e8b0f5f2668`. Its pins are literals. It does not hash the file being built.
- The source tree is the Jaryo checkout that contains this patch. The nine sync inputs stay, including `web/simple-view.mjs` and `web/simple-view.css`.
- Node 18 or later is on `PATH`.

## Apply

From the isolated copy:

```sh
rm -rf scripts/web-edition
cp /path/to/wrapper-patch/sync-web.sh scripts/sync-web.sh
cp -a /path/to/wrapper-patch/web-edition scripts/web-edition
bash -n scripts/sync-web.sh
bash scripts/sync-web.sh --stage-only "$SOURCE_TREE"
```

`--stage-only` returns after the edition build and the DemoGolden tests, before the first `mv` into `App/Resources/web`. A successful run prints `sync-web: stage-only` and exits 0. The built `app.js` is 50732 bytes and `map-layout.mjs` is 40500 bytes, with 24 replaced statements. The old patch produced 50061 and 40452 bytes and 23 replacements, and those sizes fail the two checks below.

`--commit REV` resolves the full commit and accepts only blob modes `100644` and `100755`.

## What this patch changes

- A demo map label is `SAMPLE MODELS · N`. A live map label stays `LOCAL MODELS · N`.
- Accepting a snapshot whose demo flag changed clears the model-control error and ignores a control reply that started before that change. The late text `old memory refusal` does not remain.
- `simple-view.mjs` from the source tree mounts when the edition switch is in the document. The full Windows web has no switch, so Fix inference stays there.
- The Mac origin serves only `/` as a document. Extensionless routes are not files. The eight-file list is unchanged. Overview, Map, Components, and Identity are buttons in that one page, not links to other files. Overview keeps the model list in the page; it is not the off-canvas drawer. Map is the local model graph. Components reads the snapshot and says this edition has no package self-check and no hosted connection. Identity is the word AGIW. Light and dark use one paper and ink pair, including the top bar, the list, and the headline. A narrow window wraps the nav so Theme stays on screen. The compact face and the four-view face do not show at the same time. The top bar is the words AGIW Suite. It does not include the AS monogram. The page opens on Overview. Simple is the switch into the compact face; without a snapshot that face says the inventory is unknown. Unload is offered only for a fresh, non-demo, live sample whose control reply has status idle, running, reported, or failed. An empty object or any other shape does not count. The model line says no local model only when that fresh sample's inventory is an empty list or every row is an object explicitly unloaded. A null or non-object sibling keeps the line unknown and does not offer control, in either order. A failed control status is a completed operation outcome, not a missing reply. An unknown, stale, or malformed row stays unknown and does not offer control. A rejected snapshot, a rejected control reply, a stale or undated sample, and an older poll do not keep that offer. The unload control only opens the confirmation line. It does not submit a model action. The engine, the denylist, and the Xcode pin are the same bytes.
- The denylist, the engine, and the negative pin tests are the same files as the archive.

A fresh extract of the archive above, with this patch and the current source tree, completed `--stage-only` with exit 0. `App/Resources/web/app.js` was byte-identical before and after. The distribution pin was still `27A266a`.

## Revert

Restore `scripts/sync-web.sh` and `scripts/web-edition/` from the archive above and delete `.build-sync/`. Do not delete `App/Resources/web` after `--stage-only`: that command does not replace it.
