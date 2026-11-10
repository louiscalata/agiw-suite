#!/bin/sh
# Installs the Android SDK pinned in android/README.md.
# Pass --emulator, or set AGIW_SDK_EMULATOR=1, to also install the API 35 image.
set -eu
ANDROID_HOME="${ANDROID_HOME:-$HOME/android-sdk}"
export ANDROID_HOME
mkdir -p "$ANDROID_HOME/cmdline-tools"
if [ ! -x "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" ]; then
    tmp=$(mktemp -d)
    curl -fsSL -o "$tmp/cmdtools.zip" https://dl.google.com/android/repository/commandlinetools-linux-14742923_latest.zip
    echo "04453066b540409d975c676d781da1477479dde3761310f1a7eb92a1dfb15af7  $tmp/cmdtools.zip" | sha256sum -c
    unzip -q "$tmp/cmdtools.zip" -d "$tmp"
    rm -rf "$ANDROID_HOME/cmdline-tools/latest"
    mv "$tmp/cmdline-tools" "$ANDROID_HOME/cmdline-tools/latest"
    rm -rf "$tmp"
fi
export PATH="$ANDROID_HOME/cmdline-tools/latest/bin:$PATH"
yes | sdkmanager --licenses >/dev/null || true
sdkmanager "platforms;android-35" "build-tools;35.0.0" "platform-tools"
if [ "${1:-}" = "--emulator" ] || [ "${AGIW_SDK_EMULATOR:-}" = "1" ]; then
    sdkmanager "emulator" "system-images;android-35;google_apis;x86_64"
fi
echo "ANDROID_HOME=$ANDROID_HOME"
