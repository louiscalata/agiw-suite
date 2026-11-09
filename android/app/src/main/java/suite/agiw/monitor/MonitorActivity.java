package suite.agiw.monitor;

import android.app.Activity;
import android.content.res.Configuration;
import android.graphics.Color;
import android.graphics.Insets;
import android.os.Build;
import android.os.Bundle;
import android.provider.Settings;
import android.view.View;
import android.view.ViewTreeObserver;
import android.view.Window;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.EditText;
import android.widget.ScrollView;
import android.widget.TextView;
import java.net.URI;
import java.time.Instant;

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
    private static final String KEY_CONNECT = "connect";
    private static final String KEY_DETAIL = "detail";
    private static final String KEY_ACCEPT = "accept";
    private static final String KEY_OBSERVED = "observed";
    private static final String KEY_RECEIPTS = "receiptsOpen";

    private final ObserverClient client = new ObserverClient();
    private final ReadGate gate = new ReadGate();
    private boolean cover;
    private String addressText = "";
    private int inflight;
    private boolean receiptsOpen;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        edgeToEdge();
        bind(savedInstanceState);
    }

    @Override
    public void onConfigurationChanged(Configuration newConfig) {
        super.onConfigurationChanged(newConfig);
        edgeToEdge();
        Bundle kept = new Bundle();
        capture(kept);
        bind(kept);
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        capture(outState);
    }

    private void bind(Bundle state) {
        cover = compactNow();
        setContentView(cover ? R.layout.activity_cover : R.layout.activity_inner);
        applyInsets();
        float scale = Settings.Global.getFloat(getContentResolver(), Settings.Global.ANIMATOR_DURATION_SCALE, 1f);
        View rule = findViewById(R.id.schematic);
        if (rule != null) rule.setVisibility(Surface.motion(scale) ? View.VISIBLE : View.INVISIBLE);
        if (state == null) show(Board.none("No address"));
        else {
            set(R.id.process, state.getString(KEY_PROCESS, ""));
            set(R.id.lane, state.getString(KEY_LANE, ""));
            set(R.id.attachment, state.getString(KEY_ATTACH, ""));
            set(R.id.connect, state.getString(KEY_CONNECT, ""));
            set(R.id.detail, state.getString(KEY_DETAIL, ""));
            set(R.id.acceptance, state.getString(KEY_ACCEPT, Acceptance.LINE));
            set(R.id.observed, state.getString(KEY_OBSERVED, ""));
            addressText = state.getString(KEY_ADDRESS, "");
            receiptsOpen = state.getBoolean(KEY_RECEIPTS, false);
            EditText address = findViewById(R.id.address);
            if (address != null) address.setText(addressText);
        }
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
        outState.putString(KEY_PROCESS, text(R.id.process));
        outState.putString(KEY_LANE, text(R.id.lane));
        outState.putString(KEY_ATTACH, text(R.id.attachment));
        outState.putString(KEY_CONNECT, text(R.id.connect));
        outState.putString(KEY_DETAIL, text(R.id.detail));
        outState.putString(KEY_ACCEPT, text(R.id.acceptance));
        outState.putString(KEY_OBSERVED, text(R.id.observed));
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
            ObserverClient.Result result = client.getSnapshot(uri, 4000);
            Board board = boardFor(result);
            runOnUiThread(() -> {
                if (isDestroyed() || !gate.accepts(token)) return;
                inflight = 0;
                show(board);
                remember(board);
                showReadButton(false);
            });
        }, "agiw-observer").start();
    }

    private static Board boardFor(ObserverClient.Result result) {
        if (result.detail().equals("Cleartext is not used") || result.detail().equals("No address")) {
            return Board.refused(result.detail());
        }
        if (!result.ok() && result.detail().equals("Observer did not answer")) {
            return Board.disconnected(result.detail());
        }
        if (!result.ok()) return Board.none(result.detail());
        return Board.read(result.body(), Instant.now());
    }

    private void show(Board board) {
        set(R.id.process, board.process);
        set(R.id.lane, cover ? "LANE  " + board.lane : board.lane);
        set(R.id.attachment, cover ? "ATTACH  " + board.attachment : board.attachment + "  " + board.runId);
        set(R.id.connect, cover ? "CONNECT  " + board.connect : board.connect);
        set(R.id.observed, cover ? board.observed : board.observed);
        set(R.id.detail, board.detail);
        set(R.id.acceptance, board.acceptance);
        if (!cover) reveal(findViewById(R.id.acceptance));
    }

    private void applyReceipts() {
        TextView receipts = findViewById(R.id.receipts);
        Button toggle = findViewById(R.id.receiptsToggle);
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
        scroller.setClipToPadding(false);
        scroller.setPadding(side, top, side, bottom);
        scroller.setOnApplyWindowInsetsListener((view, insets) -> {
            int left = side;
            int padTop = top;
            int right = side;
            int padBottom = bottom;
            if (Build.VERSION.SDK_INT >= 30) {
                Insets bars = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
                Insets ime = insets.getInsets(WindowInsets.Type.ime());
                left += bars.left;
                padTop += bars.top;
                right += bars.right;
                padBottom += Math.max(bars.bottom, ime.bottom);
                view.setPadding(left, padTop, right, padBottom);
                return WindowInsets.CONSUMED;
            }
            left += insets.getSystemWindowInsetLeft();
            padTop += insets.getSystemWindowInsetTop();
            right += insets.getSystemWindowInsetRight();
            padBottom += insets.getSystemWindowInsetBottom();
            view.setPadding(left, padTop, right, padBottom);
            return insets;
        });
        scroller.requestApplyInsets();
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
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

    private String text(int id) {
        TextView view = findViewById(id);
        return view == null || view.getText() == null ? "" : view.getText().toString();
    }

    private void set(int id, String value) {
        TextView view = findViewById(id);
        if (view != null) view.setText(value);
    }
}
