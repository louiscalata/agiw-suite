#!/bin/bash
# Syncs the shared web UI into App/Resources/web as the App Store edition, and records provenance in
# App/Resources/web-SOURCES.txt (next to the folder, so it is tracked but never bundled into the app).
#
#   scripts/sync-web.sh [--commit REV] [SOURCE_TREE]
#
# SOURCE_TREE is the monitor tree that holds web/ and usage-format.mjs. It is taken from the argument, else
# $AGIW_WEB_SOURCE, else the first line of .private-notes/web-source.path (gitignored), so no private path
# is ever written into a tracked file. The source tree is only read: git runs with --no-optional-locks so
# not even its index is refreshed.
#
# Default: copy the working tree (web-SOURCES.txt then says how many synced files differ from the commit).
# --commit REV: extract the files from that commit instead (release rule: extract from a commit).
#
# The upstream files are never shipped as they are. scripts/web-edition/build.mjs removes the private edition's
# panels and their code (appstore-rules.mjs; guideline 2.3.1: nothing hidden or dormant), then
# scripts/web-edition/test_web_edition.mjs runs the result. Only when both pass is App/Resources/web replaced.
# Needs node (18 or later). Nothing is deleted: the stage and the previous bundle stay in .build-sync/
# (gitignored scratch; clear it by hand).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REV=""
SRC=""
STAGE_ONLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --commit) REV="${2:?--commit needs a revision}"; shift 2 ;;
    --stage-only) STAGE_ONLY=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) SRC="$1"; shift ;;
  esac
done
if [[ -z "$SRC" ]]; then SRC="${AGIW_WEB_SOURCE:-}"; fi
if [[ -z "$SRC" && -f "$ROOT/.private-notes/web-source.path" ]]; then
  SRC="$(head -n 1 "$ROOT/.private-notes/web-source.path")"
fi
if [[ -z "$SRC" || ! -d "$SRC" ]]; then
  echo "sync-web: no source tree (pass it, set AGIW_WEB_SOURCE, or write .private-notes/web-source.path)" >&2
  exit 2
fi
if [[ -n "$REV" ]] && ! git --no-optional-locks -C "$SRC" cat-file -e "$REV^{commit}" 2>/dev/null; then
  echo "sync-web: source checkout does not contain commit $REV; update the ignored web-source.path or pass a checkout containing it" >&2
  exit 2
fi
NODE="$(command -v node || true)"
[[ -n "$NODE" ]] || { echo "sync-web: node not found; the App Store edition transform needs it" >&2; exit 2; }

# Source path (relative to SOURCE_TREE) and bundled name, in load order.
SOURCES=(web/index.html web/style.css web/app.js usage-format.mjs web/online-code-mode.mjs web/map-layout.mjs
         web/model-control-view.mjs web/simple-view.mjs web/simple-view.css web/monitor-store.mjs web/fleet-state.mjs)

DEST="$ROOT/App/Resources/web"
STAMP="$(date -u +%Y%m%dT%H%M%SZ).$$"
STAGE="$ROOT/.build-sync/stage-$STAMP"
mkdir -p "$STAGE/upstream"

git_ro() { git --no-optional-locks -C "$SRC" "$@"; }

if [[ -n "$REV" ]]; then
  FULL="$(git_ro rev-parse "$REV^{commit}")"
  bad=()
  for rel in "${SOURCES[@]}"; do
    line="$(git_ro ls-tree "$FULL" -- "$rel")"
    mode="${line%% *}"
    if [[ "$mode" != "100644" && "$mode" != "100755" ]]; then
      bad+=("$rel")
    fi
  done
  if [[ ${#bad[@]} -gt 0 ]]; then
    printf 'sync-web: not a regular blob:\n' >&2
    printf '  %s\n' "${bad[@]}" >&2
    exit 1
  fi
  REVISION="$FULL"
  ORIGIN="commit $REVISION"
elif [[ "$(git_ro rev-parse --show-toplevel 2>/dev/null || true)" != "$(cd "$SRC" && pwd -P)" ]]; then
  ORIGIN="a snapshot directory, not a git tree (each file's upstream sha256 is below)"
else
  REVISION="$(git_ro rev-parse --short=9 HEAD 2>/dev/null || echo unknown)"
  CHANGED="$(git_ro status --porcelain -- "${SOURCES[@]}" 2>/dev/null | wc -l | tr -d ' ')"
  ORIGIN="working tree at $REVISION; $CHANGED of ${#SOURCES[@]} synced files differ from that commit"
fi

for rel in "${SOURCES[@]}"; do
  name="$(basename "$rel")"
  if [[ -n "$REV" ]]; then
    git_ro show "$FULL:./$rel" > "$STAGE/upstream/$name"
  else
    if [[ -L "$SRC/$rel" || ! -f "$SRC/$rel" ]]; then
      echo "sync-web: $rel is missing or not a regular file" >&2
      exit 1
    fi
    cp "$SRC/$rel" "$STAGE/upstream/$name"
  fi
done

echo "== App Store edition (scripts/web-edition/build.mjs)"
"$NODE" "$ROOT/scripts/web-edition/build.mjs" "$STAGE/upstream" "$STAGE/web"
echo "== edition tests (scripts/web-edition/test_web_edition.mjs test_jaryo_admission.mjs)"
AGIW_WEB_DIR="$STAGE/web" AGIW_WEB_UPSTREAM="$STAGE/upstream" "$NODE" --test --test-reporter=dot \
  "$ROOT/scripts/web-edition/test_web_edition.mjs" \
  "$ROOT/scripts/web-edition/test_jaryo_admission.mjs"

RULES_SUM="$(cat "$ROOT/scripts/web-edition/engine.mjs" "$ROOT/scripts/web-edition/appstore-rules.mjs" \
  "$ROOT/scripts/web-edition/appstore-overrides.mjs" "$ROOT/scripts/web-edition/jaryo-adapt.mjs" \
  "$ROOT/scripts/web-edition/feature-denylist.txt" | shasum -a 256 | cut -c1-16)"
{
  echo "# Shared web UI synced into the App Store edition by scripts/sync-web.sh. Do not edit these files here:"
  echo "# change the shared UI upstream (or the edition rules in scripts/web-edition/), then re-sync."
  echo "# Paths are relative to the source monitor tree."
  echo "# source: $ORIGIN"
  echo "# edition: appstore, rules $RULES_SUM (sha256 of engine.mjs + appstore-rules.mjs + appstore-overrides.mjs + jaryo-adapt.mjs + feature-denylist.txt)"
  echo "# synced-at: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "# upstream-sha256  bundled-name  source-path  bundled-sha256 (not-shipped: left out of the edition)"
} > "$STAGE/web-SOURCES.txt"
for rel in "${SOURCES[@]}"; do
  name="$(basename "$rel")"
  up="$(shasum -a 256 "$STAGE/upstream/$name" | cut -d ' ' -f 1)"
  if [[ -f "$STAGE/web/$name" ]]; then
    out="$(shasum -a 256 "$STAGE/web/$name" | cut -d ' ' -f 1)"
  else
    out="not-shipped"
  fi
  echo "$up  $name  $rel  $out" >> "$STAGE/web-SOURCES.txt"
done

if [[ "$STAGE_ONLY" == 1 ]]; then
  echo "sync-web: stage-only $STAGE (App/Resources/web was not replaced)"
  exit 0
fi
mkdir -p "$(dirname "$DEST")"
if [[ -e "$DEST" ]]; then mv "$DEST" "$ROOT/.build-sync/previous-$STAMP"; fi
mv "$STAGE/web" "$DEST"
mv "$STAGE/web-SOURCES.txt" "$ROOT/App/Resources/web-SOURCES.txt"
echo "sync-web: $(ls "$DEST" | wc -l | tr -d ' ') edition files -> App/Resources/web ($ORIGIN)"
