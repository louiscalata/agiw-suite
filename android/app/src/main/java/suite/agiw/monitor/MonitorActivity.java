package suite.agiw.monitor;

import android.app.Activity;
import android.content.res.Configuration;
import android.graphics.Color;
import android.graphics.Insets;
import android.graphics.Rect;
import android.graphics.drawable.ColorDrawable;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.view.View;
import android.view.ViewTreeObserver;
import android.view.Window;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import androidx.core.util.Consumer;
import androidx.window.java.layout.WindowInfoTrackerCallbackAdapter;
import androidx.window.layout.DisplayFeature;
import androidx.window.layout.FoldingFeature;
import androidx.window.layout.WindowInfoTracker;
import androidx.window.layout.WindowLayoutInfo;
import java.net.URI;
import java.time.Instant;
import java.util.concurrent.Executor;
import java.util.function.LongSupplier;

/**
 * Read-only monitor. The address is typed on the tall window. The square window
 * keeps that address and shows Read without the form. There is no default host,
 * no stored credential, and no repair control. A square window is not the Flip 6 cover.
 */
public final class MonitorActivity extends Activity {
    private static final String KEY_ADDRESS = "address";
    private static final String KEY_PROCESS = "process";
    private static final String KEY_LANE = "lane";
    private static final String KEY_ATTACH = "attach";
    private static final String KEY_RUN = "runId";
    private static final String KEY_CONNECT = "connect";
    private static final String KEY_DETAIL = "detail";
    private static final String KEY_ACCEPT = "accept";
    private static final String KEY_OBSERVED = "observed";
    private static final String KEY_RECEIPTS = "receiptsOpen";

    private final ObserverClient client = new ObserverClient();
    private final ReadGate gate = new ReadGate();
    private final Executor mainExecutor = command -> new Handler(Looper.getMainLooper()).post(command);
    private final Handler agingHandler = new Handler(Looper.getMainLooper());
    private final Consumer<WindowLayoutInfo> foldConsumer = this::onWindowLayout;
    private WindowInfoTrackerCallbackAdapter foldAdapter;
    private boolean cover;
    private String addressText = "";
    private int inflight;
    private boolean receiptsOpen;
    private Board shown = Board.none("No address");
    LongSupplier displayClock = System::currentTimeMillis;
    private final Runnable ageDisplay = this::refreshDisplay;
    private boolean started;
    private Fold.Posture posture = Fold.Posture.FLAT;
    private int foldLeft;
    private int foldTop;
    private int foldRight;
    private int foldBottom;
    private int lastWidthDp = -1;
    private int lastHeightDp = -1;
    private int lastKeyboard = -1;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        noteConfig(getResources().getConfiguration(), false);
        edgeToEdge();
        bind(savedInstanceState);
    }

    @Override
    public void onConfigurationChanged(Configuration newConfig) {
        super.onConfigurationChanged(newConfig);
        noteConfig(newConfig, true);
        setTheme(R.style.Theme_Agiw);
        edgeToEdge();
        Bundle kept = new Bundle();
        capture(kept);
        bind(kept);
    }

    /**
     * A new window size drops the previous hinge until the next layout callback.
     * Font scale, night mode, and the keyboard keep it. The keyboard also changes
     * the reported height, so a keyboard change is not treated as a new window.
     */
    private void noteConfig(Configuration config, boolean resetOnResize) {
        boolean sizeChanged = lastWidthDp >= 0
                && (config.screenWidthDp != lastWidthDp || config.screenHeightDp != lastHeightDp);
        boolean keyboardChanged = lastKeyboard >= 0 && config.keyboardHidden != lastKeyboard;
        lastWidthDp = config.screenWidthDp;
        lastHeightDp = config.screenHeightDp;
        lastKeyboard = config.keyboardHidden;
        if (resetOnResize && sizeChanged && !keyboardChanged) clearFold();
    }

    private void clearFold() {
        posture = Fold.Posture.FLAT;
        foldLeft = 0;
        foldTop = 0;
        foldRight = 0;
        foldBottom = 0;
    }

    @Override
    protected void onStart() {
        super.onStart();
        started = true;
        show(shown);
        if (foldAdapter == null) {
            foldAdapter = new WindowInfoTrackerCallbackAdapter(WindowInfoTracker.getOrCreate(this));
        }
        foldAdapter.addWindowLayoutInfoListener(this, mainExecutor, foldConsumer);
    }

    @Override
    protected void onStop() {
        started = false;
        agingHandler.removeCallbacks(ageDisplay);
        if (foldAdapter != null) foldAdapter.removeWindowLayoutInfoListener(foldConsumer);
        super.onStop();
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        capture(outState);
    }

    private void bind(Bundle state) {
        cover = compactNow();
        setContentView(cover ? R.layout.activity_cover : R.layout.activity_inner);
        markHeading(R.id.wordmark);
        applyFontScale();
        applyPostureLabel();
        applyInsets();
        float scale = Settings.Global.getFloat(getContentResolver(), Settings.Global.ANIMATOR_DURATION_SCALE, 1f);
        View rule = findViewById(R.id.schematic);
        if (rule != null) rule.setVisibility(Surface.motion(scale) ? View.VISIBLE : View.INVISIBLE);
        Board restored;
        if (state == null) restored = Board.none("No address");
        else {
            restored = Board.stored(
                    state.getString(KEY_PROCESS, "UNKNOWN"),
                    state.getString(KEY_LANE, "NOT MEASURED"),
                    state.getString(KEY_ATTACH, "NO RUN"),
                    state.getString(KEY_RUN, "not reported"),
                    state.getString(KEY_DETAIL, ""),
                    state.getString(KEY_CONNECT, "NONE"),
                    state.getString(KEY_OBSERVED, "not reported"));
            addressText = state.getString(KEY_ADDRESS, "");
            receiptsOpen = state.getBoolean(KEY_RECEIPTS, false);
            EditText address = findViewById(R.id.address);
            if (address != null) address.setText(addressText);
        }
        show(restored);
        TextView receipts = findViewById(R.id.receipts);
        if (receipts != null) receipts.setText(Acceptance.notes());
        Button read = findViewById(R.id.read);
        if (read != null) read.setOnClickListener(view -> readOnce());
        showReadButton(inflight != 0);
        EditText address = findViewById(R.id.address);
        if (address != null) {
            address.setOnFocusChangeListener((view, focused) -> {
                if (focused) reveal(view);
            });
            address.setOnEditorActionListener((view, actionId, event) -> {
                readOnce();
                return true;
            });
        }
        Button receiptsToggle = findViewById(R.id.receiptsToggle);
        if (receiptsToggle != null) receiptsToggle.setOnClickListener(view -> {
            receiptsOpen = !receiptsOpen;
            applyReceipts();
        });
        applyReceipts();
        if (cover) openAtTop();
        else reveal(findViewById(R.id.acceptance));
    }

    private void remember(Board board) {
        try {
            SampleStore.write(StatusWidget.file(this), board, System.currentTimeMillis());
        } catch (java.io.IOException ignored) {
            return;
        }
        StatusWidget.publish(this);
    }

    private boolean compactNow() {
        int width = getResources().getDisplayMetrics().widthPixels;
        int height = getResources().getDisplayMetrics().heightPixels;
        return Surface.compact(width, height);
    }

    private void capture(Bundle outState) {
        EditText address = findViewById(R.id.address);
        if (address != null) addressText = address.getText().toString();
        outState.putString(KEY_ADDRESS, addressText);
        outState.putString(KEY_PROCESS, shown.process);
        outState.putString(KEY_LANE, shown.lane);
        outState.putString(KEY_ATTACH, shown.attachment);
        outState.putString(KEY_RUN, shown.runId);
        outState.putString(KEY_CONNECT, shown.connect);
        outState.putString(KEY_DETAIL, shown.detail);
        outState.putString(KEY_ACCEPT, shown.acceptance);
        outState.putString(KEY_OBSERVED, shown.observed);
        outState.putBoolean(KEY_RECEIPTS, receiptsOpen);
    }

    private void readOnce() {
        if (inflight != 0) {
            gate.cancel();
            inflight = 0;
            showReadButton(false);
            show(Board.none("Read cancelled"));
            remember(Board.none("Read cancelled"));
            return;
        }
        EditText address = findViewById(R.id.address);
        if (address != null) addressText = address.getText().toString().trim();
        String raw = addressText.trim();
        if (raw.isEmpty()) {
            show(Board.none("No address"));
            return;
        }
        URI uri;
        try {
            uri = URI.create(raw);
        } catch (IllegalArgumentException ex) {
            show(Board.refused("Address was not read"));
            return;
        }
        int token = gate.begin();
        inflight = token;
        showReadButton(true);
        new Thread(() -> {
            Board board;
            try {
                board = Board.from(client.getSnapshot(uri, 4000), Instant.now());
            } catch (RuntimeException ex) {
                board = Board.disconnected("Observer did not answer");
            }
            Board shown = board;
            runOnUiThread(() -> {
                if (isDestroyed() || !gate.accepts(token)) return;
                inflight = 0;
                show(shown);
                remember(shown);
                showReadButton(false);
            });
        }, "agiw-observer").start();
    }

    void refreshDisplay() {
        show(shown);
    }

    void show(Board board) {
        long now = displayClock.getAsLong();
        board = DisplayedSample.present(board, now, "Last sample is old. Read again to refresh.", false);
        shown = board;
        agingHandler.removeCallbacks(ageDisplay);
        long expires = DisplayedSample.expiresAt(board, now);
        if (started && expires >= 0) agingHandler.postDelayed(ageDisplay, expires - now);
        set(R.id.process, board.process);
        set(R.id.lane, Face.lane(cover, board));
        set(R.id.attachment, Face.attachment(cover, board));
        set(R.id.connect, Face.connect(cover, board));
        set(R.id.observed, board.observed);
        set(R.id.detail, board.detail);
        set(R.id.acceptance, board.acceptance);
        if (!cover) reveal(findViewById(R.id.acceptance));
    }

    private void applyReceipts() {
        TextView receipts = findViewById(R.id.receipts);
        Button toggle = findViewById(R.id.receiptsToggle);
        if (cover) {
            if (receipts != null) receipts.setVisibility(View.GONE);
            if (toggle != null) toggle.setVisibility(View.GONE);
            return;
        }
        if (receipts != null) receipts.setVisibility(receiptsOpen ? View.VISIBLE : View.GONE);
        if (toggle != null) {
            toggle.setText(getString(receiptsOpen ? R.string.receipts_hide : R.string.receipts));
            toggle.setContentDescription(getString(receiptsOpen
                    ? R.string.receipts_hide_description
                    : R.string.receipts_show_description));
        }
    }

    private void showReadButton(boolean cancel) {
        Button read = findViewById(R.id.read);
        if (read == null) return;
        read.setText(getString(cancel ? R.string.cancel : R.string.read));
        read.setContentDescription(getString(cancel
                ? R.string.cancel_description
                : R.string.read_description));
    }

    private void edgeToEdge() {
        Window window = getWindow();
        window.setBackgroundDrawable(new ColorDrawable(getColor(R.color.agiw_bg)));
        if (Build.VERSION.SDK_INT >= 30) window.setDecorFitsSystemWindows(false);
        if (Build.VERSION.SDK_INT >= 28) {
            window.setNavigationBarDividerColor(Color.TRANSPARENT);
            WindowManager.LayoutParams attrs = window.getAttributes();
            attrs.layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;
            window.setAttributes(attrs);
        }
        if (Build.VERSION.SDK_INT >= 29) {
            window.setStatusBarContrastEnforced(false);
            window.setNavigationBarContrastEnforced(false);
            window.getDecorView().setForceDarkAllowed(false);
        }
        boolean night = (getResources().getConfiguration().uiMode & Configuration.UI_MODE_NIGHT_MASK)
                == Configuration.UI_MODE_NIGHT_YES;
        if (Build.VERSION.SDK_INT >= 30) {
            WindowInsetsController controller = window.getInsetsController();
            if (controller == null) return;
            int light = WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS
                    | WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS;
            controller.setSystemBarsAppearance(night ? 0 : light, light);
            return;
        }
        if (Build.VERSION.SDK_INT >= 27) {
            View decor = window.getDecorView();
            int flags = decor.getSystemUiVisibility();
            if (night) flags &= ~View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR;
            else flags |= View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR;
            decor.setSystemUiVisibility(flags);
        }
    }

    /**
     * Target SDK 35 draws edge to edge, and fitsSystemWindows replaces padding
     * with the raw system insets. Side insets are often zero, which drops the
     * Jaryo margin. Keep a 4dp-rhythm inset (16dp inner, 12dp cover) and add
     * the bars, cutout, and keyboard on top of it.
     */
    private void applyInsets() {
        ScrollView scroller = findViewById(R.id.scroller);
        if (scroller == null) return;
        final int side = dp(cover ? 12 : 16);
        final int top = dp(cover ? 16 : 24);
        final int bottom = dp(24);
        final boolean folded = foldLeft != 0 || foldTop != 0 || foldRight != 0 || foldBottom != 0;
        // A flat page may draw into the Jaryo margin. A hinge pad is the crease,
        // and clipToPadding false would paint the rows through it.
        scroller.setClipToPadding(folded);
        scroller.setPadding(side, top, side, bottom);
        scroller.setOnApplyWindowInsetsListener((view, insets) -> {
            int left = side;
            int padTop = top;
            int right = side;
            int padBottom = bottom;
            ((ScrollView) view).setClipToPadding(folded);
            if (Build.VERSION.SDK_INT >= 30) {
                Insets bars = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
                Insets ime = insets.getInsets(WindowInsets.Type.ime());
                left += bars.left + foldLeft;
                padTop += bars.top + foldTop;
                right += bars.right + foldRight;
                padBottom += Math.max(Math.max(bars.bottom, ime.bottom), foldBottom);
                view.setPadding(left, padTop, right, padBottom);
                return WindowInsets.CONSUMED;
            }
            left += insets.getSystemWindowInsetLeft() + foldLeft;
            padTop += insets.getSystemWindowInsetTop() + foldTop;
            right += insets.getSystemWindowInsetRight() + foldRight;
            padBottom += Math.max(insets.getSystemWindowInsetBottom(), foldBottom);
            view.setPadding(left, padTop, right, padBottom);
            return insets;
        });
        scroller.requestApplyInsets();
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    /**
     * FoldingFeature bounds are in window coordinates. DisplayMetrics pixels
     * are the whole panel, which is larger than the window in multi-window.
     */
    private int[] windowPx() {
        if (Build.VERSION.SDK_INT >= 30) {
            Rect bounds = getWindowManager().getCurrentWindowMetrics().getBounds();
            return new int[] {bounds.width(), bounds.height()};
        }
        View decor = getWindow().getDecorView();
        return new int[] {decor.getWidth(), decor.getHeight()};
    }

    /**
     * The short window used to scroll the acceptance line into the padded
     * viewport, which lands on the bottom of that screen. Open at the top.
     * A later read does not pull the scroll position back.
     */
    private void openAtTop() {
        ScrollView scroller = findViewById(R.id.scroller);
        if (scroller == null) return;
        scroller.scrollTo(0, 0);
        scroller.post(() -> scroller.scrollTo(0, 0));
    }

    private void reveal(View child) {
        ScrollView scroller = findViewById(R.id.scroller);
        if (child == null || scroller == null) return;
        ViewTreeObserver observer = scroller.getViewTreeObserver();
        observer.addOnGlobalLayoutListener(new ViewTreeObserver.OnGlobalLayoutListener() {
            @Override
            public void onGlobalLayout() {
                ViewTreeObserver current = scroller.getViewTreeObserver();
                if (current.isAlive()) current.removeOnGlobalLayoutListener(this);
                place(scroller, child);
                scroller.post(() -> place(scroller, child));
            }
        });
    }

    private void place(ScrollView scroller, View child) {
        if (child.getHeight() == 0 || scroller.getHeight() == 0) return;
        int contentTop = child.getTop();
        if (child.getParent() instanceof View parent && parent != scroller) contentTop += parent.getTop();
        int contentBottom = contentTop + child.getHeight();
        if (Surface.fullyVisible(contentTop, contentBottom, scroller.getHeight(),
                scroller.getPaddingTop(), scroller.getPaddingBottom(), scroller.getScrollY())) {
            return;
        }
        int visible = scroller.getHeight() - scroller.getPaddingTop() - scroller.getPaddingBottom();
        scroller.scrollTo(0, Math.max(0, contentBottom - visible));
    }

    private void set(int id, String value) {
        TextView view = findViewById(id);
        if (view == null) return;
        String shown = value == null ? "" : value;
        view.setText(shown);
        int template = descriptionFor(id);
        if (template != 0) view.setContentDescription(getString(template, shown));
    }

    private int descriptionFor(int id) {
        if (id == R.id.process) return R.string.desc_process;
        if (id == R.id.detail) return R.string.desc_detail;
        if (id == R.id.acceptance) return R.string.desc_acceptance;
        if (cover) return 0;
        if (id == R.id.lane) return R.string.desc_lane;
        if (id == R.id.attachment) return R.string.desc_attach;
        if (id == R.id.connect) return R.string.desc_connect;
        if (id == R.id.observed) return R.string.desc_time;
        return 0;
    }

    private void markHeading(int id) {
        View view = findViewById(id);
        if (view != null && Build.VERSION.SDK_INT >= 28) view.setAccessibilityHeading(true);
    }

    private void applyFontScale() {
        if (!Surface.stackRows(getResources().getConfiguration().fontScale)) return;
        int[] rows = {R.id.rowProcess, R.id.rowLane, R.id.rowAttach, R.id.rowConnect, R.id.rowTime};
        for (int id : rows) {
            View view = findViewById(id);
            if (!(view instanceof LinearLayout row)) continue;
            row.setOrientation(LinearLayout.VERTICAL);
            for (int i = 0; i < row.getChildCount(); i++) {
                View child = row.getChildAt(i);
                child.setLayoutParams(new LinearLayout.LayoutParams(
                        LinearLayout.LayoutParams.MATCH_PARENT,
                        LinearLayout.LayoutParams.WRAP_CONTENT));
            }
        }
    }

    private void onWindowLayout(WindowLayoutInfo info) {
        FoldingFeature hinge = null;
        for (DisplayFeature feature : info.getDisplayFeatures()) {
            if (feature instanceof FoldingFeature fold) {
                hinge = fold;
                break;
            }
        }
        if (hinge == null) {
            posture = Fold.Posture.FLAT;
            foldLeft = 0;
            foldTop = 0;
            foldRight = 0;
            foldBottom = 0;
        } else {
            boolean half = hinge.getState() == FoldingFeature.State.HALF_OPENED;
            boolean horizontal = hinge.getOrientation() == FoldingFeature.Orientation.HORIZONTAL;
            posture = Fold.posture(true, half, horizontal);
            int[] window = windowPx();
            int width = window[0];
            int height = window[1];
            int[] extra = Fold.avoid(width, height,
                    hinge.getBounds().left, hinge.getBounds().top,
                    hinge.getBounds().right, hinge.getBounds().bottom, posture);
            foldLeft = extra[0];
            foldTop = extra[1];
            foldRight = extra[2];
            foldBottom = extra[3];
        }
        if (findViewById(R.id.scroller) == null) return;
        applyPostureLabel();
        applyInsets();
    }

    private void applyPostureLabel() {
        TextView view = findViewById(R.id.posture);
        if (view == null) return;
        String label = switch (posture) {
            case FLAT -> null;
            case TABLETOP -> getString(R.string.posture_flex);
            case BOOK -> getString(R.string.posture_book);
        };
        String description = switch (posture) {
            case FLAT -> null;
            case TABLETOP -> getString(R.string.posture_flex_description);
            case BOOK -> getString(R.string.posture_book_description);
        };
        if (label == null) {
            view.setVisibility(View.GONE);
            return;
        }
        view.setVisibility(View.VISIBLE);
        view.setText(label);
        view.setContentDescription(description);
    }
}
