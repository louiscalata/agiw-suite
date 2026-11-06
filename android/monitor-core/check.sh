#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT
find "$root/src/main/java" "$root/src/test/java" -name '*.java' > "$out/sources"
javac --release 17 -d "$out/classes" @"$out/sources"
java -cp "$out/classes" suite.agiw.monitor.OverviewTest
java -cp "$out/classes" suite.agiw.monitor.ClientTest
python3 - "$root/../app/src/main/res/layout" <<'PY'
import sys
from pathlib import Path
import xml.etree.ElementTree as ET
layout = Path(sys.argv[1])
required = {"read", "schematic", "process", "lane", "attachment", "connect", "acceptance", "observed", "detail"}

def ids(path):
    root = ET.parse(path).getroot()
    found = []
    for node in root.iter():
        for key, value in node.attrib.items():
            if key.endswith("id") and value.startswith("@+id/"):
                found.append(value.split("/", 1)[1])
    return found

def check(path, extra, banned):
    found = ids(path)
    if len(found) != len(set(found)):
        raise SystemExit(f"duplicate id in {path.name}")
    missing = required - set(found)
    if missing:
        raise SystemExit(f"{path.name} missing {sorted(missing)}")
    for name in extra:
        if name not in found:
            raise SystemExit(f"{path.name} missing {name}")
    for name in banned:
        if name in found:
            raise SystemExit(f"{path.name} must not include {name}")

check(layout / "activity_inner.xml", {"address"}, set())
check(layout / "activity_cover.xml", set(), {"address"})
print("android layouts: ids checked")
PY
