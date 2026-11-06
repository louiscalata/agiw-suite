# Isolated wrapper patch

Apply this only to a copy of the sanitized App Store tree. Do not apply it to the dirty checkout, and do not add a git remote.

## Preconditions

- The copy is the sanitized tree whose archive SHA-256 is `68db1c1d68f0d929d46c9aefe96801b385eec3959f1764dfbbd968a3d3fe4e2f`.
- That tree is HEAD `317aad2f10ac26d3626ab767af73ecdab1c2e326` and has no git remote.
- `scripts/appstore-distribution.sh` still pins Xcode `27A266a`. This patch does not change that file and does not add a skip.
- `scripts/web-edition/feature-denylist.txt` stays byte-for-byte. Its SHA-256 is `03378746a130f9217b679a475a14ac14236c53e90c404c3fd3a5c426d0b761f9`.
- `scripts/web-edition/engine.mjs` stays the archive copy. Admission still fails closed on a missing statement, a wrong anchor count, or a pinned statement whose text hash differs.
- The nine `scripts/sync-web.sh` inputs stay, including `web/simple-view.mjs` and `web/simple-view.css`.
- Node 18 or later is on `PATH`.

## Apply

From the isolated copy, replace the sync script and the edition directory, then check the shell script:

```sh
cp /path/to/wrapper-patch/sync-web.sh scripts/sync-web.sh
rm -rf scripts/web-edition
cp -a /path/to/wrapper-patch/web-edition scripts/web-edition
bash -n scripts/sync-web.sh
```

Then, with the current Jaryo checkout as `SOURCE_TREE`:

```sh
bash scripts/sync-web.sh --stage-only "$SOURCE_TREE"
```

`--stage-only` returns after the edition build and tests, before the first `mv` into `App/Resources/web`. A dirty Resources directory can stay in place. The stage is `.build-sync/stage-<stamp>/`.

`--commit REV` resolves the full commit and accepts only blob modes `100644` and `100755`. Missing or non-regular paths are reported together and the script exits 1.

## What the migration does

`jaryo-adapt.mjs` keeps the removal policy and retargets it at the current Jaryo sources.

- Statement pins that still describe the current text stay. Pins whose upstream text changed are literals in `PINS`. They are not hashed from the file at build time. A one-character drift in `vitalsInput` is refused.
- Rules whose targets are gone are removed. No dummy elements are added so an old id can be deleted.
- The compact face is `web/simple-view.mjs`. It shows three rows, asks before unload, and posts `open-runtime-app` through the existing native bridge. It does not fetch a control `POST` while the confirmation is only open.
- Store output drops the private panels, the Jaryo multi-page nav, and modules the eight-file ship list does not include. `simple-view.mjs` and `simple-view.css` stay on that list.
- The sanitized archive has no DemoGolden fixture, so the old bundled page tests skip until that fixture is present. `test_jaryo_admission.mjs` is the gate that runs here: denylist-clean output, private ids absent, exact pins, missing file refused, a private word refused, and the compact-face behavior.

A measured stage of this tree exited 0. `App/Resources/web` was not replaced. The rules digest recorded in that stage was `665df4d2d5d06af0`.

## Revert

```sh
git checkout -- scripts/sync-web.sh scripts/web-edition
```

If the isolated copy has no usable git object for those paths, restore them from the archive above and delete `.build-sync/`. Do not delete `App/Resources/web` if this patch was used with `--stage-only`: that command does not replace it.
