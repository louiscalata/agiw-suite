# Isolated wrapper patch

Replace `scripts/sync-web.sh` and the whole `scripts/web-edition/` directory on a copy of the complete sanitized wrapper. Do not apply this to a dirty checkout, and do not add a git remote.

The web-edition already inside that archive still emits only `LOCAL MODELS` and does not drop a late control error. Copying the Jaryo source bundle does not replace those scripts. That is why `4089e55` and `cb25a7f` stage logs still fail the two DemoGolden checks.

## Preconditions

- The copy is a fresh extract of the source-only archive whose SHA-256 is `acf07b5d0aadfc5930f3a1db6200d1a012cb43109b19903adf61e3982aedc301`.
- That archive includes `Packages/AgiwCore/Tests/AgiwCoreTests/Fixtures/DemoGolden/demo-idle.json` and `RUNTIME-MANIFEST.sha256`. It does not include the `VendorRuntime/NisiNode` binary.
- `scripts/appstore-distribution.sh` still pins Xcode `27A266a`. This patch does not change that file and does not add a skip.
- After the copy, `scripts/web-edition/feature-denylist.txt` must still be SHA-256 `03378746a130f9217b679a475a14ac14236c53e90c404c3fd3a5c426d0b761f9`.
- After the copy, `scripts/web-edition/engine.mjs` must still be SHA-256 `159bb12d14edae6fca2d37643300dcac7c5e341d7d97c6e4dd2928c925ed576f`.
- `jaryo-adapt.mjs` in this patch is SHA-256 `1fc9aba2ff76ea2f93db5b3e3d530d29c743deb0c652f53913bfbd2f92059a3e`. Its pins are literals. It does not hash the file being built.
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
- The shipped `index.html` keeps the Overview landing. The nav contains `<a href="/" aria-current="page">Overview</a>` and the title is `AGIW · Overview`. Map, Components, and Identity stay out of this edition on purpose: they are separate documents, and the unchanged engine refuses an href to a file outside the eight shipped files. The theme control stays out because `jaryo.mjs` is not one of those eight.
- The denylist, the engine, and the negative pin tests are the same files as the archive.

A fresh extract of the archive above, with this patch and the current source tree, completed `--stage-only` with exit 0. `App/Resources/web/app.js` was byte-identical before and after. The distribution pin was still `27A266a`.

## Revert

Restore `scripts/sync-web.sh` and `scripts/web-edition/` from the archive above and delete `.build-sync/`. Do not delete `App/Resources/web` after `--stage-only`: that command does not replace it.
