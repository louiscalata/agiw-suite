package suite.agiw.monitor;

import android.app.Activity;
import android.content.res.Configuration;
import android.os.Bundle;
import android.provider.Settings;
import android.view.View;
import android.view.ViewTreeObserver;
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
        bind(savedInstanceState);
    }

    @Override
    public void onConfigurationChanged(Configuration newConfig) {
        super.onConfigurationChanged(newConfig);
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
        if (read != null) {
            read.setOnClickListener(view -> readOnce());
            read.setText(inflight == 0 ? "Read" : "Cancel");
            read.setContentDescription(inflight == 0 ? "Read the observer snapshot" : "Cancel the observer read");
        }
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
        reveal(findViewById(R.id.acceptance));
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
        Button read = findViewById(R.id.read);
        if (inflight != 0) {
            gate.cancel();
            inflight = 0;
            if (read != null) {
                read.setText("Read");
                read.setContentDescription("Read the observer snapshot");
            }
            show(Board.none("Read cancelled"));
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
        if (read != null) {
            read.setText("Cancel");
            read.setContentDescription("Cancel the observer read");
        }
        new Thread(() -> {
            ObserverClient.Result result = client.getSnapshot(uri, 4000);
            Board board = boardFor(result);
            runOnUiThread(() -> {
                if (isDestroyed() || !gate.accepts(token)) return;
                inflight = 0;
                show(board);
                Button again = findViewById(R.id.read);
                if (again != null) {
                    again.setText("Read");
                    again.setContentDescription("Read the observer snapshot");
                }
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
        reveal(findViewById(R.id.acceptance));
    }

    private void applyReceipts() {
        TextView receipts = findViewById(R.id.receipts);
        Button toggle = findViewById(R.id.receiptsToggle);
        if (receipts != null) receipts.setVisibility(receiptsOpen ? View.VISIBLE : View.GONE);
        if (toggle != null) {
            toggle.setText(receiptsOpen ? "Hide receipts" : "Receipts");
            toggle.setContentDescription(receiptsOpen
                    ? "Hide recorded receipts"
                    : "Show recorded receipts. They are not a pass.");
        }
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
