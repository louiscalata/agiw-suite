#!/bin/sh
# Boots an API 35 emulator, runs the instrumentation tests, then checks a tall
# window and a 720x748 window. The square window is not a Galaxy Z Flip6 cover.
set -eu
ANDROID_HOME="${ANDROID_HOME:-$HOME/android-sdk}"
export ANDROID_HOME
export ANDROID_SDK_ROOT="$ANDROID_HOME"
# Keep the AVD next to the user home. A runner that also sets ANDROID_SDK_ROOT
# would otherwise leave the ini under a different tree.
export ANDROID_USER_HOME="${ANDROID_USER_HOME:-$HOME/.android}"
export ANDROID_AVD_HOME="${ANDROID_AVD_HOME:-$ANDROID_USER_HOME/avd}"
export ANDROID_SDK_HOME="$HOME"
mkdir -p "$ANDROID_AVD_HOME"
export PATH="$ANDROID_HOME/cmdline-tools/latest/bin:$ANDROID_HOME/platform-tools:$ANDROID_HOME/emulator:$PATH"
android_root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
cd "$android_root"

echo no | avdmanager create avd --name agiw --package "system-images;android-35;google_apis;x86_64" --force
config="$ANDROID_AVD_HOME/agiw.avd/config.ini"
if [ ! -f "$config" ]; then
    config=$(find "$HOME" "$ANDROID_HOME" -path '*/agiw.avd/config.ini' 2>/dev/null | head -1 || true)
fi
if [ -z "$config" ] || [ ! -f "$config" ]; then
    echo "avdmanager did not write agiw.avd/config.ini" >&2
    find "$HOME" "$ANDROID_HOME" -type d -name '*.avd' 2>/dev/null | head
    exit 1
fi
printf '\nshowDeviceFrame=no\nskin.dynamic=yes\nhw.lcd.width=1080\nhw.lcd.height=2400\nhw.lcd.density=420\nhw.keyboard=no\n' >> "$config"

if [ ! -x "$ANDROID_HOME/emulator/emulator" ]; then
    echo "emulator binary is missing under $ANDROID_HOME/emulator" >&2
    exit 1
fi
if [ -e /dev/kvm ] && [ ! -w /dev/kvm ]; then
    sudo chmod 666 /dev/kvm || true
fi
if [ -w /dev/kvm ]; then
    accel=on
    boot_tries=150
else
    accel=off
    boot_tries=400
fi
"$ANDROID_HOME/emulator/emulator" -avd agiw -no-window -no-audio -no-boot-anim -no-snapshot -no-metrics -gpu swiftshader_indirect -accel "$accel" -memory 2048 -port 5554 >/tmp/agiw-emulator.log 2>&1 &
pid=$!
trap 'kill "$pid" 2>/dev/null || true' EXIT

registered=0
i=0
while [ "$i" -lt 90 ]; do
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "emulator process exited before adb saw it" >&2
        tail -n 120 /tmp/agiw-emulator.log || true
        exit 1
    fi
    if adb devices | tr -d '\r' | grep -q '^emulator-'; then
        registered=1
        break
    fi
    i=$((i + 1))
    sleep 2
done
if [ "$registered" != "1" ]; then
    echo "emulator did not register with adb" >&2
    tail -n 120 /tmp/agiw-emulator.log || true
    exit 1
fi

booted=0
i=0
while [ "$i" -lt "$boot_tries" ]; do
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "emulator process exited during boot" >&2
        tail -n 120 /tmp/agiw-emulator.log || true
        exit 1
    fi
    boot=$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')
    if [ "$boot" = "1" ]; then
        booted=1
        break
    fi
    i=$((i + 1))
    sleep 2
done
if [ "$booted" != "1" ]; then
    echo "emulator did not finish booting" >&2
    tail -n 120 /tmp/agiw-emulator.log || true
    exit 1
fi

adb shell settings put global window_animation_scale 0
adb shell settings put global transition_animation_scale 0
adb shell settings put global animator_duration_scale 0

./gradlew :app:connectedDebugAndroidTest --no-daemon
# The test task removes the APK when it finishes. Put the debug build back for the window checks.
adb install -r app/build/outputs/apk/debug/app-debug.apk

launch() {
    adb shell am force-stop suite.agiw.monitor
    adb shell am start -n suite.agiw.monitor/.MonitorActivity >/dev/null
    sleep 2
}

dump_ui() {
    adb shell uiautomator dump /sdcard/uidump.xml >/dev/null
    adb exec-out cat /sdcard/uidump.xml
}

echo "inner window"
adb shell wm size 1080x2640
adb shell wm density 420
launch
inner=$(dump_ui)
printf '%s\n' "$inner" | grep -q 'suite.agiw.monitor:id/address'
printf '%s\n' "$inner" | grep -q 'suite.agiw.monitor:id/read'
printf '%s\n' "$inner" | grep -q 'content-desc="Read the observer snapshot"'
printf '%s\n' "$inner" | grep -q 'AGIW'

echo "cover-sized window"
adb shell wm size 720x748
adb shell wm density 306
launch
cover=$(dump_ui)
if printf '%s\n' "$cover" | grep -q 'suite.agiw.monitor:id/address'; then
    echo "a 720x748 window still shows the address field"
    exit 1
fi
printf '%s\n' "$cover" | grep -q 'suite.agiw.monitor:id/read'
printf '%s\n' "$cover" | grep -q 'content-desc="Read the observer snapshot"'
printf '%s\n' "$cover" | grep -q 'AGIW'

echo "night mode"
adb shell cmd uimode night yes
launch
adb shell pidof suite.agiw.monitor >/dev/null
night=$(dump_ui)
printf '%s\n' "$night" | grep -q 'AGIW'

echo "font scale"
adb shell settings put system font_scale 1.3
launch
adb shell pidof suite.agiw.monitor >/dev/null
scaled=$(dump_ui)
printf '%s\n' "$scaled" | grep -q 'suite.agiw.monitor:id/process'

echo "emulator smoke passed"
echo "physical Galaxy Z Flip6: NOT_RUN"
echo "TalkBack service: NOT_RUN; content-desc was read from the accessibility node dump"
