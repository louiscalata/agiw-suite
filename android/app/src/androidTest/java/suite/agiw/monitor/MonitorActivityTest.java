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
