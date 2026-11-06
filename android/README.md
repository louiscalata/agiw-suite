# AGIW Android monitor

Read-only screen. A person can type an `https` address on the tall window and read `/api/snapshot` once. The nearly square window keeps that address in memory and shows one Read action. It does not show the address form. There is no default host, no stored credential, and no repair control. `http` is refused before a socket opens. Cleartext is off in the network security config. The desktop monitors stay on `127.0.0.1` and this module does not open them, change a firewall, or open a tunnel.

The name is the word AGIW. With no sample, the words are PROCESS UNKNOWN, LANE NOT MEASURED, ATTACH NO RUN, CONNECT NONE, TIME not reported, and the acceptance line stays blocked. Empty JSON, a non-object, a missing timestamp, and `pipeline.status` of `idle` without a `pipelines` array stay UNKNOWN. They do not become IDLE. A sample older than 30 seconds is STALE. Stale is not idle and not unknown. Nisi `needs-action` is BLOCKED. A live route wins over that blocked row. A run id without a matching analysis is UNATTACHED. The observation time is the payload string. Missing numbers stay missing. `Surface.compact` multiplies in 64-bit arithmetic so a huge window cannot overflow into a square result.

The screen is Java. A PC review proposed Kotlin and Compose, three destinations, and a cover identified by fold posture. Those proposals are not this client. Fold information is not read. A nearly square window is only the window aspect. It is not a hinge angle and it is not proof that Samsung launched the activity on the Flip 6 cover.

The debug APK from the earlier static build hashed `37b44f0f4992be2f05a29f8250c3a5e1db5f1640ade24427ea48ba2ca0ba8d2b`. A written report of that file used a final `d`. The file ended in `b`. Hash the current `app-debug.apk` again after `assembleDebug`. Linux compilation is not Mac or PC native acceptance.

## Flip 6 constraints

Sources, read 6 Nov 2026:

- Samsung UK, Flip 6 versus Flip 5: cover 3.4 inch Super AMOLED 60 Hz, 720×748; inner 6.7 inch, 2640×1080. https://www.samsung.com/uk/support/mobile-devices/what-is-the-difference-between-the-galaxy-z-flip6-and-z-flip5/
- Samsung developer note: the cover is primarily for widgets. A full app reaches it through Good Lock, Life up, MultiStar, I ♡ Galaxy Foldable, Launcher Widget. https://developer.samsung.com/sdp/blog/en/2024/01/09/best-practices-of-app-development-for-various-screen-sizespowered-by-good-lock
- One UI Labs can allow a short list of apps on the cover. That list is the phone setting, not something this repository turns on. https://www.androidcentral.com/apps-software/how-to-set-up-and-use-apps-on-the-samsung-galaxy-z-flip-6-cover-screen

This activity does not start itself on the cover. It uses the square layout only when the window is nearly square.

## Reproduce the debug APK

Tools pinned for the cloud build:

- Command-line tools `commandlinetools-linux-14742923_latest.zip` from https://dl.google.com/android/repository/commandlinetools-linux-14742923_latest.zip
- The Android Studio download page labels checksum `48833c34b761c10cb20bcd16582129395d121b27` as SHA-256. That string is 40 hex digits. `sha1sum -c` matches it. The SHA-256 of that same zip is `04453066b540409d975c676d781da1477479dde3761310f1a7eb92a1dfb15af7`.
- Gradle `8.11.1` from https://services.gradle.org/distributions/gradle-8.11.1-bin.zip checked with the published `.sha256` file.
- SDK packages `platforms;android-35` and `build-tools;35.0.0`, installed with `sdkmanager`.

```bash
export ANDROID_HOME="$HOME/android-sdk"
export PATH="$ANDROID_HOME/cmdline-tools/latest/bin:$PATH"
cd android
./gradlew assembleDebug --no-daemon
```

The debug APK is `android/app/build/outputs/apk/debug/app-debug.apk`. It is a build output, not a store package, and it is not installed on a device.
