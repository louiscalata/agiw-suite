# Isolated wrapper patch

Apply this only to a copy of `~/agiw-suite-appstore`. Do not apply it to the dirty checkout, and do not add a git remote.

## Preconditions

- The copy is the sanitized tree whose archive SHA-256 is `68db1c1d68f0d929d46c9aefe96801b385eec3959f1764dfbbd968a3d3fe4e2f`.
- That tree is HEAD `317aad2f10ac26d3626ab767af73ecdab1c2e326` and has no git remote.
- `scripts/appstore-distribution.sh` still pins Xcode `27A266a`. This patch does not change that file and does not add a skip.
- The nine `scripts/sync-web.sh` inputs stay, including `web/simple-view.mjs` and `web/simple-view.css`. An empty stand-in is not a successor.
- Node 18 or later is on `PATH`. `shasum` is the checksum tool the script already uses.

## Apply

From the isolated copy:

```sh
cp /path/to/wrapper-patch/sync-web.sh scripts/sync-web.sh
bash -n scripts/sync-web.sh
```

Then, with the current Jaryo checkout as `SOURCE_TREE`:

```sh
scripts/sync-web.sh --stage-only "$SOURCE_TREE"
```

`--stage-only` returns after the edition build and tests, before the first `mv` into `App/Resources/web`. A dirty Resources directory can stay in place. The stage is `.build-sync/stage-<stamp>/`.

`--commit REV` resolves the full commit and accepts only blob modes `100644` and `100755`. Missing or non-regular paths are reported together and the script exits 1.

## What this patch does not do

A run of the unchanged edition rules against the current Jaryo web fails closed. The first measured failures are missing `simple-view.mjs` and `simple-view.css`, missing old ids (`inferenceReceipts`, `monitorConnectionSwitches`), and `app.js` statement pins that no longer match (`function:vitalsInput` is now `74659ef90805`, the rule still pins `1a4a58ac1fbd`). Those pins were not bulk-updated. Replacing them without reading each statement would weaken the App Store removal rules.

## Revert

```sh
git checkout -- scripts/sync-web.sh
```

If the isolated copy has no usable git object for that path, restore `scripts/sync-web.sh` from the archive above and delete `.build-sync/`. Do not delete `App/Resources/web` if this patch was used with `--stage-only`: that command does not replace it.
