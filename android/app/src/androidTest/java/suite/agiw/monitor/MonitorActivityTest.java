package suite.agiw.monitor;

import static androidx.test.espresso.Espresso.onView;
import static androidx.test.espresso.action.ViewActions.click;
import static androidx.test.espresso.action.ViewActions.replaceText;
import static androidx.test.espresso.assertion.ViewAssertions.matches;
import static androidx.test.espresso.matcher.ViewMatchers.isDisplayed;
import static androidx.test.espresso.matcher.ViewMatchers.withContentDescription;
import static androidx.test.espresso.matcher.ViewMatchers.withId;
import static androidx.test.espresso.matcher.ViewMatchers.withText;
import static org.hamcrest.Matchers.containsString;
import static org.junit.Assert.assertEquals;

import android.content.Context;
import android.content.res.Configuration;
import android.graphics.Color;
import android.os.SystemClock;
import android.widget.FrameLayout;
import android.widget.TextView;
import java.time.Instant;
import androidx.test.core.app.ActivityScenario;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import org.junit.Test;
import org.junit.runner.RunWith;

/**
 * Runs on a tall window, which is the inner layout. A 720 by 748 window is the
 * cover-sized smoke in android/ci/emulator-smoke.sh. Neither is a physical Flip 6.
 */
@RunWith(AndroidJUnit4.class)
public final class MonitorActivityTest {
    @Test
    public void foregroundTimerExpiresWithoutReading() {
        try (ActivityScenario<MonitorActivity> scenario = ActivityScenario.launch(MonitorActivity.class)) {
            scenario.onActivity(activity -> activity.show(idle(System.currentTimeMillis() - 29_500L)));
            SystemClock.sleep(1_500L);
            onView(withId(R.id.process)).check(matches(withText("STALE")));
            onView(withId(R.id.process)).check(matches(withContentDescription("Process STALE")));
        }
    }

    @Test
    public void displayedSampleExpiresWithoutReadingAndAfterRestoration() {
        try (ActivityScenario<MonitorActivity> scenario = ActivityScenario.launch(MonitorActivity.class)) {
            scenario.onActivity(activity -> {
                long now = System.currentTimeMillis();
                activity.displayClock = () -> now;
                activity.show(idle(now));
                assertEquals("IDLE", ((TextView) activity.findViewById(R.id.process)).getText().toString());
                activity.displayClock = () -> now + 30_001L;
                activity.refreshDisplay();
                assertEquals("STALE", ((TextView) activity.findViewById(R.id.process)).getText().toString());
                long old = System.currentTimeMillis() - 31_000L;
                activity.displayClock = () -> old;
                activity.show(idle(old));
            });
            scenario.recreate();
            onView(withId(R.id.process)).check(matches(withText("STALE")));
        }
    }

    @Test
    public void widgetRendersExpiredWordAndAccessibility() throws Exception {
        Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
        long now = System.currentTimeMillis();
        java.nio.file.Path file = java.nio.file.Files.createTempFile(context.getCacheDir().toPath(), "aging", ".txt");
        SampleStore.write(file, idle(now), now + 29_000L);
        Board board = SampleStore.present(file, now + 30_001L);
        InstrumentationRegistry.getInstrumentation().runOnMainSync(() -> {
            android.view.View view = StatusWidget.views(context, board).apply(context, new FrameLayout(context));
            TextView process = view.findViewById(R.id.widgetProcess);
            assertEquals("STALE", process.getText().toString());
            assertEquals("STALE", process.getContentDescription().toString());
        });
        java.nio.file.Files.deleteIfExists(file);
    }

    private static Board idle(long now) {
        return Board.read("{\"schemaVersion\":1,\"observedAt\":\"" + Instant.ofEpochMilli(now)
                + "\",\"pipeline\":{\"status\":\"idle\",\"pipelines\":[]}}", Instant.ofEpochMilli(now));
    }

    @Test
    public void emptyReadKeepsTheAcceptanceLine() {
        try (ActivityScenario<MonitorActivity> scenario = ActivityScenario.launch(MonitorActivity.class)) {
            onView(withId(R.id.wordmark)).check(matches(withText("AGIW")));
            onView(withId(R.id.address)).check(matches(isDisplayed()));
            onView(withId(R.id.read)).check(matches(withContentDescription("Read the observer snapshot")));
            onView(withId(R.id.receiptsToggle)).check(matches(withContentDescription(containsString("not a pass"))));
            onView(withId(R.id.acceptance)).check(matches(withText(containsString("ROLLBACK OPEN"))));
            onView(withId(R.id.process)).check(matches(withContentDescription("Process UNKNOWN")));
            onView(withId(R.id.read)).perform(click());
            onView(withId(R.id.detail)).check(matches(withText("No address")));
            onView(withId(R.id.connect)).check(matches(withText("NONE")));
            onView(withId(R.id.acceptance)).check(matches(isDisplayed()));
        }
    }

    @Test
    public void cleartextIsRefused() {
        try (ActivityScenario<MonitorActivity> scenario = ActivityScenario.launch(MonitorActivity.class)) {
            onView(withId(R.id.address)).perform(replaceText("http://127.0.0.1:9/"));
            onView(withId(R.id.read)).perform(click());
            waitForText(R.id.connect, "REFUSED");
            onView(withId(R.id.detail)).check(matches(withText("Cleartext is not used")));
            onView(withId(R.id.acceptance)).check(matches(withText(containsString("ROLLBACK OPEN"))));
        }
    }

    @Test
    public void unreachableHostStaysOnScreen() {
        try (ActivityScenario<MonitorActivity> scenario = ActivityScenario.launch(MonitorActivity.class)) {
            onView(withId(R.id.address)).perform(replaceText("https://127.0.0.1:9/"));
            onView(withId(R.id.read)).perform(click());
            waitForText(R.id.connect, "DISCONNECTED");
            onView(withId(R.id.detail)).check(matches(withText("Observer did not answer")));
            onView(withId(R.id.process)).check(matches(withContentDescription("Process UNKNOWN")));
            onView(withId(R.id.acceptance)).check(matches(isDisplayed()));
        }
    }

    @Test
    public void nightTokensInvertInk() {
        Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
        assertEquals(Color.BLACK, themed(context, false).getColor(R.color.agiw_ink));
        assertEquals(Color.WHITE, themed(context, false).getColor(R.color.agiw_bg));
        assertEquals(Color.WHITE, themed(context, true).getColor(R.color.agiw_ink));
        assertEquals(Color.BLACK, themed(context, true).getColor(R.color.agiw_bg));
    }

    private static Context themed(Context context, boolean night) {
        Configuration config = new Configuration(context.getResources().getConfiguration());
        int mode = night ? Configuration.UI_MODE_NIGHT_YES : Configuration.UI_MODE_NIGHT_NO;
        config.uiMode = (config.uiMode & ~Configuration.UI_MODE_NIGHT_MASK) | mode;
        return context.createConfigurationContext(config);
    }

    private static void waitForText(int id, String text) {
        long end = SystemClock.uptimeMillis() + 8_000L;
        RuntimeException last = null;
        while (SystemClock.uptimeMillis() < end) {
            try {
                onView(withId(id)).check(matches(withText(text)));
                return;
            } catch (RuntimeException ex) {
                last = ex;
                SystemClock.sleep(100);
            }
        }
        if (last != null) throw last;
        throw new AssertionError(text);
    }
}
