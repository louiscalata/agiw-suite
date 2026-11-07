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
for name in ("activity_inner.xml", "activity_cover.xml"):
    text = (layout / name).read_text()
    for needle in ('android:id="@+id/scroller"', 'android:clipToPadding="true"', 'android:fitsSystemWindows="true"',
                   'android:paddingBottom="24dp"', 'android:isScrollContainer="true"', "<ScrollView"):
        if needle not in text:
            raise SystemExit(f"{name} missing {needle}")
main = Path(sys.argv[1]).parents[1]
activity = main / "java/suite/agiw/monitor/MonitorActivity.java"
manifest = main / "AndroidManifest.xml"
activity_text = activity.read_text()
manifest_text = manifest.read_text()
for needle in ("gate.accepts", "Read cancelled", "adjustResize"):
    if needle not in activity_text and needle not in manifest_text:
        raise SystemExit("missing safeguard " + needle)
if "gate.accepts" not in activity_text or "Read cancelled" not in activity_text:
    raise SystemExit("activity does not keep the latest read")
if "adjustResize" not in manifest_text:
    raise SystemExit("keyboard does not resize the window")
for name in ("activity_inner.xml", "activity_cover.xml"):
    text = (layout / name).read_text()
    if "astra1103" in text or "jaryo-codex" in text:
        raise SystemExit(name + " puts receipts in the main layout")
    if "minHeight=\"48dp\"" not in text or "@font/inter_semibold" not in text:
        raise SystemExit(name + " missing type or target size")
widget = (main / "java/suite/agiw/monitor/StatusWidget.java").read_text()
samsung = (main / "res/xml/samsung_status_widget.xml").read_text()
info = (main / "res/xml/status_widget_info.xml").read_text()
if "com.samsung.android.appwidget.provider" not in manifest_text:
    raise SystemExit("manifest has no Samsung widget metadata")
if 'display="sub_screen"' not in samsung:
    raise SystemExit("Samsung metadata does not name the cover display")
if 'android:minWidth="352dp"' not in info or "keyguard" not in info:
    raise SystemExit("widget provider misses the published Flex Window size or category")
for banned in ("HttpURLConnection", "openConnection", "https://", "http://"):
    if banned in widget:
        raise SystemExit("status widget contacts a network")
if "tools:ignore" in widget or "RemoteViewLayout" in widget:
    raise SystemExit("status widget suppresses RemoteViewLayout instead of using an allowed view")
widget_layout = ET.parse(layout / "widget_status.xml").getroot()
allowed = {"LinearLayout", "FrameLayout", "RelativeLayout", "GridLayout", "TextView", "ImageView", "Button"}
tags = {node.tag.split("}", 1)[-1] for node in widget_layout.iter()}
illegal = sorted(tags - allowed)
if illegal:
    raise SystemExit("widget_status uses views RemoteViews cannot host: " + ", ".join(illegal))
if "View" in tags:
    raise SystemExit("widget_status includes android.view.View")
rule = next(node for node in widget_layout.iter() if node.attrib.get("{http://schemas.android.com/apk/res/android}id") == "@+id/widgetRule")
if rule.attrib.get("{http://schemas.android.com/apk/res/android}importantForAccessibility") != "no":
    raise SystemExit("decorative widget rule is an accessibility target")
for needle in ("setContentDescription(R.id.widgetProcess", "setContentDescription(R.id.widgetConnect",
               "setContentDescription(R.id.widgetDetail", "setContentDescription(R.id.widgetAcceptance",
               "FLAG_IMMUTABLE", "MonitorActivity.class"):
    if needle not in widget:
        raise SystemExit("status widget missing " + needle)
if "FLAG_MUTABLE" in widget or "launchDisplayId" in widget:
    raise SystemExit("status widget opens a mutable or cover-display intent")
if "getData(" in activity_text or "getStringExtra(" in activity_text:
    raise SystemExit("activity reads an intent address")
if 'cleartextTrafficPermitted="false"' not in (main / "res/xml/network_security_config.xml").read_text():
    raise SystemExit("cleartext is permitted")
if manifest_text.count('android:exported="true"') != 2:
    raise SystemExit("unexpected exported component")
if "APPWIDGET_UPDATE" not in manifest_text:
    raise SystemExit("widget has no update action")
print("android layouts: ids and clipping checked")
print("status widget: RemoteViews allowlist, descriptions, and intent checked")
PY
