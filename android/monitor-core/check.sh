#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT
find "$root/src/main/java" "$root/src/test/java" -name '*.java' > "$out/sources"
javac --release 17 -d "$out/classes" @"$out/sources"
java -cp "$out/classes" suite.agiw.monitor.OverviewTest
