# AGIW Android monitor

Read-only monitor. Version 1.1.0 (`versionCode` 2). One activity draws two layouts.

The tall window (`activity_inner.xml`) is the inner screen: the word AGIW, an `https` address field, Read, and labeled rows. The nearly square window (`activity_cover.xml`) is the cover screen: the same readings, with LANE, ATTACH, and CONNECT written on the lines, and no address field. Read on the cover uses the address last typed on the inner screen. Turning the window keeps the board fields (process, lane, attachment, run id, connection, detail, time) and draws them again for that layout. It stores those fields, not the prefixed line, so a cover prefix is not shown on the inner screen and is not doubled on the way back.

Receipt ids stay behind Receipts. The blocked acceptance line stays visible. `http` is refused before a socket opens. Cleartext is off in the network security config. The desktop monitors stay on `127.0.0.1` and this module does not open them, change a firewall, or open a tunnel.

A nearly square window is the window aspect (`Surface.compact`). It is not a hinge angle and it is not proof that Samsung launched the activity on a Flip 6 cover. While the activity is in the foreground it reads fold posture from `WindowInfoTracker`. A half-open horizontal hinge pads the page so the viewport ends at the crease. A half-open vertical hinge keeps the page in the wider pane. The Samsung cover host and a physical Flip 6 are **NOT_RUN**.

The name is the word AGIW, set in Inter. Inter is provisional. Louis did not choose it. The files in this module are the regular and semibold cuts, SIL Open Font License 1.1 (the copyright line and licence text ship in the APK at `assets/licenses/Inter-OFL-1.1.txt`). With no sample, the words are PROCESS UNKNOWN, LANE NOT MEASURED, ATTACH NO RUN, CONNECT NONE, TIME not reported, and the acceptance line stays blocked. Empty JSON, a non-object, a missing timestamp, and `pipeline.status` of `idle` without a `pipelines` array stay UNKNOWN. They do not become IDLE. A sample older than 30 seconds is STALE. Stale is not idle and not unknown. Nisi `needs-action` is BLOCKED. A live route wins over that blocked row. A run id without a matching analysis is UNATTACHED. The observation time is the payload string. Missing numbers stay missing. `Surface.compact` multiplies in 64-bit arithmetic so a huge window cannot overflow into a square result.

The screen is Java. A PC review proposed Kotlin and Compose and three destinations. Those proposals are not this client. Fold posture is read, as the paragraph above says.

The debug APK from the earlier static build hashed `37b44f0f4992be2f05a29f8250c3a5e1db5f1640ade24427ea48ba2ca0ba8d2b`. A written report of that file used a final `d`. The file ended in `b`. Hash the current `app-debug.apk` again after `assembleDebug`. Linux compilation is not Mac or PC native acceptance.

## Flip 6 cover widget

The square activity layout above is the short window. It is not the Samsung cover host. The cover host in this module is the read-only `StatusWidget`. It is an App Widget. It is not a cover Activity. `MonitorActivity` stays the primary activity and opens from the launcher. A nearly square window uses the short layout. That layout is the window aspect. It does not mean the activity is running on the cover.

The widget shows the last stored sample: process, connection, detail, and the blocked acceptance line. It does not send a repair, load or unload a model, or open a network connection. Its update period only rereads `last-sample.txt`. Cleartext stays off. A tap builds an immutable `PendingIntent` to `MonitorActivity` and does not set `launchDisplayId`. One UI can open that activity on the inner screen. This intent is not a claim that the activity is hosted on the cover.

Samsung metadata is `com.samsung.android.appwidget.provider` with `display="sub_screen"`. The provider is 352 by 339 dp, `resizeMode` `horizontal|vertical`, and `widgetCategory` `home_screen|keyguard`. Samsung's sample uses `keyguard` alone. `home_screen` is included so a normal launcher can host the same widget. Whether a Flip 6 cover list requires the category string to be exactly `keyguard` was not measured on a device.

The person turns the widget on under Settings, Cover screen, Widgets. Labs and Good Lock are phone settings the person can change. This app does not enable them and does not receive an entitlement from them.

Not proven:

- No physical Flip 6, and no cover host. The phone was disconnected.
- A stock emulator, including a 720×748 window, is not Samsung Flex Window.
- Nothing was installed and nothing was published to a store.
- The widget does not prove a live observer. With no stored sample it shows UNKNOWN and CONNECT NONE.

## Flip 6 constraints

Sources, read 6 Nov 2026:

- Samsung UK, Flip 6 versus Flip 5: cover 3.4 inch Super AMOLED 60 Hz, 720×748; inner 6.7 inch, 2640×1080. https://www.samsung.com/uk/support/mobile-devices/what-is-the-difference-between-the-galaxy-z-flip6-and-z-flip5/
- Samsung developer, Flex Window: the cover path for an app is an App Widget with `display="sub_screen"`, after the person enables it under Cover screen Widgets. https://developer.samsung.com/galaxy-z/flex_window.html
- Samsung developer note: the cover is primarily for widgets. A full app reaches it through Good Lock, Life up, MultiStar, I ♡ Galaxy Foldable, Launcher Widget. https://developer.samsung.com/sdp/blog/en/2024/01/09/best-practices-of-app-development-for-various-screen-sizespowered-by-good-lock
- One UI Labs can allow a short list of apps on the cover. That list is the phone setting, not something this repository turns on. https://www.androidcentral.com/apps-software/how-to-set-up-and-use-apps-on-the-samsung-galaxy-z-flip-6-cover-screen

This activity does not start itself on the cover. The square layout is used when the window is nearly square. That is not the operating system placing the activity on the cover. A stock emulator does not reproduce Labs, Good Lock, or the cover-widget list.

The read-only status widget uses the published Flex Window metadata: an `appwidget-provider` at 352 by 339 dp, `resizeMode` horizontal and vertical, `widgetCategory` `home_screen|keyguard`, and `com.samsung.android.appwidget.provider` with `display="sub_screen"`. Samsung's own sample sets the category to `keyguard` alone. This module also sets `home_screen` so a normal launcher can host the same widget. Whether a Flip 6 cover list requires the category string to be exactly `keyguard` was not measured on a device. Sources, read 6 Nov 2026: [Flex Window](https://developer.samsung.com/galaxy-z/flex_window.html) and [Develop a widget for Flex Window](https://developer.samsung.com/codelab/galaxy-z/widget-flex-window.html). Samsung's pages say the person turns the widget on under Settings, Cover screen, Widgets. This repository does not turn that setting on. The widget shows the last sample stored by a read in the activity. Its update period only rereads that file. It does not open a connection. A tap opens the activity on the normal display. `launchDisplayId` is not set to the cover.

The rule under the wordmark is a `TextView`. `RemoteViews` cannot host `android.view.View`. A lint log from the earlier `View` layout failed `lintDebug` with one `RemoteViewLayout` error. That log is not evidence for the current layout. A later `lintDebug --rerun-tasks` on the `TextView` layout finished `BUILD SUCCESSFUL` and the text report had no `RemoteViewLayout` error. The debug APK that contains this layout is the file whose SHA-256 is printed by `sha256sum` after `assembleDebug`. Re-hash it after the next widget change. A stock API 35 emulator can show the activity at 1080×2400 and at a 720×748 override. That override is not the Flip 6 cover, and this run did not place the widget on a cover host. Cover host: untested.

## Reproduce the debug APK

Tools pinned for the cloud build:

- Command-line tools `commandlinetools-linux-14742923_latest.zip` from https://dl.google.com/android/repository/commandlinetools-linux-14742923_latest.zip
- The Android Studio download page labels checksum `48833c34b761c10cb20bcd16582129395d121b27` as SHA-256. That string is 40 hex digits. `sha1sum -c` matches it. The SHA-256 of that same zip is `04453066b540409d975c676d781da1477479dde3761310f1a7eb92a1dfb15af7`.
- Gradle `8.11.1` from https://services.gradle.org/distributions/gradle-8.11.1-bin.zip checked with the published `.sha256` file.
- SDK packages `platforms;android-35` and `build-tools;35.0.0`, installed with `sdkmanager`.
- `app/build.gradle.kts` pins `buildToolsVersion = "35.0.0"`. Without the pin the Android Gradle Plugin 8.7.3 asks for build-tools 34.0.0, which is not in the list above and fails with "Failed to find Build Tools revision 34.0.0" on a machine that installed only the pinned packages.
- On macOS (Apple silicon) the same build works with OpenJDK 17 (`brew install openjdk@17`, then `JAVA_HOME=/opt/homebrew/opt/openjdk@17`). The Android command-line tools zip for macOS is a different file from the pinned Linux zip; verify it against the checksum Google publishes for it.

```bash
export ANDROID_HOME="$HOME/android-sdk"
export PATH="$ANDROID_HOME/cmdline-tools/latest/bin:$PATH"
cd android
./gradlew assembleDebug --no-daemon
```

The debug APK is `android/app/build/outputs/apk/debug/app-debug.apk`. It is a build output, not a store package, and it is not installed on a device.
